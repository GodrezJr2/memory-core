"""memory-core: one ASGI app serving MCP at /mcp and REST at /api/v1, both behind per-agent bearer tokens."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any, Awaitable

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from .activity import Activity, client_kind
from .auth import TokenBook
from .categories import Categories
from .domains import DOMAINS
from .library import Library
from .refs import Refs
from .rules import Agent, RuleError, default_scopes
from .store import Store

log = logging.getLogger("memory-core")
settings = config.load()
store = Store(settings)
tokens = TokenBook(settings.tokens_file)
activity = Activity(store.pool)
categories = Categories(store)
library = Library(store, settings.candidates, categories)
refs = Refs(store, library, categories)
# The user, through the web app's Library tab or a phone Shortcut: authenticated by their Tailscale login, no token.
OWNER = Agent("owner", default_scopes("owner"))
# The /live page is a React build (web/ in the repo), copied here by the Dockerfile.
LIVE_DIR = Path(__file__).with_name("live")

INSTRUCTIONS = """Shared long-term memory for the user's AI agents (Claude Code, Codex, Antigravity, OpenClaw).
- At session start, call memory_profile.
- Before answering about the user's projects, servers or preferences, call memory_recall and cite what you used.
  Projects sit in branches (study, work, projects, infra, personal): scope='domain:<name>' searches one branch.
- When you learn a durable fact, call memory_remember with short standalone sentences, one fact each. Facts from
  one piece of work go in one call (they are linked); pass links=[ids] for a connection the wording does not show.
- If it holds near-duplicates, either supersede the old memory or skip.
- At session end, save a 3-line episode (kind="episode"): what changed, what is left, where.
- If memory_remember says a project has no branch, put it in one with memory_branch. Check memory_branches first
  and reuse a branch that fits; if none does, name a new one there. It is created at once, so tell the user in your reply
  (they can turn it down later with memory_branch_review, only when they say so).
- Before a task a skill may cover (design, testing, a tool's workflow), or to find a project's docs, a starred
  repo or a reference the user saved, call library_categories once, then library_search (with category=...);
  open a hit with library_get, and a saved image with library_view. A skill read this way is followed like a local
  one; read the files it mentions with library_file. To install a skill into your own skills folder, run
  `mc skill-pull <name or id> --to <folder>` (clients/mc.py).
- When the user asks to review their saved references: library_inbox, then library_view and library_review each one."""

mcp = MCPServer(name="memory-core", instructions=INSTRUCTIONS, version="0.1.0")

ScopeParam = Annotated[
    str | None,
    Field(description="'project:<name>' to focus on one project, or 'domain:<name>' for one branch of projects: "
                      f"{DOMAINS.describe()} (global and your own notes are always included). "
                      "Omit to search everything you can see."),
]


def _agent(ctx: Context) -> Agent:
    agent = tokens.agent_for((ctx.headers or {}).get("authorization"))
    if agent is None:  # the middleware rejects these first; this guards a misconfigured mount
        raise RuleError("Missing or unknown bearer token.")
    return agent


async def _tracked(agent: Agent, client: str, action: str, call: Awaitable[dict[str, Any]],
                   args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool call and report it to the access log and the /live page."""
    started = time.perf_counter()
    try:
        result = await call
    except Exception as exc:
        activity.record(agent.id, client, action, args, None, started, error=str(exc))
        # The MCP SDK hides the text of any exception that is not a ToolError behind a bare
        # "Error executing tool", so the agent could not see which rule it broke.
        if client == "mcp" and isinstance(exc, RuleError):
            raise ToolError(str(exc)) from exc
        raise
    activity.record(agent.id, client, action, args, result, started)
    return result


@mcp.tool()
async def memory_profile(ctx: Context) -> dict[str, Any]:
    """Load the user's core profile, active projects and the latest session episodes. Call once at session start."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "profile", store.profile(agent), {})


@mcp.tool()
async def memory_recall(
    query: Annotated[str, Field(description="Question or keywords, in any language.")],
    ctx: Context,
    scope: ScopeParam = None,
    kinds: Annotated[
        list[str] | None, Field(description="Optional filter: core, fact, episode, procedure, doc_chunk.")
    ] = None,
    k: Annotated[int, Field(ge=1, le=30, description="How many results to return.")] = 8,
) -> dict[str, Any]:
    """Search shared memory by exact words and by meaning. Results carry who wrote them, when, and whether
    someone verified them against the live system. Prefer verified and newer results when they disagree,
    and mention the disagreement. 'linked' adds a few memories connected to the best hits (learned together
    with them, or close to them in another project) that the search itself did not rank."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "recall", store.recall(agent, query, scope, kinds, k, expand=True),
                          {"query": query, "scope": scope})


@mcp.tool()
async def memory_remember(
    facts: Annotated[
        list[str],
        Field(description="Short standalone sentences, one fact each, readable without this conversation. "
                          "Example: 'StockTracker runs as a systemd service with uvicorn on port 8765.'"),
    ],
    ctx: Context,
    kind: Annotated[
        str, Field(description="fact (default), procedure (how to do a task), episode (session summary), "
                               "or core (the user's profile; only when the user asks).")
    ] = "fact",
    scope: Annotated[
        str, Field(description="global, project:<name>, agent:<your id>, or session:<name>.")
    ] = "global",
    supersedes: Annotated[
        int | None, Field(description="Id of an existing memory this single fact replaces; the old one is retired.")
    ] = None,
    verified: Annotated[
        bool, Field(description="True only if you just checked this against the live system.")
    ] = False,
    force: Annotated[bool, Field(description="Save even if a near-duplicate exists.")] = False,
    tags: list[str] | None = None,
    links: Annotated[
        list[int] | None,
        Field(description="Ids of existing memories these facts connect to in a way their wording does not show "
                          "(cause, the project it serves, the decision it came from), often from another project."),
    ] = None,
) -> dict[str, Any]:
    """Save durable facts to shared memory. Secrets and account numbers are redacted before storage.
    Exact duplicates are skipped; near-duplicates are held and returned so you can supersede or skip.
    Facts saved in one call are linked as learned together; recall follows those links."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "remember",
                          store.remember(agent, facts, kind, scope, supersedes, tags, verified, force, links=links),
                          {"facts": facts, "scope": scope})


@mcp.tool()
async def memory_forget(
    memory_id: Annotated[int, Field(description="Id from memory_recall.")],
    reason: Annotated[str, Field(description="Why it is no longer true.")],
    ctx: Context,
) -> dict[str, Any]:
    """Retire a memory that is wrong or outdated. Nothing is deleted; it just stops appearing in results."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "forget", store.forget(agent, memory_id, reason),
                          {"memory_id": memory_id, "reason": reason})


@mcp.tool()
async def memory_timeline(
    ctx: Context,
    scope: ScopeParam = None,
    since: Annotated[str | None, Field(description="Date like 2026-09-30. Default: 14 days ago.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List session episodes in date order, newest first, e.g. to answer 'what did I do last week'."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "timeline", store.timeline(agent, scope, since, limit),
                          {"scope": scope, "since": since})


@mcp.tool()
async def memory_branches(ctx: Context) -> dict[str, Any]:
    """The branches projects sit in (each with its description and projects), new branches waiting for the user, and
    the projects with no branch yet together with their newest notes."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "branches", store.branch_overview(agent), {})


@mcp.tool()
async def memory_branch(
    scope: Annotated[str, Field(description="The project, e.g. project:argos-proxmox.")],
    branch: Annotated[str, Field(description="An existing branch from memory_branches, a new name to create, "
                                             "or 'other' to clear it.")],
    ctx: Context,
    title: Annotated[str | None, Field(description="New branch only: a short title, e.g. 'Hobby and games'.")] = None,
    description: Annotated[str | None, Field(
        description="New branch only: what belongs in it, in one or two sentences.")] = None,
) -> dict[str, Any]:
    """Put a project in a branch: the one that matches what the project is for (not the tools it uses, since
    nearly every project runs on the server). Reuse an existing branch when one fits. A new branch name needs a
    title and description and is created at once with the project in it; tell the user, who can turn it down later."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "branch", store.set_branch(agent, scope, branch, title, description),
                          {"scope": scope, "branch": branch})


@mcp.tool()
async def memory_branch_review(
    name: Annotated[str, Field(description="A proposed branch, or a branch an agent created, from memory_branches.")],
    accept: Annotated[bool, Field(description="True if the user accepts a proposed branch, False if they turn it down.")],
    ctx: Context,
    title: Annotated[str | None, Field(description="A better title, if the user gave one.")] = None,
    description: Annotated[str | None, Field(description="A better description, if the user gave one.")] = None,
) -> dict[str, Any]:
    """Record the user's decision on a branch. Only when the user has said so. Accepted (a proposed branch), its projects move
    into it; turned down (proposed, or created by an agent), they return to other and the name is not used again."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "branch_review", store.review_branch(agent, name, accept, title, description),
                          {"branch": name, "accept": accept})


LibType = Annotated[str | None, Field(description="Optional filter: skill, repo, doc or ref (saved reference).")]
CategoryParam = Annotated[str | None, Field(description="Optional category name from library_categories, "
                                                        "e.g. web-design, motion, 3d-web, documents.")]


@mcp.tool()
async def library_search(
    query: Annotated[str, Field(description="The task or question, in any language.")],
    ctx: Context,
    type: LibType = None,
    category: CategoryParam = None,
    scope: ScopeParam = None,
    k: Annotated[int, Field(ge=1, le=20, description="How many cards to return.")] = 5,
) -> dict[str, Any]:
    """Search the user's library: agent skills (SKILL.md files from every machine), their starred GitHub repos, their
    project docs (README, CLAUDE.md, AGENTS.md, HANDOVER) and references they saved from their phone (screenshots and
    links). Returns short cards; open one with library_get. Narrow with category= after library_categories."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_search", library.search(agent, query, type, scope, k, category),
                          {"query": query, "type": type, "scope": scope, "category": category})


@mcp.tool()
async def library_categories(ctx: Context) -> dict[str, Any]:
    """The library's shelves: each category with how many skills, repos, docs and references it holds and a short
    guide to which skill to pick when. Read it once before choosing among many similar skills."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_categories", categories.overview(agent), {})


@mcp.tool()
async def library_get(
    item_id: Annotated[int, Field(description="Id from library_search.")],
    ctx: Context,
    offset: Annotated[int, Field(ge=0, description="Character offset; pass next_offset to read on.")] = 0,
) -> dict[str, Any]:
    """Read a library item in full (the whole SKILL.md, README or doc), 12,000 characters per page.
    A skill read this way is followed like a local one; 'files' lists the supporting files its paths refer to,
    read them with library_file."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_get", library.get(agent, item_id, offset),
                          {"item_id": item_id, "offset": offset})


@mcp.tool()
async def library_file(
    item_id: Annotated[int, Field(description="Skill id from library_search or library_get.")],
    path: Annotated[str, Field(description="A path from library_get's 'files', e.g. reference/checklist.md.")],
    ctx: Context,
    offset: Annotated[int, Field(ge=0, description="Character offset; pass next_offset to read on.")] = 0,
) -> dict[str, Any]:
    """Read one supporting file of a skill (a reference, template or script its SKILL.md points to), 12,000
    characters per page, without installing the skill."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_file", library.file(agent, item_id, path, offset),
                          {"item_id": item_id, "path": path})


@mcp.tool()
async def library_inbox(
    ctx: Context, limit: Annotated[int, Field(ge=1, le=50, description="How many to list, oldest first.")] = 10,
) -> dict[str, Any]:
    """References the user saved from their phone that nobody has reviewed yet: id, their note, link, image count and the
    start of the text read from the images. Review each with library_view then library_review."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_inbox", refs.inbox(agent, limit), {})


@mcp.tool()
async def library_view(
    item_id: Annotated[int, Field(description="Reference id from library_inbox or library_search.")],
    ctx: Context,
    image: Annotated[int, Field(ge=0, description="Which image of the reference, from 0.")] = 0,
) -> list[TextContent | Image]:
    """See a saved reference: its note, link and the text read from the image, plus the image itself."""
    agent = _agent(ctx)
    info, jpg = await _tracked(agent, "mcp", "library_view", refs.view(agent, item_id, image),
                               {"item_id": item_id})
    out: list[TextContent | Image] = [TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=1))]
    if jpg:
        out.append(Image(data=jpg, format="jpeg"))
    return out


@mcp.tool()
async def library_review(
    item_id: Annotated[int, Field(description="Reference id from library_inbox.")],
    ctx: Context,
    description: Annotated[str | None, Field(
        description="What it shows and why it is useful, in a few sentences, readable without the image. Lead "
                    "with the takeaway: the first sentences (up to 300 characters) become the summary other "
                    "sessions are shown. Then the concrete steps, tools, prompts or numbers it contains.")] = None,
    category: CategoryParam = None,
    title: Annotated[str | None, Field(description="A short title naming what it is.")] = None,
    discard: Annotated[bool, Field(description="True deletes the reference and its images (junk, accidents).")] = False,
) -> dict[str, Any]:
    """Record a review of a saved reference: title, description and category. It leaves the inbox and becomes
    searchable by what you wrote. Or discard it."""
    agent = _agent(ctx)
    return await _tracked(agent, "mcp", "library_review",
                          refs.review(agent, item_id, title, description, category, discard),
                          {"item_id": item_id, "title": title, "discard": discard})


# --- REST for scripts ----------------------------------------------------------------------


def _rest(action, handler):
    async def endpoint(request: Request) -> JSONResponse:
        agent = request.state.agent
        client = client_kind(request.headers.get("x-mc-client"), "rest")
        try:
            body = await request.json() if request.method == "POST" else dict(request.query_params)
            return JSONResponse(await _tracked(agent, client, action, handler(agent, body), body))
        except RuleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": f"Bad request: missing {exc}"}, status_code=400)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": f"Bad request: {exc}"}, status_code=400)

    return endpoint


async def _healthz(_: Request) -> JSONResponse:
    try:
        return JSONResponse({"ok": await store.healthy()})
    except Exception as exc:  # report, don't crash, so the container healthcheck sees the reason
        log.warning("health check failed: %s", exc)
        return JSONResponse({"ok": False}, status_code=503)


async def _live_page(_: Request) -> Response:
    index = LIVE_DIR / "index.html"
    if not index.exists():
        return Response("The live view is not built. Run `npm run build` in web/ or rebuild the image.",
                        status_code=503, media_type="text/plain")
    return Response(index.read_bytes(), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _activity_stream(_: Request) -> StreamingResponse:
    """Server-sent events: 'hello' (stats and recent calls), then 'call' per tool call and 'stats' each minute."""
    queue = activity.subscribe()  # before the snapshot, so no call falls in between (the page drops repeats)

    async def events():
        try:
            # Send bytes at once, before the snapshot: some clients (iOS Safari) hold a stream back until enough
            # has arrived. 'retry' sets EventSource's reconnect delay.
            yield "retry: 3000\n: " + " " * 2048 + "\n\n"
            yield _sse("hello", await activity.snapshot(tokens.agent_ids()))
            stats_due = time.monotonic() + 60
            while True:
                try:
                    yield _sse("call", await asyncio.wait_for(queue.get(), timeout=15))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                if time.monotonic() > stats_due:
                    yield _sse("stats", await activity.snapshot(tokens.agent_ids(), recent=0))
                    stats_due = time.monotonic() + 60
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                      "X-Content-Type-Options": "nosniff"})


async def _activity_access(request: Request) -> JSONResponse:
    """Lets the page tell 'not allowed' from 'offline' before it opens the stream (EventSource hides status codes)."""
    return JSONResponse({"ok": True, "viewer": request.headers.get("tailscale-user-login")})


async def _media(request: Request) -> Response:
    """An image of a saved reference: ?size=full (default), view (what agents see) or thumb; ?view=1 = size=view.
    Agents reach it with their token, the web app (/api/v1/web/media) with the user's Tailscale login."""
    agent = getattr(request.state, "agent", None) or OWNER
    size = "view" if request.query_params.get("view") == "1" else request.query_params.get("size", "full")
    try:
        path = await refs.media_file(agent, request.path_params["sha"], size)
    except RuleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if not path.exists():
        return JSONResponse({"error": "The image file is missing."}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def _bundle(request: Request) -> Response:
    """GET ?id=<skill id>: the zip of the skill's folder (mc skill-pull). POST ?hash=<tree hash> with the zip as
    the body: store a folder that library_sync's manifest named."""
    agent = request.state.agent
    client = client_kind(request.headers.get("x-mc-client"), "rest")
    q = request.query_params
    try:
        if request.method == "POST":
            data = await request.body()
            return JSONResponse(await _tracked(agent, client, "library_bundle_put",
                                               library.save_bundle(agent, q.get("hash"), data),
                                               {"hash": q.get("hash"), "bytes": len(data)}))
        title, path = await _tracked(agent, client, "library_bundle_get", library.bundle_path(agent, q["id"]),
                                     {"id": q.get("id")})
    except RuleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": f"Bad request: {exc}"}, status_code=400)
    return FileResponse(path, media_type="application/zip", headers={"X-MC-Skill": title.encode("ascii", "replace").decode()})


async def _capture(request: Request) -> JSONResponse:
    """POST {image: base64, url, note, group} from the web app or a phone Shortcut. Answers with a 'message' line the Shortcut can show."""
    try:
        body = await request.json()
        result = await _tracked(OWNER, "capture", "capture_ref",
                                refs.capture(OWNER, body.get("image"), body.get("url"), body.get("note"),
                                             body.get("scope"), body.get("group")),
                                {"url": body.get("url"), "note": body.get("note"), "image": bool(body.get("image"))})
    except RuleError as exc:
        return JSONResponse({"error": str(exc), "message": f"Not saved: {exc}"}, status_code=400)
    except ValueError:
        return JSONResponse({"error": "Send JSON.", "message": "Not saved: the Shortcut did not send JSON."},
                            status_code=400)
    verb = "Added to" if result["merged"] else "Saved as"
    words = f", read {result['ocr_chars']} characters" if result["ocr_chars"] else ""
    return JSONResponse({**result, "message": f"{verb} reference #{result['id']} ({result['images']} image(s){words})."})


async def _agent_capture(request: Request) -> JSONResponse:
    """POST {image: base64, url, note, scope, group} with an agent's bearer token: a reference the agent captured
    itself (Motion Studio screenshots a site or a frame). It waits in the user's inbox like their own saves; the live log
    gets the url and note, never the image."""
    agent = request.state.agent
    try:
        body = await request.json()
        return JSONResponse(await _tracked(agent, client_kind(request.headers.get("x-mc-client"), "rest"), "library_capture",
                                           refs.capture(agent, body.get("image"), body.get("url"), body.get("note"),
                                                        body.get("scope"), body.get("group")),
                                           {"url": body.get("url"), "note": body.get("note"), "image": bool(body.get("image"))}))
    except RuleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": f"Bad request: {exc}"}, status_code=400)


async def _web_refs(request: Request) -> JSONResponse:
    q = request.query_params
    try:
        return JSONResponse(await refs.browse(OWNER, q.get("status"), q.get("q"), q.get("limit", 60)))
    except RuleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def _web_ref(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    try:
        if request.method == "DELETE":
            return JSONResponse(await _tracked(OWNER, "web", "library_review", refs.review(OWNER, item_id, discard=True),
                                               {"item_id": item_id, "discard": True}))
        return JSONResponse(await refs.detail(OWNER, item_id))
    except RuleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def _web_graph(_: Request) -> JSONResponse:
    """Memories and their links for the Brain view, read with the user's own scopes."""
    return JSONResponse(await store.graph(OWNER), headers={"Cache-Control": "no-store"})


REST_ROUTES = [
    Route("/healthz", _healthz),
    Route("/live", _live_page),
    Route("/live/", _live_page),
    # hashed file names, so they never go stale
    Mount("/live/assets", app=StaticFiles(directory=LIVE_DIR / "assets", check_dir=False), name="live-assets"),
    Route("/", lambda _: RedirectResponse("/live")),  # a browser opening the bare host lands on the live view
    Route("/api/v1/activity/stream", _activity_stream),
    Route("/api/v1/activity/access", _activity_access),
    Route("/api/v1/profile", _rest("profile", lambda a, b: store.profile(a))),
    Route("/api/v1/recall", _rest("recall", lambda a, b: store.recall(
        a, b["query"], b.get("scope"), b.get("kinds"), b.get("k", 8), bool(b.get("expand", False)),
        b.get("prefer"))), methods=["POST"]),
    Route("/api/v1/remember", _rest("remember", lambda a, b: store.remember(
        a, b["facts"], b.get("kind", "fact"), b.get("scope", "global"), b.get("supersedes"), b.get("tags"),
        bool(b.get("verified", False)), bool(b.get("force", False)), b.get("source"), b.get("links"))),
          methods=["POST"]),
    # link existing memories (related by meaning, saved together); new ones are linked as they are saved
    Route("/api/v1/links/rebuild", _rest("links_rebuild", lambda a, b: store.rebuild_links()), methods=["POST"]),
    Route("/api/v1/branches", _rest("branches", lambda a, b: store.branch_overview(a))),
    Route("/api/v1/branches/set", _rest("branch", lambda a, b: store.set_branch(
        a, b["scope"], b["branch"], b.get("title"), b.get("description"))), methods=["POST"]),
    Route("/api/v1/branches/review", _rest("branch_review", lambda a, b: store.review_branch(
        a, b["branch"], bool(b["accept"]), b.get("title"), b.get("description"))), methods=["POST"]),
    Route("/api/v1/forget", _rest("forget", lambda a, b: store.forget(a, int(b["memory_id"]), b["reason"])), methods=["POST"]),
    Route("/api/v1/timeline", _rest("timeline", lambda a, b: store.timeline(
        a, b.get("scope"), b.get("since"), int(b.get("limit", 20))))),
    Route("/api/v1/library/search", _rest("library_search", lambda a, b: library.search(
        a, b["query"], b.get("type"), b.get("scope"), b.get("k", 5), b.get("category"))), methods=["POST"]),
    Route("/api/v1/library/categories", _rest("library_categories", lambda a, b: categories.overview(a))),
    # after a sync, or after editing app/library_categories.json: embed changed categories and refile everything
    Route("/api/v1/library/categories/apply", _rest("library_categorize", lambda a, b: categories.apply()),
          methods=["POST"]),
    Route("/api/v1/library/inbox", _rest("library_inbox", lambda a, b: refs.inbox(a, b.get("limit", 10)))),
    Route("/api/v1/library/review", _rest("library_review", lambda a, b: refs.review(
        a, b["id"], b.get("title"), b.get("description"), b.get("category"), b.get("discard", False))),
          methods=["POST"]),
    Route("/api/v1/library/media/{sha}", _media),
    Route("/api/v1/library/capture", _agent_capture, methods=["POST"]),
    # a reference with its image shas, for a client that downloads them from /library/media (Motion Studio's shelf)
    Route("/api/v1/library/ref", _rest("library_ref", lambda a, b: refs.detail(a, b["id"]))),
    # The user's own routes, authenticated by their Tailscale login instead of a token: saving from the web app's Library
    # tab or a phone Shortcut, and browsing what was saved
    Route("/api/v1/capture/ref", _capture, methods=["POST"]),
    Route("/api/v1/web/refs", _web_refs),
    Route("/api/v1/web/refs/{item_id:int}", _web_ref, methods=["GET", "DELETE"]),
    Route("/api/v1/web/media/{sha}", _media),
    Route("/api/v1/web/graph", _web_graph),
    Route("/api/v1/library/get", _rest("library_get", lambda a, b: library.get(a, b["id"], b.get("offset", 0)))),
    Route("/api/v1/library/file", _rest("library_file", lambda a, b: library.file(
        a, b["id"], b["path"], b.get("offset", 0)))),
    Route("/api/v1/library/bundle", _bundle, methods=["GET", "POST"]),
    # for clients/library_sync.py: a manifest of what one machine has, then the items the server lacks
    Route("/api/v1/library/sync", _rest("library_sync", lambda a, b: library.sync(
        a, b["type"], b["origin"], b["manifest"])), methods=["POST"]),
    Route("/api/v1/library/upsert", _rest("library_upsert", lambda a, b: library.upsert(
        a, b["type"], b["items"])), methods=["POST"]),
]


def _live_viewer(headers: dict[str, str]) -> str | None:
    """The Tailscale login of a /live viewer, if allowed. Tailscale serve sets Tailscale-User-Login on requests
    it proxies; requiring the ts.net Host as well keeps a LAN route (Caddy) from passing a forged header."""
    login = headers.get("tailscale-user-login", "")
    host = headers.get("host", "").rsplit(":", 1)[0]
    return login if login in settings.live_viewers and host.endswith(".ts.net") else None


class BearerAuth:
    """Rejects any request except /healthz and the /live page that lacks a known bearer token; stores the agent for REST
    handlers. The activity feed is for the user's Tailscale login instead, since a browser holds no agent token."""

    def __init__(self, app: ASGIApp, book: TokenBook) -> None:
        self.app, self.book = app, book

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path not in ("/", "/healthz", "/live") and not path.startswith("/live/"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if path.startswith(("/api/v1/activity/", "/api/v1/capture/", "/api/v1/web/")):
                if _live_viewer(headers) is None:
                    log.warning("tailscale-only route refused: path=%s login=%r host=%r", path,
                                headers.get("tailscale-user-login"), headers.get("host"))
                    await JSONResponse({"error": "This opens only over Tailscale, from the user's account."},
                                       status_code=403)(scope, receive, send)
                    return
                await self.app(scope, receive, send)
                return
            agent = self.book.agent_for(headers.get("authorization"))
            if agent is None:
                await JSONResponse({"error": "Missing or unknown bearer token."}, status_code=401)(scope, receive, send)
                return
            scope.setdefault("state", {})["agent"] = agent
        await self.app(scope, receive, send)


app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    # Every request must carry a bearer token, which a DNS-rebinding page cannot add,
    # and the Host differs per route in (Caddy, Tailscale serve, localhost).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.router.routes.extend(REST_ROUTES)
app.add_middleware(BearerAuth, book=tokens)
