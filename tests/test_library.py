import pytest

from app.activity import describe
from app.library import (
    CHUNK_CHARS,
    MAX_CHUNKS,
    check_origin,
    check_type,
    chunk_contents,
    chunk_text,
    clean_item,
    item_hash,
    snippet,
)
from app.rules import RuleError


def _item(**over):
    raw = {"title": "tdd", "summary": "Test-driven development.", "scope": "global", "body": "# TDD\n\nRed, green."}
    raw.update(over)
    raw["hash"] = item_hash("skill", raw["title"], raw["summary"], raw["scope"], raw["body"])
    return raw


def test_hash_is_stable_and_covers_every_field():
    a = item_hash("skill", "t", "s", "global", "b")
    assert a == item_hash("skill", "t", "s", "global", "b") and len(a) == 64
    assert len({a, item_hash("doc", "t", "s", "global", "b"), item_hash("skill", "t", "s", "project:x", "b"),
                item_hash("skill", "t", "s", "global", "b2")}) == 4


def test_clean_item_checks_hash_and_redacts():
    item = clean_item("skill", _item(body="Use token: abcdef123456 to log in."))
    assert "abcdef123456" not in item["body"] and "[REDACTED:password]" in item["body"]
    bad = _item()
    bad["body"] += " changed"
    with pytest.raises(RuleError, match="hash"):
        clean_item("skill", bad)
    with pytest.raises(RuleError, match="scope"):
        clean_item("skill", _item(scope="Not A Scope"))


def test_type_and_origin_validation():
    assert check_type(" Skill ") == "skill"
    assert check_origin("PREDATOR") == "predator"
    with pytest.raises(RuleError):
        check_type("link")
    with pytest.raises(RuleError):
        check_origin("../etc")


def test_chunks_skip_frontmatter_and_respect_limits():
    body = "---\nname: x\ndescription: y\n---\n# Title\n\nIntro.\n\n" + "\n\n".join(
        f"## Part {i}\n\n" + "word " * 150 for i in range(20))
    chunks = chunk_text(body)
    assert not chunks[0].startswith("---") and chunks[0].startswith("# Title")
    assert len(chunks) == MAX_CHUNKS
    assert all(len(c) <= CHUNK_CHARS for c in chunks)


def test_one_long_line_is_cut():
    chunks = chunk_text("x" * (CHUNK_CHARS * 3))
    assert len(chunks) == 3 and all(len(c) <= CHUNK_CHARS for c in chunks)


def test_contents_name_the_item_and_empty_body_still_indexes():
    item = clean_item("skill", _item())
    contents = chunk_contents(item)
    assert contents[0].startswith("skill tdd: Test-driven development.\n\n# TDD")
    repo = {"type": "repo", "title": "a/b", "summary": "", "body": ""}
    assert chunk_contents(repo) == ["repo a/b"]
    assert snippet(contents[0]).startswith("# TDD")


def test_activity_describes_library_calls():
    d = describe("library_search", {"query": "landing page", "type": "skill"}, {"results": [{}, {}]})
    assert d["detail"] == "[skill] landing page" and d["results"] == 2
    d = describe("library_sync", {"type": "skill", "origin": "predator", "manifest": [{}] * 3},
                 {"need": ["a"], "removed": 2})
    assert d == {"scope": None, "detail": "skill from predator: 3 found", "results": 1, "note": "2 removed"}
    d = describe("library_upsert", {"items": [{"title": "tdd"}, {"title": "x"}]}, {"saved": [{}], "unchanged": 1})
    assert d["detail"] == "tdd (+1 more)" and d["note"] == "1 unchanged"
    assert describe("library_get", {"id": "7"}, {"title": "tdd"})["detail"] == "#7 tdd"
