import { AnimatePresence, motion } from "framer-motion";
import { memo } from "react";
import { actionLabel, clock, eventKey, fmtMs, resultLabel, toneOf } from "../lib/format";
import { EASE_IOS, IOS_SPRING, TONE_VAR } from "../lib/motion";
import type { CallEvent } from "../lib/types";
import { TileHeader } from "./Tile";

const ROWS = 80;
const COLS = "md:grid-cols-[84px_minmax(0,190px)_118px_minmax(0,1fr)_minmax(0,180px)_72px]";

interface Props {
  feed: CallEvent[];
  filter: string | null;
  onClear: () => void;
}

/** Every tool call as it happens, newest on top. */
export function LiveFeed({ feed, filter, onClear }: Props) {
  const rows = (filter ? feed.filter((e) => e.agent === filter) : feed).slice(0, ROWS);
  return (
    <div>
      <TileHeader
        label="Live feed"
        meta={
          filter ? (
            <button type="button" onClick={onClear} className="border border-line-strong px-2 py-0.5 text-ink transition-colors hover:bg-surface-2">
              {filter} ✕
            </button>
          ) : (
            `${rows.length} latest`
          )
        }
      />
      <div className={`mt-3 hidden border-y border-line px-5 py-2 font-mono text-[10px] uppercase tracking-[0.16em] text-ink-3 md:grid md:gap-4 ${COLS}`}>
        <span>Time</span>
        <span>Agent</span>
        <span>Action</span>
        <span>Detail</span>
        <span>Result</span>
        <span className="text-right">Latency</span>
      </div>
      <ul className="divide-y divide-line border-b border-line max-md:mt-3 max-md:border-t">
        <AnimatePresence initial={false}>
          {rows.map((e) => (
            <FeedRow key={eventKey(e)} e={e} />
          ))}
        </AnimatePresence>
      </ul>
      {!rows.length && (
        <p className="px-5 py-8 text-[13px] text-ink-3">Quiet so far. A call lands here the moment an agent touches memory.</p>
      )}
    </div>
  );
}

const FeedRow = memo(function FeedRow({ e }: { e: CallEvent }) {
  const color = TONE_VAR[toneOf(e)];
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, transition: { duration: 0.15 } }}
      transition={IOS_SPRING}
      className={`relative grid grid-cols-[auto_minmax(0,1fr)_auto] items-baseline gap-x-4 gap-y-1 px-5 py-2.5 md:gap-y-0 ${COLS}`}
    >
      <motion.span
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{ background: color }}
        initial={{ opacity: 0.1 }}
        animate={{ opacity: 0 }}
        transition={{ duration: 1.8, ease: EASE_IOS }}
      />
      <time dateTime={e.at} className="font-mono text-[11.5px] tabular-nums text-ink-3">
        {clock(e.at)}
      </time>
      <span className="min-w-0 truncate text-[13px] font-medium" title={`${e.agent} via ${e.client}`}>
        {e.agent}
        <span className="ml-1.5 font-mono text-[10.5px] font-normal text-ink-3">{e.client}</span>
      </span>
      <span className="flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.12em] max-md:col-start-1 max-md:row-start-2" style={{ color }}>
        <span className="size-1.5 shrink-0" style={{ background: color }} />
        {actionLabel(e)}
      </span>
      <span
        className={`min-w-0 truncate text-[13px] max-md:col-span-3 max-md:row-start-3 max-md:whitespace-normal ${e.detail ? "text-ink" : "text-ink-3"}`}
        title={[e.detail, e.scope].filter(Boolean).join("\n")}
      >
        {e.detail ?? "—"}
        {e.scope && <span className="ml-2 font-mono text-[10.5px] text-ink-3">{e.scope}</span>}
      </span>
      <span
        className={`min-w-0 truncate font-mono text-[11.5px] max-md:col-start-2 max-md:row-start-2 max-md:text-right ${e.ok ? "text-ink-2" : "text-error"}`}
        title={resultLabel(e)}
      >
        {resultLabel(e)}
      </span>
      <span className="text-right font-mono text-[11.5px] tabular-nums text-ink-3 max-md:col-start-3 max-md:row-start-1">{fmtMs(e.ms)}</span>
    </motion.li>
  );
});
