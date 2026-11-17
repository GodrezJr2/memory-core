import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { CORE, FIXED_BRANCHES, branchOf, clusterOf, hash01, paintedBranches, shortScope, type Graph, type GraphNode, type LinkKind } from "./brain";

/**
 * The Brain view's canvas: memories laid out by a force simulation, one cluster per project, the projects of a branch
 * packed together around the core, links
 * drawn as curved axons. Signals run along axons: faint ones all the time (ambient), bright ones from the memories a
 * live call touched, cascading one step to their neighbours (what recall's link expansion does).
 * Imperative on purpose: hundreds of nodes animate every frame, which React state would only slow down.
 */

export interface Neuron extends SimulationNodeDatum {
  x: number;
  y: number;
  id: number;
  data: GraphNode;
  branch: string;
  cluster: string; // project scope, or the core
  r: number; // radius in layout units
  axons: Axon[];
  born: number; // ms (performance.now) when it starts to grow in
  phase: number; // breathing offset
  excite: number; // ms of the last excitation
  exciteColor: string;
  exciteStrength: number;
  dying: number; // ms when it was forgotten (ghosts only)
}

export interface Axon extends SimulationLinkDatum<Neuron> {
  source: Neuron;
  target: Neuron;
  kind: LinkKind;
  w: number;
  cross: boolean; // joins two branches
  apart: boolean; // joins two clusters
  bend: number; // signed curvature, stable per pair
}

interface Pulse {
  axon: Axon;
  forward: boolean; // source -> target
  t0: number;
  dur: number;
  color: string;
  alpha: number;
  size: number;
  cascade: boolean; // excites the neuron it reaches
}

interface Ring {
  node: Neuron;
  t0: number;
  color: string;
}

interface View {
  k: number;
  x: number;
  y: number;
}

export interface Palette {
  dark: boolean;
  ink: string;
  ink3: string;
  surface: string;
  branch: Record<string, string>;
  fallback: string;
}

export interface SceneEvents {
  onHover: (node: Neuron | null, x: number, y: number) => void;
  onSelect: (id: number | null) => void;
  onView: (zoomed: boolean) => void;
}

const WIDE = 1.35; // branches sit on rays from the core, stretched sideways to fill a landscape canvas
const CLUSTER_GAP = 26; // between projects of one branch
const BRANCH_GAP = 80; // between branches
const GROW_MS = 520;
const EXCITE_MS = 1400;
const RING_MS = 1100;
const GHOST_MS = 1200;
const MAX_PULSES = 320;
const MAX_FIRE_AXONS = 12;
const PAD = 44;

const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;
const easeOutBack = (t: number) => 1 + 2.2 * (t - 1) ** 3 + 1.2 * (t - 1) ** 2;
const easeInOutSine = (t: number) => -(Math.cos(Math.PI * t) - 1) / 2;
const clamp01 = (t: number) => Math.min(1, Math.max(0, t));

/** About how far a cluster of n neurons spreads once the simulation settles. */
const clusterRadius = (n: number) => 7.5 * Math.sqrt(n) + 12;

interface Circle {
  x: number;
  y: number;
  r: number;
}

const clear = (c: Circle, placed: Circle[], gap: number) => placed.every((p) => Math.hypot(c.x - p.x, c.y - p.y) >= c.r + p.r + gap);

/** Circles packed tightly around the first one (at 0, 0), biggest first: each takes the nearest free spot. */
function pack(radii: number[], gap: number): Circle[] {
  const placed: Circle[] = [];
  for (const r of radii) {
    if (placed.length === 0) {
      placed.push({ x: 0, y: 0, r });
      continue;
    }
    let spot: Circle | null = null;
    for (let d = placed[0].r + r + gap; !spot; d += 3) {
      for (let i = 0; i < 36 && !spot; i++) {
        const a = -Math.PI / 2 + (i / 36) * Math.PI * 2;
        const c = { x: Math.cos(a) * d, y: Math.sin(a) * d, r };
        if (clear(c, placed, gap)) spot = c;
      }
    }
    placed.push(spot);
  }
  return placed;
}

export function readPalette(): Palette {
  const root = document.documentElement;
  const cs = getComputedStyle(root);
  const v = (name: string) => cs.getPropertyValue(name).trim();
  const branch: Record<string, string> = {};
  for (const b of [...FIXED_BRANCHES, ...paintedBranches()]) branch[b] = v(`--b-${b}`);
  return { dark: root.classList.contains("dark"), ink: v("--ink"), ink3: v("--ink-3"), surface: v("--surface"), branch, fallback: v("--b-other") };
}

function rgba(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function hashText(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 1000) / 1000;
}

/** Nudge a hex colour's hue and lightness so the projects inside one branch read as siblings, not copies. */
function tint(hex: string, t: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const r = ((n >> 16) & 255) / 255;
  const g = ((n >> 8) & 255) / 255;
  const b = (n & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h /= 6;
  }
  const hh = (h + (t - 0.5) * 0.12 + 1) % 1;
  const ss = Math.min(1, Math.max(0, s * (0.85 + t * 0.3)));
  const ll = Math.min(0.82, Math.max(0.18, l * (0.82 + t * 0.36)));
  const q = ll < 0.5 ? ll * (1 + ss) : ll + ss - ll * ss;
  const p = 2 * ll - q;
  const f = (x: number) => {
    const u = x < 0 ? x + 1 : x > 1 ? x - 1 : x;
    if (u < 1 / 6) return p + (q - p) * 6 * u;
    if (u < 1 / 2) return q;
    if (u < 2 / 3) return p + (q - p) * (2 / 3 - u) * 6;
    return p;
  };
  const to = (x: number) => Math.round(x * 255).toString(16).padStart(2, "0");
  return `#${to(f(hh + 1 / 3))}${to(f(hh))}${to(f(hh - 1 / 3))}`;
}

export class BrainScene {
  private ctx: CanvasRenderingContext2D;
  private W = 0;
  private H = 0;
  private dpr = 1;
  private raf = 0;
  private sim: Simulation<Neuron, Axon>;
  private simHot = false;
  private neurons: Neuron[] = [];
  private axons: Axon[] = [];
  private byId = new Map<number, Neuron>();
  private ghosts: Neuron[] = [];
  private pulses: Pulse[] = [];
  private rings: Ring[] = [];
  private anchors = new Map<string, { x: number; y: number }>();
  private branchOrder: string[] = [];
  private palette: Palette;
  private sprites = new Map<string, HTMLCanvasElement>();
  private view: View = { k: 1, x: 0, y: 0 };
  private target: View | null = null;
  private autoFit = true;
  private selected: number | null = null;
  private hovered: number | null = null;
  private highlight: Set<number> | null = null;
  private showApart = false;
  private colorBy: "branch" | "cluster" = "branch";
  private clusterColors = new Map<string, string>();
  private nextAmbient = 0;
  private nextSpark = 0;
  private pointers = new Map<number, { x: number; y: number }>();
  private press: { x: number; y: number; moved: boolean } | null = null;
  private pinch: number | null = null;
  private zoomed = false;

  constructor(
    private canvas: HTMLCanvasElement,
    private events: SceneEvents,
    private reduced: boolean,
  ) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("This browser cannot draw on a canvas.");
    this.ctx = ctx;
    this.palette = readPalette();
    // a fixed random source keeps the layout the same on every load
    let seed = 1;
    const lcg = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
    this.sim = forceSimulation<Neuron, Axon>([])
      .randomSource(lcg)
      .force("link", forceLink<Neuron, Axon>([])
        .distance((a) => (a.apart ? 110 : a.kind === "together" ? 22 : 32))
        .strength((a) => (a.apart ? 0 : 0.06 + a.w * 0.22))) // clusters keep their place; links between them are only drawn
      .force("charge", forceManyBody<Neuron>().strength(-18).distanceMax(120))
      .force("x", forceX<Neuron>((n) => this.anchor(n.cluster).x).strength(0.12))
      .force("y", forceY<Neuron>((n) => this.anchor(n.cluster).y).strength(0.12))
      .force("collide", forceCollide<Neuron>((n) => n.r + 2.4).iterations(2))
      .stop();
    this.bind();
    this.raf = requestAnimationFrame(this.frame);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.sim.stop();
    this.unbind();
  }

  // --- data ---------------------------------------------------------------------------------

  setGraph(graph: Graph) {
    const now = performance.now();
    const first = this.neurons.length === 0;
    const order = [...graph.domains.map((d) => d.name)];
    const present = new Set(graph.nodes.map(branchOf));
    this.branchOrder = [CORE, ...order.filter((b) => present.has(b)), ...[...present].filter((b) => b !== CORE && !order.includes(b))];
    const clusters = new Map<string, { branch: string; n: number }>();
    for (const data of graph.nodes) {
      const c = clusters.get(clusterOf(data)) ?? { branch: branchOf(data), n: 0 };
      c.n += 1;
      clusters.set(clusterOf(data), c);
    }
    this.placeAnchors(clusters);

    const degree = new Map<number, number>();
    for (const l of graph.links) {
      degree.set(l.a, (degree.get(l.a) ?? 0) + 1);
      degree.set(l.b, (degree.get(l.b) ?? 0) + 1);
    }
    const linkedTo = new Map<number, number[]>();
    for (const l of graph.links) {
      linkedTo.set(l.a, [...(linkedTo.get(l.a) ?? []), l.b]);
      linkedTo.set(l.b, [...(linkedTo.get(l.b) ?? []), l.a]);
    }

    const old = this.byId;
    const next = new Map<number, Neuron>();
    const neurons = graph.nodes.map((data) => {
      const branch = branchOf(data);
      const cluster = clusterOf(data);
      const deg = degree.get(data.id) ?? 0;
      const r = 2.3 + Math.sqrt(deg) * 0.95 + (data.kind === "core" ? 2.4 : data.kind === "episode" ? 0.9 : 0);
      const prev = old.get(data.id);
      if (prev) {
        Object.assign(prev, { data, branch, cluster, r, axons: [] });
        next.set(data.id, prev);
        return prev;
      }
      // a new memory grows next to something it links to in its project, else at its project
      const near = (linkedTo.get(data.id) ?? []).map((id) => old.get(id)).find((m) => m?.cluster === cluster);
      const at = near ?? this.anchor(cluster);
      const spread = near ? 24 : first ? clusterRadius(clusters.get(cluster)!.n) * 1.6 : 30;
      const bi = this.branchOrder.indexOf(branch);
      const n: Neuron = {
        id: data.id, data, branch, cluster, r, axons: [],
        x: at.x + (hash01(data.id) - 0.5) * spread,
        y: at.y + (hash01(data.id * 31) - 0.5) * spread,
        born: this.reduced ? 0 : first ? now + 120 + bi * 55 + hash01(data.id * 7) * 220 : now,
        phase: hash01(data.id * 13) * Math.PI * 2,
        excite: -1e9, exciteColor: "", exciteStrength: 0, dying: 0,
      };
      next.set(data.id, n);
      return n;
    });
    for (const [id, n] of old) {
      if (!next.has(id)) {
        n.dying = now;
        this.ghosts.push(n);
      }
    }
    this.byId = next;
    this.neurons = neurons;

    this.axons = [];
    for (const l of graph.links) {
      const s = next.get(l.a);
      const t = next.get(l.b);
      if (!s || !t) continue;
      const axon: Axon = {
        source: s, target: t, kind: l.kind, w: l.w, cross: s.branch !== t.branch, apart: s.cluster !== t.cluster,
        bend: (hash01(l.a * 7919 + l.b) - 0.5) * 0.42,
      };
      s.axons.push(axon);
      t.axons.push(axon);
      this.axons.push(axon);
    }
    for (const n of neurons) n.axons.sort((a, b) => b.w - a.w);
    this.pulses = this.pulses.filter((p) => next.has(p.axon.source.id) && next.has(p.axon.target.id));

    this.sim.nodes(neurons);
    (this.sim.force("link") as ReturnType<typeof forceLink<Neuron, Axon>>).links(this.axons);
    if (first) {
      this.sim.alpha(1);
      for (let i = 0; i < 320; i++) this.sim.tick();
      this.simHot = false;
    } else {
      this.sim.alpha(0.3);
      this.simHot = true;
    }
    if (this.selected != null && !next.has(this.selected)) this.selected = null;
  }

  /**
   * One anchor per project: the projects of a branch packed around its biggest one, and each branch moved out along
   * its own ray from the core until it clears the core and the branches placed before it.
   */
  private placeAnchors(clusters: Map<string, { branch: string; n: number }>) {
    this.anchors.clear();
    const placed: Circle[] = [];
    const outer = this.branchOrder.filter((b) => b !== CORE);
    for (const b of this.branchOrder) {
      const members = [...clusters].filter(([, c]) => c.branch === b).sort((x, y) => y[1].n - x[1].n || (x[0] < y[0] ? -1 : 1));
      if (members.length === 0) continue;
      const circles = pack(members.map(([, c]) => clusterRadius(c.n)), CLUSTER_GAP);
      const extent = Math.max(...circles.map((c) => Math.hypot(c.x, c.y) + c.r));
      let at = { x: 0, y: 0, r: extent };
      if (b !== CORE) {
        const i = outer.indexOf(b);
        const a = -Math.PI / 2 + (i / Math.max(1, outer.length)) * Math.PI * 2;
        for (let d = 0; ; d += 4) {
          at = { x: Math.cos(a) * d * WIDE, y: Math.sin(a) * d, r: extent };
          if (clear(at, placed, BRANCH_GAP)) break;
        }
      }
      placed.push(b === CORE ? { ...at, r: at.r + 70 } : at); // more air around the core, where every branch links to
      members.forEach(([key], i) => this.anchors.set(key, { x: at.x + circles[i].x, y: at.y + circles[i].y }));
    }
  }

  private anchor(cluster: string) {
    return this.anchors.get(cluster) ?? { x: 0, y: 0 };
  }

  setPalette(p: Palette) {
    this.palette = p;
    this.sprites.clear();
    this.clusterColors.clear(); // hues derive from the branch colour, which changes with the theme
  }

  setReduced(reduced: boolean) {
    this.reduced = reduced;
  }

  setHighlight(ids: Set<number> | null) {
    this.highlight = ids;
  }

  setSelected(id: number | null, focus = false) {
    this.selected = id;
    if (id != null && focus) this.focusOn(id);
  }

  /** Cross-project axons are off by default: they are drawn but do not pull, so they only add noise. */
  setShowApart(on: boolean) {
    this.showApart = on;
  }

  /** "cluster" paints one hue per project (siblings of a branch stay family), "branch" keeps one per domain. */
  setColorBy(mode: "branch" | "cluster") {
    this.colorBy = mode;
  }

  private colorOf(n: Neuron): string {
    if (this.colorBy === "branch") return this.color(n.branch);
    let c = this.clusterColors.get(n.cluster);
    if (!c) {
      c = tint(this.color(n.branch), hashText(n.cluster));
      this.clusterColors.set(n.cluster, c);
    }
    return c;
  }

  neighbours(id: number): { node: Neuron; axon: Axon }[] {
    const n = this.byId.get(id);
    if (!n) return [];
    return n.axons.map((a) => ({ node: a.source === n ? a.target : a.source, axon: a }));
  }

  // --- signals ------------------------------------------------------------------------------

  /** Light up memories a call touched; signals then run from them along their strongest axons. */
  fire(ids: number[], color: string, cascade = true) {
    const now = performance.now();
    ids.forEach((id, i) => {
      const n = this.byId.get(id);
      if (!n) return;
      const at = now + i * 60;
      this.excite(n, color, 1, at);
      this.rings.push({ node: n, t0: at, color });
      if (this.reduced || !cascade) return;
      for (const axon of n.axons.slice(0, MAX_FIRE_AXONS)) {
        this.emit(axon, n, color, at + 200, 1, 1.25, true);
      }
    });
    this.rings = this.rings.slice(-60);
  }

  private excite(n: Neuron, color: string, strength: number, at: number) {
    const now = performance.now();
    const current = this.exciteLevel(n, now);
    if (strength >= current) {
      n.excite = at;
      n.exciteColor = color;
      n.exciteStrength = strength;
    }
  }

  private emit(axon: Axon, from: Neuron, color: string, t0: number, alpha: number, size: number, cascade: boolean) {
    const forward = axon.source === from;
    const dx = axon.target.x - axon.source.x;
    const dy = axon.target.y - axon.source.y;
    const len = Math.hypot(dx, dy);
    this.pulses.push({ axon, forward, t0, dur: 520 + len * 2.4, color, alpha, size, cascade });
    if (this.pulses.length > MAX_PULSES) this.pulses.splice(0, this.pulses.length - MAX_PULSES);
  }

  private ambient(now: number) {
    if (this.reduced || this.axons.length === 0) return;
    if (now >= this.nextAmbient) {
      const axon = this.axons[Math.floor(Math.random() * this.axons.length)];
      const from = Math.random() < 0.5 ? axon.source : axon.target;
      this.emit(axon, from, this.color(from.branch), now, 0.55, 0.8, false);
      this.nextAmbient = now + 140 + Math.random() * 420;
    }
    if (now >= this.nextSpark) {
      // now and then a neuron fires on its own, softly
      const candidates = this.neurons.filter((n) => n.axons.length >= 2);
      const n = candidates[Math.floor(Math.random() * candidates.length)];
      if (n) {
        this.excite(n, this.color(n.branch), 0.45, now);
        for (const axon of n.axons.slice(0, 4)) this.emit(axon, n, this.color(n.branch), now + 120, 0.75, 1, false);
      }
      this.nextSpark = now + 2600 + Math.random() * 3200;
    }
  }

  private exciteLevel(n: Neuron, now: number) {
    const dt = now - n.excite;
    if (dt < 0 || dt > EXCITE_MS) return 0;
    return n.exciteStrength * (1 - easeOutCubic(dt / EXCITE_MS));
  }

  // --- view ---------------------------------------------------------------------------------

  resize(width: number, height: number) {
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    this.W = width;
    this.H = height;
    this.canvas.width = Math.round(width * this.dpr);
    this.canvas.height = Math.round(height * this.dpr);
    if (this.autoFit) this.view = this.fit();
  }

  private fit(): View {
    if (this.neurons.length === 0 || this.W === 0) return { k: 1, x: this.W / 2, y: this.H / 2 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of this.neurons) {
      x0 = Math.min(x0, n.x - n.r);
      y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r);
      y1 = Math.max(y1, n.y + n.r);
    }
    const top = PAD + 30; // room for the branch and project labels
    const k = Math.min((this.W - PAD * 2) / Math.max(1, x1 - x0), (this.H - top - PAD) / Math.max(1, y1 - y0), 2.4);
    return { k, x: this.W / 2 - ((x0 + x1) / 2) * k, y: top + (this.H - top - PAD) / 2 - ((y0 + y1) / 2) * k };
  }

  reset() {
    this.autoFit = true;
    this.target = this.fit();
  }

  zoomBy(factor: number, cx = this.W / 2, cy = this.H / 2) {
    const base = this.target ?? this.view;
    const k = Math.min(12, Math.max(0.3, base.k * factor));
    const f = k / base.k;
    this.autoFit = false;
    this.target = { k, x: cx - (cx - base.x) * f, y: cy - (cy - base.y) * f };
  }

  private focusOn(id: number) {
    const n = this.byId.get(id);
    if (!n) return;
    const k = Math.max(this.view.k, this.fit().k * 2);
    this.autoFit = false;
    this.target = { k, x: this.W / 2 - n.x * k, y: this.H / 2 - n.y * k };
  }

  private toScreen(n: { x: number; y: number }) {
    return { x: n.x * this.view.k + this.view.x, y: n.y * this.view.k + this.view.y };
  }

  private hit(px: number, py: number): Neuron | null {
    let best: Neuron | null = null;
    let bestD = Infinity;
    for (const n of this.neurons) {
      const p = this.toScreen(n);
      const d = Math.hypot(p.x - px, p.y - py);
      const reach = Math.max(9, n.r * this.view.k + 5);
      if (d < reach && d < bestD) {
        best = n;
        bestD = d;
      }
    }
    return best;
  }

  // --- input --------------------------------------------------------------------------------

  private local(e: PointerEvent | WheelEvent | MouseEvent) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  private onDown = (e: PointerEvent) => {
    const p = this.local(e);
    this.pointers.set(e.pointerId, p);
    if (this.pointers.size === 1) this.press = { ...p, moved: false };
    if (this.pointers.size === 2) {
      const [a, b] = [...this.pointers.values()];
      this.pinch = Math.hypot(a.x - b.x, a.y - b.y);
      if (this.press) this.press.moved = true;
    }
    this.canvas.setPointerCapture(e.pointerId);
  };

  private onMove = (e: PointerEvent) => {
    const p = this.local(e);
    const prev = this.pointers.get(e.pointerId);
    if (prev) {
      this.pointers.set(e.pointerId, p);
      if (this.pointers.size === 2 && this.pinch) {
        const [a, b] = [...this.pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        this.zoomAt(d / this.pinch, (a.x + b.x) / 2, (a.y + b.y) / 2);
        this.pinch = d;
        return;
      }
      if (this.press && Math.hypot(p.x - this.press.x, p.y - this.press.y) > 4) this.press.moved = true;
      if (this.press?.moved) {
        this.autoFit = false;
        this.target = null;
        this.view = { ...this.view, x: this.view.x + p.x - prev.x, y: this.view.y + p.y - prev.y };
        this.canvas.style.cursor = "grabbing";
        return;
      }
    }
    const n = this.hit(p.x, p.y);
    this.hovered = n?.id ?? null;
    this.canvas.style.cursor = n ? "pointer" : "grab";
    this.events.onHover(n, p.x, p.y);
  };

  private onUp = (e: PointerEvent) => {
    const p = this.local(e);
    this.pointers.delete(e.pointerId);
    if (this.pointers.size < 2) this.pinch = null;
    if (this.press && !this.press.moved && this.pointers.size === 0) {
      const n = this.hit(p.x, p.y);
      this.selected = n?.id ?? null;
      this.events.onSelect(this.selected);
      if (n) this.fire([n.id], this.color(n.branch));
    }
    if (this.pointers.size === 0) this.press = null;
    this.canvas.style.cursor = this.hovered != null ? "pointer" : "grab";
  };

  private onLeave = () => {
    this.hovered = null;
    this.events.onHover(null, 0, 0);
  };

  private onWheel = (e: WheelEvent) => {
    // plain scrolling keeps scrolling the page; ctrl/cmd + scroll (and a trackpad pinch) zooms
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const p = this.local(e);
    this.zoomAt(Math.exp(-e.deltaY * 0.01), p.x, p.y);
  };

  private onDouble = () => this.reset();

  private zoomAt(factor: number, cx: number, cy: number) {
    const k = Math.min(12, Math.max(0.3, this.view.k * factor));
    const f = k / this.view.k;
    this.autoFit = false;
    this.target = null;
    this.view = { k, x: cx - (cx - this.view.x) * f, y: cy - (cy - this.view.y) * f };
  }

  private bind() {
    const c = this.canvas;
    c.addEventListener("pointerdown", this.onDown);
    c.addEventListener("pointermove", this.onMove);
    c.addEventListener("pointerup", this.onUp);
    c.addEventListener("pointercancel", this.onUp);
    c.addEventListener("pointerleave", this.onLeave);
    c.addEventListener("wheel", this.onWheel, { passive: false });
    c.addEventListener("dblclick", this.onDouble);
    c.style.cursor = "grab";
  }

  private unbind() {
    const c = this.canvas;
    c.removeEventListener("pointerdown", this.onDown);
    c.removeEventListener("pointermove", this.onMove);
    c.removeEventListener("pointerup", this.onUp);
    c.removeEventListener("pointercancel", this.onUp);
    c.removeEventListener("pointerleave", this.onLeave);
    c.removeEventListener("wheel", this.onWheel);
    c.removeEventListener("dblclick", this.onDouble);
  }

  // --- drawing ------------------------------------------------------------------------------

  color(branch: string) {
    return this.palette.branch[branch] || this.palette.fallback;
  }

  /** A soft round glow, drawn once per colour and stamped with drawImage (far cheaper than a gradient per node). */
  private sprite(color: string) {
    let s = this.sprites.get(color);
    if (!s) {
      s = document.createElement("canvas");
      s.width = s.height = 64;
      const g = s.getContext("2d")!;
      const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      grad.addColorStop(0, rgba(color, 1));
      grad.addColorStop(0.22, rgba(color, 0.55));
      grad.addColorStop(0.55, rgba(color, 0.14));
      grad.addColorStop(1, rgba(color, 0));
      g.fillStyle = grad;
      g.fillRect(0, 0, 64, 64);
      this.sprites.set(color, s);
    }
    return s;
  }

  private control(a: Axon) {
    const { source: s, target: t } = a;
    const mx = (s.x + t.x) / 2;
    const my = (s.y + t.y) / 2;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    return { x: mx - dy * a.bend, y: my + dx * a.bend };
  }

  private growth(n: Neuron, now: number) {
    if (n.born === 0) return 1;
    return clamp01((now - n.born) / GROW_MS);
  }

  private frame = (now: number) => {
    this.raf = requestAnimationFrame(this.frame);
    if (document.hidden || this.W === 0) return;
    if (this.simHot) {
      this.sim.tick();
      if (this.sim.alpha() < 0.02) this.simHot = false;
    }
    if (this.autoFit) this.target = this.fit();
    if (this.target) {
      const t = this.target;
      const s = this.reduced ? 1 : 0.14;
      this.view = { k: this.view.k + (t.k - this.view.k) * s, x: this.view.x + (t.x - this.view.x) * s, y: this.view.y + (t.y - this.view.y) * s };
      if (Math.abs(t.k - this.view.k) < 0.001 && Math.abs(t.x - this.view.x) < 0.3 && Math.abs(t.y - this.view.y) < 0.3) {
        this.view = t;
        if (!this.autoFit) this.target = null;
      }
    }
    const zoomed = this.view.k > this.fit().k * 1.15;
    if (zoomed !== this.zoomed) {
      this.zoomed = zoomed;
      // at the fitted view a finger scrolls the page; zoomed in, it pans the brain
      this.canvas.style.touchAction = zoomed ? "none" : "pan-y";
      this.events.onView(zoomed);
    }
    this.ambient(now);
    this.ghosts = this.ghosts.filter((g) => now - g.dying < GHOST_MS);
    this.draw(now);
  };

  /** Which neurons are in focus: the selected one and its neighbours, else the hovered one's, else the filter. */
  private focus(): { nodes: Set<number>; hub: Neuron | null; dim: number } | null {
    const hubId = this.selected ?? this.hovered;
    const hub = hubId != null ? this.byId.get(hubId) ?? null : null;
    if (hub) {
      const nodes = new Set([hub.id, ...hub.axons.map((a) => (a.source === hub ? a.target.id : a.source.id))]);
      return { nodes, hub, dim: this.selected != null ? 0.1 : 0.3 };
    }
    if (this.highlight) return { nodes: this.highlight, hub: null, dim: 0.1 };
    return null;
  }

  private draw(now: number) {
    const { ctx, palette: pal, view } = this;
    const { k } = view;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.W, this.H);
    const focus = this.focus();
    const alphaOf = (n: Neuron) => (focus && !focus.nodes.has(n.id) ? focus.dim : 1);
    const glow = pal.dark ? 1 : 0.55;

    // project centroids and extents: a faint nebula and a dotted rim around each cluster, labels drawn last
    const groups = new Map<string, { branch: string; x: number; y: number; n: number; spread: number }>();
    for (const n of this.neurons) {
      const g = groups.get(n.cluster) ?? { branch: n.branch, x: 0, y: 0, n: 0, spread: 0 };
      g.x += n.x;
      g.y += n.y;
      g.n += 1;
      groups.set(n.cluster, g);
    }
    for (const g of groups.values()) {
      g.x /= g.n;
      g.y /= g.n;
    }
    for (const n of this.neurons) {
      const g = groups.get(n.cluster)!;
      g.spread = Math.max(g.spread, Math.hypot(n.x - g.x, n.y - g.y) + n.r);
    }
    const branchTop = new Map<string, { x0: number; x1: number; top: number }>();
    for (const g of groups.values()) {
      const t = branchTop.get(g.branch) ?? { x0: Infinity, x1: -Infinity, top: Infinity };
      t.x0 = Math.min(t.x0, g.x - g.spread);
      t.x1 = Math.max(t.x1, g.x + g.spread);
      t.top = Math.min(t.top, g.y - g.spread - 8);
      branchTop.set(g.branch, t);
    }
    const inFocus = (key: string) => !focus || [...focus.nodes].some((id) => this.byId.get(id)?.cluster === key);
    ctx.globalCompositeOperation = pal.dark ? "lighter" : "source-over";
    for (const [key, g] of groups) {
      const p = this.toScreen(g);
      const r = (g.spread + 22) * k;
      const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
      const faded = this.highlight && focus && !inFocus(key);
      grad.addColorStop(0, rgba(this.color(g.branch), (pal.dark ? 0.08 : 0.055) * (faded ? 0.3 : 1)));
      grad.addColorStop(1, rgba(this.color(g.branch), 0));
      ctx.fillStyle = grad;
      ctx.fillRect(p.x - r, p.y - r, r * 2, r * 2);
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 1;
    ctx.setLineDash([1, 4]);
    for (const [key, g] of groups) {
      if (key === CORE) continue;
      const p = this.toScreen(g);
      ctx.globalAlpha = (pal.dark ? 0.32 : 0.4) * (inFocus(key) ? 1 : 0.35);
      ctx.strokeStyle = this.color(g.branch);
      ctx.beginPath();
      ctx.arc(p.x, p.y, (g.spread + 8) * k, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    // axons
    ctx.globalCompositeOperation = "source-over";
    ctx.lineCap = "round";
    for (const a of this.axons) {
      const s = a.source;
      const t = a.target;
      const grow = Math.min(this.growth(s, now), this.growth(t, now));
      if (grow <= 0) continue;
      const hub = focus?.hub ?? null;
      if (a.apart && !this.showApart && !(hub && (a.source === hub || a.target === hub))) continue;
      let alpha = (pal.dark ? 0.1 + a.w * 0.2 : 0.13 + a.w * 0.22) * (a.kind === "together" ? 0.85 : 1);
      if (focus) {
        const on = focus.hub ? (a.source === focus.hub || a.target === focus.hub) : focus.nodes.has(s.id) && focus.nodes.has(t.id);
        alpha = on ? Math.min(0.9, alpha * 3.2) : alpha * 0.25;
      }
      const p0 = this.toScreen(s);
      const p1 = this.toScreen(t);
      const c = this.toScreen(this.control(a));
      ctx.globalAlpha = alpha * grow;
      if (a.cross) {
        const grad = ctx.createLinearGradient(p0.x, p0.y, p1.x, p1.y);
        grad.addColorStop(0, this.colorOf(s));
        grad.addColorStop(1, this.colorOf(t));
        ctx.strokeStyle = grad;
      } else {
        ctx.strokeStyle = this.colorOf(s);
      }
      ctx.lineWidth = Math.max(0.6, Math.min(2, (a.kind === "explicit" ? 1.4 : 0.9) * Math.sqrt(k)));
      ctx.setLineDash(a.kind === "together" ? [1.5, 3.5] : []);
      ctx.beginPath();
      ctx.moveTo(p0.x, p0.y);
      ctx.quadraticCurveTo(c.x, c.y, p1.x, p1.y);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // signals travelling along axons
    ctx.globalCompositeOperation = pal.dark ? "lighter" : "source-over";
    const alive: Pulse[] = [];
    for (const p of this.pulses) {
      const u = (now - p.t0) / p.dur;
      if (u >= 1) {
        if (p.cascade) {
          const to = p.forward ? p.axon.target : p.axon.source;
          this.excite(to, p.color, 0.6, now);
        }
        continue;
      }
      alive.push(p);
      if (p.axon.apart && !this.showApart) continue;
      if (u < 0) continue;
      const a = p.axon;
      const p0 = this.toScreen(a.source);
      const p1 = this.toScreen(a.target);
      const c = this.toScreen(this.control(a));
      const dimmed = focus && !(focus.nodes.has(a.source.id) && focus.nodes.has(a.target.id)) ? 0.3 : 1;
      const sprite = this.sprite(p.color);
      for (let i = 0; i < 4; i++) {
        const e = easeInOutSine(clamp01(u - i * 0.035));
        const t = p.forward ? e : 1 - e;
        const x = (1 - t) ** 2 * p0.x + 2 * (1 - t) * t * c.x + t ** 2 * p1.x;
        const y = (1 - t) ** 2 * p0.y + 2 * (1 - t) * t * c.y + t ** 2 * p1.y;
        const size = (7 + 5 * Math.min(1.6, k)) * p.size * (1 - i * 0.2);
        ctx.globalAlpha = p.alpha * dimmed * glow * (1 - i * 0.24) * Math.sin(Math.PI * Math.min(1, u * 1.1 + 0.05));
        ctx.drawImage(sprite, x - size / 2, y - size / 2, size, size);
      }
    }
    this.pulses = alive;

    // neurons: glow, then body
    const all = this.ghosts.length ? [...this.neurons, ...this.ghosts] : this.neurons;
    for (const n of all) {
      const grow = this.growth(n, now);
      if (grow <= 0) continue;
      const ghost = n.dying ? 1 - clamp01((now - n.dying) / GHOST_MS) : 1;
      const ex = this.exciteLevel(n, now);
      const breathe = this.reduced ? 0 : Math.sin(now / 900 + n.phase) * 0.06;
      const scale = (this.reduced ? 1 : easeOutBack(grow)) * ghost;
      const r = Math.max(1.4, n.r * k) * scale * (1 + breathe + ex * 0.7);
      const p = this.toScreen(n);
      const base = n.data.kind === "core" ? 0.42 : 0.2;
      const color = ex > 0.04 ? n.exciteColor : this.colorOf(n);
      const g = r * (3 + ex * 3.5);
      ctx.globalAlpha = Math.min(1, (base + ex * 0.8) * alphaOf(n) * glow * ghost);
      ctx.drawImage(this.sprite(color), p.x - g, p.y - g, g * 2, g * 2);
    }
    ctx.globalCompositeOperation = "source-over";
    for (const n of all) {
      const grow = this.growth(n, now);
      if (grow <= 0) continue;
      const ghost = n.dying ? 1 - clamp01((now - n.dying) / GHOST_MS) : 1;
      const ex = this.exciteLevel(n, now);
      const breathe = this.reduced ? 0 : Math.sin(now / 900 + n.phase) * 0.06;
      const scale = (this.reduced ? 1 : easeOutBack(grow)) * ghost;
      const r = Math.max(1.4, n.r * k) * scale * (1 + breathe + ex * 0.7);
      const p = this.toScreen(n);
      const color = this.colorOf(n);
      ctx.globalAlpha = alphaOf(n) * ghost;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      if (n.data.kind === "episode") {
        ctx.fillStyle = pal.surface;
        ctx.fill();
        ctx.lineWidth = Math.max(1, r * 0.38);
        ctx.strokeStyle = color;
        ctx.stroke();
      } else {
        ctx.fillStyle = color;
        ctx.fill();
        if (n.data.kind === "core") {
          ctx.beginPath();
          ctx.arc(p.x, p.y, r * 0.42, 0, Math.PI * 2);
          ctx.fillStyle = pal.surface;
          ctx.fill();
        }
      }
      if (n.id === this.selected) {
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = pal.ink;
        ctx.stroke();
      }
    }

    // rings spreading from neurons a call touched
    const rings: Ring[] = [];
    for (const ring of this.rings) {
      const u = (now - ring.t0) / RING_MS;
      if (u >= 1) continue;
      rings.push(ring);
      if (u < 0) continue;
      const p = this.toScreen(ring.node);
      const r0 = Math.max(1.4, ring.node.r * k);
      ctx.globalAlpha = (1 - u) * 0.9;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r0 + 4 + easeOutCubic(u) * 22, 0, Math.PI * 2);
      ctx.lineWidth = 1.5 * (1 - u) + 0.5;
      ctx.strokeStyle = ring.color;
      ctx.stroke();
    }
    this.rings = rings;

    // labels in screen pixels so they stay readable at any zoom: each project above its rim once the cluster is big
    // enough on screen, each branch above its topmost project
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.font = '400 9.5px "JetBrains Mono Variable", ui-monospace, monospace';
    for (const [key, g] of groups) {
      if (key === CORE || (g.spread + 8) * k < 20) continue;
      const p = this.toScreen({ x: g.x, y: g.y - g.spread - 8 });
      ctx.globalAlpha = inFocus(key) ? 0.9 : 0.3;
      ctx.fillStyle = pal.ink3;
      ctx.fillText(shortScope(key), p.x, p.y - 4);
    }
    ctx.font = '500 10.5px "JetBrains Mono Variable", ui-monospace, monospace';
    for (const [b, t] of branchTop) {
      const p = this.toScreen({ x: (t.x0 + t.x1) / 2, y: t.top });
      const faded = focus && ![...focus.nodes].some((id) => this.byId.get(id)?.branch === b);
      ctx.globalAlpha = faded ? 0.35 : 1;
      const label = b.toUpperCase();
      const y = p.y - 20;
      ctx.fillStyle = this.color(b);
      const w = ctx.measureText(label).width;
      ctx.fillRect(p.x - w / 2 - 10, y - 7, 5, 5);
      ctx.fillStyle = pal.ink3;
      ctx.fillText(label, p.x + 3, y);
    }
    ctx.globalAlpha = 1;
  }
}
