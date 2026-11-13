import { LIVE_MS, RECENT_MS } from "./motion";
import type { CallEvent, Tone } from "./types";

export function toneOf(e: CallEvent): Tone {
  if (!e.ok) return "error";
  if (e.action === "recall" || e.action === "remember" || e.action === "forget") return e.action;
  if (e.action.startsWith("library_") || e.action === "capture_ref") return "library";
  return "context";
}

const LIBRARY_LABEL: Record<string, string> = {
  library_search: "library",
  library_get: "open",
  library_sync: "sync",
  library_upsert: "index",
  library_categories: "shelves",
  library_categorize: "file",
  library_inbox: "inbox",
  library_view: "view",
  library_review: "review",
  capture_ref: "capture",
  branches: "branches",
  branch: "branch",
  branch_review: "branch review",
};

export function actionLabel(e: CallEvent): string {
  if (e.action === "profile" && e.client === "hook") return "session";
  return LIBRARY_LABEL[e.action] ?? e.action;
}

export function resultLabel(e: CallEvent): string {
  if (!e.ok) return e.error ? `failed · ${e.error}` : "failed";
  const n = e.results ?? 0;
  switch (e.action) {
    case "recall":
      return n ? `${n} hit${n === 1 ? "" : "s"}` : "no hits";
    case "remember":
      return `${n} saved${e.note ? ` · ${e.note}` : ""}`;
    case "forget":
      return "retired";
    case "timeline":
      return `${n} episode${n === 1 ? "" : "s"}`;
    case "library_search":
      return n ? `${n} match${n === 1 ? "" : "es"}` : "no matches";
    case "library_get":
      return "opened";
    case "library_sync":
      return `${n} to index${e.note ? ` · ${e.note}` : ""}`;
    case "library_upsert":
      return `${n} indexed${e.note ? ` · ${e.note}` : ""}`;
    case "library_categories":
      return `${n} shelves`;
    case "library_categorize":
      return `${n} auto-filed`;
    case "library_inbox":
      return `${n} waiting`;
    case "library_view":
      return "viewed";
    case "library_review":
      return e.note ? `reviewed · ${e.note}` : "done";
    case "capture_ref":
      return `${n} image${n === 1 ? "" : "s"}${e.note ? ` · ${e.note}` : ""}`;
    case "branches":
      return `${n} without a branch`;
    case "branch":
      return e.note === "proposed" ? "proposed, waits for the user" : e.note === "created" ? "new branch" : "filed";
    case "branch_review":
      return `${n} project${n === 1 ? "" : "s"} moved`;
    default:
      return "context loaded";
  }
}

export function ago(iso: string | null, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 5) return "now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export type Freshness = "live" | "recent" | "idle";

export function freshness(lastSeen: string | null, now: number): Freshness {
  if (!lastSeen) return "idle";
  const age = now - Date.parse(lastSeen);
  return age < LIVE_MS ? "live" : age < RECENT_MS ? "recent" : "idle";
}

export const clock = (iso: string) => new Date(iso).toLocaleTimeString("en-GB", { hour12: false });

export const fmtInt = (n: number) => Math.round(n).toLocaleString("en-US");

export const fmtMs = (n: number | null) => (n == null ? "–" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`);

export const eventKey = (e: CallEvent) => `${e.at}|${e.agent}|${e.action}|${e.detail ?? ""}`;
