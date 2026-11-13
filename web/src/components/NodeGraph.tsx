import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ago, fmtInt, freshness, toneOf } from "../lib/format";
import { computeGeometry, type Box, type Geometry, type NodeGeom } from "../lib/geometry";
import { useAnimatedNumber, useElementWidth } from "../lib/hooks";
import { EASE_IOS, TONE_VAR } from "../lib/motion";
import type { AgentState, CallEvent, Tone } from "../lib/types";

interface Signal {
  id: number;
  agent: string;
  tone: Tone;
  results: number;
  reverse: boolean; // the answer travelling back from the bank
}
interface Ripple {
  id: number;
  tone: Tone;
  agent: string;
}

interface Props {
  agents: Record<string, AgentState>;
  order: string[];
  memories: number;
  now: number;
  subscribe: (fn: (e: CallEvent) => void) => () => void;
}

let nextId = 1;

/** Agents wired to the memory bank. Each call sends a dash down its connector; arrival ripples the bank. */
export function NodeGraph({ agents, order, memories, now, subscribe }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const width = useElementWidth(wrap);
  const geo = useMemo(() => computeGeometry(order, width > 0 && width < 640), [order, width]);
  const reduced = useReducedMotion() ?? false;

  const [signals, setSignals] = useState<Signal[]>([]);
  const [ripples, setRipples] = useState<Ripple[]>([]);
  const [flashes, setFlashes] = useState<Record<string, { tone: Tone; n: number }>>({});
  // The bank's number changes when a signal lands, not when the event arrives.
  const [bankCount, setBankCount] = useState(memories);
  const latest = useRef(memories);
  latest.current = memories;

  useEffect(() => {
    if (signals.length === 0) setBankCount(memories);
  }, [memories, signals.length]);

  useEffect(
    () =>
      subscribe((e) => {
        const tone = toneOf(e);
        setFlashes((f) => ({ ...f, [e.agent]: { tone, n: (f[e.agent]?.n ?? 0) + 1 } }));
        if (reduced) {
          setRipples((r) => [...r.slice(-6), { id: nextId++, tone, agent: e.agent }]);
          return;
        }
        const results = e.ok ? e.results ?? 0 : 0;
        setSignals((s) => [...s.slice(-30), { id: nextId++, agent: e.agent, tone, results, reverse: false }]);
      }),
    [subscribe, reduced],
  );

  const arrive = useCallback((sig: Signal) => {
    setRipples((r) => [...r.slice(-6), { id: nextId++, tone: sig.tone, agent: sig.agent }]);
    setBankCount(latest.current);
    if (sig.results > 0) setSignals((s) => [...s, { ...sig, id: nextId++, reverse: true }]);
  }, []);
  const finish = useCallback((id: number) => setSignals((s) => s.filter((x) => x.id !== id)), []);
  const unripple = useCallback((id: number) => setRipples((r) => r.filter((x) => x.id !== id)), []);

  const live = (id: string) => freshness(agents[id]?.last_seen ?? null, now) === "live";
  const anyLive = order.some(live);
  const byId = new Map(geo.nodes.map((n) => [n.id, n]));

  return (
    <div ref={wrap} className="relative">
      <svg viewBox={`0 0 ${geo.W} ${geo.H}`} className="block h-auto w-full select-none" role="img" aria-label="Agents connected to the memory bank">
        <Defs />
        <rect width={geo.W} height={geo.H} fill="url(#mc-grid)" mask="url(#mc-fade)" />

        {geo.nodes.map((n) => (
          <Connector key={n.id} node={n} live={live(n.id)} tone={agents[n.id]?.tone ?? "context"} />
        ))}

        {signals.map((s) => {
          const node = byId.get(s.agent);
          return node ? <SignalDash key={s.id} signal={s} d={node.d} onArrive={arrive} onDone={finish} /> : null;
        })}

        <Bank geo={geo} count={bankCount} anyLive={anyLive} ripples={ripples} onRippleDone={unripple} reduced={reduced} />

        {geo.nodes.map((n) => {
          const agent = agents[n.id];
          return agent ? (
            <AgentNode key={n.id} node={n} agent={agent} live={live(n.id)} now={now} flash={flashes[n.id]} reduced={reduced} compact={geo.compact} />
          ) : null;
        })}
      </svg>
    </div>
  );
}

function Defs() {
  return (
    <defs>
      <pattern id="mc-grid" width="24" height="24" patternUnits="userSpaceOnUse">
        <path d="M 24 0 H 0 V 24" fill="none" className="stroke-grid" strokeWidth="1" />
      </pattern>
      <radialGradient id="mc-fade-g" cx="50%" cy="50%" r="65%">
        <stop offset="0%" stopColor="white" />
        <stop offset="100%" stopColor="white" stopOpacity="0" />
      </radialGradient>
      <mask id="mc-fade">
        <rect width="100%" height="100%" fill="url(#mc-fade-g)" />
      </mask>
      <filter id="mc-glow" x="-30%" y="-60%" width="160%" height="220%">
        <feGaussianBlur stdDeviation="4" />
      </filter>
    </defs>
  );
}

function Connector({ node, live, tone }: { node: NodeGeom; live: boolean; tone: Tone }) {
  const sq = (p: { x: number; y: number }, s: number) => ({ x: p.x - s / 2, y: p.y - s / 2, width: s, height: s });
  return (
    <g shapeRendering="crispEdges">
      <path
        d={node.d}
        fill="none"
        strokeWidth={1}
        className={`transition-[stroke] duration-500 ${live ? "stroke-ink-3" : "stroke-line-strong"}`}
      />
      {node.bends.map((b, i) => (
        <rect key={i} {...sq(b, 3)} className={live ? "fill-ink-3" : "fill-line-strong"} />
      ))}
      <rect {...sq(node.end, 5)} style={live ? { fill: TONE_VAR[tone] } : undefined} className={live ? "" : "fill-line-strong"} />
    </g>
  );
}

/** A short glowing dash that runs along the connector. Forward: agent -> bank. Reverse: the answer coming back. */
function SignalDash({ signal, d, onArrive, onDone }: {
  signal: Signal;
  d: string;
  onArrive: (s: Signal) => void;
  onDone: (id: number) => void;
}) {
  const landed = useRef(false);
  const color = TONE_VAR[signal.tone];
  const length = signal.reverse ? 0.05 : 0.09;
  return (
    <motion.path
      d={d}
      fill="none"
      stroke={color}
      strokeWidth={signal.reverse ? 1.5 : 2.5}
      strokeLinecap="butt"
      strokeLinejoin="miter"
      style={{ filter: `drop-shadow(0 0 3px ${color}) drop-shadow(0 0 8px ${color})` }}
      initial={{ pathLength: length, pathOffset: signal.reverse ? 1 - length : 0, opacity: signal.reverse ? 0.75 : 1 }}
      animate={{ pathOffset: signal.reverse ? -length : 1 }}
      transition={{ duration: signal.reverse ? 0.75 : 0.95, ease: EASE_IOS }}
      onUpdate={(v) => {
        // expo-out creeps over the last stretch, so count the dash as landed once its head is at the bank
        const offset = Number(v.pathOffset ?? 0);
        if (!signal.reverse && !landed.current && offset >= 1 - length - 0.015) {
          landed.current = true;
          onArrive(signal);
        }
      }}
      onAnimationComplete={() => onDone(signal.id)}
    />
  );
}

function Bank({ geo, count, anyLive, ripples, onRippleDone, reduced }: {
  geo: Geometry;
  count: number;
  anyLive: boolean;
  ripples: Ripple[];
  onRippleDone: (id: number) => void;
  reduced: boolean;
}) {
  const { hub, compact } = geo;
  const text = useAnimatedNumber(count, fmtInt, 0.9);
  const last = ripples.at(-1);
  const cx = hub.x + hub.w / 2;
  const cy = hub.y + hub.h / 2;
  return (
    <g>
      <CornerTicks box={hub} />
      <rect x={hub.x} y={hub.y} width={hub.w} height={hub.h} className="fill-surface stroke-line-strong" strokeWidth={1} shapeRendering="crispEdges" />
      <rect x={hub.x + 6} y={hub.y + 6} width={hub.w - 12} height={hub.h - 12} fill="none" className="stroke-line" strokeWidth={1} shapeRendering="crispEdges" />

      {/* border flash on each landing */}
      {last && (
        <motion.rect
          key={last.id}
          x={hub.x} y={hub.y} width={hub.w} height={hub.h}
          fill="none" stroke={TONE_VAR[last.tone]} strokeWidth={1.5} shapeRendering="crispEdges"
          initial={{ opacity: 1 }} animate={{ opacity: 0 }} transition={{ duration: 1.1, ease: EASE_IOS }}
        />
      )}
      <AnimatePresence>
        {ripples.map((r) => (
          <motion.rect
            key={r.id}
            x={hub.x} y={hub.y} width={hub.w} height={hub.h}
            fill="none" stroke={TONE_VAR[r.tone]} strokeWidth={1.25} shapeRendering="crispEdges"
            style={{ transformBox: "fill-box", transformOrigin: "center" }}
            initial={{ scale: 1, opacity: 0.9 }}
            animate={{ scale: reduced ? 1 : 1.15, opacity: 0 }}
            transition={{ duration: 0.9, ease: EASE_IOS }}
            onAnimationComplete={() => onRippleDone(r.id)}
          />
        ))}
      </AnimatePresence>

      <text x={hub.x + 18} y={hub.y + 28} className="fill-ink-3 font-mono text-[10px] uppercase tracking-[0.2em]">
        Memory bank
      </text>
      <rect x={hub.x + hub.w - 24} y={hub.y + 21} width={6} height={6} className={anyLive ? "" : "fill-line-strong"} style={anyLive ? { fill: "var(--recall)" } : undefined}>
        {anyLive && !reduced && <animate attributeName="opacity" values="1;0.3;1" dur="1.6s" repeatCount="indefinite" />}
      </rect>
      <motion.text
        x={cx}
        y={cy + (compact ? 12 : 14)}
        textAnchor="middle"
        className={`fill-ink font-mono font-medium tabular-nums tracking-[-0.05em] ${compact ? "text-[40px]" : "text-[52px]"}`}
      >
        {text}
      </motion.text>
      <text x={cx} y={cy + (compact ? 34 : 40)} textAnchor="middle" className="fill-ink-2 text-[12px] font-medium tracking-[0.01em]">
        memories
      </text>
      {!compact && (
        <g className="fill-ink-3 font-mono text-[9.5px] tracking-[0.08em]">
          <text x={hub.x + 18} y={hub.y + hub.h - 18}>PGVECTOR · HNSW</text>
          <text x={hub.x + hub.w - 18} y={hub.y + hub.h - 18} textAnchor="end">1024D</text>
        </g>
      )}
    </g>
  );
}

function CornerTicks({ box, o = 9, len = 14 }: { box: Box; o?: number; len?: number }) {
  const { x, y, w, h } = box;
  const d = [
    `M ${x - o} ${y - o + len} V ${y - o} H ${x - o + len}`,
    `M ${x + w + o - len} ${y - o} H ${x + w + o} V ${y - o + len}`,
    `M ${x + w + o} ${y + h + o - len} V ${y + h + o} H ${x + w + o - len}`,
    `M ${x - o + len} ${y + h + o} H ${x - o} V ${y + h + o - len}`,
  ].join(" ");
  return <path d={d} fill="none" className="stroke-ink-3" strokeWidth={1} shapeRendering="crispEdges" />;
}

function AgentNode({ node, agent, live, now, flash, reduced, compact }: {
  node: NodeGeom;
  agent: AgentState;
  live: boolean;
  now: number;
  flash?: { tone: Tone; n: number };
  reduced: boolean;
  compact: boolean;
}) {
  const { box } = node;
  const color = TONE_VAR[agent.tone ?? "context"];
  const breathing = live && !reduced;
  return (
    <g transform={`translate(${box.x} ${box.y})`}>
      {/* breathing glow while syncing; gone the moment the agent goes quiet */}
      <motion.rect
        width={box.w}
        height={box.h}
        fill="none"
        stroke={color}
        strokeWidth={2}
        filter="url(#mc-glow)"
        initial={false}
        animate={breathing ? { opacity: [0.18, 0.6, 0.18] } : { opacity: live ? 0.4 : 0 }}
        transition={breathing ? { duration: 2.6, repeat: Infinity, ease: "easeInOut" } : { duration: 0.12 }}
      />
      <rect width={box.w} height={box.h} className="fill-surface stroke-line-strong" strokeWidth={1} shapeRendering="crispEdges" />
      {live && <rect width={2} height={box.h} style={{ fill: color }} shapeRendering="crispEdges" />}
      {flash && (
        <motion.rect
          key={flash.n}
          width={box.w}
          height={box.h}
          fill="none"
          stroke={TONE_VAR[flash.tone]}
          strokeWidth={1.5}
          shapeRendering="crispEdges"
          initial={{ opacity: 1 }}
          animate={{ opacity: 0 }}
          transition={{ duration: 1.2, ease: EASE_IOS }}
        />
      )}
      <rect
        x={16}
        y={box.h / 2 - 9}
        width={6}
        height={6}
        shapeRendering="crispEdges"
        className={live ? "" : agent.last_seen ? "fill-ink-3" : "fill-line-strong"}
        style={live ? { fill: color } : undefined}
      />
      <text x={30} y={box.h / 2 - 2} className={`font-medium tracking-[-0.01em] ${compact ? "text-[13.5px]" : "text-[14px]"} ${agent.last_seen ? "fill-ink" : "fill-ink-2"}`}>
        {agent.id}
      </text>
      <text x={30} y={box.h / 2 + 15} className="fill-ink-3 font-mono text-[10.5px] tabular-nums">
        {ago(agent.last_seen, now)} · {fmtInt(agent.calls_24h)} calls
      </text>
    </g>
  );
}
