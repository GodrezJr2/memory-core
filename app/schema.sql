-- memory-core schema. Idempotent: applied on every start.
create extension if not exists vector;

create table if not exists memory (
    id            bigserial primary key,
    kind          text not null check (kind in ('core', 'fact', 'episode', 'procedure', 'doc_chunk')),
    scope         text not null,
    content       text not null,
    embedding     vector(1024) not null,                 -- Qwen3-Embedding-0.6B
    -- 'simple' config: notes mix Indonesian and English, so no language stemming or stopwords
    tsv           tsvector generated always as (to_tsvector('simple', content)) stored,
    source        jsonb not null default '{}',
    tags          text[] not null default '{}',
    created_by    text not null,
    created_at    timestamptz not null default now(),
    valid_from    timestamptz not null default now(),
    valid_to      timestamptz,                            -- null = still true
    superseded_by bigint references memory (id),
    verified_at   timestamptz
);

create index if not exists memory_embedding_hnsw on memory using hnsw (embedding vector_cosine_ops);
create index if not exists memory_tsv_gin on memory using gin (tsv);
create index if not exists memory_live_scope on memory (scope) where valid_to is null;
create index if not exists memory_kind_created on memory (kind, created_at desc);

-- Links between memories, 8 Oct 2026: recall follows them one step from its best hits, across projects.
-- Undirected, stored once with src < dst. 'related': close in meaning (cosine >= 0.70, any scope), weight = cosine;
-- 'together': saved in the same remember call, i.e. learned in one piece of work; 'explicit': an agent named the
-- link (remember's links=). A retired memory's links pass to the memory that supersedes it.
create table if not exists memory_link (
    src        bigint not null references memory (id) on delete cascade,
    dst        bigint not null references memory (id) on delete cascade,
    kind       text not null check (kind in ('related', 'together', 'explicit')),
    weight     real not null,
    created_by text not null,
    created_at timestamptz not null default now(),
    primary key (src, dst, kind),
    check (src < dst)
);

create index if not exists memory_link_dst on memory_link (dst);

-- One row per tool call, for the /live page. detail is redacted and at most 300 characters; kept 14 days.
create table if not exists access_log (
    id      bigserial primary key,
    at      timestamptz not null default now(),
    agent   text not null,
    client  text not null,              -- mcp, hook (Claude Code hook), cli (mc), rest
    action  text not null,              -- profile, recall, remember, forget, timeline
    scope   text,
    detail  text,                       -- query, first fact, or forget reason
    results int,                        -- hits, saved facts, episodes
    note    text,                       -- e.g. '1 held'
    ms      int not null,
    ok      boolean not null,
    error   text
);

create index if not exists access_log_at on access_log (at desc);

-- Library: skills, starred repos and project docs that live elsewhere, indexed so agents can find them.
-- Items are content-addressed (hash of type, title, summary, scope and body): the same skill on two machines
-- is one item with two locations, and an edited file becomes a new item while the old one is dropped once
-- no location points at it. Clients sync with clients/library_sync.py.
create table if not exists lib_item (
    id         bigserial primary key,
    hash       text not null unique,
    type       text not null check (type in ('skill', 'repo', 'doc', 'ref')),
    scope      text not null,
    title      text not null,
    summary    text not null default '',
    body       text not null,                          -- redacted; library_get returns it in pages
    meta       jsonb not null default '{}',
    created_by text not null,
    created_at timestamptz not null default now()
);

-- Where each item was found. A sync replaces the rows of its origin (laptop, server, github) and type.
create table if not exists lib_location (
    origin  text not null,
    type    text not null,
    scope   text not null,
    path    text not null,
    hash    text not null,
    seen_at timestamptz not null default now(),
    primary key (origin, type, path)
);

create index if not exists lib_location_hash on lib_location (hash);

create table if not exists lib_chunk (
    id        bigserial primary key,
    item_id   bigint not null references lib_item (id) on delete cascade,
    ord       int not null,
    content   text not null,                           -- "<type> <title>" line, then the chunk
    embedding vector(1024) not null,
    tsv       tsvector generated always as (to_tsvector('simple', content)) stored
);

create index if not exists lib_chunk_embedding_hnsw on lib_chunk using hnsw (embedding vector_cosine_ops);
create index if not exists lib_chunk_tsv_gin on lib_chunk using gin (tsv);
create index if not exists lib_chunk_item on lib_chunk (item_id);

-- Library, 4 Oct 2026: categories (app/library_categories.json), saved references from the phone ('ref': screenshots
-- and links), their review state and their images.
do $$ begin
    if exists (select 1 from pg_constraint where conname = 'lib_item_type_check'
               and pg_get_constraintdef(oid) not like '%ref%') then
        alter table lib_item drop constraint lib_item_type_check;
        alter table lib_item add constraint lib_item_type_check check (type in ('skill', 'repo', 'doc', 'ref'));
    end if;
end $$;

alter table lib_item add column if not exists category text;
alter table lib_item add column if not exists category_auto boolean not null default true;
alter table lib_item add column if not exists hidden boolean not null default false;   -- legacy or duplicate skills
alter table lib_item add column if not exists status text;           -- refs: 'inbox' until an agent reviews them
alter table lib_item add column if not exists updated_at timestamptz not null default now();
create index if not exists lib_item_inbox on lib_item (created_at) where status = 'inbox';

create table if not exists lib_category (
    name        text primary key,
    title       text not null,
    description text not null,                             -- what automatic filing compares items with
    guide       text not null,                             -- which item to pick when, for agents
    ord         int not null,
    embedding   vector(1024) not null,
    source_hash text not null                              -- re-embed only when the text changes
);

-- Images of refs, stored once by content under MEDIA_DIR (<sha256>.<ext>, plus <sha256>.view.jpg for agents).
create table if not exists lib_media (
    sha256     text primary key,
    item_id    bigint not null references lib_item (id) on delete cascade,
    ext        text not null,
    width      int not null,
    height     int not null,
    bytes      int not null,
    ocr        text not null default '',
    created_at timestamptz not null default now()
);

create index if not exists lib_media_item on lib_media (item_id);

-- Skill bundles, 8 Oct 2026: the tree hash of the skill's whole folder at this location, stored as
-- BUNDLE_DIR/<hash>.zip (app/bundles.py). Null for other types and for clients that send none.
alter table lib_location add column if not exists bundle text;
create index if not exists lib_location_bundle on lib_location (bundle) where bundle is not null;

-- Branches agents set, 13 Oct 2026 (app/domains.py). A project scope no pattern in domains.json matches takes the
-- branch the agent that knows the project gives it (memory_branch). A new branch an agent names is 'active' at once
-- (since 15 Oct 2026; before that it waited as 'proposed'); the user can turn it down ('rejected', kept so nobody reuses it).
create table if not exists branch (
    name        text primary key,
    title       text not null,
    description text not null,
    status      text not null default 'proposed' check (status in ('proposed', 'active', 'rejected')),
    created_by  text not null,
    created_at  timestamptz not null default now(),
    decided_by  text,
    decided_at  timestamptz
);

-- Its branch may be a domains.json name or a row of branch; only active ones count (Domains.overlay).
create table if not exists scope_branch (
    scope  text primary key,
    branch text not null,
    set_by text not null,
    set_at timestamptz not null default now()
);
