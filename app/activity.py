"""Access log and live feed for the /live page: which agent called which tool, when, and how it went.

Each call is pushed to open /live streams at once and written to Postgres (access_log) in the
background, so logging never slows down or fails the call itself. Details are redacted like memories
and cut short, and rows older than RETENTION are pruned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .rules import redact

log = logging.getLogger("memory-core.activity")

CLIENTS = ("mcp", "hook", "cli", "rest", "sync", "capture", "web")
DETAIL_CHARS = 300
RETENTION = "14 days"
PRUNE_EVERY_S = 3600
RECENT = 200
QUEUE_SIZE = 200  # per viewer; a stalled viewer misses events instead of holding memory
COLUMNS = "at, agent, client, action, scope, detail, results, note, ms, ok, error"


def client_kind(header: str | None, default: str) -> str:
    """X-MC-Client tells REST calls apart: 'hook' (Claude Code hook), 'cli' (mc) or 'sync' (library_sync).
    Anything else is the default."""
    value = (header or "").strip().lower()
    return value if value in CLIENTS else default


def _short(text: str | None) -> str | None:
    if not text:
        return None
    text = redact(" ".join(text.split()))[0]
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…"


def describe(action: str, args: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    """What the /live page shows for one call. result is None when the call failed."""
    detail: str | None = None
    results: int | None = None
    note: str | None = None
    if action == "recall":
        detail = args.get("query")
        results = len(result["results"]) if result else None
    elif action == "remember":
        facts = [str(f) for f in args.get("facts") or []]
        if facts:
            detail = facts[0] + (f" (+{len(facts) - 1} more)" if len(facts) > 1 else "")
        if result:
            results = len(result.get("saved", []))
            held = [f"{len(result[key])} {label}" for key, label in
                    (("held_near_duplicates", "held"), ("skipped_duplicates", "duplicate")) if result.get(key)]
            note = ", ".join(held) or None
    elif action == "forget":
        detail = f"#{args.get('memory_id')}: {args.get('reason', '')}"
        results = 1 if result else None
    elif action == "timeline":
        detail = f"since {args['since']}" if args.get("since") else None
        results = len(result["episodes"]) if result else None
    elif action == "profile":
        results = len(result["profile"]) if result else None
    elif action == "library_search":
        detail = (f"[{args['type']}] " if args.get("type") else "") + str(args.get("query") or "")
        results = len(result["results"]) if result else None
    elif action == "library_get":
        detail = f"#{args.get('item_id', args.get('id'))}" + (f" {result['title']}" if result else "")
        results = 1 if result else None
    elif action == "library_sync":
        detail = f"{args.get('type')} from {args.get('origin')}: {len(args.get('manifest') or [])} found"
        if result:
            results = len(result["need"])
            note = f"{result['removed']} removed" if result["removed"] else None
    elif action == "library_upsert":
        items = [i for i in args.get("items") or [] if isinstance(i, dict)]
        if items:
            detail = str(items[0].get("title")) + (f" (+{len(items) - 1} more)" if len(items) > 1 else "")
        if result:
            results = len(result["saved"])
            note = f"{result['unchanged']} unchanged" if result["unchanged"] else None
    elif action == "library_categories":
        results = len(result["categories"]) if result else None
    elif action == "library_categorize":
        if result:
            detail = f"{result['categories']} categories, {result['embedded']} re-embedded"
            results = result["auto_filed"]
    elif action == "library_inbox":
        results = result["waiting"] if result else None
    elif action == "library_view":
        detail = f"#{args.get('item_id')}" + (f" {result[0]['title']}" if result else "")
        results = 1 if result else None
    elif action == "library_review":
        detail = f"#{args.get('item_id', args.get('id'))}" + (
            " discarded" if args.get("discard") else f" {args.get('title') or ''}")
        results = 1 if result else None
        if result and result.get("category"):
            note = result["category"]
    elif action == "branches":
        results = len(result["unbranched"]) if result else None
    elif action == "branch":
        detail = f"{args.get('scope')} -> {args.get('branch')}"
        results = 1 if result else None
        note = result["status"] if result else None
    elif action == "branch_review":
        detail = f"{args.get('branch')} " + ("accepted" if args.get("accept") else "turned down")
        results = len(result["projects"]) if result else None
    elif action == "links_rebuild":
        if result:
            detail = f"{result['related']} related, {result['together']} together"
            results = result["links"]
    elif action == "library_file":
        detail = f"#{args.get('item_id', args.get('id'))} {args.get('path') or ''}"
        results = 1 if result else None
    elif action == "library_bundle_get":
        detail = f"#{args.get('id')}" + (f" {result[0]}" if result else "")
        results = 1 if result else None
    elif action == "library_bundle_put":
        detail = f"{str(args.get('hash') or '')[:12]}, {args.get('bytes')} bytes"
        results = result["files"] if result else None
    elif action == "capture_ref":
        detail = str(args.get("note") or args.get("url") or ("an image" if args.get("image") else ""))
        if result:
            results = result["images"]
            note = "added to existing" if result["merged"] else f"filed {result['category']}"
    return {"scope": args.get("scope") or None, "detail": _short(detail), "results": results, "note": note}


def memory_ids(action: str, args: dict[str, Any], result: dict[str, Any] | None) -> list[int]:
    """The memories a call touched, for the Brain view to light up: recall hits (and what they linked to), saved
    memories, the forgotten one. Sent on the live stream only, not stored."""
    if result is None:
        return []
    if action == "recall":
        return [r["id"] for r in result.get("results", []) + result.get("linked", []) if "id" in r]
    if action == "remember":
        return [s["id"] for s in result.get("saved", []) if "id" in s]
    if action == "forget" and isinstance(args.get("memory_id"), (int, str)) and str(args["memory_id"]).isdigit():
        return [int(args["memory_id"])]
    return []


def _event(row: Any) -> dict[str, Any]:
    event = dict(row)
    event["at"] = event["at"].isoformat(timespec="milliseconds")
    return event


class Activity:
    def __init__(self, pool: Callable[[], Awaitable[Any]]) -> None:
        self._pool = pool
        self._viewers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_prune = 0.0

    def record(
        self,
        agent: str,
        client: str,
        action: str,
        args: dict[str, Any],
        result: dict[str, Any] | None,
        started: float,
        error: str | None = None,
    ) -> None:
        at = datetime.now(timezone.utc)
        event = {
            "at": at.isoformat(timespec="milliseconds"),
            "agent": agent,
            "client": client,
            "action": action,
            **describe(action, args, result),
            "ms": round((time.perf_counter() - started) * 1000),
            "ok": error is None,
            "error": error[:200] if error else None,
        }
        if ids := memory_ids(action, args, result):
            event["ids"] = ids[:40]
        for queue in list(self._viewers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        task = asyncio.create_task(self._save(at, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _save(self, at: datetime, e: dict[str, Any]) -> None:
        try:
            pool = await self._pool()
            async with pool.acquire() as con:
                await con.execute(
                    f"insert into access_log ({COLUMNS}) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    at, e["agent"], e["client"], e["action"], e["scope"], e["detail"], e["results"], e["note"],
                    e["ms"], e["ok"], e["error"],
                )
                if time.monotonic() - self._last_prune > PRUNE_EVERY_S:
                    self._last_prune = time.monotonic()
                    await con.execute(f"delete from access_log where at < now() - interval '{RETENTION}'")
        except Exception as exc:  # the call already succeeded; a lost log line must not surface
            log.warning("could not log %s by %s: %s", e["action"], e["agent"], exc)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        self._viewers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._viewers.discard(queue)

    async def snapshot(self, known_agents: list[str], recent: int = RECENT) -> dict[str, Any]:
        """Per-agent stats for the last 24 h (hourly buckets, oldest first), totals, and the latest calls."""
        pool = await self._pool()
        async with pool.acquire() as con:
            rows = await con.fetch(f"select {COLUMNS} from access_log order by at desc limit $1", recent) if recent else []
            seen = await con.fetch(
                """select agent, max(at) as last_seen,
                          count(*) filter (where at > now() - interval '24 hours') as calls_24h
                   from access_log group by agent"""
            )
            hourly = await con.fetch(
                """select agent, floor(extract(epoch from now() - at) / 3600)::int as ago, count(*) as n
                   from access_log where at > now() - interval '24 hours' group by 1, 2"""
            )
            written = await con.fetch("select created_by, count(*) as n from memory where valid_to is null group by 1")
            avg_ms = await con.fetchval(
                "select round(avg(ms)) from access_log where ok and at > now() - interval '24 hours'"
            )

        agents: dict[str, dict[str, Any]] = {
            a: {"id": a, "last_seen": None, "calls_24h": 0, "hourly": [0] * 24, "memories": 0}
            for a in known_agents
        }

        def entry(agent_id: str) -> dict[str, Any]:
            return agents.setdefault(
                agent_id, {"id": agent_id, "last_seen": None, "calls_24h": 0, "hourly": [0] * 24, "memories": 0}
            )

        for r in seen:
            entry(r["agent"]).update(last_seen=r["last_seen"].isoformat(timespec="milliseconds"), calls_24h=r["calls_24h"])
        for r in hourly:
            if 0 <= r["ago"] < 24:
                entry(r["agent"])["hourly"][23 - r["ago"]] = r["n"]
        for r in written:
            entry(r["created_by"])["memories"] = r["n"]

        ordered = sorted(agents.values(), key=lambda a: a["id"])  # stable order keeps the map layout still
        return {
            "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "agents": ordered,
            "totals": {
                "memories": sum(r["n"] for r in written),
                "calls_24h": sum(a["calls_24h"] for a in ordered),
                "avg_ms_24h": int(avg_ms) if avg_ms is not None else None,
            },
            "recent": [_event(r) for r in rows],
        }
