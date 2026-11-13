import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "claude_code_hook", pathlib.Path(__file__).parents[1] / "clients" / "claude_code_hook.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def hit(similarity, **keywords):
    return {"similarity": similarity, "keywords": keywords}


def test_a_rare_word_is_enough():
    assert hook.relevant(hit(0.46, restay=3), corpus=155)  # ln(155/3) = 3.9


def test_a_weak_match_needs_more_than_one_rare_word():
    assert not hook.relevant(hit(0.379, request=2, per=10), corpus=160)  # 4.4 < 3.8 + 10 * 0.071; 'per' is filler
    assert hook.relevant(hit(0.379, request=2, osaka=1), corpus=160)  # 4.4 + 5.1


def test_common_words_alone_are_not():
    assert not hook.relevant(hit(0.50, laptop=30), corpus=155)  # 1.6
    assert not hook.relevant(hit(0.50, laptop=30, test=23), corpus=155)  # 1.6 + 1.9
    assert hook.relevant(hit(0.50, laptop=30, server=14), corpus=155)  # 1.6 + 2.4: two such words do count


def test_close_meaning_rescues_a_common_word():
    assert hook.relevant(hit(0.60, gpu=11), corpus=155)


def test_the_bars_hold_as_the_corpus_grows():
    # 13 Nov 2026, 2,626 memories: these passed on similarity alone and were noise
    assert not hook.relevant(hit(0.61, server=175), corpus=2626)
    assert not hook.relevant(hit(0.60, **{"2026": 1338, "app": 142}), corpus=2626)
    assert not hook.relevant(hit(0.50, foto=8), corpus=2626)  # one word, rare by count only because the corpus is big
    # and these were relevant
    assert hook.relevant(hit(0.56, ptsn=17, market=57), corpus=2626)
    assert hook.relevant(hit(0.57, pearl=25), corpus=2626)
    assert hook.relevant(hit(0.51, caveman=2, inject=10), corpus=2626)


def test_subagent_hand_backs_are_not_prompts():
    assert hook._prompt_context("http://unused", "", '<agent-message from="a1"> [Subagent hand-back] done', "s") is None


def test_the_handoff_lists_the_projects_last_episodes_only(monkeypatch):
    found = [{"id": i, "scope": s, "date": "2026-11-13", "text": f"e{i}"} for i, s in
             [(9, "project:memory-core"), (8, "global"), (7, "project:memory-core"), (6, "project:memory-core"),
              (5, "project:memory-core"), (4, "project:memory-core")]]
    monkeypatch.setattr(hook, "_call", lambda *a, **k: {"episodes": found})
    monkeypatch.setattr(hook, "_git", lambda *a: "")
    episodes, _ = hook._handoff("http://unused", "", r"D:\Project\memory-core", shown=[7])
    assert [e["id"] for e in episodes] == [9, 6, 5]  # not the global one, not one already listed, at most 3


def test_numbers_letters_and_filler_are_not_evidence():
    assert not hook.relevant(hit(0.53, **{"2": 36, "d": 20, "its": 8}), corpus=155)
    assert hook.is_evidence("9router") and hook.is_evidence("8420") and not hook.is_evidence("12")


def test_old_server_without_word_stats_keeps_the_hit():
    assert hook.relevant({"similarity": 0.4}, corpus=0)


def test_images_and_pasted_blocks_are_not_the_users_words():
    text = '[Image #2] fix this <pasted_content id="ab12"> #### Slide 1 lots of text </pasted_content id="ab12"> pls'
    assert " ".join(hook.NOT_WORDS.sub(" ", text).split()) == "fix this pls"


def _skill(id_, title):
    return {"id": id_, "type": "skill", "title": title, "summary": "", "similarity": 0.8, "matched": "both"}


def test_a_fitting_skill_is_pulled_unless_installed_or_over_the_session_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(hook.mc, "pull_skill", lambda client, ref, **kw: calls.append(ref) or {
        "status": "pulled", "title": ref, "id": kw["item"][0], "path": f"~/.claude/skills/{ref}"})
    monkeypatch.setattr(hook, "_installed_here", lambda cwd: {"pdf-extract"})
    monkeypatch.setattr(hook.mc, "pulled", lambda dest=None: [])
    lib = [_skill(1, "motion-design"), _skill(2, "animejs"), {**_skill(3, "x"), "type": "repo"}]
    assert [p["title"] for p in hook._pull_skills("b", "a", lib, "s1", None)] == ["motion-design"]  # best one only
    assert hook._pull_skills("b", "a", [_skill(4, "pdf-extract")], "s1", None) == []  # Skill tool has it
    assert hook._pull_skills("b", "a", lib, "", None) == []  # no session to tie it to
    monkeypatch.setattr(hook.mc, "pulled", lambda dest=None: [(None, {"sessions": ["s1"], "title": t})
                                                             for t in ("a", "b", "c")])
    assert hook._pull_skills("b", "a", lib, "s1", None) == [] and calls == ["motion-design"]


def test_visual_skills_are_named_not_pulled_where_the_router_is_installed(monkeypatch, tmp_path):
    lib = [{**_skill(1, "remotion"), "category": "motion"},
           {**_skill(2, "scroll-world"), "category": "other",
            "where": ["laptop:~/.claude/skills/visual/vendor/oso95__scroll-world/skills/scroll-world/SKILL.md"]},
           {**_skill(3, "pdf-extract"), "category": "documents"},
           {**_skill(4, "x"), "type": "repo", "category": "motion"}]
    monkeypatch.setattr(hook, "VISUAL_ROUTER", tmp_path / "visual" / "SKILL.md")
    assert hook._visual_routed(lib) == []  # no router here: pull as before
    (tmp_path / "visual").mkdir()
    (tmp_path / "visual" / "SKILL.md").write_text("---\nname: visual\n---\n")
    assert [r["title"] for r in hook._visual_routed(lib)] == ["remotion", "scroll-world"]


def test_the_working_folder_names_the_project_to_prefer():
    assert hook.cwd_scope(r"D:\Project\memory-core") == "project:memory-core"
    assert hook.cwd_scope("/home/user/stacks/memory-core/") == "project:memory-core"
    assert hook.cwd_scope("D:/Project/Automate Logistic") == "project:automate-logistic"
    assert hook.cwd_scope(None) is None
