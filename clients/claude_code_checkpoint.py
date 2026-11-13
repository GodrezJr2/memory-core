#!/usr/bin/env python3
"""Claude Code Stop hook: a save checkpoint for shared memory (memory-core).

Reading memory is automatic (memory_context.py). Writing needs the model, since no LLM runs on the server.
So when a session has done real work (edits, commands) since its last save, this hook blocks the stop once
and asks the agent to save the durable facts and keep one current episode for the session. It never blocks
twice in a row, waits for more work before asking again, and stays silent when memory-core is down.

It is also the fallback writer when the 'memory' MCP server is not connected:
  python memory_checkpoint.py save [--scope S] [--kind K] [--supersedes ID] [--verified] "fact" ["fact" ...]

URL and token come from the 'memory' entry in ~/.claude.json. Stdlib only; runs on Windows and Linux.
"""

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

# Edits weigh more than commands: four edits, or eight commands, or a mix, make a session worth saving.
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
COMMAND_TOOLS = {"Bash", "PowerShell"}
THRESHOLD = 8
# After the first checkpoint of a session, ask again only after much more work: each ask is a full extra turn
# that Claude Code shows as a "Stop hook blocking error" (22 Oct 2026: 162 asks over 58 sessions).
REPEAT_THRESHOLD = 40
SAVE_MARKERS = ("/api/v1/remember", "mc remember", "mc.py remember", "memory_checkpoint.py save")
STATE = pathlib.Path.home() / ".claude" / "hooks" / "state" / "memory_checkpoint.json"

REASON = """Memory checkpoint (automatic, from the memory-core Stop hook): this session has done real work since its \
last save to shared memory. Before you finish:
1. Save the durable facts this work established or changed (ports, paths, configs, decisions, root causes, how-tos) \
with memory_remember (MCP server 'memory'; load it with ToolSearch if it is deferred). One standalone sentence per \
fact, scope 'project:<name>' ('global' for facts about the user), verified=true only if you checked it live. If a \
fact replaces an older memory, pass supersedes=<id>. Skip what memory already holds, secrets, and chit-chat.
2. Keep one episode for this session: kind='episode', at most 3 lines (what changed, what is left, where). If you \
already saved one this session, replace it with supersedes=<its id>.
If the memory tools are unavailable, run: {cli}
If nothing durable happened, save nothing. Then end with one short line for the user, such as \
"Saved to memory: #123, #124", without repeating your previous answer."""


def _server() -> tuple[str, str]:
    cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["memory"]
    return entry["url"].rsplit("/mcp", 1)[0], entry["headers"]["Authorization"]


def _tool_uses(transcript: pathlib.Path) -> list[tuple[str, str]]:
    """(tool name, its input as text) for each tool call of the main agent, in order."""
    uses: list[tuple[str, str]] = []
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if entry.get("type") != "assistant" or entry.get("isSidechain") or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    uses.append((str(block.get("name", "")), json.dumps(block.get("input", {}))[:4000]))
    return uses


def _is_save(name: str, text: str) -> bool:
    return name.endswith("memory_remember") or (name in COMMAND_TOOLS and any(m in text for m in SAVE_MARKERS))


def work_since_save(uses: list[tuple[str, str]], since: int) -> int:
    """Weighted work after the last memory save and after tool call number `since` (the last checkpoint)."""
    start = since
    for i, (name, text) in enumerate(uses):
        if _is_save(name, text):
            start = max(start, i + 1)
    return sum(2 if name in EDIT_TOOLS else 1 if name in COMMAND_TOOLS else 0 for name, _ in uses[start:])


def _load_state() -> dict[str, int]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, int]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(dict(list(state.items())[-50:])), encoding="utf-8")


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _cli() -> str:
    exe = pathlib.Path(sys.executable).as_posix()
    me = pathlib.Path(__file__).resolve().as_posix()
    return f'"{exe}" "{me}" save --scope project:<name> [--kind episode] [--supersedes ID] [--verified] "fact" ...'


def checkpoint() -> None:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        if event.get("stop_hook_active"):  # already continuing because of a Stop hook: let it finish
            return
        transcript = pathlib.Path(event.get("transcript_path") or "")
        if not transcript.is_file():
            return
        uses = _tool_uses(transcript)
        state = _load_state()
        session = str(event.get("session_id", ""))
        if work_since_save(uses, state.get(session, 0)) < (REPEAT_THRESHOLD if session in state else THRESHOLD):
            return
        base, _ = _server()
        if not _healthy(base):
            return
        state[session] = len(uses)  # ask again only after more work than this
        _save_state(state)
    except Exception:
        return  # a checkpoint must never break a session
    print(json.dumps({"decision": "block", "reason": REASON.format(cli=_cli())}))


def save(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="memory_checkpoint.py save", description="Save facts to memory-core.")
    ap.add_argument("facts", nargs="+")
    ap.add_argument("--scope", default="global")
    ap.add_argument("--kind", default="fact")
    ap.add_argument("--supersedes", type=int)
    ap.add_argument("--verified", action="store_true")
    a = ap.parse_args(argv)
    base, auth = _server()
    body = {"facts": a.facts, "scope": a.scope, "kind": a.kind, "supersedes": a.supersedes, "verified": a.verified}
    req = urllib.request.Request(
        base + "/api/v1/remember",
        data=json.dumps(body).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json", "X-MC-Client": "cli"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            r = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"memory-core refused: {exc.read().decode(errors='replace')}")
    for s in r.get("saved", []):
        print(f"saved #{s['id']}: {s['text']}")
        for x in s.get("related", []):
            print(f"  related #{x['id']} (similarity {x['similarity']}): {x['text']}  <- retire it if outdated")
    for s in r.get("skipped_duplicates", []):
        print(f"skipped, already stored as #{s['duplicate_of']}: {s['text']}")
    for h in r.get("held_near_duplicates", []):
        print(f"held (near-duplicate): {h['text']}")
        for x in h["similar"]:
            print(f"  #{x['id']} (similarity {x['similarity']}): {x['text']}")
    if r.get("retired"):
        print(f"retired #{r['retired']}")
    if r.get("redacted"):
        print(f"redacted before saving: {', '.join(r['redacted'])}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "save":
        save(sys.argv[2:])
    else:
        checkpoint()
