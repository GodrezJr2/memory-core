import { motion } from "framer-motion";
import { ago, fmtInt, freshness } from "../lib/format";
import { IOS_SPRING, TONE_VAR } from "../lib/motion";
import type { AgentState } from "../lib/types";
import { HourBars, TileHeader } from "./Tile";

interface Props {
  agents: AgentState[];
  now: number;
  filter: string | null;
  onFilter: (id: string | null) => void;
}

/** Every agent with a token, most recently active first. Selecting one filters the feed. */
export function AgentsPanel({ agents, now, filter, onFilter }: Props) {
  const sorted = [...agents].sort(
    (a, b) => (b.last_seen ? Date.parse(b.last_seen) : 0) - (a.last_seen ? Date.parse(a.last_seen) : 0) || a.id.localeCompare(b.id),
  );
  return (
    <div className="flex h-full flex-col">
      <TileHeader label="Agents" meta="24h · select to filter" />
      <ul className="mt-3 flex-1 divide-y divide-line border-t border-line">
        {sorted.map((a) => {
          const f = freshness(a.last_seen, now);
          const selected = filter === a.id;
          return (
            <li key={a.id} className="relative">
              {selected && <motion.span layoutId="agent-select" className="absolute inset-y-0 left-0 w-0.5 bg-ink" transition={IOS_SPRING} />}
              <button
                type="button"
                aria-pressed={selected}
                onClick={() => onFilter(selected ? null : a.id)}
                className={`grid w-full grid-cols-[10px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 px-5 py-3.5 text-left outline-none transition-colors duration-200 hover:bg-surface-2 focus-visible:bg-surface-2 ${selected ? "bg-surface-2" : ""}`}
              >
                <StatusSquare state={f} color={TONE_VAR[a.tone ?? "context"]} />
                <span className="flex min-w-0 items-center gap-2">
                  <span className={`truncate text-[13.5px] font-medium tracking-[-0.01em] ${a.last_seen ? "text-ink" : "text-ink-2"}`}>{a.id}</span>
                  {a.clients.map((c) => (
                    <span key={c} className="border border-line px-1 font-mono text-[9.5px] uppercase leading-[15px] tracking-[0.08em] text-ink-3">
                      {c}
                    </span>
                  ))}
                </span>
                <span className="font-mono text-[11.5px] tabular-nums text-ink-2">{ago(a.last_seen, now)}</span>
                <span className="col-start-2 col-end-4 flex items-end justify-between gap-3">
                  <HourBars hourly={a.hourly} height={16} bar={3} gap={1} />
                  <span className="truncate font-mono text-[10.5px] tabular-nums text-ink-3">
                    {fmtInt(a.calls_24h)} calls · {fmtInt(a.memories)} written
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function StatusSquare({ state, color }: { state: "live" | "recent" | "idle"; color: string }) {
  if (state === "live") {
    return (
      <span className="relative block size-2">
        <motion.span
          className="absolute -inset-1"
          style={{ background: color, filter: "blur(3px)" }}
          animate={{ opacity: [0.15, 0.6, 0.15] }}
          transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
        />
        <span className="absolute inset-0" style={{ background: color }} />
      </span>
    );
  }
  return <span className={`block size-2 ${state === "recent" ? "bg-ink-3" : "border border-line-strong"}`} />;
}
