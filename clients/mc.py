#!/usr/bin/env python3
"""mc: command-line client for memory-core, for agents without MCP (Aider) and for scripts.

  mc profile [--out FILE]                       profile, projects, recent sessions (as Markdown)
  mc recall "question" [--scope S] [-k N]        search memory (S: project:<name> or domain:<branch>)
  mc remember "fact" ["fact" ...] [--scope S] [--kind K] [--supersedes ID] [--link ID ...] [--verified] [--force]
  mc forget ID "reason"                          retire a memory
  mc timeline [--scope S] [--since YYYY-MM-DD]   session episodes, newest first
  mc lib "task or question" [--type T] [-k N]    search the library (skills, starred repos, project docs)
  mc lib-get ID [--offset N]                     read a library item in full
  mc lib-file ID PATH [--offset N]               read one supporting file of a skill (a path library_get lists)
  mc skill-pull NAME|ID [--to DIR] [--session S] install a skill's whole folder from the library
  mc skill-drop NAME [--from DIR]                remove a skill that skill-pull installed
  mc skill-list [--from DIR]                     skills that skill-pull installed there

Skills: --to/--from default to ~/.claude/skills; Codex uses ~/.codex/skills, opencode ~/.config/opencode/skills.
A pulled folder carries .mc-pulled.json. Pulled by hand it stays until skill-drop; pulled with --session (the
Claude Code hook) it goes when that session ends, or after a day if the end was missed. A folder without that
file was installed some other way, and mc leaves it alone unless --force.

Config: MC_URL and MC_TOKEN, or ~/.memory-core.json {"url": ..., "token": ...}, or the 'memory' MCP entry in
~/.claude.json. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HOME = pathlib.Path.home()
CONFIG = HOME / ".memory-core.json"
SKILLS_DIR = HOME / ".claude" / "skills"
MARKER = ".mc-pulled.json"
# A session that crashed never sends SessionEnd; its pulls go at the next session start after this long.
STALE_S = 24 * 3600
# Aider runs with yes-always, which auto-declines shell commands the model suggests (Aider requires an
# explicit yes for those), so the model asks the user to type the /run command instead of suggesting it.
AIDER_HINT = """\
How to use shared memory from here: you cannot run shell commands yourself in this setup, so ask the user
to type the command, and its output will be added to the chat.
- To look something up: /run mc recall "<question>" --scope project:<name>
- To save a durable fact: /run mc remember "<one standalone sentence>" --scope project:<name>
- At the end of a work session: /run mc remember "<date>: what changed, what is left, where" --kind episode --scope project:<name>
"""


class McError(Exception):
    pass


class Client:
    def __init__(self, base: str, auth: str, client: str = "cli") -> None:
        self.base, self.auth, self.client = base.rstrip("/"), auth, client

    @classmethod
    def from_config(cls, client: str = "cli") -> "Client":
        url, token = os.environ.get("MC_URL"), os.environ.get("MC_TOKEN")
        if not (url and token) and CONFIG.exists():
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            url, token = url or cfg.get("url"), token or cfg.get("token")
        if url and token:
            return cls(url, f"Bearer {token}", client)
        try:
            entry = json.loads((HOME / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["memory"]
            return cls(entry["url"].rsplit("/mcp", 1)[0], entry["headers"]["Authorization"], client)
        except (OSError, ValueError, KeyError):
            raise McError(f"no server configured. Set MC_URL and MC_TOKEN, or create {CONFIG}.") from None

    def _open(self, path: str, body: dict | None, query: dict | None, timeout: float):
        if query:
            url_q = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in query.items() if v is not None)
            path = f"{path}?{url_q}" if url_q else path
        req = urllib.request.Request(
            f"{self.base}/api/v1{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": self.auth, "Content-Type": "application/json", "X-MC-Client": self.client},
            method="GET" if body is None else "POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise McError(detail) from None
        except urllib.error.URLError as exc:
            raise McError(f"cannot reach memory-core at {self.base} ({exc.reason}). Is Tailscale up?") from None

    def json(self, path: str, body: dict | None = None, query: dict | None = None, timeout: float = 20) -> dict:
        with self._open(path, body, query, timeout) as resp:
            return json.load(resp)

    def raw(self, path: str, query: dict | None = None, timeout: float = 60) -> bytes:
        with self._open(path, None, query, timeout) as resp:
            return resp.read()


def _call(path: str, body: dict | None = None, query: dict | None = None) -> dict:
    try:
        return Client.from_config().json(path, body, query)
    except McError as exc:
        sys.exit(f"mc: {exc}")


# --- skills: install from the library, and clean up --------------------------------------------

def tree_hash(files: dict[str, bytes]) -> str:
    """A skill folder's content address, as app/bundles.py and library_sync compute it."""
    lines = "".join(f"{name}\0{hashlib.sha256(data).hexdigest()}\n" for name, data in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def folder_name(title: str) -> str:
    """A skill title as a folder name on any OS ('ckm:banner-design' -> 'ckm-banner-design')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-.")[:80] or "skill"


def _safe(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}
    bad = [n for n in files if not _safe(n)]
    if bad or "SKILL.md" not in files:
        raise McError(f"the server sent a bad skill folder ({bad[0] if bad else 'no SKILL.md'}).")
    return files


def _rmtree(path: pathlib.Path) -> None:
    def writable(func, p, _exc):  # Windows refuses to delete read-only files (git objects, some assets)
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=writable)


def read_marker(folder: pathlib.Path) -> dict | None:
    try:
        return json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_marker(folder: pathlib.Path, marker: dict) -> None:
    (folder / MARKER).write_text(json.dumps(marker, indent=1), encoding="utf-8")


def resolve_skill(client: Client, ref: str) -> tuple[int, str]:
    """A skill's library id and title, from its id or its exact name."""
    if ref.isdigit():
        r = client.json("/library/get", query={"id": ref})
        if r["type"] != "skill":
            raise McError(f"library item {ref} is a {r['type']}, not a skill.")
        return r["id"], r["title"]
    res = client.json("/library/search", {"query": ref, "type": "skill", "k": 10})["results"]
    exact = [r for r in res if r["title"].lower() == ref.lower()]
    if exact:
        return exact[0]["id"], exact[0]["title"]
    near = ", ".join(f"{r['title']} (#{r['id']})" for r in res[:5]) or "none"
    raise McError(f"no skill named {ref!r} in the library. Closest: {near}.")


def pull_skill(client: Client, ref: str, dest: pathlib.Path = SKILLS_DIR, session: str | None = None,
               item: tuple[int, str] | None = None, force: bool = False, timeout: float = 60) -> dict:
    """Install a skill's folder into dest/<name>. Returns {status, title, id, path}; status is 'pulled',
    'updated', 'already' (same content there) or 'local' (a folder mc did not install; left alone)."""
    item_id, title = item or resolve_skill(client, ref)
    target = dest / folder_name(title)
    marker = read_marker(target)
    out = {"title": title, "id": item_id, "path": str(target)}
    if target.exists() and marker is None and not force:
        return {**out, "status": "local"}
    files = _unzip(client.raw("/library/bundle", {"id": item_id}, timeout=timeout))
    bundle, now = tree_hash(files), time.time()
    sessions = list((marker or {}).get("sessions", []))
    if session and session not in sessions:
        sessions.append(session)
    # by hand (no session) it stays; once kept, a later session's pull does not make it temporary
    keep = session is None or bool((marker or {}).get("keep"))
    new_marker = {"id": item_id, "title": title, "bundle": bundle, "pulled_at": (marker or {}).get("pulled_at", now),
                  "touched_at": now, "sessions": sessions, "keep": keep}
    if marker and marker.get("bundle") == bundle:
        _write_marker(target, new_marker)
        return {**out, "status": "already"}
    dest.mkdir(parents=True, exist_ok=True)
    # unpack beside dest, not in it, so the agent's skill watcher never sees a half-written folder
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".mc-pull-", dir=dest.parent))
    try:
        for name, data in files.items():
            path = tmp.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _write_marker(tmp, new_marker)
        existed = target.exists()
        if existed:
            _rmtree(target)
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            _rmtree(tmp)
        raise
    return {**out, "status": "updated" if existed else "pulled"}


def pulled(dest: pathlib.Path = SKILLS_DIR) -> list[tuple[pathlib.Path, dict]]:
    if not dest.is_dir():
        return []
    return [(d, m) for d in sorted(dest.iterdir()) if d.is_dir() and (m := read_marker(d)) is not None]


def release_session(session: str, dest: pathlib.Path = SKILLS_DIR) -> list[str]:
    """A session ended: forget it on every pulled skill, and remove those no live session and no person keeps."""
    removed = []
    for folder, marker in pulled(dest):
        if session not in marker.get("sessions", []):
            continue
        marker["sessions"] = [s for s in marker["sessions"] if s != session]
        if marker["sessions"] or marker.get("keep"):
            _write_marker(folder, marker)
        else:
            _rmtree(folder)
            removed.append(marker.get("title", folder.name))
    return removed


def sweep_stale(dest: pathlib.Path = SKILLS_DIR, max_age_s: float = STALE_S) -> list[str]:
    """Remove session pulls whose sessions ended without saying so (crash, killed terminal)."""
    removed = []
    for folder, marker in pulled(dest):
        if not marker.get("keep") and time.time() - float(marker.get("touched_at", 0)) > max_age_s:
            _rmtree(folder)
            removed.append(marker.get("title", folder.name))
    return removed


def drop_skill(name: str, dest: pathlib.Path = SKILLS_DIR, force: bool = False) -> str:
    folder = dest / folder_name(name)
    if not folder.is_dir():
        raise McError(f"no skill folder {folder}.")
    if read_marker(folder) is None and not force:
        raise McError(f"{folder} was not installed by mc skill-pull; remove it yourself, or pass --force.")
    _rmtree(folder)
    return str(folder)


def local_skill_names(roots: list[pathlib.Path]) -> set[str]:
    """Names of the skills installed in these folders (frontmatter name, and the folder name), lowercased."""
    names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in list(root.glob("*/SKILL.md")) + list(root.glob("*/skills/*/SKILL.md")):
            names.add(skill_md.parent.name.lower())
            try:
                head = skill_md.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            m = re.search(r"^name:\s*['\"]?([^'\"\r\n]+)", head, re.M)
            if m:
                names.add(m.group(1).strip().lower())
    return names


# --- commands -----------------------------------------------------------------------------------

def _line(r: dict) -> str:
    meta = ", ".join(x for x in (r.get("scope"), r.get("date"), "verified" if r.get("verified") else None) if x)
    return f"- #{r['id']} [{meta}] {r['text']}"


def cmd_profile(a: argparse.Namespace) -> str:
    p = _call("/profile")
    out = ["# Shared memory (memory-core)", "", "## Profile", *(f"- {x['text']}" for x in p["profile"])]
    if p["projects"]:
        out += ["", "## Projects with memories (by branch; recall --scope domain:<branch> searches one)"]
        for x in sorted(p["projects"], key=lambda x: x.get("domain") or "~"):
            out.append(f"- {x.get('domain') or '-'} / {x['scope']} ({x['memories']}, last {x['last_update']})")
    if p["recent_episodes"]:
        out += ["", "## Recent sessions", *(f"- [{x['date']}, {x['by']}] {x['text']}" for x in p["recent_episodes"])]
    if a.out:
        out += ["", "## Using it", AIDER_HINT]
    return "\n".join(out) + "\n"


def cmd_recall(a: argparse.Namespace) -> str:
    r = _call("/recall", {"query": a.query, "scope": a.scope, "k": a.k, "expand": True})
    if not r["results"]:
        return "No stored memory matches.\n"
    out = [_line(x) for x in r["results"]]
    if r.get("linked"):
        out += ["Linked to those:"] + [f"  {_line(x)} (via #{x['via']}, {x['link']})" for x in r["linked"]]
    return "\n".join(out) + "\n"


def cmd_remember(a: argparse.Namespace) -> str:
    body = {"facts": a.facts, "kind": a.kind, "scope": a.scope, "supersedes": a.supersedes,
            "verified": a.verified, "force": a.force, "links": a.link}
    r = _call("/remember", body)
    out = [f"saved #{s['id']}: {s['text']}" for s in r.get("saved", [])]
    for s in r.get("saved", []):
        out += [f"  related (retire it if outdated): {_line(x)}" for x in s.get("related", [])]
    out += [f"skipped, already stored as #{s['duplicate_of']}: {s['text']}" for s in r.get("skipped_duplicates", [])]
    for h in r.get("held_near_duplicates", []):
        out.append(f"held, looks like an existing memory: {h['text']}")
        out += [f"  {_line(x)}  (similarity {x['similarity']})" for x in h["similar"]]
    if r.get("held_near_duplicates"):
        out.append("To replace one: mc remember \"<fact>\" --supersedes <id>. If it is different: add --force.")
    if r.get("redacted"):
        out.append(f"redacted before saving: {', '.join(r['redacted'])}")
    return "\n".join(out) + "\n"


def cmd_forget(a: argparse.Namespace) -> str:
    return f"retired #{_call('/forget', {'memory_id': a.id, 'reason': a.reason})['retired']}\n"


def cmd_timeline(a: argparse.Namespace) -> str:
    eps = _call("/timeline", query={"scope": a.scope, "since": a.since})["episodes"]
    return "\n".join(f"- #{e['id']} [{e['date']}, {e['by']}, {e['scope']}] {e['text']}" for e in eps) + "\n" \
        if eps else "No episodes in that range.\n"


def cmd_lib(a: argparse.Namespace) -> str:
    res = _call("/library/search", {"query": a.query, "type": a.type, "k": a.k})["results"]
    lines = [f"- #{r['id']} [{r['type']}] {r['title']}: {r.get('summary') or r.get('snippet', '')}"
             + (f" ({r['where'][0]})" if r["where"] else "") for r in res]
    return "\n".join(lines) + "\n" if lines else "Nothing in the library matches.\n"


def cmd_lib_get(a: argparse.Namespace) -> str:
    r = _call("/library/get", query={"id": a.id, "offset": a.offset})
    more = (f"\n[{r['total_chars'] - r['next_offset']} more characters: mc lib-get {a.id} --offset {r['next_offset']}]"
            if "next_offset" in r else "")
    files = (f"\n\nFiles of this skill (mc lib-file {a.id} <path>, or mc skill-pull {a.id}):\n"
             + "\n".join(f"- {f}" for f in r["files"])) if r.get("files") else ""
    return f"# {r['title']} ({r['type']}, {', '.join(r['where']) or 'no location'})\n\n{r['body']}{more}{files}\n"


def cmd_lib_file(a: argparse.Namespace) -> str:
    r = _call("/library/file", query={"id": a.id, "path": a.path, "offset": a.offset})
    if r.get("binary"):
        return f"{r['path']}: {r['bytes']} bytes, not text. {r['note']}\n"
    more = (f"\n[{r['total_chars'] - r['next_offset']} more characters: "
            f"mc lib-file {a.id} {a.path} --offset {r['next_offset']}]" if "next_offset" in r else "")
    return f"# {r['skill']}: {r['path']}\n\n{r['body']}{more}\n"


def _skills_dir(value: str | None) -> pathlib.Path:
    return pathlib.Path(value).expanduser() if value else SKILLS_DIR


def cmd_skill_pull(a: argparse.Namespace) -> str:
    try:
        r = pull_skill(Client.from_config(), a.ref, _skills_dir(a.to), a.session, force=a.force)
    except McError as exc:
        sys.exit(f"mc: {exc}")
    if r["status"] == "local":
        return f"{r['title']} is already installed at {r['path']} (not by mc); left alone. --force replaces it.\n"
    verb = {"pulled": "installed", "updated": "updated", "already": "already up to date"}[r["status"]]
    until = f"until session {a.session} ends" if a.session else "until mc skill-drop"
    return f"{r['title']} (library #{r['id']}) {verb} at {r['path']}, kept {until}.\n"


def cmd_skill_drop(a: argparse.Namespace) -> str:
    try:
        return f"removed {drop_skill(a.name, _skills_dir(a.dest), a.force)}\n"
    except McError as exc:
        sys.exit(f"mc: {exc}")


def cmd_skill_list(a: argparse.Namespace) -> str:
    rows = pulled(_skills_dir(a.dest))
    if not rows:
        return "No skills pulled by mc there.\n"
    return "\n".join(f"- {m.get('title', d.name)} (#{m.get('id')}) {d}"
                     + ("" if m.get("keep") else f", for {len(m.get('sessions', []))} session(s)") for d, m in rows) + "\n"


def main() -> None:
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="mc", description="Shared memory (memory-core) from the command line.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile"); p.add_argument("--out", help="write to this file instead of printing")
    p.set_defaults(fn=cmd_profile)
    p = sub.add_parser("recall"); p.add_argument("query"); p.add_argument("--scope"); p.add_argument("-k", type=int, default=6)
    p.set_defaults(fn=cmd_recall)
    p = sub.add_parser("remember"); p.add_argument("facts", nargs="+"); p.add_argument("--scope", default="global")
    p.add_argument("--kind", default="fact"); p.add_argument("--supersedes", type=int)
    p.add_argument("--verified", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--link", type=int, action="append", help="id of a memory these facts connect to (repeatable)")
    p.set_defaults(fn=cmd_remember)
    p = sub.add_parser("forget"); p.add_argument("id", type=int); p.add_argument("reason")
    p.set_defaults(fn=cmd_forget)
    p = sub.add_parser("timeline"); p.add_argument("--scope"); p.add_argument("--since")
    p.set_defaults(fn=cmd_timeline)
    p = sub.add_parser("lib"); p.add_argument("query"); p.add_argument("--type", choices=["skill", "repo", "doc", "ref"])
    p.add_argument("-k", type=int, default=5)
    p.set_defaults(fn=cmd_lib)
    p = sub.add_parser("lib-get"); p.add_argument("id", type=int); p.add_argument("--offset", type=int, default=0)
    p.set_defaults(fn=cmd_lib_get)
    p = sub.add_parser("lib-file"); p.add_argument("id", type=int); p.add_argument("path")
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(fn=cmd_lib_file)
    p = sub.add_parser("skill-pull"); p.add_argument("ref", help="skill name (exact) or library id")
    p.add_argument("--to", help="skills folder (default ~/.claude/skills)")
    p.add_argument("--session", help="remove it when this Claude Code session ends (what the hook uses)")
    p.add_argument("--force", action="store_true", help="replace a folder that mc did not install")
    p.set_defaults(fn=cmd_skill_pull)
    p = sub.add_parser("skill-drop"); p.add_argument("name"); p.add_argument("--from", dest="dest")
    p.add_argument("--force", action="store_true", help="remove it even if mc did not install it")
    p.set_defaults(fn=cmd_skill_drop)
    p = sub.add_parser("skill-list"); p.add_argument("--from", dest="dest")
    p.set_defaults(fn=cmd_skill_list)
    a = ap.parse_args()
    text = a.fn(a)
    if getattr(a, "out", None):
        pathlib.Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
