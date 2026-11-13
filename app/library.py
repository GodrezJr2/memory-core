"""Library: skills, starred repos and project docs that live elsewhere, so any agent can find and read them.

Sync (clients/library_sync.py, per machine and type):
1. sync: the client sends a manifest (path, scope, content hash) of everything it found. The server replaces
   that origin's locations, drops items no location points at any more, and answers which hashes it lacks.
2. upsert: the client uploads those items a few at a time; the server redacts, chunks and embeds them.
3. bundle (skills only): the manifest also names each skill folder's tree hash; the client uploads the folders the
   server lacks as zips (app/bundles.py), so library_file and mc skill-pull reach a skill's supporting files.
An unchanged file costs one manifest line. No LLM: titles and one-line summaries come from the source
(skill frontmatter, repo description, a doc's first line).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import bundles
from .embed import LIBRARY_TASK, to_pgvector
from .rules import KEYWORD_WEIGHT, VECTOR_WEIGHT, Agent, RuleError, check_scope, redact, rrf

TYPES = ("skill", "repo", "doc")  # what clients sync
SEARCH_TYPES = (*TYPES, "ref")  # plus saved references (app/refs.py)
ORIGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TITLE_CHARS = 200
SUMMARY_CHARS = 300
CARD_SUMMARY_CHARS = 160
PATH_CHARS = 500
META_CHARS = 2000
MAX_BODY = 200_000
MAX_MANIFEST = 5000
MAX_UPLOAD = 5
# About 300 tokens per chunk. Only the first chunks are embedded: they say what a skill or README is for, and
# the server's CPU embeds about one chunk per second (2 Oct 2026). library_get still returns the whole body.
CHUNK_CHARS = 1200
MAX_CHUNKS = 6
# The embedder has one slot, so a hook's query waits behind the batch in progress; two chunks (~2 s) keep it
# under the hook's 4 s timeout while a sync runs.
EMBED_BATCH = 2
PAGE_CHARS = 12_000
SNIPPET_CHARS = 180
MAX_LISTED_FILES = 60  # library_get lists a skill's supporting files up to this many

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(\r?\n|\Z)", re.S)


def item_hash(item_type: str, title: str, summary: str, scope: str, body: str) -> str:
    """Content address of an item. clients/library_sync.py computes the same, to know what to upload."""
    return hashlib.sha256(
        json.dumps([item_type, title, summary, scope, body], ensure_ascii=False).encode()
    ).hexdigest()


def check_type(value: Any) -> str:
    item_type = str(value or "").strip().lower()
    if item_type not in TYPES:
        raise RuleError(f"Invalid type {value!r}. Use one of: {', '.join(TYPES)}.")
    return item_type


def check_origin(value: Any) -> str:
    origin = str(value or "").strip().lower()
    if not ORIGIN_RE.match(origin):
        raise RuleError(f"Invalid origin {value!r}: lowercase letters, digits, '.', '_' or '-', at most 32.")
    return origin


def _check_hash(value: Any) -> str:
    if not isinstance(value, str) or not HASH_RE.match(value):
        raise RuleError(f"Invalid hash {value!r}: expected 64 lowercase hex characters (sha256).")
    return value


def clean_item(item_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate one uploaded item and check its hash. Returns the fields to store, redacted."""
    title, summary, body = (str(raw.get(k) or "") for k in ("title", "summary", "body"))
    raw_scope = str(raw.get("scope") or "global")
    scope = check_scope(raw_scope)
    if not title.strip():
        raise RuleError("Each item needs a title.")
    if len(body) > MAX_BODY:
        raise RuleError(f"{title!r}: the body has {len(body)} characters; at most {MAX_BODY}.")
    expected = item_hash(item_type, title, summary, raw_scope, body)
    if raw.get("hash") != expected:
        raise RuleError(f"{title!r}: hash does not match the content (compute it as clients/library_sync.py does).")
    meta = raw.get("meta") or {}
    if not isinstance(meta, dict) or len(json.dumps(meta)) > META_CHARS:
        raise RuleError(f"{title!r}: meta must be an object of at most {META_CHARS} characters as JSON.")
    return {
        "hash": expected,
        "type": item_type,
        "scope": scope,
        "title": redact(" ".join(title.split()))[0][:TITLE_CHARS],
        "summary": redact(" ".join(summary.split()))[0][:SUMMARY_CHARS],
        "body": redact(body)[0],
        "meta": meta,
    }


def _units(text: str, size: int):
    """Paragraphs (blank-line separated), each cut to at most `size` characters at a line or word break."""
    for block in re.split(r"\n[ \t]*\n", text):
        block = block.strip()
        while len(block) > size:
            cut = block.rfind("\n", 0, size)
            if cut < size // 2:
                cut = block.rfind(" ", 0, size)
            if cut < size // 2:
                cut = size
            yield block[:cut].rstrip()
            block = block[cut:].lstrip()
        if block:
            yield block


def chunk_text(body: str, size: int = CHUNK_CHARS, limit: int = MAX_CHUNKS) -> list[str]:
    """Split Markdown into at most `limit` chunks of up to `size` characters, preferring to break at headings."""
    text = _FRONTMATTER.sub("", body.replace("\r\n", "\n"), count=1).strip()
    chunks: list[str] = []
    current = ""
    for unit in _units(text, size):
        heading = unit.startswith("#")
        if current and (len(current) + 2 + len(unit) > size or (heading and len(current) >= size // 2)):
            chunks.append(current)
            if len(chunks) == limit:
                return chunks
            current = unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        chunks.append(current)
    return chunks[:limit]


def chunk_contents(item: dict[str, Any]) -> list[str]:
    """What gets embedded and keyword-indexed: a line naming the item (the first also carries the summary),
    then the chunk. An empty body still yields one chunk, so a repo without a README is findable."""
    head = f"{item['type']} {item['title']}"
    contents = []
    for n, piece in enumerate(chunk_text(item["body"]) or [""]):
        first = f"{head}: {item['summary']}" if n == 0 and item["summary"] else head
        contents.append(f"{first}\n\n{piece}".rstrip())
    return contents


def snippet(content: str) -> str:
    text = " ".join(content.split("\n\n", 1)[1].split()) if "\n\n" in content else ""
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Library:
    def __init__(self, store: Any, candidates: int, categories: Any = None) -> None:
        self._store = store  # for its pool and embedder
        self._candidates = candidates
        self.categories = categories  # app/categories.py; files new items

    async def embed_contents(self, contents: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(contents), EMBED_BATCH):
            vectors += await self._store.embedder.documents(contents[start:start + EMBED_BATCH])
        return vectors

    @staticmethod
    async def replace_chunks(con: Any, item_id: int, contents: list[str], vectors: list[list[float]]) -> None:
        await con.execute("delete from lib_chunk where item_id = $1", item_id)
        await con.executemany(
            "insert into lib_chunk (item_id, ord, content, embedding) values ($1, $2, $3, $4::vector)",
            [(item_id, n, c, to_pgvector(v)) for n, (c, v) in enumerate(zip(contents, vectors))],
        )

    async def sync(self, agent: Agent, item_type: Any, origin: Any, manifest: Any) -> dict[str, Any]:
        item_type, origin = check_type(item_type), check_origin(origin)
        if not isinstance(manifest, list):
            raise RuleError("manifest must be a list of {path, scope, hash}.")
        if len(manifest) > MAX_MANIFEST:
            raise RuleError(f"At most {MAX_MANIFEST} manifest entries per sync.")
        rows: dict[str, tuple[str, str, str | None]] = {}
        for entry in manifest:
            path = " ".join(str(entry.get("path") or "").split())[:PATH_CHARS]
            if not path:
                raise RuleError("Each manifest entry needs a path.")
            scope = check_scope(str(entry.get("scope") or "global"))
            if not agent.can_access(scope):
                raise RuleError(f"Agent {agent.id!r} may not write to {scope!r}.")
            # a skill's whole folder, uploaded separately (app/bundles.py); older clients send none
            bundle = bundles.check_hash(entry["bundle"]) if item_type == "skill" and entry.get("bundle") else None
            rows[path] = (scope, _check_hash(entry.get("hash")), bundle)
        paths = list(rows)
        scopes, hashes, folder = ([rows[p][n] for p in paths] for n in range(3))
        allowed = agent.like_patterns

        pool = await self._store.pool()
        async with pool.acquire() as con, con.transaction():
            await con.execute(
                "delete from lib_location where origin = $1 and type = $2 and scope like any($3)",
                origin, item_type, allowed,
            )
            await con.execute(
                """insert into lib_location (origin, type, scope, path, hash, bundle)
                   select $1, $2, s, p, h, b from unnest($3::text[], $4::text[], $5::text[], $6::text[]) as t(s, p, h, b)""",
                origin, item_type, scopes, paths, hashes, folder,
            )
            removed = await con.fetch(
                """delete from lib_item i where type = $1 and scope like any($2)
                   and not exists (select 1 from lib_location l where l.hash = i.hash) returning id""",
                item_type, allowed,
            )
            have = {r["hash"] for r in await con.fetch(
                "select hash from lib_item where hash = any($1)", list(set(hashes)))}
            referenced = {r["bundle"] for r in await con.fetch(
                "select distinct bundle from lib_location where bundle is not null")}
        result = {"type": item_type, "origin": origin, "found": len(paths),
                  "need": sorted(set(hashes) - have), "removed": len(removed)}
        if item_type == "skill":
            wanted = {b for b in folder if b}
            result["need_bundles"] = sorted(wanted - bundles.have(wanted))
            result["bundles_removed"] = bundles.collect(referenced)
        return result

    async def save_bundle(self, agent: Agent, bundle: Any, data: bytes) -> dict[str, Any]:
        """Store a skill folder that a sync named. Only bundles some location points at are accepted."""
        bundle = bundles.check_hash(bundle)
        pool = await self._store.pool()
        async with pool.acquire() as con:
            named = await con.fetchval(
                "select count(*) from lib_location where bundle = $1 and scope like any($2)",
                bundle, agent.like_patterns)
        if not named:
            raise RuleError("No synced skill names this bundle; run the sync's manifest step first.")
        return {"bundle": bundle, "files": bundles.save(bundle, data), "bytes": len(data)}

    async def _bundle_of(self, con: Any, agent: Agent, item_id: int) -> tuple[Any, str | None]:
        """A skill item and the bundle of one of its locations that is on disk (None if no machine sent one)."""
        item = await con.fetchrow(
            "select id, type, title, hash from lib_item where id = $1 and scope like any($2)",
            item_id, agent.like_patterns)
        if item is None:
            raise RuleError(f"Library item {item_id} does not exist; it may have changed since. Search again.")
        rows = await con.fetch(
            """select bundle, max(seen_at) as seen from lib_location
               where hash = $1 and bundle is not null group by bundle order by seen desc""", item["hash"])
        return item, next((r["bundle"] for r in rows if bundles.path_for(r["bundle"]).exists()), None)

    async def bundle_path(self, agent: Agent, item_id: Any) -> tuple[str, Any]:
        """The zip of a skill's folder, for mc skill-pull. Returns (skill title, path)."""
        pool = await self._store.pool()
        async with pool.acquire() as con:
            item, bundle = await self._bundle_of(con, agent, int(item_id))
        if item["type"] != "skill" or bundle is None:
            raise RuleError(f"Library item {item_id} has no folder to download; library_get has its text.")
        return item["title"], bundles.path_for(bundle)

    async def file(self, agent: Agent, item_id: Any, path: Any, offset: Any = 0) -> dict[str, Any]:
        """One supporting file of a skill (a reference, script or template it mentions), in pages."""
        item_id, offset, name = int(item_id), max(0, int(offset or 0)), str(path or "").strip().removeprefix("./")
        pool = await self._store.pool()
        async with pool.acquire() as con:
            item, bundle = await self._bundle_of(con, agent, item_id)
        if bundle is None:
            raise RuleError(f"Library item {item_id} has no supporting files stored; only library_get's text.")
        data = bundles.read_file(bundle, name)
        try:
            text = data.decode("utf-8")
            if "\0" in text:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "nul byte")
        except UnicodeDecodeError:
            return {"id": item_id, "skill": item["title"], "path": name, "binary": True, "bytes": len(data),
                    "note": "Not text. Install the skill with `mc skill-pull` to use this file."}
        text = redact(text)[0]
        result = {"id": item_id, "skill": item["title"], "path": name, "offset": offset, "total_chars": len(text),
                  "body": text[offset:offset + bundles.FILE_PAGE_CHARS]}
        if offset + bundles.FILE_PAGE_CHARS < len(text):
            result["next_offset"] = offset + bundles.FILE_PAGE_CHARS
        return result

    async def upsert(self, agent: Agent, item_type: Any, raw_items: Any) -> dict[str, Any]:
        item_type = check_type(item_type)
        if not isinstance(raw_items, list) or not raw_items:
            raise RuleError("Pass at least one item.")
        if len(raw_items) > MAX_UPLOAD:
            raise RuleError(f"At most {MAX_UPLOAD} items per call.")
        items = [clean_item(item_type, raw) for raw in raw_items]
        for item in items:
            if not agent.can_access(item["scope"]):
                raise RuleError(f"Agent {agent.id!r} may not write to {item['scope']!r}.")

        pool = await self._store.pool()
        async with pool.acquire() as con:
            have = {r["hash"] for r in await con.fetch(
                "select hash from lib_item where hash = any($1)", [i["hash"] for i in items])}
        todo = [i for i in items if i["hash"] not in have]

        prepared = []  # embedding is the slow part, so it runs before the transaction
        for item in todo:
            contents = chunk_contents(item)
            prepared.append((item, contents, await self.embed_contents(contents)))

        saved = []
        async with pool.acquire() as con, con.transaction():
            for item, contents, vectors in prepared:
                item_id = await con.fetchval(
                    """insert into lib_item (hash, type, scope, title, summary, body, meta, created_by)
                       values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict (hash) do nothing returning id""",
                    item["hash"], item_type, item["scope"], item["title"], item["summary"], item["body"],
                    item["meta"], agent.id,
                )
                if item_id is None:  # the same content arrived twice
                    continue
                await self.replace_chunks(con, item_id, contents, vectors)
                saved.append({"id": item_id, "title": item["title"], "chunks": len(contents)})
            if saved and self.categories is not None:
                await self.categories.assign(con, [s["id"] for s in saved])
        return {"saved": saved, "unchanged": len(items) - len(saved)}

    async def search(self, agent: Agent, query: Any, item_type: Any = None, scope: str | None = None,
                     k: Any = 5, category: Any = None) -> dict[str, Any]:
        from .store import keyword_query, scope_filter  # here, so the pure helpers above import without asyncpg (tests)

        query = " ".join(str(query or "").split())
        if not query:
            raise RuleError("Pass a non-empty query.")
        k = max(1, min(int(k), 20))
        if item_type:
            types = [str(item_type).strip().lower()]
            if types[0] not in SEARCH_TYPES:
                raise RuleError(f"Invalid type {item_type!r}. Use one of: {', '.join(SEARCH_TYPES)}.")
        else:
            types = list(SEARCH_TYPES)
        cats = [self.categories.check(category)] if category and self.categories else None
        allowed, n = agent.like_patterns, self._candidates
        qvec = to_pgvector(await self._store.embedder.query(query, LIBRARY_TASK))
        # $5: a category to stay in, or null for all; hidden items (legacy skill versions) never show
        visible = ("i.type = any($2) and i.scope like any($3) and i.scope like any($4) and not i.hidden "
                   "and ($5::text[] is null or i.category = any($5::text[]))")

        pool = await self._store.pool()
        async with pool.acquire() as con:
            want = await scope_filter(con, agent, scope)
            tsq = await keyword_query(con, query)
            vec_ids = [r["id"] for r in await con.fetch(
                f"""select c.id from lib_chunk c join lib_item i on i.id = c.item_id where {visible}
                    order by c.embedding <=> $1::vector limit {n}""",
                qvec, types, want, allowed, cats,
            )]
            kw_ids: list[int] = []
            if tsq:
                kw_ids = [r["id"] for r in await con.fetch(
                    f"""select c.id from lib_chunk c join lib_item i on i.id = c.item_id
                        where {visible} and c.tsv @@ to_tsquery('simple', $1)
                        order by ts_rank_cd(c.tsv, to_tsquery('simple', $1)) desc limit {n}""",
                    tsq, types, want, allowed, cats,
                )]
            fused = rrf(kw_ids, vec_ids, weights=(KEYWORD_WEIGHT, VECTOR_WEIGHT))
            if not fused:
                return {"results": [], "note": "Nothing in the library matches this yet."}
            chunks = await con.fetch(
                """select id, item_id, content, 1 - (embedding <=> $2::vector) as similarity
                   from lib_chunk where id = any($1)""",
                list(fused), qvec,
            )
            best: dict[int, Any] = {}  # item id -> its best chunk, in ranking order
            for c in sorted(chunks, key=lambda c: fused[c["id"]], reverse=True):
                best.setdefault(c["item_id"], c)
            ids = list(best)
            items = {r["id"]: r for r in await con.fetch(
                "select id, type, scope, title, summary, meta, category, status from lib_item where id = any($1)", ids)}
            where: dict[int, list[str]] = {}
            for r in await con.fetch(
                """select i.id, l.origin, l.path from lib_location l join lib_item i on i.hash = l.hash
                   where i.id = any($1) order by l.origin, l.path""",
                ids,
            ):
                where.setdefault(r["id"], []).append(f"{r['origin']}:{r['path']}")

        by_item = {c["id"]: c["item_id"] for c in chunks}
        kw_items, vec_items = {by_item[i] for i in kw_ids if i in by_item}, {by_item[i] for i in vec_ids if i in by_item}
        results: list[dict[str, Any]] = []
        shown: set[tuple[str, str]] = set()
        for item_id in ids:
            item, chunk = items[item_id], best[item_id]
            if (item["type"], item["title"]) in shown:  # another version of the same skill: one card is enough
                continue
            shown.add((item["type"], item["title"]))
            card: dict[str, Any] = {
                "id": item_id,
                "type": item["type"],
                "title": item["title"],
                # a reviewed reference's summary is its review: worth the full 300 characters
                "summary": _short(item["summary"], SUMMARY_CHARS if item["type"] == "ref" else CARD_SUMMARY_CHARS),
                "category": item["category"],
                "scope": item["scope"],
                "where": where.get(item_id, [])[:2],
                "matched": "both" if item_id in kw_items and item_id in vec_items else (
                    "keyword" if item_id in kw_items else "meaning"),
                "similarity": round(chunk["similarity"], 3),
            }
            if not item["summary"]:
                card["snippet"] = snippet(chunk["content"])
            if (item["meta"] or {}).get("url"):
                card["url"] = item["meta"]["url"]
            if item["status"]:  # a saved reference: 'inbox' until reviewed
                card["status"] = item["status"]
            results.append(card)
            if len(results) == k:
                break
        return {"results": results}

    async def get(self, agent: Agent, item_id: Any, offset: Any = 0) -> dict[str, Any]:
        item_id, offset = int(item_id), max(0, int(offset or 0))
        pool = await self._store.pool()
        async with pool.acquire() as con:
            item = await con.fetchrow(
                """select id, type, scope, title, summary, body, meta, created_at, category, category_auto, status
                   from lib_item where id = $1 and scope like any($2)""",
                item_id, agent.like_patterns,
            )
            if item is None:
                raise RuleError(f"Library item {item_id} does not exist; it may have changed since. Search again.")
            where = [f"{r['origin']}:{r['path']}" for r in await con.fetch(
                "select origin, path from lib_location where hash = (select hash from lib_item where id = $1) "
                "order by origin, path", item_id)]
            images = await con.fetchval("select count(*) from lib_media where item_id = $1", item_id)
            bundle = (await self._bundle_of(con, agent, item_id))[1] if item["type"] == "skill" else None
        body = item["body"]
        result: dict[str, Any] = {
            "id": item["id"],
            "type": item["type"],
            "title": item["title"],
            "summary": item["summary"],
            "category": item["category"] + (" (filed automatically)" if item["category_auto"] else "")
            if item["category"] else None,
            "scope": item["scope"],
            "where": where,
            "meta": item["meta"],
            "indexed": item["created_at"].date().isoformat(),
            "offset": offset,
            "total_chars": len(body),
            "body": body[offset:offset + PAGE_CHARS],
        }
        if item["status"]:
            result["status"] = item["status"]
        if images:
            result["images"] = f"{images}; see them with library_view({item['id']})"
        if bundle and offset == 0:
            files = [n for n in bundles.names(bundle) if n != "SKILL.md"]
            if files:
                result["files"] = files[:MAX_LISTED_FILES] + (
                    [f"... and {len(files) - MAX_LISTED_FILES} more"] if len(files) > MAX_LISTED_FILES else [])
                result["files_note"] = (f"Paths the skill mentions are these files: read one with "
                                        f"library_file({item['id']}, path), or install the whole skill with "
                                        f"`mc skill-pull {item['id']}`.")
        if offset + PAGE_CHARS < len(body):
            result["next_offset"] = offset + PAGE_CHARS
        return result
