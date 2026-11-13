import base64
import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image, ImageStat

from app.activity import describe
from app.categories import load
from app.refs import (
    VIEW_EDGE,
    clean_ocr,
    decode_image,
    default_title,
    normalize_url,
    ocr_input,
    prepare_image,
    ref_body,
    ref_hash,
    summary_of,
)
from app.rules import RuleError


def png(width, height, color):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


def test_share_links_lose_tracking_and_aliases():
    assert normalize_url("https://twitter.com/someone/status/123?s=20&t=abc") == "https://x.com/someone/status/123"
    assert normalize_url("https://www.threads.net/@dev/post/C9x/?igsh=xyz") == "https://threads.net/@dev/post/C9x"
    assert normalize_url("http://Example.com/a/?utm_source=x&id=7") == "https://example.com/a?id=7"
    assert normalize_url("") == ""
    with pytest.raises(RuleError):
        normalize_url("javascript:alert(1)")


def test_ref_identity_prefers_the_link():
    assert ref_hash("https://x.com/a/status/1", "f" * 64) == ref_hash("https://x.com/a/status/1", None)
    assert ref_hash("", "a" * 64) != ref_hash("", "b" * 64)
    assert ref_hash("", "a" * 64, "share-1") == ref_hash("", "b" * 64, "share-1")  # one share, several images


def test_default_titles():
    when = datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc)
    assert default_title("Claude Code workflow with hooks. More text", "", when) == "Claude Code workflow with hooks"
    assert default_title("", "https://x.com/dev/status/1", when) == "x.com/dev/status/1"
    assert default_title("", "", when) == "Screenshot 04 Oct 2026 09:30"


def test_summaries_end_on_a_sentence():
    text = "First sentence here. " * 20
    assert summary_of(text).endswith("here.") and len(summary_of(text)) <= 300
    assert summary_of("short one.") == "short one."
    assert summary_of("word " * 100).endswith("…")


def test_ocr_cleanup_drops_debris():
    assert clean_ocr("  Step 1:  install   uv \n|\n~ .\n\nRun it  ") == "Step 1: install uv\nRun it"


def test_body_puts_the_review_first():
    body = ref_body("why: nice", "A Claude Code hook setup", "https://x.com/a/status/1", ["text one", ""])
    assert body.startswith("Description: A Claude Code hook setup")
    assert "The user's note: why: nice" in body and "Text in image 1:\ntext one" in body and "image 2" not in body


def test_images_are_checked_and_scaled_for_agents():
    data = png(3000, 1000, "white")
    img = prepare_image(data)
    assert img["ext"] == "png" and (img["width"], img["height"]) == (3000, 1000) and len(img["sha256"]) == 64
    view = Image.open(io.BytesIO(img["view"]))
    assert view.format == "JPEG" and max(view.size) == VIEW_EDGE
    assert Image.open(io.BytesIO(img["thumb"])).size[0] == 480
    assert decode_image(base64.b64encode(data).decode()) == data
    assert decode_image("data:image/png;base64," + base64.b64encode(data).decode()) == data
    with pytest.raises(RuleError):
        prepare_image(b"not an image")
    with pytest.raises(RuleError):
        decode_image("%%%")


def test_dark_screenshots_are_inverted_for_ocr():
    dark = Image.open(io.BytesIO(ocr_input(png(400, 200, (20, 20, 20)))))
    assert dark.size == (800, 400) and ImageStat.Stat(dark).mean[0] > 200
    light = Image.open(io.BytesIO(ocr_input(png(1200, 200, (250, 250, 250)))))
    assert light.size == (1200, 200) and ImageStat.Stat(light).mean[0] > 200


def test_category_file_is_consistent(tmp_path):
    data = load()
    names = [c["name"] for c in data["categories"]]
    assert "web-design" in names and "project-docs" in names
    assert set(data["skills"].values()) <= set(names)
    assert set(data["hidden"]) <= set(data["skills"])
    bad = dict(data, skills={"x": "no-such-category"})
    (tmp_path / "c.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load(tmp_path / "c.json")


def test_activity_describes_capture_and_review():
    d = describe("capture_ref", {"note": "nice workflow", "image": True},
                 {"images": 2, "merged": True, "category": "agents"})
    assert d["detail"] == "nice workflow" and d["results"] == 2 and d["note"] == "added to existing"
    d = describe("library_review", {"item_id": 4, "title": "Hook setup"}, {"category": "agents"})
    assert d["detail"] == "#4 Hook setup" and d["note"] == "agents"
    assert describe("library_view", {"item_id": 4}, ({"title": "Hook setup"}, b""))["detail"] == "#4 Hook setup"
