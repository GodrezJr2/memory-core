/** Shapes sent by memory-core's /api/v1/activity/stream (see app/activity.py). */

export interface CallEvent {
  at: string; // ISO timestamp, UTC
  agent: string;
  client: string; // mcp | hook | cli | rest
  action: string; // profile | recall | remember | forget | timeline | library_{search,get,sync,upsert}
  scope: string | null;
  detail: string | null;
  results: number | null;
  note: string | null;
  ms: number;
  ok: boolean;
  error: string | null;
  ids?: number[]; // memories the call touched (recall hits, saved, forgotten); live stream only
}

export interface AgentStats {
  id: string;
  last_seen: string | null;
  calls_24h: number;
  hourly: number[]; // 24 buckets, oldest first; the last one is the current hour
  memories: number; // live memories this agent wrote
}

export interface Totals {
  memories: number;
  calls_24h: number;
  avg_ms_24h: number | null;
}

export interface Snapshot {
  now: string;
  agents: AgentStats[];
  totals: Totals;
  recent: CallEvent[]; // newest first
}

export interface AgentState extends AgentStats {
  clients: string[];
  tone: Tone | null; // tone of the latest call, for colouring the node
}

export type Tone = "recall" | "remember" | "forget" | "library" | "context" | "error";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "denied" | "demo";
