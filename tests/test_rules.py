import pytest

from app.rules import (
    Agent,
    RuleError,
    check_scope,
    clean_facts,
    default_scopes,
    final_score,
    keyword_share,
    read_filter,
    redact,
    rrf,
    to_like,
    word_idf,
)

codex = Agent("codex-server", default_scopes("codex-server"))


class TestScopes:
    def test_valid_scopes_are_normalised(self):
        assert check_scope(" Project:StockTracker ") == "project:stocktracker"
        assert check_scope("global") == "global"

    @pytest.mark.parametrize("bad", ["", "projects:x", "project:", "project:has space", "agent:x;drop"])
    def test_invalid_scopes_are_rejected(self, bad):
        with pytest.raises(RuleError):
            check_scope(bad)

    def test_agent_can_access_own_but_not_other_agent_scope(self):
        assert codex.can_access("agent:codex-server")
        assert codex.can_access("project:tracker")
        assert not codex.can_access("agent:claude-laptop")

    def test_admin_star_reaches_everything(self):
        assert Agent("owner", ("*",)).can_access("agent:claude-laptop")

    def test_like_translation_escapes_underscore(self):
        assert to_like("project:*") == "project:%"
        assert to_like("agent:my_bot") == r"agent:my\_bot"

    def test_project_recall_includes_global_and_own_notes(self):
        assert read_filter(codex, "project:tracker") == ["project:tracker", "global", "agent:codex-server"]

    def test_no_scope_means_everything_visible(self):
        assert read_filter(codex, None) == ["%"]
        assert read_filter(codex, "all") == ["%"]


class TestFacts:
    def test_whitespace_collapsed(self):
        assert clean_facts(["  StockTracker   runs\n on :8765 "], "fact") == ["StockTracker runs on :8765"]

    def test_too_long_fact_names_the_limit(self):
        with pytest.raises(RuleError, match="500"):
            clean_facts(["x" * 501], "fact")

    def test_empty_list_rejected(self):
        with pytest.raises(RuleError):
            clean_facts([], "fact")

    def test_episode_allows_longer_text(self):
        assert clean_facts(["x" * 1000], "episode")


class TestRedaction:
    @pytest.mark.parametrize(
        "text,kind",
        [
            ("key is sk-e0bAbCdEfGhIjKlMnOpQrStUv", "api-key"),
            ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "github-token"),
            ("password: Lr7secret@", "password"),
            ("BCA rekening 1234567890", "account-number"),
            ("Authorization: Bearer abcdefghijklmnop123456", "bearer"),
        ],
    )
    def test_secrets_are_replaced(self, text, kind):
        out, found = redact(text)
        assert kind in found
        assert f"[REDACTED:{kind}]" in out

    @pytest.mark.parametrize(
        "text",
        [
            "StockTracker runs on port 8765",
            "Tailscale IP 100.64.0.1",
            "Deployed on 2026-09-30 at 14:30",
            "Swap 5.1 / 8.0 GiB",
        ],
    )
    def test_ordinary_facts_untouched(self, text):
        assert redact(text) == (text, [])


class TestRanking:
    def test_item_in_both_lists_beats_item_in_one(self):
        scores = rrf([1, 2, 3], [3, 4, 5])
        assert max(scores, key=scores.get) == 3

    def test_rrf_values(self):
        assert rrf([7])[7] == pytest.approx(1 / 61)

    def test_keyword_weight_widens_meanings_lead(self):
        # 30 Sep case: keyword list led by a noisy match (23), vector list led by the answer (20)
        keyword, vector = [23, 33, 20], [20, 34, 22, 23]
        equal, weighted = rrf(keyword, vector), rrf(keyword, vector, weights=(0.5, 1.0))
        assert max(weighted, key=weighted.get) == 20
        assert weighted[20] - weighted[23] > equal[20] - equal[23]

    def test_weights_must_match_lists(self):
        with pytest.raises(ValueError):
            rrf([1], [2], weights=(1.0,))

    def test_a_rare_word_weighs_more_than_a_common_one(self):
        assert word_idf(2626, 3) > word_idf(2626, 175) > word_idf(2626, 1338) > 0

    def test_keyword_share_is_the_part_of_the_querys_rarity_a_memory_holds(self):
        idf = {"ptsn": word_idf(2626, 17), "server": word_idf(2626, 175)}
        assert keyword_share(["ptsn", "server"], idf) == pytest.approx(1.0)
        assert keyword_share(["ptsn"], idf) > 0.5 > keyword_share(["server"], idf) > 0
        assert keyword_share(["ptsn", "ptsn"], idf) == keyword_share(["ptsn"], idf)
        assert keyword_share([], idf) == 0 and keyword_share(["ptsn"], {}) == 0

    def test_a_common_word_does_not_outrank_closer_meaning(self):
        # 13 Nov case: rank fusion put any memory sharing a word above the closest one that shared none
        idf = {"freeze": word_idf(2626, 4), "server": word_idf(2626, 175)}
        answer = final_score(0.62, keyword_share(["freeze"], idf), "fact", False)
        noisy = final_score(0.55, keyword_share(["server"], idf), "fact", False)
        assert answer > noisy
        assert final_score(0.62, 0.0, "fact", False) > noisy  # even with no shared word at all

    def test_the_rare_words_lift_a_memory_past_slightly_closer_ones(self):
        assert final_score(0.55, 1.0, "fact", False) > final_score(0.62, 0.0, "fact", False)

    def test_verified_breaks_a_tie_only(self):
        assert final_score(0.60, 0.5, "fact", True) > final_score(0.60, 0.5, "fact", False)
        assert final_score(0.61, 0.5, "fact", False) > final_score(0.60, 0.5, "fact", True)

    def test_current_project_moves_up_among_neighbours_only(self):
        assert final_score(0.585, 0.0, "fact", False, True) > final_score(0.60, 0.0, "fact", False)
        assert final_score(0.60, 0.0, "fact", False) > final_score(0.57, 0.0, "fact", False, True)
