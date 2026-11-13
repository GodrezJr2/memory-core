#!/usr/bin/env python3
"""Claude Code hook: bring shared memory (memory-core) into context, whatever model is running.

SessionStart      -> the user's profile, projects with memories, latest session episodes; removes skills pulled for
                     sessions that ended without saying so
UserPromptSubmit  -> stored memories that share a specific word with the prompt and are close in meaning,
                     each injected at most once per session (what is already in context is not repeated),
                     plus at most one line naming library items (skills, starred repos, project docs) that fit;
                     a fitting skill that is not installed here is pulled into ~/.claude/skills for this session
                     (Claude Code picks up new skill folders within seconds)
SessionEnd        -> removes the skills pulled for this session

URL and token come from the 'memory' MCP server entry in ~/.claude.json, so rotating the token
means changing one place. Any failure prints nothing: a missing memory must never block a prompt.
Stdlib only, so the same file runs on the laptop (Windows) and on homeserver. Pulling skills needs mc.py
(clients/mc.py) next to this file; without it the hook only names the skill.
"""

import json
import math
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import mc
except ImportError:
    mc = None

# Similarity alone cannot tell a real question from vague chat: "why is this not working" scored 0.54,
# as high as "restart the bank categorizer" (0.59). Requiring a keyword match as well (filler words are
# stopwords on the server) gave 0 false injections in 11 noise prompts and 0 misses in 10 real ones
# (30 Sep 2026, Qwen3-Embedding-0.6B; the prompts were also the tuning set).
MIN_SIMILARITY = 0.35
MIN_WORDS = 3
MAX_RESULTS = 4
TIMEOUT_S = 4
# Replaying 39 real prompts (2 Oct 2026): hits more than 0.20 below the best one were 5 of 6 times noise
# (a Minecraft fact on an SD card question, the NVMe on a USB reader one).
MAX_GAP = 0.20
# A shared word is evidence only if it is rare among memories. Hand-labelling what the hook injected for 131 real
# prompts (4 Oct 2026) found half of 415 lines were noise, matched on words like "2" (from "[Image #2]", in 36 of
# 155 memories), "laptop" (30), "server" (14) or "config" (5). Keeping a hit only when its shared words add up to
# an IDF (sum of ln(corpus/df)) of 3.8, or its similarity is at least 0.54, kept 87% of the relevant lines and 20%
# of the noise: 1.7 lines per prompt instead of 3.2. Tuned on those same prompts.
MIN_IDF = 3.8
SURE_SIMILARITY = 0.54
# Below a similarity of 0.45 one rare word is often a coincidence ("short" from "answer in short lines" matched a
# memory about short cards), so weak matches need more evidence: +1 IDF per 0.10 below 0.45. On the same 131
# labelled prompts: noise lines 42 -> 37, relevant lines unchanged (182).
IDF_SLOPE = 10
SLOPE_FROM = 0.45
# The bars above were tuned on 155 memories. ln(corpus/df) grows with the corpus, and so does the similarity of the
# nearest memories: at 2,626 memories (13 Nov 2026) 64% of injected lines passed on SURE_SIMILARITY alone, with only
# words like "server" (in 175 memories) or "2026" (1,338) shared, and 39% of lines were noise. So the IDF sum is
# scaled back to the tuned corpus size, and close meaning counts only with a shared word that is itself fairly rare.
TUNED_CORPUS = 155
SURE_MIN_IDF = 1.9
# Episodes of the folder's project shown at SessionStart, newest first: a new session starts from where the last
# few stopped, not only the last one.
HANDOFF_EPISODES = 3
# Filler the server does not treat as stopwords (it still searches by them) but that says nothing about relevance.
NOISE_WORDS = frozenset("""its more over use go first add open new set main start only real plan best give need after
    under max high total look file per""".split())
# Messages Claude Code itself sends as a "prompt" (background task done, slash command output).
NOT_A_PROMPT = ("/", "<task-notification>", "<command-", "<local-command-", "<agent-message")
# Not the user's words: image placeholders and pasted blocks (one pasted slide script matched 4 memories).
NOT_WORDS = re.compile(r"\[Image #\d+\]|<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
# Library hint: titles only, the agent opens one with library_get if it helps. Skills are many and generic, so chat
# scores high too (3 Oct 2026, 248 items): at 0.70, 6 of 10 task prompts ("extract text from this pdf" -> pdf-extract
# 0.84) got a hint and 2 of 37 replayed real prompts did, both half-relevant; at 0.65 it was 8 of 37.
LIB_MIN_SIMILARITY = 0.70
# A reference the user saved and had reviewed is their own pick, and there are few: 0.60 lets a real question that wraps the
# topic in instructions still reach it (0.61 for "routebench pick haiku/sonnet/opus per request ... answer in 6
# lines"), while only 1 of 131 past prompts would have triggered it, a related one.
LIB_REF_MIN_SIMILARITY = 0.60
LIB_MAX = 2
LIB_SUMMARY_CHARS = 70
# The best skill hit is pulled, one per prompt and a few per session: each pulled skill adds its description to the
# skill list for the rest of the session, and goes when it ends, so a wrong pull is cheap. The right skill scored
# 0.65-0.68 on 4 task prompts on 8 Oct 2026 (motion-design 0.648, reverse-engineer-anything 0.676), below the hint
# bar. The download shares the hook's 8 s budget.
PULL_MIN_SIMILARITY = 0.64
PULL_PER_SESSION = 3
# Never pulled: Codex-only (skill-installer writes to $CODEX_HOME) or already bundled under another name (anthropic-skills:skill-creator);
# a prompt about skills themselves matched them (4 Nov 2026) and filled the listing for nothing.
PULL_SKIP = frozenset({"skill-installer", "skill-creator"})
PULL_TIMEOUT_S = 3
# Design, motion, 3D and brand skills go through the visual router skill where it is installed (18 Oct 2026): a
# per-prompt pull of one of them (the Stitch-only remotion skill, on a "make a video ad" prompt) sat beside the router
# with its own, conflicting rules. Skills synced into the router's vendor folder count as covered too.
VISUAL_CATEGORIES = {"web-design", "motion", "3d-web", "visual-assets"}
VISUAL_ROUTER = pathlib.Path.home() / ".claude" / "skills" / "visual" / "SKILL.md"
# Ids already in this session's context, per session, so a memory is injected once instead of every turn
# (one 2 Oct session got 69 memory lines of which only 28 were different).
STATE = pathlib.Path.home() / ".claude" / "hooks" / "state" / "memory_context.json"
KEEP_SESSIONS = 50
GIT_STATUS_LINES = 8


def _server() -> tuple[str, str]:
    cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["memory"]
    return entry["url"].rsplit("/mcp", 1)[0], entry["headers"]["Authorization"]


def _call(base: str, auth: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json", "X-MC-Client": "hook"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def _load_state() -> dict[str, list]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, list]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(dict(list(state.items())[-KEEP_SESSIONS:])), encoding="utf-8")


def _remember_shown(session: str, ids: list, fresh: bool = False) -> None:
    """Record ids now in the session's context (memory ids, and library ids as 'L<id>'). A SessionStart (new, resumed, cleared or compacted
    context) starts the list over, because whatever was injected before may no longer be there."""
    if not session:
        return
    state = _load_state()
    shown = state.pop(session, [])  # pop, so this session moves to the newest end and survives pruning
    shown = [] if fresh else shown
    state[session] = shown + [i for i in ids if i not in shown]
    _save_state(state)


def _projects_line(projects: list[dict]) -> str:
    """Projects grouped by branch (newest activity first within each), or flat from a server without domains."""
    def name(x: dict) -> str:
        return f"{x['scope'].split(':', 1)[1]} ({x['memories']})"
    if not any(x.get("domain") for x in projects):
        return "Projects with memories: " + ", ".join(name(x) for x in projects)
    branches: dict[str, list[str]] = {}
    for x in projects:
        branches.setdefault(x.get("domain") or "other", []).append(name(x))
    return ("Projects with memories, by branch (memory_recall scope='domain:<branch>' searches one): "
            + "; ".join(f"{b}: {', '.join(names)}" for b, names in branches.items()))


def _proposals_line(proposals: list[dict]) -> str:
    """New branches agents named, waiting for the user: mention them; record their decision only when they give one."""
    return ("New branches waiting for the user (tell them; memory_branch_review records their decision): "
            + "; ".join(f"{x['name']} ({x['title']}): {', '.join(x['projects'])}" for x in proposals))


def _session_context(base: str, auth: str, session: str, cwd: str | None = None, source: str = "") -> str:
    p = _call(base, auth, "/api/v1/profile")
    lines = [
        "Shared memory (MCP server 'memory', memory-core on homeserver). This is the memory_profile result, "
        "so there is no need to call it. Search with memory_recall; save durable facts with memory_remember.",
        "Profile:",
        *(f"- {x['text']}" for x in p["profile"]),
    ]
    if p["projects"]:
        lines.append(_projects_line(p["projects"]))
    if p.get("branch_proposals"):
        lines.append(_proposals_line(p["branch_proposals"]))
    if p["recent_episodes"]:
        lines.append("Recent sessions:")
        lines += [f"- [{x['date']}, {x['by']}] {x['text']}" for x in p["recent_episodes"]]
    shown = [x["id"] for x in p["recent_episodes"]]
    episodes, git = _handoff(base, auth, cwd, shown)
    if episodes or git:
        lines.append(f"Handoff for this folder ({cwd}):")
        for n, episode in enumerate(episodes):
            label = "Last episode" if n == 0 else "Before that,"
            lines.append(f"- {label} of {episode['scope']} [{episode['date']}]: {episode['text']}")
            shown.append(episode["id"])
        lines += git
    if source == "compact":
        lines.append("The context was just auto-compacted in the middle of work. Carry on with the task from the summary "
                     "without waiting for the user; re-read only the files you need for the next step. If the summary lists items under "
                     "'Unsaved to memory', save them first with memory_remember (one fact each), then continue.")
    _remember_shown(session, shown, fresh=True)
    return "\n".join(lines)


def _git(cwd: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2,
                         encoding="utf-8", errors="replace")
    return out.stdout.strip() if out.returncode == 0 else ""


def _handoff(base: str, auth: str, cwd: str | None, shown: list) -> tuple[list[dict], list[str]]:
    """Where work in this folder stopped: the project's newest episodes (those not already listed) and the repo
    state, which the hook reads itself so a cleared or compacted session can pick up without re-exploring."""
    if not cwd:
        return [], []
    episodes: list[dict] = []
    scope = cwd_scope(cwd)
    if scope:
        try:
            # the timeline also returns global and the agent's own episodes; only the project's belong here
            query = urllib.parse.urlencode({"scope": scope, "since": "2026-01-01", "limit": HANDOFF_EPISODES * 4})
            found = _call(base, auth, "/api/v1/timeline?" + query).get("episodes", [])
            episodes = [x for x in found if x["id"] not in shown and x["scope"] == scope][:HANDOFF_EPISODES]
        except Exception:
            pass
    git: list[str] = []
    try:
        if _git(cwd, "rev-parse", "--is-inside-work-tree") == "true":
            git.append(f"- Git: {_git(cwd, 'branch', '--show-current') or 'detached'}, last commit "
                       f"{_git(cwd, 'log', '-1', '--format=%h %s (%cr)')}")
            status = _git(cwd, "status", "--short").splitlines()
            if status:
                more = f" (+{len(status) - GIT_STATUS_LINES} more)" if len(status) > GIT_STATUS_LINES else ""
                git.append(f"- Uncommitted: {', '.join(s.strip() for s in status[:GIT_STATUS_LINES])}{more}")
    except (OSError, subprocess.SubprocessError):
        pass
    return episodes, git


def is_evidence(word: str) -> bool:
    return len(word) > 1 and not (word.isdigit() and len(word) < 3) and word not in NOISE_WORDS


def relevant(hit: dict, corpus: int) -> bool:
    """Keep a keyword-matched hit if its shared words are rare enough, or it is close in meaning anyway.
    A server without word statistics (no 'keywords') gets the older rule: any keyword match."""
    if "keywords" not in hit or not corpus:
        return True
    words = {w: df for w, df in hit["keywords"].items() if is_evidence(w) and df > 0}
    if not words:
        return False
    scale = math.log(TUNED_CORPUS) / math.log(corpus) if corpus > TUNED_CORPUS else 1.0
    idf = scale * sum(math.log(corpus / df) for df in words.values())
    rarest = scale * max(math.log(corpus / df) for df in words.values())
    needed = MIN_IDF + IDF_SLOPE * max(0.0, SLOPE_FROM - hit["similarity"])
    return idf >= needed or (hit["similarity"] >= SURE_SIMILARITY and rarest >= SURE_MIN_IDF)


def cwd_scope(cwd: str | None) -> str | None:
    """The project scope a working directory most likely means: D:/Project/memory-core -> project:memory-core.
    A folder that names no project (D:/Project) gives a scope with no memories, which boosts nothing."""
    if not cwd:
        return None
    name = re.sub(r"[^a-z0-9._-]+", "-", pathlib.PurePath(cwd.replace("\\", "/")).name.lower()).strip("-")
    return f"project:{name}" if name else None


def _memory_lines(base: str, auth: str, prompt: str, shown: set, cwd: str | None = None) -> list[dict]:
    reply = _call(base, auth, "/api/v1/recall",
                  {"query": prompt[:1000], "k": MAX_RESULTS + 2, "prefer": cwd_scope(cwd)})
    hits = [r for r in reply["results"] if r["matched"] != "meaning" and r["similarity"] >= MIN_SIMILARITY]
    best = max((r["similarity"] for r in hits), default=0)
    # Core memories are the profile, already loaded at SessionStart.
    return [r for r in hits if r["similarity"] >= best - MAX_GAP and r["kind"] != "core"
            and relevant(r, reply.get("corpus", 0)) and r["id"] not in shown][:MAX_RESULTS]


def _library_hits(base: str, auth: str, prompt: str, shown: set) -> list[dict]:
    """Library items worth naming; a skill hit below the hint bar but above PULL_MIN_SIMILARITY comes back marked
    'pull_only', for _pull_skills."""
    results = _call(base, auth, "/api/v1/library/search", {"query": prompt[:1000], "k": 4})["results"]
    def bar(r: dict) -> float:
        if r["type"] == "ref" and r.get("status") == "reviewed":
            return LIB_REF_MIN_SIMILARITY
        return PULL_MIN_SIMILARITY if r["type"] == "skill" else LIB_MIN_SIMILARITY
    hits = [r for r in results if r["matched"] == "both" and r["similarity"] >= bar(r)
            and f"L{r['id']}" not in shown][:LIB_MAX]
    for r in hits:
        if r["similarity"] < LIB_MIN_SIMILARITY and r["type"] == "skill":
            r["pull_only"] = True
    return hits


def _visual_routed(lib: list[dict]) -> list[dict]:
    """Skill hits the visual router covers, when it is installed here; they are named, not pulled."""
    if not VISUAL_ROUTER.exists():
        return []
    return [r for r in lib if r["type"] == "skill" and (
        r.get("category") in VISUAL_CATEGORIES
        or any("/skills/visual/" in w.replace("\\", "/") for w in r.get("where") or []))]


def _installed_here(cwd: str | None) -> set[str]:
    """Skill names Claude Code already has: user skills, the project's, and enabled plugins'."""
    home = pathlib.Path.home() / ".claude"
    roots = [home / "skills"] + ([pathlib.Path(cwd) / ".claude" / "skills"] if cwd else [])
    try:
        installed = json.loads((home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        enabled = json.loads((home / "settings.json").read_text(encoding="utf-8")).get("enabledPlugins", {})
        roots += [pathlib.Path(i["installPath"]) for name, installs in (installed.get("plugins") or {}).items()
                  if enabled.get(name) for i in installs if i.get("installPath")]
    except (OSError, ValueError):
        pass
    return mc.local_skill_names(roots)


def _pull_skills(base: str, auth: str, lib: list[dict], session: str, cwd: str | None) -> list[dict]:
    """Pull the best skill hit if Claude Code does not have it. Returns pull results (status pulled/updated/already)."""
    if mc is None or not session:
        return []
    skills = [r for r in lib if r["type"] == "skill" and r["title"].lower() not in PULL_SKIP]
    if not skills:
        return []
    mine = [m for _, m in mc.pulled() if session in m.get("sessions", [])]
    if len(mine) >= PULL_PER_SESSION:
        return []
    installed, pulled_titles = _installed_here(cwd), {m.get("title") for _, m in mc.pulled()}
    for r in skills[:1]:
        if r["title"].lower() in installed and r["title"] not in pulled_titles:
            continue  # installed some other way: the Skill tool already lists it
        client = mc.Client(base, auth, client="hook")
        result = mc.pull_skill(client, r["title"], session=session, item=(r["id"], r["title"]),
                               timeout=PULL_TIMEOUT_S)
        if result["status"] != "local":
            return [result]
    return []


def _prompt_context(base: str, auth: str, prompt: str, session: str, cwd: str | None = None) -> str | None:
    if prompt.strip().startswith(NOT_A_PROMPT):
        return None
    prompt = " ".join(NOT_WORDS.sub(" ", prompt).split())
    if len(prompt.split()) < MIN_WORDS:
        return None
    shown = set(_load_state().get(session, []))
    with ThreadPoolExecutor(2) as pool:  # both wait on the server; together they take as long as the slower
        memories = pool.submit(_memory_lines, base, auth, prompt, shown, cwd)
        library = pool.submit(_library_hits, base, auth, prompt, shown)
        hits = memories.result()
        try:
            lib = library.result()
        except Exception:  # an older server without the library must not cost the memories
            lib = []
    routed = _visual_routed(lib)
    lib = [r for r in lib if r not in routed]
    if not hits and not lib and not routed:
        return None
    try:
        pulls = _pull_skills(base, auth, lib, session, cwd)
    except Exception:  # no skill rather than no memories
        pulls = []
    pulled_ids = {p["id"] for p in pulls}
    lib = [r for r in lib if r["id"] not in pulled_ids and not r.get("pull_only")]  # the rest are hints
    if not hits and not lib and not pulls and not routed:
        return None
    _remember_shown(session, [r["id"] for r in hits]
                    + [f"L{i}" for i in pulled_ids | {r["id"] for r in lib} | {r["id"] for r in routed}])
    parts = []
    if routed:
        parts.append("This reads as visual work: load the visual skill first; it routes to the right guide "
                     f"(library match: {', '.join(r['title'] for r in routed)}).")
    for p in pulls:
        parts.append(f"Skill {p['title']} (library #{p['id']}) fits this prompt and was installed from the library "
                     f"into {p['path']} for this session; it joins the Skill tool's list within seconds. Use it if "
                     f"it fits. If the Skill tool does not know it yet, read {p['path']}/SKILL.md and follow it.")
    if hits:
        parts.append("Shared memories that may be relevant (cite the id if you use one; verified and newer win):\n"
                     + "\n".join(f"- #{r['id']} [{r['scope']}, {r['date']}{', verified' if r['verified'] else ''}] "
                                 f"{r['text']}" for r in hits))
    # A reference the user saved and had reviewed is their own pick: its review goes in, not just a title, because a
    # title-only hint was skipped by a fresh session on 4 Oct 2026 even when it matched the question exactly.
    refs = [r for r in lib if r["type"] == "ref" and r.get("status") == "reviewed"]
    for r in refs:
        parts.append(f"The user saved a reference about this (library #{r['id']}): {r['title']}. {r.get('summary', '')} "
                     f"Bring it up if it helps; library_get({r['id']}) has the full review, library_view({r['id']}) "
                     "the image.")
    others = [r for r in lib if r not in refs]
    if others:
        def short(text: str) -> str:
            return text if len(text) <= LIB_SUMMARY_CHARS else text[: LIB_SUMMARY_CHARS - 1] + "…"
        parts.append("Library, if useful (library_get <id>): " + "; ".join(
            f"{r['type']} {r['title']} #{r['id']}" + (f" ({short(r['summary'])})" if r.get("summary") else "")
            for r in others))
    return "\n".join(parts)


def main() -> None:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        name = event.get("hook_event_name")
        session = str(event.get("session_id", ""))
        if name == "SessionEnd":
            if mc is not None and session:
                mc.release_session(session)
            return
        if name == "SessionStart" and mc is not None:
            try:
                mc.sweep_stale()
            except Exception:
                pass
        base, auth = _server()
        if name == "SessionStart":
            context = _session_context(base, auth, session, event.get("cwd"), str(event.get("source", "")))
        elif name == "UserPromptSubmit":
            context = _prompt_context(base, auth, event.get("prompt", ""), session, event.get("cwd"))
        else:
            context = None
    except Exception:
        return
    if context:
        # ensure_ascii keeps the output safe on a Windows console code page
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}))


if __name__ == "__main__":
    main()
