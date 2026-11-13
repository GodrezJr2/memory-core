"""Near-duplicate report: groups of live memories in one scope that say nearly the same thing.

Recall degrades as a project piles up many close notes (measured 21 Oct 2026: hit@1 0.675 -> 0.60 from 290 to
~1050 memories, misses mostly a neighbour from the same project ranked first). This lists each group so an agent
can supersede or forget the stale ones; it changes nothing itself (the server runs no LLM, so merging is the
agent's call).

Pairs at or above --min cosine in the same scope are joined into groups (union-find), largest first. Episodes are
left out by default: each one summarises a different session, so they are close by design.

Run on homeserver:
  cd ~/stacks/memory-core && docker compose run --rm --no-deps -T \
    -v $PWD/scripts:/srv/scripts:ro --entrypoint python app scripts/near_duplicates.py --min 0.85
Options: --scope project:<name> limits it to one scope, --episodes includes episodes, --json FILE saves the groups.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store as store_mod  # noqa: E402
from app.config import load  # noqa: E402


async def run(args: argparse.Namespace) -> list[list[dict]]:
    store = store_mod.Store(load())
    pool = await store.pool()
    kinds = ["fact", "procedure", "core"] + (["episode"] if args.episodes else [])
    async with pool.acquire() as con:
        pairs = await con.fetch(
            f"""select a.id as a, b.id as b, 1 - (a.embedding <=> b.embedding) as sim
                from memory a join memory b on a.scope = b.scope and a.id < b.id
                where a.{store_mod.LIVE} and b.{store_mod.LIVE} and a.kind = any($1) and b.kind = any($1)
                  and ($2::text is null or a.scope = $2) and 1 - (a.embedding <=> b.embedding) >= $3""",
            kinds, args.scope, args.min,
        )
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        best: dict[int, float] = {}
        for p in pairs:
            parent[find(p["a"])] = find(p["b"])
            for mid in (p["a"], p["b"]):
                best[mid] = max(best.get(mid, 0.0), p["sim"])
        groups: dict[int, list[int]] = {}
        for mid in parent:
            groups.setdefault(find(mid), []).append(mid)
        rows = {r["id"]: r for r in await con.fetch(
            "select id, kind, scope, content, created_at, verified_at from memory where id = any($1)", list(parent))}

    out = []
    for ids in sorted(groups.values(), key=len, reverse=True):
        out.append([{
            "id": mid, "scope": rows[mid]["scope"], "kind": rows[mid]["kind"],
            "date": rows[mid]["created_at"].date().isoformat(), "verified": rows[mid]["verified_at"] is not None,
            "max_sim": round(best[mid], 3), "text": rows[mid]["content"],
        } for mid in sorted(ids)])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=float, default=0.85, help="cosine at or above which two memories are paired")
    ap.add_argument("--scope", help="only this scope, e.g. project:memory-core")
    ap.add_argument("--episodes", action="store_true", help="include episodes")
    ap.add_argument("--json", help="write the groups to this file")
    args = ap.parse_args()
    groups = asyncio.run(run(args))
    if args.json:
        Path(args.json).write_text(json.dumps(groups, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(groups)} groups, {sum(len(g) for g in groups)} memories (cosine >= {args.min}, same scope)")
    for g in groups:
        print(f"\n== {g[0]['scope']} ({len(g)})")
        for m in g:
            flag = "v" if m["verified"] else " "
            print(f"  #{m['id']:<5} {m['date']} {flag} {m['max_sim']:.2f} {m['text'][:160]}")


if __name__ == "__main__":
    main()
