"""Per-agent bearer tokens. tokens.json stores only SHA-256 hashes:

{"agents": {"claude-laptop": {"sha256": "<hex>", "scopes": ["global", "project:*", ...]}}}

Scopes default to rules.default_scopes(agent_id). Mint tokens with scripts/mint_token.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from .rules import Agent, default_scopes


class TokenBook:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime = -1.0
        self._by_hash: dict[str, Agent] = {}

    def _reload_if_changed(self) -> None:
        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return
        raw = json.loads(self._path.read_text())
        by_hash: dict[str, Agent] = {}
        for agent_id, entry in raw.get("agents", {}).items():
            scopes = tuple(entry.get("scopes") or default_scopes(agent_id))
            by_hash[entry["sha256"].lower()] = Agent(agent_id, scopes)
        self._by_hash, self._mtime = by_hash, mtime

    def agent_ids(self) -> list[str]:
        self._reload_if_changed()
        return sorted(agent.id for agent in self._by_hash.values())

    def agent_for(self, authorization: str | None) -> Agent | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        self._reload_if_changed()
        digest = hashlib.sha256(authorization[7:].strip().encode()).hexdigest()
        for known, agent in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return agent
        return None
