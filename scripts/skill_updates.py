"""Weekly check: which installed skills differ from their upstream repo.

Every skill location the library knows (laptop and homeserver, each stored as a bundle zip) is compared,
file by file with line endings normalised, against the skill of the same name in the repos listed in
scripts/skill_sources.json (default branch, downloaded as a tarball, so no git is needed). Nothing is
installed or changed: the result is printed and, when the list of outdated skills changed since the last
run, saved as one memory (superseding the previous one) so any agent asked "are my skills up to date?"
finds it. Updating stays a decision for the user.

Run on homeserver (cron, Mondays 04:15):
  cd ~/stacks/memory-core && docker compose run --rm --no-deps -T \
    -v $PWD/scripts:/srv/scripts:ro --entrypoint python app scripts/skill_updates.py
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bundles import path_for  # noqa: E402
from app.config import load  # noqa: E402
from app.rules import Agent, default_scopes  # noqa: E402
from app.store import Store  # noqa: E402

SOURCES = Path(__file__).with_name("skill_sources.json")
TAG = "skill-update-check"
SCOPE = "project:memory-core"
MAX_REPO_BYTES = 200_000_000


def norm(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def skill_name(text: str) -> str | None:
    m = re.search(r"^name:\s*['\"]?([\w.:-]+)", text[:3000], re.M)
    return m.group(1) if m else None


def download(repo: str) -> dict[str, dict[str, str]]:
    """{skill dir inside the repo: {relative file: hash}} for every SKILL.md folder of the repo's default branch."""
    url = f"https://codeload.github.com/{repo}/tar.gz/HEAD"
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read(MAX_REPO_BYTES + 1)
    if len(data) > MAX_REPO_BYTES:
        raise RuntimeError(f"{repo} is larger than {MAX_REPO_BYTES} bytes")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile() and "/.git/" not in m.name and "/node_modules/" not in m.name:
                f = tar.extractfile(m)
                if f:
                    files[m.name.split("/", 1)[1] if "/" in m.name else m.name] = f.read()
    skills = {}
    for path in files:
        if PurePosixPath(path).name == "SKILL.md" and not path.startswith(("tests/", "test/")):
            root = str(PurePosixPath(path).parent)
            prefix = "" if root == "." else root + "/"
            skills[root] = {p[len(prefix):]: norm(b) for p, b in files.items() if p.startswith(prefix)}
            skills[root]["\0name"] = skill_name(files[path].decode("utf-8", "replace")) or PurePosixPath(root).name
    return skills


def ignored(path: str, ignore: set[str]) -> bool:
    """Files never compared: stamps and licences, plus .env* (library_sync never uploads them)."""
    name = PurePosixPath(path).name
    return name in ignore or name.startswith(".env")


def bundle_files(bundle: str, ignore: set[str]) -> dict[str, str]:
    with zipfile.ZipFile(path_for(bundle)) as z:
        return {n: norm(z.read(n)) for n in z.namelist()
                if not n.endswith("/") and not ignored(n, ignore)}


async def main() -> None:
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))
    ignore = set(cfg["ignore_files"])
    upstream: dict[str, list[tuple[str, str, dict[str, str]]]] = {}  # skill name -> [(repo, dir, files)]
    failed = []
    for repo in cfg["repos"]:
        try:
            for root, fl in download(repo).items():
                name = fl.pop("\0name")
                fl = {p: h for p, h in fl.items() if not ignored(p, ignore)}
                for key in {name, PurePosixPath(root).name}:
                    upstream.setdefault(key, []).append((repo, root, fl))
        except Exception as exc:  # one dead repo must not stop the check
            failed.append(f"{repo} ({exc})")

    store = Store(load())
    pool = await store.pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """select l.origin, l.path, l.bundle, i.title from lib_location l join lib_item i on i.hash = l.hash
               where l.type = 'skill' and l.bundle is not null order by l.origin, l.path""")

    outdated, current, unknown = [], 0, 0
    for r in rows:
        if any(s in r["path"] for s in cfg["skip_paths"]) or r["title"] in cfg["skip_titles"]:
            continue
        name_dir = PurePosixPath(r["path"]).parent.name
        cands = upstream.get(r["title"], []) + upstream.get(name_dir, [])
        if not cands or not Path(path_for(r["bundle"])).exists():
            unknown += 1
            continue
        local = bundle_files(r["bundle"], ignore)
        best = None
        for repo, root, up in cands:
            stale = sorted(p for p in up if local.get(p) != up[p])  # upstream files that are missing or differ here
            if best is None or len(stale) < len(best[2]):
                best = (repo, root, stale)
        repo, root, stale = best
        if not stale:
            current += 1
        else:
            outdated.append({"origin": r["origin"], "path": r["path"], "skill": r["title"], "repo": repo,
                             "upstream_dir": root, "files": len(stale), "sample": stale[:3]})

    today = date.today().isoformat()
    print(json.dumps({"date": today, "checked": current + len(outdated), "current": current, "unknown": unknown,
                      "outdated": outdated, "repos_failed": failed}, indent=1))

    if outdated:
        listed = "; ".join(f"{o['origin']} {o['skill']} ({o['repo']}, {o['files']} files)" for o in outdated)
        text = (f"Skill update check {today}: {len(outdated)} installed skills differ from upstream: {listed}. "
                f"Update a skill by recopying its upstream folder once the user approves.")
    else:
        text = f"Skill update check {today}: all {current} tracked skill installs match their upstream repos."
    if failed:
        text += f" Repos that could not be downloaded: {', '.join(failed)}."

    async with pool.acquire() as con:
        prev = await con.fetchrow(
            "select id, content from memory where valid_to is null and $1 = any(tags) order by created_at desc limit 1",
            TAG)
    # the list, not the date, decides whether anything new happened
    same = prev is not None and prev["content"].split(": ", 1)[-1] == text.split(": ", 1)[-1]
    if same:
        print("unchanged since memory", prev["id"])
        return
    agent = Agent("skill-updates", default_scopes("skill-updates"))
    res = await store.remember(agent, [text], "fact", SCOPE, prev["id"] if prev else None, [TAG], True, True,
                               {"script": "scripts/skill_updates.py"})
    print("saved", [s["id"] for s in res.get("saved", [])])


if __name__ == "__main__":
    asyncio.run(main())
