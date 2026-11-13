#!/usr/bin/env python3
"""library_sync: index this machine's agent skills and project docs, and the user's GitHub stars, into memory-core.

  python library_sync.py [skills] [docs] [stars] [--origin NAME] [--docs-root DIR ...] [--skills-root DIR ...]
                         [--dry-run]

No source named = skills and docs. For each source it scans, sends the server a manifest (path, scope, sha256),
and uploads only the items the server lacks, three at a time, so a daily run costs a few requests when nothing
changed. The server redacts, chunks and embeds; agents search with library_search and read with library_get.

Skills also send their whole folder (references, scripts, assets) as a zip named by its tree hash, again only when
the server lacks it: agents read those files with library_file and install a skill with mc skill-pull. Besides the
agents' own skill folders, ~/skill-vault is scanned: skills kept there are in the library without being loaded
into any agent's context until one pulls them.

Server and token: MC_URL and MC_TOKEN, else the 'memory' MCP entry in ~/.claude.json (as the Claude Code hook).
Stdlib only, so the same file runs on the laptop (Windows) and on homeserver.
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
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

HOME = pathlib.Path.home()
# Skills kept for the library only: no agent loads this folder, so they cost no context until pulled
VAULT = HOME / "skill-vault"
# A skill folder's files go to the server as one zip (app/bundles.py has the same limits). Never files that
# usually hold credentials: a bundle is readable by every agent.
BUNDLE_MAX_FILES = 1000
BUNDLE_MAX_BYTES = 60_000_000
SECRET_FILE = re.compile(r"^(\.env(\..*)?|\.netrc|id_(rsa|ed25519|ecdsa)(\.pub)?|credentials.*\.json|.*\.(pem|key|p12|pfx))$",
                         re.I)
MAX_BODY = 200_000  # the server's limit; longer files are cut, so a huge README still gets its start indexed
UPLOAD_BATCH = 3
GITHUB_USER = os.environ.get("GITHUB_USER", "GodrezJr2")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".tmp", "dist", "build", ".next",
             ".pytest_cache", "_backup", "target", "coverage", "vendor", ".cache"}
# Folders under a docs root that are not projects
NOT_PROJECTS = {"node_modules", "venv", "config", "data", "logs", "docs", "graphify-out", "lost+found", "models",
                "jellyfin_cache", "downloads_temp", "pgdata"}
DOC_NAME = re.compile(r"^(readme|handover[\w-]*|claude|agents|architecture[\w-]*|design[\w-]*)\.md$", re.I)
DOCS_PER_PROJECT = 10
MIN_DOC_CHARS = 80  # a CLAUDE.md that only says '@AGENTS.md' says nothing
# Folder names whose memory scope is spelled differently (memory-core scopes are project:<name>)
SCOPE_ALIASES = {"japan-2027-trip": "trip-2027", "japan2027": "trip-2027",
                 "e-statement-project": "bank-categorizer"}


# --- shared with the server (app/library.py) ----------------------------------------------------

def item_hash(item_type: str, title: str, summary: str, scope: str, body: str) -> str:
    return hashlib.sha256(
        json.dumps([item_type, title, summary, scope, body], ensure_ascii=False).encode()
    ).hexdigest()


def tree_hash(files: dict[str, bytes]) -> str:
    """A skill folder's content address: the same as app/bundles.py computes from the zip."""
    lines = "".join(f"{name}\0{hashlib.sha256(data).hexdigest()}\n" for name, data in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


# --- helpers ------------------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return re.sub(r"-{2,}", "-", s)[:64] or "misc"


def _label(path: pathlib.Path) -> str:
    """Path as agents will see it: ~/... under the home folder, forward slashes everywhere."""
    try:
        return "~/" + path.relative_to(HOME).as_posix()
    except ValueError:
        return path.as_posix()


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_BODY]
    except OSError:
        return None


def _frontmatter(text: str) -> dict[str, str]:
    """name/description from YAML frontmatter, including folded (>) and literal (|) values. Not a YAML parser."""
    m = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|\Z)", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    key, buf = None, []
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            if key:
                out[key] = " ".join(buf).strip()
            key, value = km.group(1).lower(), km.group(2).strip()
            buf = [] if value in ("", "|", ">", "|-", ">-", "|+", ">+") else [value]
        elif key:
            buf.append(line.strip())
    if key:
        out[key] = " ".join(buf).strip()
    return {k: v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v for k, v in out.items()}


def _first_line(text: str) -> str:
    """A doc's summary: its first sentence-like line, or else its first heading."""
    body = re.sub(r"\A---.*?\n---\s*\n", "", text, count=1, flags=re.S)
    heading = ""
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("```", "<!--", "|", "![", "[![", "<")):
            continue
        if line.startswith("#"):
            heading = heading or line.lstrip("#").strip()
            continue
        return re.sub(r"[*_`]", "", line)[:200]
    return heading[:200]


def _item(item_type: str, path: str, scope: str, title: str, summary: str, body: str,
          meta: dict | None = None) -> dict:
    return {"path": path, "scope": scope, "title": title, "summary": summary, "body": body, "meta": meta or {},
            "hash": item_hash(item_type, title, summary, scope, body)}


def _walk(root: pathlib.Path, max_depth: int):
    """Files under root, at most max_depth folders down, skipping dependency and build folders."""
    root_depth = len(root.parts)
    for folder, dirs, files in os.walk(root, onerror=lambda e: None):
        depth = len(pathlib.Path(folder).parts) - root_depth
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and depth < max_depth)
        for f in sorted(files):
            yield pathlib.Path(folder) / f


# --- sources ------------------------------------------------------------------------------------

def bundle_files(folder: pathlib.Path) -> dict[str, pathlib.Path] | None:
    """The files of a skill folder by their path inside it, or None if it is too big to send."""
    files, total = {}, 0
    for path in _walk(folder, 6):
        if path.is_symlink() or not path.is_file() or SECRET_FILE.match(path.name):
            continue
        total += path.stat().st_size
        files[path.relative_to(folder).as_posix()] = path
        if len(files) > BUNDLE_MAX_FILES or total > BUNDLE_MAX_BYTES:
            return None
    return files


def bundle_zip(files: dict[str, pathlib.Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            archive.write(files[name], name)
    return buf.getvalue()


def _skill_bundle(skill_md: pathlib.Path, root: pathlib.Path) -> tuple[str | None, dict | None]:
    """(tree hash, files) of the folder holding this SKILL.md. A SKILL.md lying loose in a root has no folder."""
    folder = skill_md.parent
    if folder == root:
        return None, None
    files = bundle_files(folder)
    if not files or "SKILL.md" not in files:
        return None, None
    try:
        return tree_hash({name: p.read_bytes() for name, p in files.items()}), files
    except OSError:
        return None, None


def _skill_roots(extra: list[str] | None = None) -> list[pathlib.Path]:
    roots = [HOME / ".claude" / "skills", HOME / ".codex" / "skills", HOME / ".config" / "opencode" / "skills",
             HOME / ".agents" / "skills", VAULT] + [pathlib.Path(e).expanduser() for e in extra or []]
    # Claude Code plugins: only installed and enabled ones (plugins/marketplaces holds every plugin on offer)
    try:
        installed = json.loads((HOME / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        enabled = json.loads((HOME / ".claude" / "settings.json").read_text(encoding="utf-8")).get("enabledPlugins", {})
    except (OSError, ValueError):
        installed, enabled = {}, {}
    for name, installs in (installed.get("plugins") or {}).items():
        if enabled.get(name):
            roots += [pathlib.Path(i["installPath"]) for i in installs if i.get("scope") == "user" and i.get("installPath")]
    return [r for r in dict.fromkeys(roots) if r.is_dir()]


def scan_skills(extra_roots: list[str] | None = None) -> list[dict]:
    items = []
    for root in _skill_roots(extra_roots):
        for path in _walk(root, 6):
            if path.name != "SKILL.md":
                continue
            text = _read(path)
            if not text:
                continue
            fm = _frontmatter(text)
            title = (fm.get("name") or path.parent.name).strip()[:120]
            item = _item("skill", _label(path), "global", title, fm.get("description", "")[:300], text)
            item["bundle"], item["files"] = _skill_bundle(path, root)
            items.append(item)
    return items


def _doc_roots(given: list[str] | None) -> list[pathlib.Path]:
    if given:
        return [pathlib.Path(g).expanduser() for g in given]
    if os.name == "nt":
        return [p for p in (pathlib.Path("D:/Project"),) if p.is_dir()]
    return [p for p in (HOME / "stacks", pathlib.Path("/mnt/data")) if p.is_dir()]


def scan_docs(roots: list[pathlib.Path]) -> list[dict]:
    items = []
    for root in roots:
        for project in sorted(p for p in root.iterdir() if p.is_dir()):
            if project.name.startswith((".", "_")) or project.name in NOT_PROJECTS:
                continue
            slug = _slug(project.name)
            scope = f"project:{SCOPE_ALIASES.get(slug, slug)}"
            docs = [p for p in _walk(project, 1) if DOC_NAME.match(p.name)]
            docs.sort(key=lambda p: (len(p.relative_to(project).parts), str(p).lower()))
            for path in docs[:DOCS_PER_PROJECT]:
                text = _read(path)
                if not text or len(text.strip()) < MIN_DOC_CHARS:
                    continue
                title = f"{project.name}/{path.relative_to(project).as_posix()}"
                items.append(_item("doc", _label(path), scope, title, _first_line(text), text))
    return items


def _github_token() -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _github(path: str, token: str | None, raw: bool = False):
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "User-Agent": "memory-core-library-sync", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if raw and exc.code == 404:  # a repo without a README
            return ""
        raise
    return data.decode("utf-8", errors="replace") if raw else json.loads(data)


def scan_stars(user: str) -> list[dict]:
    """Every starred repo with its README. Any API error aborts, so a partial list never retires stars."""
    token = _github_token()
    repos, page = [], 1
    while True:
        batch = _github(f"/users/{user}/starred?per_page=100&page={page}", token)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    items = []
    for r in repos:
        readme = _github(f"/repos/{r['full_name']}/readme", token, raw=True)
        topics = r.get("topics") or []
        head = [f"Language: {r.get('language') or '-'}"] + ([f"Topics: {', '.join(topics)}"] if topics else []) + (
            [f"Homepage: {r['homepage']}"] if r.get("homepage") else [])
        body = ("\n".join(head) + "\n\n" + readme)[:MAX_BODY]
        meta = {"url": r["html_url"], "language": r.get("language"), "stars": r.get("stargazers_count"),
                "topics": topics[:20]}
        items.append(_item("repo", r["full_name"], "global", r["full_name"], (r.get("description") or "")[:300],
                           body, meta))
    return items


# --- talking to memory-core ---------------------------------------------------------------------

def _server() -> tuple[str, str]:
    url, token = os.environ.get("MC_URL"), os.environ.get("MC_TOKEN")
    if url and token:
        return url.rstrip("/"), f"Bearer {token}"
    entry = json.loads((HOME / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["memory"]
    return entry["url"].rsplit("/mcp", 1)[0], entry["headers"]["Authorization"]


class Api:
    def __init__(self) -> None:
        self.base, self.auth = _server()

    def post(self, path: str, body: dict | bytes, content_type: str = "application/json") -> dict:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers={
            "Authorization": self.auth, "Content-Type": content_type, "X-MC-Client": "sync"})
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.load(resp)


def sync(api: Api | None, item_type: str, origin: str, items: list[dict], allow_empty: bool) -> int:
    """Returns the number of items that failed to upload."""
    if not items and not allow_empty:
        print(f"{item_type}: nothing found, not syncing (an empty manifest would drop this origin's items; "
              "pass --allow-empty if that is intended)")
        return 0
    if api is None:  # dry run
        for i in items:
            files = f"  +{len(i['files']) - 1} files" if i.get("files") and len(i["files"]) > 1 else ""
            print(f"  {item_type:5} {i['scope']:28} {i['title'][:50]:50} {len(i['body']):>7} chars  {i['path']}{files}")
        print(f"{item_type}: {len(items)} found (dry run)")
        return 0
    manifest = [{"path": i["path"], "scope": i["scope"], "hash": i["hash"],
                 **({"bundle": i["bundle"]} if i.get("bundle") else {})} for i in items]
    r = api.post("/api/v1/library/sync", {"type": item_type, "origin": origin, "manifest": manifest})
    need, todo = set(r["need"]), {}
    for i in items:
        if i["hash"] in need:
            todo.setdefault(i["hash"], i)
    todo_list = list(todo.values())
    print(f"{item_type}: {r['found']} found, {len(todo_list)} to index, {r['removed']} removed", flush=True)
    failed = 0
    for start in range(0, len(todo_list), UPLOAD_BATCH):
        batch = todo_list[start:start + UPLOAD_BATCH]
        body = {"type": item_type, "items": [
            {k: i[k] for k in ("title", "summary", "scope", "body", "meta", "hash")} for i in batch]}
        try:
            res = api.post("/api/v1/library/upsert", body)
            done = ", ".join(f"{s['title']} ({s['chunks']})" for s in res["saved"])
            print(f"  [{start + len(batch)}/{len(todo_list)}] {done}", flush=True)
        except urllib.error.HTTPError as exc:  # one bad item must not stop the rest; it is retried next run
            failed += len(batch)
            print(f"  [{start + len(batch)}/{len(todo_list)}] FAILED {exc.code}: "
                  f"{exc.read().decode(errors='replace')[:200]}", flush=True)
    return failed + upload_bundles(api, items, r.get("need_bundles") or [], r.get("bundles_removed", 0))


def upload_bundles(api: Api, items: list[dict], need: list[str], removed: int) -> int:
    """Send the skill folders the server lacks. Returns how many failed (retried next run)."""
    if not need and not removed:
        return 0
    folders = {i["bundle"]: i for i in items if i.get("bundle") in set(need)}
    print(f"bundles: {len(folders)} to send, {removed} removed", flush=True)
    failed = 0
    for n, (bundle, item) in enumerate(folders.items(), 1):
        try:
            data = bundle_zip(item["files"])
            res = api.post(f"/api/v1/library/bundle?hash={bundle}", data, "application/zip")
            print(f"  [{n}/{len(folders)}] {item['title']} ({res['files']} files, {res['bytes'] // 1024} KiB)", flush=True)
        except (OSError, urllib.error.HTTPError) as exc:
            failed += 1
            detail = exc.read().decode(errors="replace")[:200] if isinstance(exc, urllib.error.HTTPError) else exc
            print(f"  [{n}/{len(folders)}] {item['title']} FAILED: {detail}", flush=True)
    return failed


def main() -> None:
    if sys.stdout:  # None under pythonw
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="library_sync", description=__doc__.split("\n\n")[0])
    ap.add_argument("sources", nargs="*", choices=["skills", "docs", "stars"], help="default: skills docs")
    ap.add_argument("--origin", default=os.environ.get("MC_ORIGIN") or (
        "laptop" if os.name == "nt" else _slug(socket.gethostname())), help="name of this machine in 'where'")
    ap.add_argument("--docs-root", action="append", help="folder whose subfolders are projects (repeatable)")
    ap.add_argument("--skills-root", action="append",
                    help="another folder of skills to index, besides the agents' own and ~/skill-vault (repeatable)")
    ap.add_argument("--github-user", default=GITHUB_USER)
    ap.add_argument("--dry-run", action="store_true", help="list what would be synced, send nothing")
    ap.add_argument("--allow-empty", action="store_true", help="sync even if a source finds nothing")
    ap.add_argument("--log", help="write output to this file (for scheduled runs under pythonw, which has no console)")
    a = ap.parse_args()
    if a.log:
        sys.stdout = sys.stderr = open(a.log, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    api = None if a.dry_run else Api()
    failed = 0
    for source in a.sources or ["skills", "docs"]:
        if source == "skills":
            failed += sync(api, "skill", a.origin, scan_skills(a.skills_root), a.allow_empty)
        elif source == "docs":
            failed += sync(api, "doc", a.origin, scan_docs(_doc_roots(a.docs_root)), a.allow_empty)
        else:
            try:
                items = scan_stars(a.github_user)
            except (urllib.error.URLError, ValueError, KeyError) as exc:
                print(f"repo: GitHub API failed ({exc}); stars not synced this run")
                failed += 1
                continue
            failed += sync(api, "repo", "github", items, a.allow_empty)
    if api is not None:  # file new and changed items into categories (and pick up edits to the category file)
        r = api.post("/api/v1/library/categories/apply", {})
        print(f"categories: {r['categories']} shelves, {r['embedded']} re-embedded, {r['auto_filed']} filed automatically")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
