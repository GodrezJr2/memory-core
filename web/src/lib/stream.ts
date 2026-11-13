import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { startDemo } from "./demo";
import { eventKey, toneOf } from "./format";
import type { AgentState, AgentStats, CallEvent, Snapshot, StreamStatus, Totals } from "./types";

const FEED_MAX = 300;

export interface StreamState {
  status: StreamStatus;
  retryIn: number | null;
  denied: string | null;
  ready: boolean;
  agents: Record<string, AgentState>;
  order: string[]; // sorted ids: a stable order keeps the graph from reshuffling
  feed: CallEvent[]; // newest first
  totals: Totals;
  skew: number; // server clock minus this clock, in ms
}

type Msg =
  | { type: "hello"; snap: Snapshot }
  | { type: "stats"; snap: Snapshot }
  | { type: "call"; e: CallEvent }
  | { type: "status"; status: StreamStatus; retryIn?: number | null; denied?: string | null };

const initial: StreamState = {
  status: "connecting",
  retryIn: null,
  denied: null,
  ready: false,
  agents: {},
  order: [],
  feed: [],
  totals: { memories: 0, calls_24h: 0, avg_ms_24h: null },
  skew: 0,
};

const blank = (id: string): AgentState => ({
  id, last_seen: null, calls_24h: 0, hourly: Array(24).fill(0), memories: 0, clients: [], tone: null,
});

function mergeStats(agents: Record<string, AgentState>, list: AgentStats[]) {
  const next = { ...agents };
  for (const a of list) next[a.id] = { ...(next[a.id] ?? blank(a.id)), ...a };
  return next;
}

function noteCall(a: AgentState, e: CallEvent): AgentState {
  return {
    ...a,
    clients: a.clients.includes(e.client) ? a.clients : [...a.clients, e.client].sort(),
    tone: toneOf(e),
  };
}

function reducer(s: StreamState, m: Msg): StreamState {
  switch (m.type) {
    case "hello": {
      const agents = mergeStats({}, m.snap.agents);
      const feed = m.snap.recent.slice(0, FEED_MAX);
      for (const e of [...feed].reverse()) agents[e.agent] = noteCall(agents[e.agent] ?? blank(e.agent), e);
      return {
        ...s, ready: true, agents, order: Object.keys(agents).sort(), feed, totals: m.snap.totals,
        skew: Date.parse(m.snap.now) - Date.now(),
      };
    }
    case "stats": {
      const agents = mergeStats(s.agents, m.snap.agents);
      return {
        ...s, agents, order: Object.keys(agents).sort(), totals: m.snap.totals,
        skew: Date.parse(m.snap.now) - Date.now(),
      };
    }
    case "call": {
      const e = m.e;
      const known = s.agents[e.agent];
      const a = noteCall(known ?? blank(e.agent), e);
      const hourly = [...a.hourly];
      hourly[23] += 1;
      const wrote = e.ok && e.action === "remember" ? e.results ?? 0 : 0;
      const retired = e.ok && e.action === "forget" ? 1 : 0;
      const agents = {
        ...s.agents,
        [e.agent]: { ...a, last_seen: e.at, calls_24h: a.calls_24h + 1, hourly, memories: a.memories + wrote },
      };
      const { memories, calls_24h, avg_ms_24h } = s.totals;
      // keep the 24 h average moving between the server's minute-by-minute stats
      const avg = !e.ok ? avg_ms_24h : avg_ms_24h == null ? e.ms : (avg_ms_24h * calls_24h + e.ms) / (calls_24h + 1);
      return {
        ...s,
        agents,
        order: known ? s.order : Object.keys(agents).sort(),
        feed: [e, ...s.feed].slice(0, FEED_MAX),
        totals: { memories: memories + wrote - retired, calls_24h: calls_24h + 1, avg_ms_24h: avg },
      };
    }
    case "status":
      return { ...s, status: m.status, retryIn: m.retryIn ?? null, denied: m.denied ?? s.denied };
  }
}

type Listener = (e: CallEvent) => void;

/**
 * Live activity from memory-core: a 'hello' snapshot, then one 'call' per tool call and 'stats' each minute.
 * subscribe() hands each new call to the graph so it can animate it. Add ?demo to the URL for fake traffic.
 */
export function useActivityStream() {
  const [state, dispatch] = useReducer(reducer, initial);
  const listeners = useRef(new Set<Listener>());
  const seen = useRef(new Set<string>()); // the stream subscribes before it snapshots, so a call can arrive twice

  const onHello = useCallback((snap: Snapshot) => {
    seen.current = new Set(snap.recent.map(eventKey));
    dispatch({ type: "hello", snap });
  }, []);

  const onCall = useCallback((e: CallEvent) => {
    const key = eventKey(e);
    if (seen.current.has(key)) return;
    seen.current.add(key);
    if (seen.current.size > 2000) seen.current = new Set([...seen.current].slice(-1000));
    dispatch({ type: "call", e });
    listeners.current.forEach((fn) => fn(e));
  }, []);

  useEffect(() => {
    if (new URLSearchParams(location.search).has("demo")) {
      dispatch({ type: "status", status: "demo" });
      return startDemo({ hello: onHello, call: onCall });
    }

    let source: EventSource | null = null;
    let timer = 0;
    let backoff = 1000;
    let stopped = false;

    const retry = () => {
      source?.close();
      source = null;
      if (stopped) return;
      dispatch({ type: "status", status: "reconnecting", retryIn: backoff });
      window.clearTimeout(timer);
      timer = window.setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 30_000);
    };

    // EventSource (not a fetch reader): iOS Safari delivers it reliably and it reconnects by itself.
    // It hides HTTP status codes, so first ask whether this viewer is allowed at all.
    const open = async () => {
      if (stopped) return;
      dispatch({ type: "status", status: "connecting" });
      try {
        const res = await fetch("/api/v1/activity/access", { cache: "no-store" });
        if (res.status === 401 || res.status === 403) {
          const body = await res.json().catch(() => ({}));
          dispatch({ type: "status", status: "denied", denied: body.error ?? "This view is not available here." });
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      } catch (err) {
        console.warn("activity access:", err);
        retry();
        return;
      }
      if (stopped) return;
      const es = new EventSource("/api/v1/activity/stream");
      source = es;
      const on = <T,>(name: string, fn: (data: T) => void) =>
        es.addEventListener(name, (ev) => fn(JSON.parse((ev as MessageEvent<string>).data) as T));
      // "live" only once data is here, not when the connection opens
      on<Snapshot>("hello", (snap) => {
        backoff = 1000;
        onHello(snap);
        dispatch({ type: "status", status: "live" });
      });
      on<CallEvent>("call", onCall);
      on<Snapshot>("stats", (snap) => dispatch({ type: "stats", snap }));
      // a dropped connection is retried by EventSource itself (CONNECTING); a refused one ends up CLOSED
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) retry();
        else dispatch({ type: "status", status: "reconnecting", retryIn: 3000 });
      };
    };

    // iOS cuts connections while the page is in the background; reopen as soon as it is visible again
    const onVisible = () => {
      if (document.visibilityState === "visible" && (!source || source.readyState === EventSource.CLOSED)) {
        window.clearTimeout(timer);
        backoff = 1000;
        open();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    open();
    return () => {
      stopped = true;
      source?.close();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [onHello, onCall]);

  const subscribe = useCallback((fn: Listener) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  }, []);

  return { state, subscribe };
}

/** Server-aligned clock that ticks once a second, for "12s ago" labels and active states. */
export function useNow(skew: number, every = 1000) {
  const [now, setNow] = useState(() => Date.now() + skew);
  useEffect(() => {
    setNow(Date.now() + skew);
    const t = window.setInterval(() => setNow(Date.now() + skew), every);
    return () => window.clearInterval(t);
  }, [skew, every]);
  return now;
}
