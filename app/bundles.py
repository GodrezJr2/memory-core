"""Skill bundles: a skill's whole folder (SKILL.md plus its references, scripts and assets) as a zip, so an agent on
any machine can read a supporting file (library_file) or install the skill locally (mc skill-pull).

A bundle is named by its tree hash: sha256 over "<path>\\0<sha256 of the file>\\n" for every file, sorted by path.
clients/library_sync.py computes the same from the folder on disk, so the hash depends only on the files, never on
how the zip was compressed. Bundles live under BUNDLE_DIR as <hash>.zip, one copy however many machines hold the
skill, and go when no lib_location row names them any more.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from pathlib import Path

from .rules import RuleError

BUNDLE_DIR = Path(os.environ.get("BUNDLE_DIR", "/data/bundles"))
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# A skill folder is mostly text and images, but media does not compress (huashu-design ships ~27 MB of background
# music for its video exports); past this it is a vendored tool and not worth copying around.
MAX_ZIP_BYTES = 40_000_000
MAX_UNZIPPED_BYTES = 60_000_000
MAX_FILES = 1000
FILE_PAGE_CHARS = 12_000


def tree_hash(files: dict[str, bytes]) -> str:
    """Content address of a bundle. clients/library_sync.py computes the same."""
    lines = "".join(f"{name}\0{hashlib.sha256(data).hexdigest()}\n" for name, data in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def check_hash(value: object) -> str:
    if not isinstance(value, str) or not HASH_RE.match(value):
        raise RuleError(f"Invalid bundle hash {value!r}: expected 64 lowercase hex characters.")
    return value


def safe_name(name: str) -> bool:
    """A relative POSIX path that stays inside the folder it is unpacked into."""
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def read_zip(data: bytes) -> dict[str, bytes]:
    """Every file in a bundle, checked for size and unsafe paths. Raises RuleError."""
    if len(data) > MAX_ZIP_BYTES:
        raise RuleError(f"The bundle has {len(data)} bytes; at most {MAX_ZIP_BYTES}.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RuleError(f"Not a zip file: {exc}") from exc
    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > MAX_FILES:
        raise RuleError(f"The bundle has {len(entries)} files; at most {MAX_FILES}.")
    if sum(i.file_size for i in entries) > MAX_UNZIPPED_BYTES:
        raise RuleError(f"The bundle unpacks to more than {MAX_UNZIPPED_BYTES} bytes.")
    files: dict[str, bytes] = {}
    for info in entries:
        if not safe_name(info.filename):
            raise RuleError(f"Unsafe path in the bundle: {info.filename!r}.")
        files[info.filename] = archive.read(info)
    return files


def path_for(bundle: str) -> Path:
    return BUNDLE_DIR / f"{check_hash(bundle)}.zip"


def have(bundles: set[str]) -> set[str]:
    return {b for b in bundles if path_for(b).exists()}


def save(bundle: str, data: bytes) -> int:
    """Store an uploaded bundle after checking its content matches its name. Returns the number of files."""
    files = read_zip(data)
    if "SKILL.md" not in files:
        raise RuleError("A skill bundle needs SKILL.md at its top level.")
    actual = tree_hash(files)
    if actual != check_hash(bundle):
        raise RuleError(f"The bundle's content hashes to {actual}, not {bundle} (compute it as library_sync does).")
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(bundle)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return len(files)


def names(bundle: str) -> list[str]:
    with zipfile.ZipFile(path_for(bundle)) as archive:
        return sorted(i.filename for i in archive.infolist() if not i.is_dir())


def read_file(bundle: str, name: str) -> bytes:
    with zipfile.ZipFile(path_for(bundle)) as archive:
        try:
            return archive.read(name)
        except KeyError:
            raise RuleError(f"No file {name!r} in this skill. Its files are listed by library_get.") from None


def collect(keep: set[str]) -> int:
    """Delete bundles nothing points at. Returns how many went."""
    if not BUNDLE_DIR.is_dir():
        return 0
    gone = 0
    for path in BUNDLE_DIR.glob("*.zip"):
        if path.stem not in keep:
            path.unlink(missing_ok=True)
            gone += 1
    return gone
