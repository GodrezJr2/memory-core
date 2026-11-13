"""Saved references: screenshots and links the user shares from their phone (a workflow seen on Threads or X, a tool, a
prompt), searchable like the rest of the library.

Capture: POST /api/v1/capture/ref from the iOS share sheet, authenticated by the user's Tailscale login, so the phone
holds no token. Each image is stored once by sha256 under MEDIA_DIR and read by OCR (tesseract, English and
Indonesian; dark-mode screenshots are inverted first), so a reference is searchable the moment it arrives. Images
sent with the same link, or in one share (the Shortcut sends a 'group' per share), join one reference, so a
thread of three screenshots is one item. Status starts as 'inbox'.

Review: when the user asks, an agent lists the inbox (library_inbox), looks at each image (library_view returns it as an
image the model can see), and writes a title, a description and a category (library_review), which moves the
reference to 'reviewed'. There is still no LLM on the server; the reviewing agent does the looking.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image as PILImage
from PIL import ImageOps, ImageStat

from .library import TITLE_CHARS, chunk_contents
from .rules import Agent, RuleError, check_scope, redact

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_PIXELS  # refuse decompression bombs
VIEW_EDGE = 1568  # longest edge of the copy agents look at; Claude scales larger images down to this anyway
THUMB_EDGE = 480  # width of the grid thumbnail in the web app
SIZES = ("thumb", "view", "full")
FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}
NOTE_CHARS = 2000
DESCRIPTION_CHARS = 4000
OCR_CHARS = 8000
OCR_TIMEOUT_S = 90
OCR_LANGS = "eng+ind"
# Share links from these sites carry tracking parameters; the post is identified by the path alone.
NO_QUERY_HOSTS = ("x.com", "threads.net", "threads.com", "instagram.com", "tiktok.com", "youtube.com", "youtu.be")
HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "mobile.x.com": "x.com"}


# --- pure helpers (unit-tested) ----------------------------------------------------------------

def normalize_url(url: str) -> str:
    """One spelling per link, so the same post shared twice joins one reference."""
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RuleError(f"url must be an http(s) link, got {url[:80]!r}.")
    host = parts.netloc.lower().split("@")[-1]
    host = host[4:] if host.startswith("www.") else host
    host = HOST_ALIASES.get(host, host)
    if host.endswith(NO_QUERY_HOSTS):
        query = ""
    else:
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def ref_hash(url: str, image_sha: str | None, group: str = "") -> str:
    key = f"url:{url}" if url else f"group:{group}" if group else f"img:{image_sha}"
    return hashlib.sha256(key.encode()).hexdigest()


def default_title(note: str, url: str, when: datetime) -> str:
    if note:
        first = note.split(". ")[0]
        return first if len(first) <= 80 else first[:79] + "…"
    if url:
        parts = urlsplit(url)
        return (parts.netloc + parts.path)[:80]
    return f"Screenshot {when:%d %b %Y %H:%M}"


def clean_ocr(text: str) -> str:
    """Drop OCR debris (lines without two letters or digits in a row) and blank runs."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    kept = [line for line in lines if re.search(r"[0-9A-Za-z]{2}", line)]
    return "\n".join(kept)[:OCR_CHARS]


def ref_body(note: str, description: str, url: str, ocr_texts: list[str]) -> str:
    """What gets searched. The reviewer's description first: chunk 0 is what search and filing lean on."""
    parts = []
    if description:
        parts.append(f"Description: {description}")
    if note:
        parts.append(f"The user's note: {note}")
    if url:
        parts.append(f"Link: {url}")
    for n, text in enumerate(ocr_texts, 1):
        if text:
            parts.append(f"Text in image {n}:\n{text}")
    return "\n\n".join(parts)


def summary_of(text: str, limit: int = 300) -> str:
    """The first sentences of a review that fit in limit characters (agents see this in hints and cards)."""
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    if cut > limit // 3:
        return text[: cut + 1]
    return text[: text.rfind(" ", 0, limit - 1)] + "…"


def decode_image(value: str) -> bytes:
    value = value.strip()
    if value.startswith("data:"):  # a data URL from a browser
        value = value.split(",", 1)[-1]
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuleError("image must be base64.") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise RuleError(f"image is {len(data) // 1_000_000} MB; at most {MAX_IMAGE_BYTES // 1_000_000} MB.")
    return data


def prepare_image(data: bytes) -> dict[str, Any]:
    """Validate an upload. Returns its sha256, format, size and a JPEG copy for agents."""
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decoding failure means "not an image we take"
        raise RuleError("image is not a PNG, JPEG, WebP or GIF picture (convert HEIC to JPEG first).") from exc
    ext = FORMATS.get(img.format or "")
    if not ext:
        raise RuleError(f"image format {img.format} is not accepted; send PNG, JPEG, WebP or GIF.")
    view = ImageOps.exif_transpose(img).convert("RGB")
    view.thumbnail((VIEW_EDGE, VIEW_EDGE))
    buf = io.BytesIO()
    view.save(buf, "JPEG", quality=85)
    thumb = view.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE * 2))  # tall screenshots keep their top, which the grid shows
    small = io.BytesIO()
    thumb.save(small, "JPEG", quality=80)
    return {"sha256": hashlib.sha256(data).hexdigest(), "ext": ext, "width": img.width, "height": img.height,
            "bytes": len(data), "view": buf.getvalue(), "thumb": small.getvalue()}


def ocr_input(data: bytes) -> bytes:
    """Grayscale, inverted if the screenshot is dark mode, doubled if small: what tesseract reads best."""
    img = ImageOps.exif_transpose(PILImage.open(io.BytesIO(data))).convert("L")
    if ImageStat.Stat(img).mean[0] < 110:
        img = ImageOps.invert(img)
    if img.width < 1000:
        img = img.resize((img.width * 2, img.height * 2))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def run_ocr(data: bytes) -> str:
    png = await asyncio.to_thread(ocr_input, data)
    proc = await asyncio.create_subprocess_exec(
        "tesseract", "stdin", "stdout", "-l", OCR_LANGS,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(png), OCR_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return ""
    return clean_ocr(out.decode("utf-8", errors="replace"))


# --- the store side ----------------------------------------------------------------------------

class Refs:
    def __init__(self, store: Any, library: Any, categories: Any) -> None:
        self._store, self._library, self._categories = store, library, categories

    async def capture(self, agent: Agent, image: Any = None, url: Any = None, note: Any = None,
                      scope: Any = None, group: Any = None) -> dict[str, Any]:
        scope = check_scope(str(scope or "global"))
        if not agent.can_access(scope):
            raise RuleError(f"Agent {agent.id!r} may not write to {scope!r}.")
        url = normalize_url(str(url or ""))
        note = " ".join(str(note or "").split())[:NOTE_CHARS]
        group = re.sub(r"[^0-9A-Za-z_.:-]", "", str(group or ""))[:64]
        data = decode_image(str(image)) if image else None
        if not data and not url:
            raise RuleError("Send an image, a link, or both.")
        img = await asyncio.to_thread(prepare_image, data) if data else None
        now = datetime.now(timezone.utc)

        pool = await self._store.pool()
        async with pool.acquire() as con:
            item_id = await con.fetchval("select item_id from lib_media where sha256 = $1", img["sha256"]) if img else None
            have_image = item_id is not None
            if item_id is None and url:
                item_id = await con.fetchval("select id from lib_item where type = 'ref' and meta->>'url' = $1", url)
            if item_id is None and group:
                item_id = await con.fetchval(
                    "select id from lib_item where type = 'ref' and meta->>'group' = $1 and created_by = $2",
                    group, agent.id)
        merged = item_id is not None

        ocr = ""
        if img and not have_image:
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            original = MEDIA_DIR / f"{img['sha256']}.{img['ext']}"
            if not original.exists():
                original.write_bytes(data)
            (MEDIA_DIR / f"{img['sha256']}.view.jpg").write_bytes(img["view"])
            (MEDIA_DIR / f"{img['sha256']}.thumb.jpg").write_bytes(img["thumb"])
            ocr = await run_ocr(data)

        async with pool.acquire() as con, con.transaction():
            if item_id is None:
                meta = {"url": url, "note": note, "description": "", "saved_by": agent.id, "group": group}
                item_id = await con.fetchval(
                    """insert into lib_item (hash, type, scope, title, summary, body, meta, created_by, status)
                       values ($1, 'ref', $6, $2, $3, '', $4, $5, 'inbox') returning id""",
                    ref_hash(url, img["sha256"] if img else None, group), redact(default_title(note, url, now))[0],
                    redact(note)[0][:300], meta, agent.id, scope,
                )
            else:
                meta = await con.fetchval("select meta from lib_item where id = $1", item_id) or {}
                if note and note not in meta.get("note", ""):  # a second share may add words to the first
                    meta["note"] = f"{meta['note']} / {note}" if meta.get("note") else note
                if url and not meta.get("url"):
                    meta["url"] = url
                await con.execute("update lib_item set meta = $2, updated_at = now() where id = $1", item_id, meta)
            if img and not have_image:
                await con.execute(
                    """insert into lib_media (sha256, item_id, ext, width, height, bytes, ocr)
                       values ($1, $2, $3, $4, $5, $6, $7) on conflict (sha256) do nothing""",
                    img["sha256"], item_id, img["ext"], img["width"], img["height"], img["bytes"], redact(ocr)[0],
                )
        await self._reindex(item_id)
        async with pool.acquire() as con:
            row = await con.fetchrow(
                """select i.status, i.category, (select count(*) from lib_media m where m.item_id = i.id) as images
                   from lib_item i where i.id = $1""", item_id)
        return {"id": item_id, "merged": merged, "status": row["status"], "category": row["category"],
                "images": row["images"], "ocr_chars": len(ocr), "duplicate_image": have_image}

    async def _reindex(self, item_id: int) -> None:
        """Rebuild a reference's searchable text from its note, description, link and OCR, then re-embed and file it."""
        pool = await self._store.pool()
        async with pool.acquire() as con:
            row = await con.fetchrow("select title, summary, meta, status from lib_item where id = $1", item_id)
            ocr = [r["ocr"] for r in await con.fetch(
                "select ocr from lib_media where item_id = $1 order by created_at", item_id)]
        meta = row["meta"] or {}
        body = redact(ref_body(meta.get("note", ""), meta.get("description", ""), meta.get("url", ""), ocr))[0]
        contents = chunk_contents({"type": "ref", "title": row["title"], "summary": row["summary"], "body": body})
        vectors = await self._library.embed_contents(contents)
        async with pool.acquire() as con, con.transaction():
            await con.execute("update lib_item set body = $2, updated_at = now() where id = $1", item_id, body)
            await self._library.replace_chunks(con, item_id, contents, vectors)
            if row["status"] != "reviewed":
                await self._categories.assign(con, [item_id])

    async def _ref(self, con: Any, agent: Agent, item_id: Any) -> Any:
        row = await con.fetchrow(
            "select * from lib_item where id = $1 and type = 'ref' and scope like any($2)",
            int(item_id), agent.like_patterns,
        )
        if row is None:
            raise RuleError(f"Reference {item_id} does not exist (or was discarded).")
        return row

    async def inbox(self, agent: Agent, limit: Any = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit or 10), 50))
        pool = await self._store.pool()
        async with pool.acquire() as con:
            total = await con.fetchval(
                "select count(*) from lib_item where type = 'ref' and status = 'inbox' and scope like any($1)",
                agent.like_patterns)
            rows = await con.fetch(
                """select i.id, i.title, i.meta, i.category, i.created_at, count(m.sha256) as images,
                          left(coalesce(string_agg(m.ocr, ' '), ''), 240) as ocr
                   from lib_item i left join lib_media m on m.item_id = i.id
                   where i.type = 'ref' and i.status = 'inbox' and i.scope like any($1)
                   group by i.id order by i.created_at limit $2""",
                agent.like_patterns, limit,
            )
        return {
            "waiting": total,
            "refs": [{"id": r["id"], "saved": r["created_at"].date().isoformat(), "title": r["title"],
                      "note": (r["meta"] or {}).get("note", ""), "url": (r["meta"] or {}).get("url", ""),
                      "images": r["images"], "guessed_category": r["category"],
                      "ocr_start": " ".join(r["ocr"].split())} for r in rows],
            "next_step": "For each: library_view(id) to see it, then library_review(id, title, description, "
                         "category), or library_review(id, discard=true) for junk.",
        }

    async def view(self, agent: Agent, item_id: Any, image: Any = 0) -> tuple[dict[str, Any], bytes | None]:
        pool = await self._store.pool()
        async with pool.acquire() as con:
            row = await self._ref(con, agent, item_id)
            media = await con.fetch(
                "select sha256, width, height, ocr from lib_media where item_id = $1 order by created_at", row["id"])
        meta = row["meta"] or {}
        info: dict[str, Any] = {"id": row["id"], "title": row["title"], "status": row["status"],
                                "category": row["category"], "note": meta.get("note", ""), "url": meta.get("url", ""),
                                "description": meta.get("description", "")}
        if not media:
            info["images"] = 0
            info["hint"] = "No image: open the link (Chrome) to see what it is."
            return info, None
        n = max(0, min(int(image or 0), len(media) - 1))
        info.update(images=len(media), showing=n, size=f"{media[n]['width']}x{media[n]['height']}",
                    ocr=media[n]["ocr"][:3000])
        if len(media) > 1:
            info["others"] = f"library_view({row['id']}, image=k) for k in 0..{len(media) - 1}"
        path = MEDIA_DIR / f"{media[n]['sha256']}.view.jpg"
        return info, path.read_bytes() if path.exists() else None

    async def review(self, agent: Agent, item_id: Any, title: Any = None, description: Any = None,
                     category: Any = None, discard: Any = False) -> dict[str, Any]:
        pool = await self._store.pool()
        async with pool.acquire() as con:
            row = await self._ref(con, agent, item_id)
            if discard:
                media = await con.fetch("select sha256, ext from lib_media where item_id = $1", row["id"])
                await con.execute("delete from lib_item where id = $1", row["id"])
                for m in media:
                    for name in (f"{m['sha256']}.{m['ext']}", f"{m['sha256']}.view.jpg", f"{m['sha256']}.thumb.jpg"):
                        (MEDIA_DIR / name).unlink(missing_ok=True)
                return {"discarded": row["id"]}
        description = " ".join(str(description or "").split())[:DESCRIPTION_CHARS]
        if not description:
            raise RuleError("Write a description of what the reference shows and why it is useful.")
        cat = self._categories.check(category)
        title = " ".join(str(title or "").split())[:TITLE_CHARS] or row["title"]
        meta = dict(row["meta"] or {})
        meta.update(description=description, reviewed_by=agent.id,
                    reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        async with pool.acquire() as con:
            await con.execute(
                """update lib_item set title = $2, summary = $3, meta = $4, category = $5, category_auto = false,
                       status = 'reviewed', updated_at = now() where id = $1""",
                row["id"], redact(title)[0], summary_of(redact(description)[0]), meta, cat,
            )
        await self._reindex(row["id"])
        return {"id": row["id"], "status": "reviewed", "title": title, "category": cat}

    async def media_file(self, agent: Agent, sha: str, size: str = "full") -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha or "") or size not in SIZES:
            raise RuleError("Unknown image.")
        pool = await self._store.pool()
        async with pool.acquire() as con:
            ext = await con.fetchval(
                """select m.ext from lib_media m join lib_item i on i.id = m.item_id
                   where m.sha256 = $1 and i.scope like any($2)""", sha, agent.like_patterns)
        if ext is None:
            raise RuleError("Unknown image.")
        return MEDIA_DIR / (f"{sha}.{ext}" if size == "full" else f"{sha}.{size}.jpg")

    # --- the web app's Library tab ---------------------------------------------------------------

    @staticmethod
    def _card(row: Any, media: list[Any]) -> dict[str, Any]:
        meta = row["meta"] or {}
        return {
            "id": row["id"], "title": row["title"], "status": row["status"], "category": row["category"],
            "category_auto": row["category_auto"], "note": meta.get("note", ""),
            "description": meta.get("description", ""), "url": meta.get("url", ""),
            "saved": row["created_at"].isoformat(timespec="seconds"), "reviewed": meta.get("reviewed_at"),
            "images": [{"sha256": m["sha256"], "width": m["width"], "height": m["height"]} for m in media],
        }

    async def browse(self, agent: Agent, status: Any = None, query: Any = None, limit: Any = 60) -> dict[str, Any]:
        """Newest references first (or by relevance when there is a query), with counts per status."""
        status = status if status in ("inbox", "reviewed") else None
        limit = max(1, min(int(limit or 60), 200))
        query = " ".join(str(query or "").split())
        ids: list[int] | None = None
        if query:
            found = await self._library.search(agent, query, "ref", None, 20)
            ids = [r["id"] for r in found["results"]]
        pool = await self._store.pool()
        async with pool.acquire() as con:
            counts = {r["status"]: r["n"] for r in await con.fetch(
                """select status, count(*) as n from lib_item where type = 'ref' and scope like any($1)
                   group by status""", agent.like_patterns)}
            rows = await con.fetch(
                """select id, title, status, category, category_auto, meta, created_at from lib_item
                   where type = 'ref' and scope like any($1) and ($2::text is null or status = $2)
                     and ($3::bigint[] is null or id = any($3::bigint[]))
                   order by created_at desc limit $4""",
                agent.like_patterns, status, ids, limit,
            )
            media = await con.fetch(
                """select item_id, sha256, width, height from lib_media where item_id = any($1)
                   order by created_at""", [r["id"] for r in rows])
        by_item: dict[int, list[Any]] = {}
        for m in media:
            by_item.setdefault(m["item_id"], []).append(m)
        cards = [self._card(r, by_item.get(r["id"], [])) for r in rows]
        if ids is not None:  # keep the search's order
            rank = {item_id: n for n, item_id in enumerate(ids)}
            cards.sort(key=lambda c: rank.get(c["id"], len(rank)))
        return {"refs": cards, "counts": {"inbox": counts.get("inbox", 0), "reviewed": counts.get("reviewed", 0)}}

    async def detail(self, agent: Agent, item_id: Any) -> dict[str, Any]:
        pool = await self._store.pool()
        async with pool.acquire() as con:
            row = await self._ref(con, agent, item_id)
            media = await con.fetch(
                "select sha256, width, height, ocr from lib_media where item_id = $1 order by created_at", row["id"])
        card = self._card(row, media)
        card["ocr"] = [m["ocr"] for m in media]
        return card
