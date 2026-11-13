"""Library categories: a fixed taxonomy (library_categories.json) with a "which one when" guide per category, so an
agent facing 170 skills can look at one shelf instead of guessing between near-duplicates.

Known skills and starred repos are filed by title. Docs go to project-docs. Everything else (new skills, starred repos, saved
references) goes to the category whose description is closest in meaning to the item's first chunk, flagged
category_auto so it can be checked. A reviewed reference keeps the category its reviewer chose. Skills listed as
hidden (legacy versions, duplicates) stay out of search.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .embed import to_pgvector
from .rules import RuleError

CATEGORIES_FILE = Path(__file__).with_name("library_categories.json")
DOCS_CATEGORY = "project-docs"
# Not offered to automatic filing: project-docs is for docs only, and "other" is too vague to be a nearest match.
NOT_AUTO = (DOCS_CATEGORY, "other")


def load(path: Path = CATEGORIES_FILE) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    names = [c["name"] for c in data["categories"]]
    data.setdefault("repos", {})
    unknown = sorted({c for c in [*data["skills"].values(), *data["repos"].values()] if c not in names})
    if unknown or len(set(names)) != len(names):
        raise ValueError(f"library_categories.json: unknown or repeated categories {unknown or names}")
    return data


def category_hash(category: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        [category["name"], category["title"], category["description"], category["guide"]]).encode()).hexdigest()


class Categories:
    def __init__(self, store: Any) -> None:
        self._store = store
        self.data = load()
        self.names = [c["name"] for c in self.data["categories"]]

    def check(self, name: Any) -> str:
        value = str(name or "").strip().lower()
        if value not in self.names:
            raise RuleError(f"Unknown category {name!r}. Use one of: {', '.join(self.names)}.")
        return value

    async def sync_taxonomy(self) -> int:
        """Bring lib_category in line with the JSON file; embeds only categories whose text changed."""
        pool = await self._store.pool()
        async with pool.acquire() as con:
            stored = {r["name"]: r["source_hash"] for r in await con.fetch("select name, source_hash from lib_category")}
        changed = [c for c in self.data["categories"] if stored.get(c["name"]) != category_hash(c)]
        vectors = []
        for c in changed:  # one at a time: the embedder has one slot and hook queries share it
            vectors += await self._store.embedder.documents([f"{c['title']}: {c['description']}"])
        async with pool.acquire() as con, con.transaction():
            for c, vec in zip(changed, vectors):
                await con.execute(
                    """insert into lib_category (name, title, description, guide, ord, embedding, source_hash)
                       values ($1, $2, $3, $4, $5, $6::vector, $7)
                       on conflict (name) do update set title = $2, description = $3, guide = $4, ord = $5,
                           embedding = $6::vector, source_hash = $7""",
                    c["name"], c["title"], c["description"], c["guide"], self.names.index(c["name"]),
                    to_pgvector(vec), category_hash(c),
                )
            await con.execute("update lib_category set ord = array_position($1::text[], name)", self.names)
            await con.execute("delete from lib_category where not (name = any($1::text[]))", self.names)
        return len(changed)

    async def assign(self, con: Any, item_ids: list[int] | None = None) -> int:
        """File items (all, or just item_ids): fixed rules first, then the nearest category by meaning.
        Returns how many were filed automatically."""
        extra: list[Any] = [item_ids] if item_ids is not None else []

        def only(n: int) -> str:  # restrict to item_ids, passed as parameter $n
            return f"and i.id = any(${n}::bigint[])" if item_ids is not None else ""

        fixed = [("skill", t, c) for t, c in self.data["skills"].items()]
        fixed += [("repo", t, c) for t, c in self.data["repos"].items()]
        types, titles, cats = [f[0] for f in fixed], [f[1] for f in fixed], [f[2] for f in fixed]
        await con.execute(
            f"""update lib_item i set category = m.category, category_auto = false
                from unnest($1::text[], $2::text[], $3::text[]) as m(type, title, category)
                where i.type = m.type and i.title = m.title {only(4)}""",
            types, titles, cats, *extra,
        )
        await con.execute(
            f"update lib_item i set hidden = (i.type = 'skill' and i.title = any($1::text[])) where true {only(2)}",
            self.data.get("hidden", []), *extra,
        )
        await con.execute(
            f"update lib_item i set category = $1, category_auto = false where i.type = 'doc' {only(2)}",
            DOCS_CATEGORY, *extra,
        )
        result = await con.execute(
            f"""update lib_item i set category = (
                    select c.name from lib_category c where not (c.name = any($2::text[]))
                    order by c.embedding <=> (select embedding from lib_chunk where item_id = i.id order by ord limit 1)
                    limit 1), category_auto = true
                where i.type in ('skill', 'repo', 'ref') and not (i.type = 'ref' and i.status = 'reviewed')
                  and not exists (select 1 from unnest($1::text[], $3::text[]) as f(type, title)
                                  where f.type = i.type and f.title = i.title) {only(4)}""",
            types, list(NOT_AUTO), titles, *extra,
        )
        return int(result.split()[-1])

    async def apply(self) -> dict[str, Any]:
        embedded = await self.sync_taxonomy()
        pool = await self._store.pool()
        async with pool.acquire() as con, con.transaction():
            auto = await self.assign(con)
        return {"categories": len(self.names), "embedded": embedded, "auto_filed": auto}

    async def overview(self, agent: Any) -> dict[str, Any]:
        pool = await self._store.pool()
        async with pool.acquire() as con:
            counts = {(r["category"], r["type"]): r["n"] for r in await con.fetch(
                """select category, type, count(*) as n from lib_item
                   where not hidden and scope like any($1) group by 1, 2""",
                agent.like_patterns,
            )}
        out = []
        for c in self.data["categories"]:
            by_type = {t: n for (name, t), n in counts.items() if name == c["name"]}
            if by_type:
                out.append({"name": c["name"], "title": c["title"], "items": by_type, "guide": c["guide"]})
        return {"categories": out,
                "next_step": "library_search(query, category=<name>) to look inside one; library_get(<id>) to read."}
