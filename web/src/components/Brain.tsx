import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CORE, branchOf, branchVar, loadGraph, paintNewBranches, shortScope, type Graph, type GraphNode, type LinkKind } from "../lib/brain";
import { BrainScene, readPalette, type Neuron } from "../lib/brainScene";
import { actionLabel, ago, fmtInt } from "../lib/format";
import { EASE_IOS } from "../lib/motion";
import type { CallEvent } from "../lib/types";
import { TileHeader } from "./Tile";

const CORE_TITLE = "Core: profile and global notes";
const ALL_KINDS = ["fact", "episode", "core", "procedure"] as const;
const LINK_LABEL: Record<LinkKind, string> = { related: "related", together: "together", explicit: "linked" };
const TONE: Record<string, string> = { recall: "--recall", remember: "--remember", forget: "--forget" };

interface Firing {
  key: number;
  agent: string;
  action: string;
  detail: string | null;
  neurons: number;
  at: string;
  tone: string;
}

let firingKey = 1;

/** The Brain tab: memories as neurons, links as axons, live calls lighting up the memories they touch. */
export function BrainView({ subscribe }: { subscribe: (fn: (e: CallEvent) => void) => () => void }) {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<{ node: GraphNode; x: number; y: number } | null>(null);
  const [branch, setBranch] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [zoomed, setZoomed] = useState(false);
  const [colorBy, setColorBy] = useState<"branch" | "cluster">("branch");
  const [showApart, setShowApart] = useState(false);
  const [kinds, setKinds] = useState<Set<string>>(() => new Set<string>(ALL_KINDS));
  const [firings, setFirings] = useState<Firing[]>([]);
  const reduced = useReducedMotion() ?? false;

  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const scene = useRef<BrainScene | null>(null);
  const pendingFire = useRef<{ ids: number[]; tone: string } | null>(null);
  const graphRef = useRef<Graph | null>(null);
  graphRef.current = graph;

  const refresh = useCallback(async () => {
    try {
      const g = await loadGraph();
      paintNewBranches(g);
      setGraph(g);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // the canvas scene lives as long as the tab
  useEffect(() => {
    if (!canvas.current || !wrap.current) return;
    const s = new BrainScene(canvas.current, {
      onHover: (n: Neuron | null, x, y) => setHover(n ? { node: n.data, x, y } : null),
      onSelect: setSelected,
      onView: setZoomed,
    }, reduced);
    scene.current = s;
    const el = wrap.current;
    const ro = new ResizeObserver(([entry]) => s.resize(entry.contentRect.width, entry.contentRect.height));
    ro.observe(el);
    // the theme is a class on <html>: re-read the palette when it flips
    const mo = new MutationObserver(() => s.setPalette(readPalette()));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      ro.disconnect();
      mo.disconnect();
      s.destroy();
      scene.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => scene.current?.setReduced(reduced), [reduced]);
  useEffect(() => scene.current?.setShowApart(showApart), [showApart]);
  useEffect(() => scene.current?.setColorBy(colorBy), [colorBy]);

  useEffect(() => {
    if (!graph || !scene.current) return;
    scene.current.setPalette(readPalette()); // a branch accepted since the last load has a hue now
    scene.current.setGraph(graph);
    const r = wrap.current?.getBoundingClientRect();
    if (r) scene.current.resize(r.width, r.height);
    if (pendingFire.current) {
      const { ids, tone } = pendingFire.current;
      pendingFire.current = null;
      scene.current.fire(ids, toneColor(tone));
    }
  }, [graph]);

  // live calls: recall lights its hits up at once; remember and forget reload the graph, then light up
  useEffect(() => {
    let timer = 0;
    const demo = new URLSearchParams(location.search).has("demo");
    return subscribe((e) => {
      if (!e.ok || !(e.action in TONE)) return;
      let ids = e.ids ?? [];
      if (demo && ids.length === 0 && graphRef.current) {
        const nodes = graphRef.current.nodes;
        ids = Array.from({ length: e.action === "recall" ? 5 : 1 }, () => nodes[Math.floor(Math.random() * nodes.length)].id);
      }
      if (ids.length === 0) return;
      setFirings((f) => [{ key: firingKey++, agent: e.agent, action: actionLabel(e), detail: e.detail, neurons: ids.length, at: e.at, tone: e.action }, ...f].slice(0, 8));
      if (e.action === "recall" || demo) {
        scene.current?.fire(ids, toneColor(e.action));
        return;
      }
      if (e.action === "forget") scene.current?.fire(ids, toneColor("forget"), false);
      else pendingFire.current = { ids: [...(pendingFire.current?.ids ?? []), ...ids], tone: e.action };
      window.clearTimeout(timer);
      timer = window.setTimeout(refresh, 700);
    });
  }, [subscribe, refresh]);

  // filter by branch or text: everything else dims
  const matches = useMemo(() => {
    if (!graph) return null;
    const q = query.trim().toLowerCase();
    const allKinds = kinds.size === ALL_KINDS.length;
    if (!q && !branch && allKinds) return null;
    return graph.nodes.filter((n) =>
      (!branch || branchOf(n) === branch) && kinds.has(n.kind) &&
      (!q || n.text.toLowerCase().includes(q) || n.scope.toLowerCase().includes(q)));
  }, [graph, query, branch, kinds]);

  useEffect(() => scene.current?.setHighlight(matches ? new Set(matches.map((n) => n.id)) : null), [matches]);
  useEffect(() => scene.current?.setSelected(selected), [selected]);

  const pick = useCallback((id: number) => {
    setSelected(id);
    scene.current?.setSelected(id, true);
    const n = graphRef.current?.nodes.find((x) => x.id === id);
    if (n) scene.current?.fire([id], readPalette().branch[branchOf(n)] || readPalette().fallback);
  }, []);

  const toggleKind = useCallback((kind: string) => {
    setKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  const stats = useMemo(() => {
    if (!graph) return null;
    const branchOfId = new Map(graph.nodes.map((n) => [n.id, branchOf(n)]));
    const cross = graph.links.filter((l) => branchOfId.get(l.a) !== branchOfId.get(l.b)).length;
    const counts = new Map<string, number>();
    for (const n of graph.nodes) counts.set(branchOf(n), (counts.get(branchOf(n)) ?? 0) + 1);
    const titles = new Map(graph.domains.map((d) => [d.name, d.title]));
    titles.set(CORE, CORE_TITLE);
    const branches = [CORE, ...graph.domains.map((d) => d.name)].filter((b) => counts.has(b))
      .map((b) => ({ name: b, title: titles.get(b) ?? b, count: counts.get(b) ?? 0 }));
    return { cross, branches };
  }, [graph]);

  const selectedNode = useMemo(() => graph?.nodes.find((n) => n.id === selected) ?? null, [graph, selected]);

  return (
    <main className="mt-5 grid grid-cols-12 gap-px border border-line bg-line">
      <section className="col-span-12 bg-surface lg:col-span-8 xl:col-span-9">
        <TileHeader
          label="Neurons"
          meta={graph && stats ? `${fmtInt(graph.nodes.length)} memories · ${fmtInt(graph.links.length)} links · ${stats.cross} across branches` : "loading"}
        />
        <div className="flex flex-wrap items-center gap-1.5 px-2 pt-3 sm:px-4">
          <Chip on={colorBy === "cluster"} onClick={() => setColorBy(colorBy === "cluster" ? "branch" : "cluster")}>
            warna per project
          </Chip>
          <Chip on={showApart} onClick={() => setShowApart((v) => !v)}>
            link antar-project
          </Chip>
          <span className="mx-1 h-3 w-px bg-line-strong" aria-hidden />
          {ALL_KINDS.map((k) => (
            <Chip key={k} on={kinds.has(k)} onClick={() => toggleKind(k)}>
              {k}
            </Chip>
          ))}
        </div>
        <div className="relative mx-2 mb-2 mt-3 sm:mx-4">
          <div
            ref={wrap}
            className="relative h-[62vh] min-h-[420px] overflow-hidden border border-line bg-bg lg:h-[min(76vh,780px)]"
          >
            <canvas
              ref={canvas}
              className="absolute inset-0 block h-full w-full"
              style={{ touchAction: "pan-y" }}
              role="img"
              aria-label={graph ? `${graph.nodes.length} memories linked by ${graph.links.length} links, grouped by branch` : "Loading memories"}
            />
            {error && (
              <div className="absolute inset-0 grid place-items-center p-6 text-center text-[13px] text-ink-2">
                <p>{error}</p>
              </div>
            )}
            <FiringOverlay firings={firings.slice(0, 3)} />
            {hover && hover.node.id !== selected && <Tooltip hover={hover} wrap={wrap.current} />}
            <div className="absolute bottom-3 right-3 flex border border-line-strong bg-surface/85 backdrop-blur">
              <ZoomButton label="Zoom in" onClick={() => scene.current?.zoomBy(1.5)}>+</ZoomButton>
              <ZoomButton label="Zoom out" onClick={() => scene.current?.zoomBy(1 / 1.5)}>−</ZoomButton>
              <ZoomButton label="Fit the whole brain" onClick={() => scene.current?.reset()} wide>{zoomed ? "Fit" : "All"}</ZoomButton>
            </div>
          </div>
        </div>
        <Legend />
      </section>

      <aside className="col-span-12 flex min-h-0 flex-col bg-surface lg:col-span-4 xl:col-span-3">
        <TileHeader label="Branches" meta={branch ? <button type="button" className="underline-offset-2 hover:underline" onClick={() => setBranch(null)}>show all</button> : undefined} />
        <ul className="mt-3 border-t border-line">
          {stats?.branches.map((b) => {
            const on = branch === b.name;
            return (
              <li key={b.name}>
                <button
                  type="button"
                  aria-pressed={on}
                  onClick={() => setBranch(on ? null : b.name)}
                  className={`flex w-full items-center gap-3 border-b border-line px-5 py-2.5 text-left outline-none transition-colors hover:bg-surface-2 focus-visible:bg-surface-2 ${branch && !on ? "opacity-45" : ""}`}
                >
                  <span className="size-2 shrink-0" style={{ background: branchVar(b.name) }} />
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink">{b.name}</span>
                    <span className="block truncate text-[12px] text-ink-3">{b.title}</span>
                  </span>
                  <span className="font-mono text-[11px] tabular-nums text-ink-2">{b.count}</span>
                </button>
              </li>
            );
          })}
        </ul>

        <div className="px-5 pt-4">
          <label className="block">
            <span className="sr-only">Find a memory</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Find a memory…"
              className="h-9 w-full border border-line bg-surface-2 px-3 text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-ink-3"
            />
          </label>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4 lg:max-h-[min(36vh,340px)]">
          {selectedNode ? (
            <Detail node={selectedNode} scene={scene.current} onPick={pick} onClose={() => setSelected(null)} />
          ) : query.trim() && matches ? (
            <Matches nodes={matches} onPick={pick} />
          ) : (
            <Idle firings={firings} />
          )}
        </div>
      </aside>
    </main>
  );
}

function toneColor(action: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(TONE[action] ?? "--recall").trim();
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`border px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] outline-none transition-colors ${on ? "border-ink-3 bg-surface-2 text-ink" : "border-line text-ink-3 hover:border-ink-3 hover:text-ink-2"}`}
    >
      {children}
    </button>
  );
}

function ZoomButton({ label, onClick, children, wide = false }: { label: string; onClick: () => void; children: ReactNode; wide?: boolean }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className={`grid h-8 place-items-center border-l border-line-strong font-mono text-[12px] text-ink-2 outline-none first:border-l-0 hover:bg-surface-2 hover:text-ink focus-visible:bg-surface-2 ${wide ? "px-3 text-[10.5px] uppercase tracking-[0.14em]" : "w-8"}`}
    >
      {children}
    </button>
  );
}

function Tooltip({ hover, wrap }: { hover: { node: GraphNode; x: number; y: number }; wrap: HTMLDivElement | null }) {
  const W = 280;
  const width = wrap?.clientWidth ?? 800;
  const left = Math.min(Math.max(8, hover.x + 14), width - W - 8);
  const top = hover.y + 16;
  const n = hover.node;
  return (
    <div
      className="pointer-events-none absolute z-10 border border-line-strong bg-surface/95 px-3 py-2.5 shadow-[0_12px_40px_-16px_rgba(0,0,0,0.5)] backdrop-blur"
      style={{ left, top, width: W }}
    >
      <Meta node={n} />
      <p className="mt-1.5 line-clamp-4 text-[12.5px] leading-[1.45] text-ink">{n.text}</p>
    </div>
  );
}

function Meta({ node }: { node: GraphNode }) {
  return (
    <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-3">
      <span className="size-1.5 shrink-0" style={{ background: branchVar(branchOf(node)) }} />
      <span className="truncate">
        {node.kind} · {shortScope(node.scope)} · {node.date}
      </span>
    </div>
  );
}

function Detail({ node, scene, onPick, onClose }: {
  node: GraphNode;
  scene: BrainScene | null;
  onPick: (id: number) => void;
  onClose: () => void;
}) {
  const links = scene?.neighbours(node.id) ?? [];
  return (
    <div>
      <div className="flex items-start justify-between gap-3">
        <Meta node={node} />
        <button type="button" onClick={onClose} className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-3 hover:text-ink">
          close
        </button>
      </div>
      <p className="mt-3 text-[13.5px] leading-[1.55] text-ink">{node.text}</p>
      <p className="mt-2 font-mono text-[10.5px] text-ink-3">
        #{node.id} · by {node.by}{node.verified ? " · verified" : ""}
      </p>
      <h3 className="mt-5 font-mono text-[10.5px] uppercase tracking-[0.18em] text-ink-3">
        {links.length ? `Linked to ${links.length}` : "No links yet"}
      </h3>
      <ul className="mt-2 space-y-px">
        {links.map(({ node: n, axon }) => (
          <li key={`${n.id}-${axon.kind}`}>
            <button
              type="button"
              onClick={() => onPick(n.id)}
              className="group w-full border border-transparent px-2 py-2 text-left outline-none hover:border-line hover:bg-surface-2 focus-visible:border-line"
            >
              <span className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-3">
                <span className="size-1.5 shrink-0" style={{ background: branchVar(n.branch) }} />
                {LINK_LABEL[axon.kind]} {axon.kind === "related" ? Math.round(axon.w * 100) + "%" : ""}
                {n.branch !== branchOf(node) && <span className="text-ink-2">· {n.branch}</span>}
              </span>
              <span className="mt-1 line-clamp-2 block text-[12.5px] leading-[1.45] text-ink-2 group-hover:text-ink">{n.data.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Matches({ nodes, onPick }: { nodes: GraphNode[]; onPick: (id: number) => void }) {
  return (
    <div>
      <h3 className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-ink-3">
        {nodes.length ? `${nodes.length} match${nodes.length === 1 ? "" : "es"}` : "No memory matches"}
      </h3>
      <ul className="mt-2 space-y-px">
        {nodes.slice(0, 40).map((n) => (
          <li key={n.id}>
            <button type="button" onClick={() => onPick(n.id)} className="group w-full border border-transparent px-2 py-2 text-left outline-none hover:border-line hover:bg-surface-2 focus-visible:border-line">
              <Meta node={n} />
              <span className="mt-1 line-clamp-2 block text-[12.5px] leading-[1.45] text-ink-2 group-hover:text-ink">{n.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Idle({ firings }: { firings: Firing[] }) {
  return (
    <div>
      <p className="text-[12.5px] leading-[1.55] text-ink-2">
        Each dot is a memory, each line a link. Click one to read it and see what it connects to. When an agent recalls,
        saves or forgets, the memories it touched light up here.
      </p>
      <h3 className="mt-5 font-mono text-[10.5px] uppercase tracking-[0.18em] text-ink-3">Recent firing</h3>
      {firings.length === 0 ? (
        <p className="mt-2 text-[12px] text-ink-3">Nothing since this page opened.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {firings.map((f) => (
            <li key={f.key} className="text-[12px] leading-[1.45]">
              <span className="font-mono text-[10.5px] text-ink-3">
                <span className="mr-1.5 inline-block size-1.5" style={{ background: `var(${TONE[f.tone]})` }} />
                {f.agent} · {f.action} · {f.neurons} neuron{f.neurons === 1 ? "" : "s"} · {ago(f.at, Date.now())}
              </span>
              {f.detail && <span className="block truncate text-ink-2">{f.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FiringOverlay({ firings }: { firings: Firing[] }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  const fresh = firings.filter((f) => Date.now() - Date.parse(f.at) < 6000);
  return (
    <div className="pointer-events-none absolute left-3 top-3 flex max-w-[min(420px,80%)] flex-col gap-1.5">
      <AnimatePresence initial={false}>
        {fresh.map((f) => (
          <motion.div
            key={f.key}
            layout
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4, transition: { duration: 0.25 } }}
            transition={{ duration: 0.35, ease: EASE_IOS }}
            className="flex items-center gap-2 border border-line bg-surface/85 px-2.5 py-1.5 font-mono text-[10.5px] text-ink-2 backdrop-blur"
          >
            <span className="size-1.5 shrink-0" style={{ background: `var(${TONE[f.tone]})` }} />
            <span className="shrink-0 text-ink">{f.agent}</span>
            <span className="shrink-0">{f.action}</span>
            {f.detail && <span className="truncate text-ink-3">{f.detail}</span>}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-line px-5 py-3 font-mono text-[10.5px] text-ink-3">
      <span className="flex items-center gap-2">
        <svg width="18" height="6" aria-hidden><path d="M1 3 H17" stroke="currentColor" strokeWidth="1.2" /></svg>related
      </span>
      <span className="flex items-center gap-2">
        <svg width="18" height="6" aria-hidden><path d="M1 3 H17" stroke="currentColor" strokeWidth="1.2" strokeDasharray="1.5 3.5" strokeLinecap="round" /></svg>saved together
      </span>
      <span className="flex items-center gap-2">
        <svg width="8" height="8" aria-hidden><circle cx="4" cy="4" r="3" fill="currentColor" /></svg>fact
      </span>
      <span className="flex items-center gap-2">
        <svg width="8" height="8" aria-hidden><circle cx="4" cy="4" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>episode
      </span>
      <span className="flex items-center gap-2">
        <svg width="10" height="10" aria-hidden><circle cx="5" cy="5" r="4.2" fill="currentColor" /><circle cx="5" cy="5" r="1.7" className="fill-surface" /></svg>core
      </span>
      <span className="ml-auto hidden text-ink-3 sm:inline">drag to pan · ctrl/⌘ + scroll to zoom · double-click to fit</span>
    </div>
  );
}
