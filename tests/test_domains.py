import importlib.util
import pathlib

import pytest

from app.domains import CORE, DOMAINS, OTHER, Domains, check_new_branch
from app.rules import Agent, RuleError, default_scopes, domain_filter, domain_name

_spec = importlib.util.spec_from_file_location(
    "claude_code_hook", pathlib.Path(__file__).parents[1] / "clients" / "claude_code_hook.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# The project scopes with memories on 8 Oct 2026, and the branch each belongs to
EXPECTED = {
    "project:course-ml-lab": "study", "project:lms-automation": "study",
    "project:course-film": "study", "project:job-hunt": "work", "project:car-api": "projects",
    "project:stocktracker": "projects", "project:bank-categorizer": "projects", "project:browser-game": "projects",
    "project:homeserver": "infra", "project:memory-core": "infra", "project:opensense": "infra",
    "project:opendesign": "infra", "project:trip-2027": "personal", "project:racing-setups": "personal",
    "project:dashcam-a410": "personal", "project:antivirus-check": "personal",
}


def test_every_current_project_has_its_branch():
    assert {s: DOMAINS.of(s) for s in EXPECTED} == EXPECTED


def test_new_course_scopes_land_in_study_and_unknown_ones_in_other():
    assert DOMAINS.of("project:course-audit") == "study"
    assert DOMAINS.of("project:island-trip") == OTHER  # 'is' alone is not a course code
    assert DOMAINS.of("project:something-new") == OTHER
    assert DOMAINS.of("global") is None and DOMAINS.of("agent:x") is None


def test_domain_filter_keeps_existing_scopes_of_the_branch_plus_global_and_own_notes():
    agent = Agent("claude-laptop", default_scopes("claude-laptop"))
    present = ["project:course-ml-lab", "project:job-hunt", "project:course-film", "global"]
    assert domain_filter(agent, "study", present) == [
        "project:course-film", r"project:course-ml-lab", "global", "agent:claude-laptop"]
    assert domain_filter(agent, "work", present)[0] == "project:job-hunt"
    with pytest.raises(RuleError, match="Unknown domain"):
        domain_filter(agent, "hobby", present)
    assert domain_name("domain:Study") == "study" and domain_name("project:x") is None and domain_name(None) is None


def test_domain_names_must_be_unique():
    with pytest.raises(ValueError):
        Domains({"domains": [{"name": "a", "scopes": []}, {"name": "a", "scopes": []}]})
    with pytest.raises(ValueError):
        Domains({"domains": [{"name": OTHER, "scopes": []}]})


def test_session_start_groups_projects_by_branch():
    projects = [{"scope": "project:memory-core", "domain": "infra", "memories": 30},
                {"scope": "project:course-ml-lab", "domain": "study", "memories": 30},
                {"scope": "project:homeserver", "domain": "infra", "memories": 22}]
    line = hook._projects_line(projects)
    assert line.endswith("infra: memory-core (30), homeserver (22); study: course-ml-lab (30)")
    assert hook._projects_line([{"scope": "project:x", "memories": 1}]) == "Projects with memories: x (1)"


def test_agents_file_scopes_no_pattern_matches_but_never_override_domains_json():
    domains = DOMAINS.overlay({"project:argos-proxmox": "infra", "project:job-hunt": "personal"}, [])
    assert domains.of("project:argos-proxmox") == "infra"
    assert domains.of("project:job-hunt") == "work"  # the pattern wins
    assert domains.of("project:local-llm") == OTHER
    assert DOMAINS.of("project:argos-proxmox") == OTHER  # the overlay is a new object; the rules stay as loaded


def test_a_proposed_branch_counts_only_once_jay_accepts_it():
    hobby = {"name": "hobby", "title": "Hobby", "description": "Games and sim racing."}
    waiting = DOMAINS.overlay({"project:fleet-app": "hobby"}, [])
    assert waiting.of("project:fleet-app") == OTHER and "hobby" not in waiting.names
    accepted = DOMAINS.overlay({"project:fleet-app": "hobby"}, [hobby])
    assert accepted.of("project:fleet-app") == "hobby" and accepted.names[-1] == "hobby"
    assert accepted.members("hobby", ["project:fleet-app", "project:job-hunt"]) == ["project:fleet-app"]
    assert DOMAINS.overlay({}, [{**hobby, "name": "work"}]).names == DOMAINS.names  # domains.json keeps its names


def test_domain_filter_reads_branches_agents_set():
    agent = Agent("claude-laptop", default_scopes("claude-laptop"))
    domains = DOMAINS.overlay({"project:argos-proxmox": "infra"}, [])
    present = ["project:argos-proxmox", "project:homeserver", "project:job-hunt"]
    assert domain_filter(agent, "infra", present, domains)[:2] == ["project:argos-proxmox", "project:homeserver"]
    assert domain_filter(agent, "infra", present)[:1] == ["project:homeserver"]


def test_new_branch_names_and_texts_are_checked():
    assert check_new_branch("hobby", " Hobby  and games ", "Games.", DOMAINS.names) == ("Hobby and games", "Games.")
    for bad in ("Hobby", "h", "hobby game", OTHER, CORE, "work", "-x"):
        with pytest.raises(RuleError, match="free name"):
            check_new_branch(bad, "T", "D", DOMAINS.names)
    with pytest.raises(RuleError, match="title and a description"):
        check_new_branch("hobby", "Hobby", "", DOMAINS.names)
    with pytest.raises(RuleError, match="within"):
        check_new_branch("hobby", "x" * 61, "D", DOMAINS.names)


def test_session_start_names_branches_waiting_for_jay():
    line = hook._proposals_line([{"name": "hobby", "title": "Hobby", "projects": ["fleet-app", "racing-setups"]}])
    assert line.endswith("hobby (Hobby): fleet-app, racing-setups")
