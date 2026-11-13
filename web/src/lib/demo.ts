import type { AgentStats, CallEvent, Snapshot } from "./types";

/** Fake traffic for ?demo: looks like a normal afternoon of agents using memory-core. */

const AGENTS: [string, number][] = [
  ["claude-laptop", 0.4],
  ["aider-laptop", 0.33],
  ["claude-server", 0.17],
  ["codex-server", 0.1],
];
const QUERIES = [
  "kenapa server sering freeze",
  "which port does stocktracker run on",
  "how do I expose a new service on .home",
  "nvme apst grub setting",
  "bank categorizer backend port",
  "is jellyfin public through funnel",
  "restart the memory-core embedder",
  "tailscale serve ports on homeserver",
  "opensense keyboard rgb service",
  "aider yes-always behaviour",
];
const FACTS = [
  "StockTracker runs as a systemd service with uvicorn on port 8765.",
  "homeserver reboots with reboot=pci so a wedged NVMe gets power-cycled.",
  "All three Aider configs run with yes-always.",
  "The memory-core live view opens only over Tailscale.",
  "OpenSense needs AcerLightingService enabled for keyboard RGB.",
];
const SCOPES = ["project:homeserver", "project:memory-core", "project:stocktracker", "project:opensense", null];

const pick = <T,>(xs: readonly T[]) => xs[Math.floor(Math.random() * xs.length)];
const between = (lo: number, hi: number) => Math.round(lo + Math.random() * (hi - lo));

function weighted(): string {
  let r = Math.random();
  for (const [id, w] of AGENTS) if ((r -= w) <= 0) return id;
  return AGENTS[0][0];
}

function makeCall(agent: string, at: Date): CallEvent {
  const r = Math.random();
  const action = r < 0.58 ? "recall" : r < 0.76 ? "remember" : r < 0.9 ? "profile" : r < 0.96 ? "timeline" : "forget";
  const client = agent === "aider-laptop" ? "cli" : agent.startsWith("claude") && action !== "remember" ? "hook" : "mcp";
  const ok = Math.random() > 0.03;
  const base = { at: at.toISOString(), agent, client, action, ok, error: ok ? null : "embedder timed out", note: null };
  switch (action) {
    case "recall":
      return { ...base, scope: pick(SCOPES), detail: pick(QUERIES), results: between(0, 5), ms: between(110, 340) };
    case "remember":
      return {
        ...base, scope: pick(SCOPES), detail: pick(FACTS), results: between(1, 2), ms: between(60, 420),
        note: Math.random() < 0.2 ? "1 held" : null,
      };
    case "forget":
      return { ...base, scope: null, detail: `#${between(20, 60)}: moved to a new port`, results: 1, ms: between(5, 20) };
    case "timeline":
      return { ...base, scope: null, detail: "since 2026-09-24", results: between(1, 6), ms: between(10, 40) };
    default:
      return { ...base, scope: null, detail: null, results: 5, ms: between(18, 60) };
  }
}

export function startDemo(sink: { hello: (s: Snapshot) => void; call: (e: CallEvent) => void }): () => void {
  const now = Date.now();
  const recent = Array.from({ length: 36 }, () => makeCall(weighted(), new Date(now - Math.random() * 40 * 60_000)))
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const agents: AgentStats[] = AGENTS.map(([id, w]) => {
    const hourly = Array.from({ length: 24 }, (_, i) => Math.round(Math.random() * w * (i > 8 ? 26 : 6)));
    return {
      id,
      last_seen: recent.find((e) => e.agent === id)?.at ?? null,
      calls_24h: hourly.reduce((a, b) => a + b, 0),
      hourly,
      memories: Math.round(w * 35),
    };
  });
  sink.hello({
    now: new Date(now).toISOString(),
    agents,
    totals: { memories: 35, calls_24h: agents.reduce((a, x) => a + x.calls_24h, 0), avg_ms_24h: 168 },
    recent,
  });
  let timer = 0;
  const tick = () => {
    sink.call(makeCall(weighted(), new Date()));
    timer = window.setTimeout(tick, 650 + Math.random() * 2300);
  };
  timer = window.setTimeout(tick, 1200);
  return () => window.clearTimeout(timer);
}
