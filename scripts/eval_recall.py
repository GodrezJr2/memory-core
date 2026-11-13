"""Recall eval: how often memory_recall puts the right memory near the top.

Runs the real Store against the live database (read-only: recall writes nothing and is not logged), so
results match what agents get. Each question in tests/eval/recall_questions.json lists the memory ids that
answer it; a hit is any of them. Reports hit@1/3/5/10, MRR@10, how often link expansion rescues a miss,
latency, and every miss with what came first instead.

Run on homeserver (same network and secrets as the app):
  cd ~/stacks/memory-core && docker compose run --rm --no-deps -T \
    -v $PWD/scripts:/srv/scripts:ro -v $PWD/tests:/srv/tests:ro --entrypoint python app scripts/eval_recall.py
Options: --keyword-weight 0.15 (what the share of matched query words adds to similarity) and --candidates 50
try other settings without a deploy; --json FILE saves the run.
--prefer expected boosts the scope of each question's answer (the hook's cwd project when the user is in that project),
--prefer project:<name> boosts one fixed scope (the user asking about something else from another project), and
--scope-boost X tries another boost size.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rules  # noqa: E402
from app import store as store_mod  # noqa: E402
from app.config import load  # noqa: E402
from app.domains import DOMAINS  # noqa: E402
from app.rules import Agent, default_scopes  # noqa: E402

QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "eval" / "recall_questions.json"
K = 10


async def live_ids(store: store_mod.Store, expect: list[int]) -> list[int]:
    """Expected ids, each followed along superseded_by to the memory that replaced it."""
    pool = await store.pool()
    out = []
    async with pool.acquire() as con:
        for mid in expect:
            seen = set()
            while mid is not None and mid not in seen:
                seen.add(mid)
                row = await con.fetchrow("select valid_to, superseded_by from memory where id = $1", mid)
                if row is None:
                    mid = None
                elif row["valid_to"] is None:
                    out.append(mid)
                    break
                else:
                    mid = row["superseded_by"]
    return out


async def run(args: argparse.Namespace) -> dict:
    if args.keyword_weight is not None:
        rules.KEYWORD_SHARE_WEIGHT = args.keyword_weight
    if args.scope_boost is not None:
        rules.SCOPE_BOOST = args.scope_boost
    settings = load()
    if args.candidates:
        settings = dataclasses.replace(settings, candidates=args.candidates)
    store = store_mod.Store(settings)
    agent = Agent("eval", default_scopes("eval"))
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]

    rows, latencies = [], []
    for item in questions:
        want = await live_ids(store, item["expect"])
        prefer = args.prefer
        if prefer == "expected":
            pool = await store.pool()
            prefer = await pool.fetchval("select scope from memory where id = $1", want[0]) if want else None
        started = time.perf_counter()
        res = await store.recall(agent, item["q"], item.get("scope"), None, K, expand=True, prefer=prefer)
        latencies.append(time.perf_counter() - started)
        ids = [r["id"] for r in res["results"]]
        rank = next((i + 1 for i, mid in enumerate(ids) if mid in want), None)
        linked = [r["id"] for r in res.get("linked", [])]
        top = res["results"][0] if res["results"] else None
        rows.append({
            "q": item["q"], "expect": want, "rank": rank,
            "rescued_by_link": rank is None and any(mid in want for mid in linked),
            "stale": not want,
            "domain": DOMAINS.of(top["scope"]) if top else None,
            "top": [{"id": r["id"], "sim": r["similarity"], "text": r["text"][:90]} for r in res["results"][:3]],
        })

    scored = [r for r in rows if not r["stale"]]
    n = len(scored)
    hit = lambda k: sum(1 for r in scored if r["rank"] and r["rank"] <= k) / n  # noqa: E731
    summary = {
        "questions": n,
        "stale_questions": len(rows) - n,
        "keyword_weight": rules.KEYWORD_SHARE_WEIGHT,
        "candidates": settings.candidates,
        "prefer": args.prefer, "scope_boost": rules.SCOPE_BOOST,
        "hit@1": round(hit(1), 3), "hit@3": round(hit(3), 3), "hit@5": round(hit(5), 3), "hit@10": round(hit(10), 3),
        "mrr@10": round(sum(1 / r["rank"] for r in scored if r["rank"]) / n, 3),
        "missed_top10_but_linked": sum(1 for r in scored if r["rescued_by_link"]),
        "latency_ms_median": round(statistics.median(latencies) * 1000),
        "latency_ms_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1] * 1000),
    }
    return {"summary": summary, "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", default=str(QUESTIONS))
    ap.add_argument("--keyword-weight", type=float, help="weight of the matched-word share added to similarity")
    ap.add_argument("--candidates", type=int, help="rows taken from each search before fusion")
    ap.add_argument("--prefer", help="'expected' or a scope to boost, as the hook does with its cwd project")
    ap.add_argument("--scope-boost", type=float, help="score added to the preferred scope's memories")
    ap.add_argument("--json", help="write the full run to this file")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()
    out = asyncio.run(run(args))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1))
    if args.quiet:
        return
    for r in out["rows"]:
        if r["stale"]:
            print(f"STALE  {r['q']}  (expected memories are gone)")
        elif r["rank"] is None or r["rank"] > 3:
            where = f"rank {r['rank']}" if r["rank"] else ("linked only" if r["rescued_by_link"] else "missed")
            print(f"{where:12} {r['q']}  want {r['expect']}")
            for t in r["top"]:
                print(f"{'':14}#{t['id']} {t['sim']:.2f} {t['text']}")


if __name__ == "__main__":
    main()
