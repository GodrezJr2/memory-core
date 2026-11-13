import { motion } from "framer-motion";
import { freshness, fmtInt, fmtMs } from "../lib/format";
import { useAnimatedNumber } from "../lib/hooks";
import { TONE_VAR } from "../lib/motion";
import type { AgentState, CallEvent, Totals } from "../lib/types";
import { HourBars, TileHeader } from "./Tile";

interface Props {
  totals: Totals;
  agents: AgentState[];
  feed: CallEvent[];
  now: number;
}

const NUMBER = "font-mono text-[40px] font-medium leading-none tracking-[-0.045em] tabular-nums sm:text-[46px]";
const CELL = "col-span-6 flex min-h-[168px] flex-col bg-surface lg:col-span-3";

/** Top row of the bento: memories, calls, latency, and who is syncing right now. */
export function StatsBento({ totals, agents, feed, now }: Props) {
  return (
    <>
      <MemoriesTile total={totals.memories} agents={agents} />
      <CallsTile total={totals.calls_24h} agents={agents} />
      <LatencyTile avg={totals.avg_ms_24h} feed={feed} />
      <SyncingTile agents={agents} now={now} />
    </>
  );
}

function MemoriesTile({ total, agents }: { total: number; agents: AgentState[] }) {
  const text = useAnimatedNumber(total, fmtInt);
  const writers = agents.filter((a) => a.memories > 0).sort((a, b) => b.memories - a.memories);
  const sum = writers.reduce((s, a) => s + a.memories, 0) || 1;
  const shades = ["bg-ink", "bg-ink-2", "bg-ink-3", "bg-line-strong"];
  return (
    <section className={CELL}>
      <TileHeader label="Memories" meta="live" metaClassName="max-sm:hidden" />
      <motion.div className={`${NUMBER} px-5 pt-5`}>{text}</motion.div>
      <div className="mt-auto px-5 pb-4">
        <div className="flex h-1.5 w-full gap-px" aria-hidden>
          {writers.map((a, i) => (
            <div key={a.id} className={shades[Math.min(i, shades.length - 1)]} style={{ width: `${(a.memories / sum) * 100}%` }} title={`${a.id}: ${a.memories}`} />
          ))}
        </div>
        <p className="mt-2 truncate font-mono text-[11px] text-ink-3">
          {writers.length ? `written by ${writers.map((a) => a.id).slice(0, 2).join(", ")}${writers.length > 2 ? ` +${writers.length - 2}` : ""}` : "nothing written yet"}
        </p>
      </div>
    </section>
  );
}

function CallsTile({ total, agents }: { total: number; agents: AgentState[] }) {
  const text = useAnimatedNumber(total, fmtInt);
  const hourly = Array.from({ length: 24 }, (_, i) => agents.reduce((s, a) => s + (a.hourly[i] ?? 0), 0));
  return (
    <section className={CELL}>
      <TileHeader label="Calls · 24h" meta="per hour" metaClassName="max-sm:hidden" />
      <motion.div className={`${NUMBER} px-5 pt-5`}>{text}</motion.div>
      <div className="mt-auto px-5 pb-4">
        <HourBars hourly={hourly} height={26} className="w-full" />
        <p className="mt-2 font-mono text-[11px] text-ink-3">24h ago → now</p>
      </div>
    </section>
  );
}

function LatencyTile({ avg, feed }: { avg: number | null; feed: CallEvent[] }) {
  const text = useAnimatedNumber(avg ?? 0, (n) => (avg == null ? "–" : String(Math.round(n))));
  const recent = feed.filter((e) => e.ok && e.action === "recall").slice(0, 32).reverse();
  const last = recent.at(-1);
  return (
    <section className={CELL}>
      <TileHeader label="Latency" meta="avg 24h" metaClassName="max-sm:hidden" />
      <div className="flex items-baseline gap-1.5 px-5 pt-5">
        <motion.span className={NUMBER}>{text}</motion.span>
        <span className="font-mono text-[13px] text-ink-3">ms</span>
      </div>
      <div className="mt-auto px-5 pb-4">
        <Sparkline values={recent.map((e) => e.ms)} />
        <p className="mt-2 font-mono text-[11px] text-ink-3">recall · last {fmtMs(last?.ms ?? null)}</p>
      </div>
    </section>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const W = 240;
  const H = 26;
  if (values.length < 2) return <div className="h-[26px] border-b border-line" />;
  const max = Math.max(...values) * 1.1;
  const step = W / (values.length - 1);
  const pts = values.map((v, i) => [i * step, H - (v / max) * H] as const);
  const [lx, ly] = pts[pts.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="block h-[26px] w-full overflow-visible" aria-hidden>
      <line x1={0} x2={W} y1={H - 0.5} y2={H - 0.5} className="stroke-line" vectorEffect="non-scaling-stroke" />
      <polyline
        points={pts.map(([x, y]) => `${x},${y}`).join(" ")}
        fill="none"
        className="stroke-ink-2"
        strokeWidth={1.25}
        strokeLinejoin="miter"
        vectorEffect="non-scaling-stroke"
      />
      <rect x={lx - 2} y={ly - 2} width={4} height={4} style={{ fill: TONE_VAR.recall }} />
    </svg>
  );
}

function SyncingTile({ agents, now }: { agents: AgentState[]; now: number }) {
  const live = agents.filter((a) => freshness(a.last_seen, now) === "live");
  return (
    <section className={CELL}>
      <TileHeader label="Syncing" meta="last 2 min" metaClassName="max-sm:hidden" />
      <div className="flex items-baseline gap-1 px-5 pt-5">
        <span className={NUMBER}>{live.length}</span>
        <span className="font-mono text-[20px] tracking-[-0.03em] text-ink-3">/{agents.length}</span>
      </div>
      <div className="mt-auto flex flex-wrap gap-1.5 px-5 pb-4">
        {agents.map((a) => {
          const on = freshness(a.last_seen, now) === "live";
          return (
            <span key={a.id} className="relative size-3 border border-line-strong" title={a.id}>
              {on && (
                <motion.span
                  className="absolute inset-0"
                  style={{ background: TONE_VAR[a.tone ?? "context"] }}
                  animate={{ opacity: [1, 0.45, 1] }}
                  transition={{ duration: 2.2, repeat: Infinity, ease: "easeInOut" }}
                />
              )}
            </span>
          );
        })}
        <p className="mt-1 w-full truncate font-mono text-[11px] text-ink-3">
          {live.length ? live.map((a) => a.id).join(", ") : "all quiet"}
        </p>
      </div>
    </section>
  );
}
