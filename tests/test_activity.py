from app.activity import DETAIL_CHARS, client_kind, describe, memory_ids


def test_client_kind_accepts_known_clients_only():
    assert client_kind("hook", "rest") == "hook"
    assert client_kind(" CLI ", "rest") == "cli"
    assert client_kind("evil<script>", "rest") == "rest"
    assert client_kind(None, "mcp") == "mcp"


def test_recall_shows_query_and_hit_count():
    d = describe("recall", {"query": "kenapa  server\nfreeze?", "scope": "project:homeserver"},
                 {"results": [{}, {}, {}]})
    assert d == {"scope": "project:homeserver", "detail": "kenapa server freeze?", "results": 3, "note": None}


def test_remember_shows_first_fact_and_what_was_held():
    result = {"saved": [{"id": 1}], "held_near_duplicates": [{}], "skipped_duplicates": [{}, {}]}
    d = describe("remember", {"facts": ["Port 8765 is StockTracker.", "Second fact."]}, result)
    assert d["detail"] == "Port 8765 is StockTracker. (+1 more)"
    assert d["results"] == 1
    assert d["note"] == "1 held, 2 duplicate"
    assert d["scope"] is None


def test_failed_call_has_no_results():
    assert describe("recall", {"query": "x"}, None)["results"] is None
    assert describe("forget", {"memory_id": 4, "reason": "moved"}, None) == {
        "scope": None, "detail": "#4: moved", "results": None, "note": None}


def test_detail_is_redacted_and_cut():
    d = describe("recall", {"query": "token=abc123secret " + "x" * 400}, {"results": []})
    assert "abc123secret" not in d["detail"]
    assert len(d["detail"]) == DETAIL_CHARS and d["detail"].endswith("…")


def test_memory_ids_name_what_the_brain_view_lights_up():
    assert memory_ids("recall", {}, {"results": [{"id": 3}, {"id": 9}], "linked": [{"id": 12}]}) == [3, 9, 12]
    assert memory_ids("remember", {}, {"saved": [{"id": 40}], "held_near_duplicates": [{"id": 2}]}) == [40]
    assert memory_ids("forget", {"memory_id": "7", "reason": "x"}, {"retired": 7}) == [7]
    assert memory_ids("recall", {}, None) == [] and memory_ids("profile", {}, {"profile": []}) == []


def test_profile_and_timeline():
    assert describe("profile", {}, {"profile": [1, 2]})["results"] == 2
    t = describe("timeline", {"since": "2026-09-30"}, {"episodes": [1]})
    assert t["detail"] == "since 2026-09-30" and t["results"] == 1
