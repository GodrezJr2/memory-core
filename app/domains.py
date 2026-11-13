"""Domains: the branches above project scopes (study, work, projects, infra, personal), from domains.json.

A scope belongs to the first domain with a matching fnmatch pattern. A scope no pattern matches takes the branch an
agent set for it (table scope_branch, memory_branch), else OTHER. Agents may also name a new branch; it is 'active'
at once and the user can turn it down later (table branch; older ones may still be 'proposed'). Pure (no database): the store reads both tables and calls overlay(),
so rules.py can turn scope='domain:<name>' into the scopes a search may read.

Agents set branches instead of an embedding match because on 13 Oct 2026 nearest-by-meaning put only 8 to 10 of the
18 hand-classified projects in their branch: every project's notes talk about servers and tools, so most drifted to
infra (stocktracker, portfolio). The agent writing the first memory knows what the project is for.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from .rules import RuleError

DOMAINS_FILE = Path(__file__).with_name("domains.json")
OTHER = "other"
CORE = "core"  # the Brain view's middle (global and agent notes), so no branch may take the name
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
MAX_TITLE, MAX_DESCRIPTION = 60, 300


class Domains:
    def __init__(self, data: dict, assigned: dict[str, str] | None = None) -> None:
        self.items = data["domains"]
        names = [d["name"] for d in self.items]
        if len(set(names)) != len(names) or OTHER in names or CORE in names:
            raise ValueError(f"domains.json: repeated or reserved domain names in {names}")
        self.names = names
        self.assigned = {s: b for s, b in (assigned or {}).items() if b in names}

    @classmethod
    def load(cls, path: Path = DOMAINS_FILE) -> "Domains":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def overlay(self, assigned: dict[str, str], extra: list[dict]) -> "Domains":
        """These rules plus the branches the user accepted (extra: name, title, description) and the branch agents set
        for scopes no rule matches. An assignment to a branch that is not active (still proposed) is ignored."""
        items = self.items + [{**d, "scopes": []} for d in extra if d["name"] not in self.names]
        return Domains({"domains": items}, assigned)

    def by_rule(self, scope: str) -> str | None:
        """The domain a domains.json pattern gives this scope, or None."""
        for d in self.items:
            if any(fnmatch.fnmatchcase(scope, p) for p in d["scopes"]):
                return d["name"]
        return None

    def of(self, scope: str) -> str | None:
        """The domain of a project scope; None for global, agent and session scopes."""
        if not scope.startswith("project:"):
            return None
        return self.by_rule(scope) or self.assigned.get(scope) or OTHER

    def members(self, name: str, present: list[str]) -> list[str] | None:
        """The scopes in present that belong to a domain; None if there is no such domain. OTHER cannot be searched
        as one branch, since it is defined by what the others leave out."""
        if name not in self.names:
            return None
        return sorted(s for s in present if self.of(s) == name)

    def describe(self) -> str:
        return ", ".join(f"{d['name']} ({d['title']})" for d in self.items)


def check_new_branch(name: str, title: str | None, description: str | None, taken: list[str]) -> tuple[str, str]:
    """Validate a branch an agent proposes; returns the cleaned (title, description)."""
    if not NAME_RE.match(name) or name in (OTHER, CORE) or name in taken:
        raise RuleError(f"A new branch needs a free name of 2-24 lowercase letters, digits or '-' (not {OTHER!r}, "
                        f"{CORE!r} or an existing branch); got {name!r}.")
    title = " ".join(str(title or "").split())
    description = " ".join(str(description or "").split())
    if not title or not description:
        raise RuleError(f"{name!r} is not a branch. Use one that exists, or create it as a new branch by passing a "
                        "title and a description of what belongs in it.")
    if len(title) > MAX_TITLE or len(description) > MAX_DESCRIPTION:
        raise RuleError(f"Keep the title within {MAX_TITLE} characters and the description within {MAX_DESCRIPTION}.")
    return title, description


DOMAINS = Domains.load()
