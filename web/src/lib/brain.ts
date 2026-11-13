/** Brain tab: every live memory as a neuron, memory_link rows as the axons between them (/api/v1/web/graph). */

export type LinkKind = "related" | "together" | "explicit";

export interface GraphNode {
  id: number;
  kind: string; // core | fact | episode | procedure | doc_chunk
  scope: string;
  domain: string | null; // null for global and agent notes: the core
  text: string;
  by: string;
  date: string;
  verified: boolean;
}

export interface GraphLink {
  a: number;
  b: number;
  kind: LinkKind;
  w: number;
}

export interface Graph {
  domains: { name: string; title: string }[];
  nodes: GraphNode[];
  links: GraphLink[];
}

/** The branch of memories without one (global profile facts, agent notes), drawn in the middle. */
export const CORE = "core";

export const branchOf = (n: GraphNode) => n.domain ?? CORE;

/** The cluster a memory sits in: its project inside its branch; the core is one cluster. */
export const clusterOf = (n: GraphNode) => (n.domain == null ? CORE : n.scope);

export const branchVar = (b: string) => `var(--b-${b}, var(--b-other))`;

/** The branches with their own hue in index.css; any other (a branch the user accepted later) borrows a spare one. */
export const FIXED_BRANCHES = [CORE, "study", "work", "projects", "infra", "personal", "other"];
const SPARE_HUES = 4;

let painted: string[] = [];

/** Point --b-<name> of each newer branch at a spare hue (--b-x1..4), so the canvas and the legend agree. */
export function paintNewBranches(graph: Graph) {
  const root = document.documentElement.style;
  painted = graph.domains.map((d) => d.name).filter((b) => !FIXED_BRANCHES.includes(b));
  painted.forEach((b, i) => root.setProperty(`--b-${b}`, i < SPARE_HUES ? `var(--b-x${i + 1})` : "var(--b-other)"));
}

export const paintedBranches = () => painted;

export async function loadGraph(signal?: AbortSignal): Promise<Graph> {
  if (new URLSearchParams(location.search).has("demo")) return demoGraph();
  const res = await fetch("/api/v1/web/graph", { cache: "no-store", signal });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
  return body as Graph;
}

export const shortScope = (scope: string) => scope.replace(/^project:/, "");

/** A stable pseudo-random number in [0, 1) for an id, so the layout and the axon bends do not change per load. */
export function hash01(n: number): number {
  let x = Math.imul(n ^ 0x9e3779b9, 0x85ebca6b);
  x ^= x >>> 13;
  x = Math.imul(x, 0xc2b2ae35);
  x ^= x >>> 16;
  return (x >>> 0) / 4294967296;
}

/** ?demo: a made-up brain shaped like the real one, for working on the page offline. */
function demoGraph(): Graph {
  const domains = [
    { name: "study", title: "Study" }, { name: "work", title: "Work" }, { name: "projects", title: "Side projects" },
    { name: "infra", title: "Infra and agent tooling" }, { name: "personal", title: "Personal" },
  ];
  const scopes: [string | null, string, number][] = [
    [null, "global", 18], ["study", "project:lms-automation", 40], ["study", "project:course-ml-lab", 28],
    ["work", "project:job-hunt", 12], ["projects", "project:car-api", 14], ["projects", "project:stocktracker", 8],
    ["infra", "project:memory-core", 34], ["infra", "project:homeserver", 22], ["infra", "project:opensense", 40],
    ["personal", "project:trip-2027", 6], ["personal", "project:racing-setups", 9],
  ];
  const nodes: GraphNode[] = [];
  let id = 1;
  for (const [domain, scope, n] of scopes) {
    for (let i = 0; i < n; i++, id++) {
      const kind = domain === null ? "core" : hash01(id) < 0.12 ? "episode" : "fact";
      nodes.push({ id, kind, scope, domain, text: `Demo memory #${id} in ${scope}.`, by: "claude-laptop", date: "2026-10-08", verified: false });
    }
  }
  const links: GraphLink[] = [];
  for (const n of nodes) {
    const same = nodes.filter((m) => m.scope === n.scope && m.id > n.id);
    if (same.length && hash01(n.id * 7) < 0.8) links.push({ a: n.id, b: same[Math.floor(hash01(n.id * 3) * same.length)].id, kind: "related", w: 0.7 + hash01(n.id) * 0.25 });
    if (same.length > 1 && hash01(n.id * 11) < 0.5) links.push({ a: n.id, b: same[0].id, kind: "together", w: 0.8 });
    if (hash01(n.id * 13) < 0.06) links.push({ a: n.id, b: nodes[Math.floor(hash01(n.id * 17) * nodes.length)].id, kind: "related", w: 0.72 });
  }
  return { domains, nodes, links: links.filter((l) => l.a !== l.b) };
}
