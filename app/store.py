from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg

from .config import Settings
from .embed import Embedder, to_pgvector
from .rules import (
    KINDS,
    STOPWORDS,
    Agent,
    RuleError,
    check_kind,
    check_scope,
    clean_facts,
    domain_filter,
    domain_name,
    final_score,
    keyword_share,
    read_filter,
    redact,
    word_idf,
)
from .domains import DOMAINS, OTHER, Domains, check_new_branch

SCHEMA = (Path(__file__).with_name("schema.sql")).read_text()
LIVE = "valid_to is null"
# Links (memory_link). A new memory links to its closest live memories in any scope at or above the 'related'
# similarity; recall then follows links one step from its best hits and adds a few it would not have ranked.
LINK_NEIGHBORS = 4
LINK_WEIGHT = {"explicit": 1.0, "together": 0.8}  # 'related' links weigh their cosine similarity (0.70-1)
MAX_TOGETHER = 10  # a bulk import is not one piece of work: larger calls get no 'together' links
LINK_SEEDS = 3
MAX_LINKED = 3
GRAPH_MAX = 3000  # the Brain view draws every node on a canvas; past this it only shows the newest


async def branches(con: asyncpg.Connection) -> Domains:
    """domains.json plus the branches the user accepted and the branches agents set for scopes no pattern matches."""
    assigned = {r["scope"]: r["branch"] for r in await con.fetch("select scope, branch from scope_branch")}
    extra = [dict(r) for r in await con.fetch(
        "select name, title, description from branch where status = 'active' order by decided_at, name")]
    return DOMAINS.overlay(assigned, extra)


async def scope_filter(con: asyncpg.Connection, agent: Agent, requested: str | None) -> list[str]:
    """read_filter, plus scope='domain:<name>' for one branch (the scopes in it that exist now)."""
    name = domain_name(requested)
    if name is None:
        return read_filter(agent, requested)
    present = [r["scope"] for r in await con.fetch(
        f"select distinct scope from memory where {LIVE} union select distinct scope from lib_item")]
    return domain_filter(agent, name, present, await branches(con))


async def add_links(con: asyncpg.Connection, links: list[tuple[int, int, str, float]], by: str) -> None:
    """Store undirected links (a, b, kind, weight); an existing link keeps the larger weight."""
    rows = [(min(a, b), max(a, b), kind, float(w), by) for a, b, kind, w in links if a != b]
    if rows:
        await con.executemany(
            """insert into memory_link (src, dst, kind, weight, created_by) values ($1, $2, $3, $4, $5)
               on conflict (src, dst, kind) do update set weight = greatest(memory_link.weight, excluded.weight)""",
            rows,
        )


async def _init_connection(con: asyncpg.Connection) -> None:
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    # pgvector >= 0.8: keep scanning the HNSW index until filtered queries return enough rows
    await con.execute("set hnsw.ef_search = 100; set hnsw.iterative_scan = relaxed_order")


def _day(ts: datetime | None) -> str | None:
    return ts.date().isoformat() if ts else None


async def keyword_query(con: asyncpg.Connection, text: str) -> str:
    """to_tsquery text: an OR of the words, without stopwords. Words of 4+ letters also match as prefixes
    ("freeze" finds "freezes"); numbers only match exactly (port 876 must not find 8765). '' if no word is left."""
    return await con.fetchval(
        """select coalesce(string_agg(quote_literal(lexeme)
               || case when length(lexeme) >= 4 and lexeme !~ '^[0-9]+$' then ':*' else '' end, ' | '), '')
           from unnest(to_tsvector('simple', $1)) where not (lexeme = any($2::text[]))""",
        text, sorted(STOPWORDS),
    )


# A query word as a tsquery term, matched the way keyword_query matches it (prefix for words of 4+ letters)
_TERM = ("to_tsquery('simple', quote_literal(w.lexeme) || case when length(w.lexeme) >= 4 "
         "and w.lexeme !~ '^[0-9]+$' then ':*' else '' end)")
MAX_STAT_WORDS = 40  # a pasted document must not turn one recall into thousands of lookups


async def keyword_matches(
    con: asyncpg.Connection, text: str, visible: str, args: tuple[Any, ...]
) -> tuple[dict[int, list[str]], dict[int, float], dict[str, int], int]:
    """The query's words (no stopwords) against memories. Returns: for each memory that passes `visible` (a SQL
    condition over $2.. with its args) and contains a query word, those words and its ts_rank_cd (a tie-break);
    each word's document frequency among all live memories; and the number of live memories."""
    corpus = await con.fetchval(f"select count(*) from memory where {LIVE}")
    lexemes = [r["lexeme"] for r in await con.fetch(
        f"""select distinct lexeme from unnest(to_tsvector('simple', $1))
            where not (lexeme = any($2::text[])) limit {MAX_STAT_WORDS}""",
        text, sorted(STOPWORDS),
    )]
    df = {r["lexeme"]: r["n"] for r in await con.fetch(
        f"""select w.lexeme, count(*) as n from unnest($1::text[]) as w(lexeme)
            join memory m on m.{LIVE} and m.tsv @@ {_TERM} group by w.lexeme""",
        lexemes,
    )} if lexemes else {}
    if not df:
        return {}, {}, {}, corpus
    found = await con.fetch(
        f"""select id, array_agg(w.lexeme) as words, ts_rank_cd(tsv, to_tsquery('simple', ${len(args) + 2})) as rank
            from memory, unnest($1::text[]) as w(lexeme)
            where {visible} and tsv @@ {_TERM} group by id""",
        sorted(df), *args, await keyword_query(con, " ".join(sorted(df))),
    )
    return {r["id"]: r["words"] for r in found}, {r["id"]: r["rank"] for r in found}, df, corpus


class Store:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self.embedder = Embedder(settings.embed_url)

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    bootstrap = await asyncpg.connect(self._s.database_url)
                    try:
                        await bootstrap.execute(SCHEMA)
                    finally:
                        await bootstrap.close()
                    self._pool = await asyncpg.create_pool(
                        self._s.database_url, min_size=1, max_size=5, init=_init_connection
                    )
        return self._pool

    # --- write -----------------------------------------------------------------------------

    async def remember(
        self,
        agent: Agent,
        facts: list[str],
        kind: str = "fact",
        scope: str = "global",
        supersedes: int | None = None,
        tags: list[str] | None = None,
        verified: bool = False,
        force: bool = False,
        source: dict[str, Any] | None = None,
        links: list[int] | None = None,
    ) -> dict[str, Any]:
        kind, scope = check_kind(kind), check_scope(scope)
        if not agent.can_access(scope):
            raise RuleError(f"Agent {agent.id!r} may not write to {scope!r}.")
        texts = clean_facts(facts, kind)
        if supersedes is not None and len(texts) != 1:
            raise RuleError("supersedes replaces one memory; pass exactly one fact with it.")
        try:
            link_to = sorted({int(i) for i in links or []})
        except (TypeError, ValueError):
            raise RuleError("links must be memory ids (numbers from memory_recall).") from None

        redacted: list[str] = []
        safe: list[str] = []
        for text in texts:
            clean, found = redact(text)
            safe.append(clean)
            redacted.extend(found)
        vectors = [to_pgvector(v) for v in await self.embedder.documents(safe)]
        src = {"agent": agent.id, **(source or {})}

        saved: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        pool = await self.pool()
        async with pool.acquire() as con, con.transaction():
            if supersedes is not None:
                old = await con.fetchrow(f"select scope from memory where id = $1 and {LIVE}", supersedes)
                if old is None:
                    raise RuleError(f"Memory {supersedes} does not exist or was already retired.")
                if not agent.can_access(old["scope"]):
                    raise RuleError(f"Agent {agent.id!r} may not change memories in {old['scope']!r}.")
            if link_to:
                found = {r["id"]: r["scope"] for r in await con.fetch(
                    f"select id, scope from memory where id = any($1) and {LIVE}", link_to)}
                missing = [i for i in link_to if i not in found or not agent.can_access(found[i])]
                if missing:
                    raise RuleError(f"links: memories {missing} do not exist, are retired or are not yours to see.")

            new_links: list[tuple[int, int, str, float]] = []
            for text, vec in zip(safe, vectors):
                if supersedes is None:
                    dup = await con.fetchval(
                        f"select id from memory where scope = $1 and {LIVE} and lower(content) = lower($2) limit 1",
                        scope, text,
                    )
                    if dup:
                        skipped.append({"text": text, "duplicate_of": dup})
                        continue
                related: list[dict[str, Any]] = []
                if supersedes is None:
                    near = [
                        {"id": r["id"], "text": r["content"], "similarity": round(r["similarity"], 3)}
                        for r in await con.fetch(
                            f"""select id, content, 1 - (embedding <=> $1::vector) as similarity
                                from memory where scope = $2 and {LIVE}
                                order by embedding <=> $1::vector limit 3""",
                            vec, scope,
                        )
                    ]
                    similar = [r for r in near if r["similarity"] >= self._s.near_duplicate]
                    if similar and not force:
                        held.append({"text": text, "similar": similar})
                        continue
                    # An update ("moved to port 8766") scores well below a paraphrase, so it is saved,
                    # but the old memories it may contradict are returned for the agent to retire.
                    related = [r for r in near if self._s.related <= r["similarity"] < self._s.near_duplicate]

                new_id = await con.fetchval(
                    """insert into memory (kind, scope, content, embedding, source, tags, created_by, verified_at)
                       values ($1, $2, $3, $4::vector, $5, $6, $7, case when $8 then now() end)
                       returning id""",
                    kind, scope, text, vec, src, tags or [], agent.id, verified,
                )
                if supersedes is not None:
                    await con.execute(
                        "update memory set valid_to = now(), superseded_by = $2 where id = $1", supersedes, new_id
                    )
                    # the replacement inherits what the old memory was connected to
                    await con.execute(
                        """insert into memory_link (src, dst, kind, weight, created_by)
                           select least(other, $2), greatest(other, $2), kind, weight, created_by from (
                               select case when src = $1 then dst else src end as other, kind, weight, created_by
                               from memory_link where src = $1 or dst = $1) l
                           where other <> $2 on conflict do nothing""",
                        supersedes, new_id,
                    )
                new_links += [(new_id, r["id"], "related", r["similarity"]) for r in await con.fetch(
                    f"""select id, 1 - (embedding <=> $1::vector) as similarity from memory
                        where {LIVE} and id <> $2 order by embedding <=> $1::vector limit {LINK_NEIGHBORS}""",
                    vec, new_id,
                ) if r["similarity"] >= self._s.related]
                new_links += [(new_id, other, "explicit", LINK_WEIGHT["explicit"]) for other in link_to]
                saved.append({"id": new_id, "text": text, **({"related": related} if related else {})})

            ids = [s["id"] for s in saved]
            if 1 < len(ids) <= MAX_TOGETHER:
                new_links += [(a, b, "together", LINK_WEIGHT["together"]) for n, a in enumerate(ids) for b in ids[n + 1:]]
            await add_links(con, new_links, agent.id)

        result: dict[str, Any] = {"saved": saved}
        if saved and scope.startswith("project:") and DOMAINS.by_rule(scope) is None:
            async with pool.acquire() as con:
                if await con.fetchval("select branch from scope_branch where scope = $1", scope) is None:
                    result["branch_needed"] = (
                        f"{scope} has no branch yet. If you know what the project is for, call memory_branch with "
                        f"one of: {(await branches(con)).describe()} (memory_branches describes each). If none "
                        "fits, name a new branch there with a title and description; it is created at once, so tell "
                        "the user, who can turn it down later."
                    )
        if any("related" in s for s in saved):
            result["check_related"] = (
                "Some saved facts resemble older memories. If an older one is now outdated, "
                "retire it with memory_forget, or next time pass supersedes=<id>."
            )
        if skipped:
            result["skipped_duplicates"] = skipped
        if held:
            result["held_near_duplicates"] = held
            result["next_step"] = (
                "Held facts look like existing memories and were not saved. To replace one, call again "
                "with that single fact and supersedes=<id>. If it is genuinely different, call again with force=true."
            )
        if supersedes is not None and saved:
            result["retired"] = supersedes
        if redacted:
            result["redacted"] = sorted(set(redacted))
        return result

    async def forget(self, agent: Agent, memory_id: int, reason: str) -> dict[str, Any]:
        reason = " ".join(reason.split())
        if not reason:
            raise RuleError("Say why in reason, so the next reader knows.")
        pool = await self.pool()
        async with pool.acquire() as con:
            row = await con.fetchrow(f"select scope from memory where id = $1 and {LIVE}", memory_id)
            if row is None:
                raise RuleError(f"Memory {memory_id} does not exist or was already retired.")
            if not agent.can_access(row["scope"]):
                raise RuleError(f"Agent {agent.id!r} may not change memories in {row['scope']!r}.")
            await con.execute(
                """update memory set valid_to = now(),
                   source = source || jsonb_build_object('retired', jsonb_build_object('by', $2::text, 'reason', $3::text))
                   where id = $1""",
                memory_id, agent.id, reason,
            )
        return {"retired": memory_id}

    # --- read ------------------------------------------------------------------------------

    async def recall(
        self, agent: Agent, query: str, scope: str | None = None, kinds: list[str] | None = None, k: int = 8,
        expand: bool = False, prefer: str | None = None,
    ) -> dict[str, Any]:
        """prefer: a scope (project:<name>) whose memories rank a little higher, without filtering others out."""
        query = " ".join(query.split())
        if not query:
            raise RuleError("Pass a non-empty query.")
        k = max(1, min(int(k), 30))
        kind_list = [check_kind(x) for x in kinds] if kinds else list(KINDS)
        allowed, n = agent.like_patterns, self._s.candidates
        qvec = to_pgvector(await self.embedder.query(query))
        visible = f"{LIVE} and kind = any($2) and scope like any($3) and scope like any($4)"

        pool = await self.pool()
        async with pool.acquire() as con:
            want = await scope_filter(con, agent, scope)
            vec_ids = [r["id"] for r in await con.fetch(
                f"select id from memory where {visible} order by embedding <=> $1::vector limit {n}",
                qvec, kind_list, want, allowed,
            )]
            words, kw_rank, df, corpus = await keyword_matches(con, query, visible, (kind_list, want, allowed))
            # how much of the query's rarity each memory holds; the n best of those join the n closest in meaning
            idf = {w: word_idf(corpus, count) for w, count in df.items()}
            share = {i: keyword_share(ws, idf) for i, ws in words.items()}
            kw_ids = sorted(share, key=lambda i: (-share[i], -kw_rank[i]))[:n]
            candidates = list(dict.fromkeys(vec_ids + kw_ids))
            if not candidates:
                return {"results": [], "note": "Nothing stored matches this yet."}
            rows = await con.fetch(
                """select id, kind, scope, content, source, tags, created_by, created_at, verified_at,
                          1 - (embedding <=> $2::vector) as similarity
                   from memory where id = any($1)""",
                candidates, qvec,
            )
            ranked = sorted(rows, reverse=True, key=lambda r: final_score(
                r["similarity"], share.get(r["id"], 0.0), r["kind"], r["verified_at"] is not None,
                r["scope"] == prefer))[:k]
            linked = await self._linked(con, ranked, want, allowed, kind_list) if expand else []

        close = set(vec_ids)
        result = {
            # Which query words each result shares, with how many live memories contain each word (out of
            # 'corpus'): a word in 3 of 155 memories is evidence of relevance, one in 30 is not.
            "corpus": corpus,
            "results": [
                {
                    "id": r["id"],
                    "text": r["content"],
                    "kind": r["kind"],
                    "scope": r["scope"],
                    "by": r["created_by"],
                    "date": _day(r["created_at"]),
                    "verified": _day(r["verified_at"]),
                    "tags": r["tags"],
                    "source": {key: v for key, v in (r["source"] or {}).items() if key != "agent"},
                    # 'meaning': shares no query word; 'keyword': shares words but is not among the closest in meaning
                    "matched": "meaning" if r["id"] not in words else ("both" if r["id"] in close else "keyword"),
                    # cosine similarity to the query: an absolute relevance signal
                    "similarity": round(r["similarity"], 3),
                    "keywords": {w: df[w] for w in sorted(set(words.get(r["id"], [])))},
                }
                for r in ranked
            ]
        }
        if linked:
            # one step along memory_link from the best hits: what was learned with them, or close to them in
            # another project, that the search itself did not rank
            result["linked"] = linked
        return result

    @staticmethod
    async def _linked(con: asyncpg.Connection, ranked: list[Any], want: list[str], allowed: list[str],
                      kinds: list[str]) -> list[dict[str, Any]]:
        seeds = {r["id"]: n for n, r in enumerate(ranked[:LINK_SEEDS])}
        if not seeds:
            return []
        rows = await con.fetch(
            """select l.seed, l.other, l.kind as link, l.weight, m.kind, m.scope, m.content, m.created_at, m.verified_at
               from (select src as seed, dst as other, kind, weight from memory_link where src = any($1)
                     union all
                     select dst, src, kind, weight from memory_link where dst = any($1)) l
               join memory m on m.id = l.other
               where m.valid_to is null and m.scope like any($2) and m.scope like any($3) and m.kind = any($4)
                 and not (m.id = any($5))""",
            list(seeds), want, allowed, kinds, [r["id"] for r in ranked],
        )
        best: dict[int, tuple[float, Any]] = {}
        for r in rows:  # a link from the top hit counts most
            score = r["weight"] / (1 + seeds[r["seed"]])
            if score > best.get(r["other"], (0.0, None))[0]:
                best[r["other"]] = (score, r)
        top = sorted(best.values(), key=lambda x: x[0], reverse=True)[:MAX_LINKED]
        return [{"id": r["other"], "text": r["content"], "kind": r["kind"], "scope": r["scope"],
                 "date": _day(r["created_at"]), "verified": _day(r["verified_at"]),
                 "via": r["seed"], "link": r["link"]} for _, r in top]

    async def rebuild_links(self) -> dict[str, int]:
        """Link existing memories: 'related' to their closest live memories in any scope, 'together' when saved in one
        call (same agent, same transaction time). Idempotent; new memories are linked as they are saved."""
        pool = await self.pool()
        async with pool.acquire() as con, con.transaction():
            before = await con.fetchval("select count(*) from memory_link")
            await con.execute(
                f"""insert into memory_link (src, dst, kind, weight, created_by)
                    select least(m.id, n.id), greatest(m.id, n.id), 'related', max(n.similarity), 'rebuild'
                    from memory m cross join lateral (
                        select o.id, 1 - (o.embedding <=> m.embedding) as similarity from memory o
                        where o.valid_to is null and o.id <> m.id
                        order by o.embedding <=> m.embedding limit {LINK_NEIGHBORS}) n
                    where m.valid_to is null and n.similarity >= $1
                    group by 1, 2 on conflict (src, dst, kind) do nothing""",
                self._s.related,
            )
            related = await con.fetchval("select count(*) from memory_link") - before
            await con.execute(
                f"""with calls as (
                        select id, created_by, created_at, count(*) over (partition by created_by, created_at) as n
                        from memory where {LIVE})
                    insert into memory_link (src, dst, kind, weight, created_by)
                    select a.id, b.id, 'together', $1, 'rebuild' from calls a
                    join calls b on a.created_by = b.created_by and a.created_at = b.created_at and a.id < b.id
                    where a.n <= $2 on conflict (src, dst, kind) do nothing""",
                LINK_WEIGHT["together"], MAX_TOGETHER,
            )
            total = await con.fetchval("select count(*) from memory_link")
        return {"related": related, "together": total - before - related, "links": total}

    async def profile(self, agent: Agent) -> dict[str, Any]:
        allowed = agent.like_patterns
        pool = await self.pool()
        async with pool.acquire() as con:
            core = await con.fetch(
                f"""select id, content from memory where kind = 'core' and {LIVE}
                    and scope in ('global', $2) and scope like any($1) order by created_at""",
                allowed, f"agent:{agent.id}",
            )
            projects = await con.fetch(
                f"""select scope, count(*) as memories, max(created_at) as last from memory
                    where {LIVE} and scope like 'project:%' and scope like any($1)
                    group by scope order by last desc limit 40""",
                allowed,
            )
            episodes = await con.fetch(
                f"""select id, scope, content, created_by, created_at from memory
                    where kind = 'episode' and {LIVE} and scope like any($1)
                    order by created_at desc limit 3""",
                allowed,
            )
            domains = await branches(con)
            proposed = await con.fetch(
                """select b.name, b.title, array_remove(array_agg(s.scope order by s.scope), null) as scopes
                   from branch b left join scope_branch s on s.branch = b.name
                   where b.status = 'proposed' group by b.name, b.title, b.created_at order by b.created_at""")
        result = {
            "you_are": agent.id,
            "profile": [{"id": r["id"], "text": r["content"]} for r in core],
            "projects": [
                {"scope": r["scope"], "domain": domains.of(r["scope"]), "memories": r["memories"],
                 "last_update": _day(r["last"])} for r in projects
            ],
            # the branches above projects; recall and timeline take scope='domain:<name>'
            "domains": [{"name": d["name"], "title": d["title"]} for d in domains.items],
            "recent_episodes": [
                {"id": r["id"], "scope": r["scope"], "text": r["content"], "by": r["created_by"],
                 "date": _day(r["created_at"])}
                for r in episodes
            ],
        }
        waiting = [(r, [s.split(":", 1)[1] for s in r["scopes"] if agent.can_access(s)]) for r in proposed]
        if any(scopes for _, scopes in waiting):  # new branches agents named, for the user to accept or turn down
            result["branch_proposals"] = [{"name": r["name"], "title": r["title"], "projects": scopes}
                                          for r, scopes in waiting if scopes]
        return result

    async def graph(self, agent: Agent, limit: int = GRAPH_MAX) -> dict[str, Any]:
        """Every live memory the agent may read (newest first, up to limit) with the links between them, for the
        /live Brain view. Global and agent notes have no branch (domain None)."""
        pool = await self.pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                f"""select id, kind, scope, content, created_by, created_at, verified_at from memory
                    where {LIVE} and scope like any($1) order by created_at desc, id desc limit $2""",
                agent.like_patterns, limit,
            )
            ids = [r["id"] for r in rows]
            links = await con.fetch(
                "select src, dst, kind, weight from memory_link where src = any($1) and dst = any($1)", ids)
            domains = await branches(con)
        return {
            "domains": [{"name": d["name"], "title": d["title"]} for d in domains.items]
                       + [{"name": OTHER, "title": "Other"}],
            "nodes": [
                {"id": r["id"], "kind": r["kind"], "scope": r["scope"], "domain": domains.of(r["scope"]),
                 "text": r["content"], "by": r["created_by"], "date": _day(r["created_at"]),
                 "verified": r["verified_at"] is not None}
                for r in rows
            ],
            "links": [{"a": r["src"], "b": r["dst"], "kind": r["kind"], "w": round(r["weight"], 3)} for r in links],
        }

    # --- branches ----------------------------------------------------------------------------

    async def branch_overview(self, agent: Agent) -> dict[str, Any]:
        """Every branch with its projects, the new branches waiting for the user, and the projects with no branch (with
        their newest notes, so an agent can tell what they are)."""
        pool = await self.pool()
        async with pool.acquire() as con:
            domains = await branches(con)
            counts = {r["scope"]: (r["memories"], r["docs"]) for r in await con.fetch(
                f"""select scope, sum(m)::int as memories, sum(d)::int as docs from (
                        select scope, count(*) as m, 0 as d from memory where {LIVE} group by scope
                        union all select scope, 0, count(*) from lib_item group by scope) x
                    where scope like 'project:%' and scope like any($1) group by scope""",
                agent.like_patterns,
            )}
            set_by = {r["scope"]: (r["branch"], r["set_by"]) for r in await con.fetch(
                "select scope, branch, set_by from scope_branch")}
            others = await con.fetch(
                "select name, title, description, status, created_by, created_at, decided_at from branch "
                "where status <> 'active' order by created_at")
            open_scopes = [s for s in counts if domains.of(s) == OTHER and s not in set_by]
            recent = await con.fetch(
                f"""select scope, content from (select scope, content, row_number() over (
                        partition by scope order by created_at desc) as n from memory where {LIVE} and scope = any($1)) x
                    where n <= 2""", open_scopes)
            docs = await con.fetch(
                """select distinct on (scope) scope, title, left(summary, 200) as summary from lib_item
                   where scope = any($1) order by scope, length(summary) desc""", open_scopes)

        def project(scope: str) -> str:
            memories, n_docs = counts[scope]
            parts = [f"{memories} memories"] + ([f"{n_docs} docs"] if n_docs else [])
            if scope in set_by and DOMAINS.by_rule(scope) is None:
                parts.append(f"set by {set_by[scope][1]}")
            return f"{scope.split(':', 1)[1]} ({', '.join(parts)})"

        notes: dict[str, list[str]] = {}
        for r in recent:
            notes.setdefault(r["scope"], []).append(r["content"][:240])
        for r in docs:
            notes.setdefault(r["scope"], []).append(f"{r['title']}: {r['summary']}")
        return {
            "branches": [
                {"name": d["name"], "title": d["title"], "description": d.get("description", ""),
                 "projects": [project(s) for s in domains.members(d["name"], list(counts)) or []]}
                for d in domains.items
            ],
            "proposed": [
                {"name": r["name"], "title": r["title"], "description": r["description"], "by": r["created_by"],
                 "date": _day(r["created_at"]),
                 "projects": [project(s) for s, (b, _) in set_by.items() if b == r["name"] and s in counts]}
                for r in others if r["status"] == "proposed"
            ],
            # The user turned these down: do not propose them again
            "rejected": [r["name"] for r in others if r["status"] == "rejected"],
            "unbranched": [{"scope": s, "memories": counts[s][0], "docs": counts[s][1], "notes": notes.get(s, [])}
                           for s in sorted(open_scopes, key=lambda s: -sum(counts[s]))],
            "next_step": "memory_branch(scope, branch) files a project you can tell apart from its notes; leave the "
                         "rest. Use an existing branch when one fits; only when none does, name a new one with a "
                         "title and description. It is created at once: tell the user, who can turn it down later.",
        }

    async def set_branch(self, agent: Agent, scope: str, branch: str, title: str | None = None,
                         description: str | None = None) -> dict[str, Any]:
        """Put a project no domains.json pattern matches in a branch, creating the branch if it is new. 'other'
        clears what an agent set."""
        scope = check_scope(scope)
        if not scope.startswith("project:"):
            raise RuleError("Only project scopes sit in branches; global and agent notes form the core.")
        if not agent.can_access(scope):
            raise RuleError(f"Agent {agent.id!r} may not change {scope!r}.")
        rule = DOMAINS.by_rule(scope)
        if rule:
            raise RuleError(f"{scope} is in {rule} by a pattern in app/domains.json; edit that file to move it.")
        name = " ".join(str(branch or "").split()).lower()
        status = "active"
        pool = await self.pool()
        async with pool.acquire() as con, con.transaction():
            if not await con.fetchval(
                    f"""select exists(select 1 from memory where scope = $1 and {LIVE})
                        or exists(select 1 from lib_item where scope = $1)""", scope):
                raise RuleError(f"{scope} has no memories or library items; save one there first.")
            if name == OTHER:
                await con.execute("delete from scope_branch where scope = $1", scope)
                return {"scope": scope, "branch": OTHER, "status": status}
            domains = await branches(con)
            if name not in domains.names:
                row = await con.fetchrow("select status, decided_at from branch where name = $1", name)
                if row is not None and row["status"] == "rejected":
                    raise RuleError(f"The user turned down a branch named {name!r} on {_day(row['decided_at'])}. "
                                    f"Use one of: {', '.join(domains.names)}, or {OTHER}.")
                if row is None:
                    clean_title, clean_description = check_new_branch(name, title, description, domains.names)
                    await con.execute(
                        """insert into branch (name, title, description, status, created_by, decided_by, decided_at)
                           values ($1, $2, $3, 'active', $4, $4, now()) on conflict (name) do nothing""",
                        name, clean_title, clean_description, agent.id,
                    )
                    status = "created"
                else:  # still waiting from before agents could create branches: it counts once the user accepts it
                    status = "proposed"
            await con.execute(
                """insert into scope_branch (scope, branch, set_by) values ($1, $2, $3)
                   on conflict (scope) do update set branch = $2, set_by = $3, set_at = now()""",
                scope, name, agent.id,
            )
        result: dict[str, Any] = {"scope": scope, "branch": name, "status": status}
        if status == "created":
            result["note"] = (f"{name!r} is a new branch and {scope} is in it now. Tell the user in your reply; they can turn "
                              "it down later (memory_branch_review, only when they say so).")
        elif status == "proposed":
            result["note"] = (f"{name!r} is a new branch waiting for the user; {scope} stays in {OTHER} until they accept "
                              "it. Tell them. Only they decide (memory_branch_review, when they say so).")
        return result

    async def review_branch(self, agent: Agent, name: str, accept: bool, title: str | None = None,
                            description: str | None = None) -> dict[str, Any]:
        """The user's decision on a branch: accepted (only a proposed one), its projects move into it; turned down (a
        proposed one, or one an agent created that is already active), they go back to other and the name cannot
        be used again."""
        name = " ".join(str(name or "").split()).lower()
        pool = await self.pool()
        async with pool.acquire() as con, con.transaction():
            row = await con.fetchrow("select title, description, status from branch where name = $1 for update", name)
            if row is None or not (row["status"] == "proposed" or (row["status"] == "active" and not accept)):
                open_ = [r["name"] for r in await con.fetch(
                    "select name from branch where status = 'proposed' or (status = 'active' and not $1)", accept)]
                raise RuleError(f"No branch named {name!r} to {'accept' if accept else 'turn down'}. "
                                f"Open: {', '.join(open_) or 'none'}.")
            scopes = [r["scope"] for r in await con.fetch(
                "select scope from scope_branch where branch = $1 order by scope", name)]
            if not all(agent.can_access(s) for s in scopes):
                raise RuleError(f"Agent {agent.id!r} may not change every project in {name!r}.")
            if accept:
                clean_title, clean_description = check_new_branch(
                    name, title or row["title"], description or row["description"], [])
                await con.execute(
                    """update branch set status = 'active', title = $2, description = $3, decided_by = $4,
                       decided_at = now() where name = $1""", name, clean_title, clean_description, agent.id)
            else:
                await con.execute(
                    "update branch set status = 'rejected', decided_by = $2, decided_at = now() where name = $1",
                    name, agent.id)
                await con.execute("delete from scope_branch where branch = $1", name)
        return {"branch": name, "status": "active" if accept else "rejected", "projects": scopes}

    async def timeline(
        self, agent: Agent, scope: str | None = None, since: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        try:
            start = date.fromisoformat(since) if since else date.today() - timedelta(days=14)
        except ValueError as exc:
            raise RuleError("since must be a date like 2026-09-30.") from exc
        start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        pool = await self.pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                f"""select id, scope, content, created_by, created_at from memory
                    where kind = 'episode' and {LIVE} and scope like any($1) and scope like any($2)
                    and created_at >= $3 order by created_at desc limit $4""",
                await scope_filter(con, agent, scope), agent.like_patterns, start_ts, max(1, min(int(limit), 100)),
            )
        return {
            "episodes": [
                {"id": r["id"], "scope": r["scope"], "text": r["content"], "by": r["created_by"],
                 "date": _day(r["created_at"])}
                for r in rows
            ]
        }

    async def healthy(self) -> bool:
        pool = await self.pool()
        async with pool.acquire() as con:
            return await con.fetchval("select true")
