from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def _secret(name: str) -> str:
    """Docker secret file first (/run/secrets/<name>), then the environment variable of the same name."""
    path = Path("/run/secrets") / name
    if path.exists():
        return path.read_text().strip()
    return os.environ.get(name.upper(), "")


@dataclass(frozen=True)
class Settings:
    database_url: str
    embed_url: str
    tokens_file: Path
    # Cosine similarity bands, calibrated on Qwen3-Embedding-0.6B (30 Sep 2026): paraphrases ~0.98,
    # an update of the same fact ~0.76, unrelated facts on the same server <= 0.58.
    near_duplicate: float  # at or above: new fact is held for the agent to decide
    related: float  # between this and near_duplicate: saved, with the older memory returned to check
    candidates: int  # rows taken from each of BM25 and vector search before fusion
    # Tailscale logins allowed to open /live. Tailscale serve adds Tailscale-User-Login to proxied requests.
    live_viewers: frozenset[str]


def load() -> Settings:
    password = quote(_secret("pg_password"), safe="")
    host = os.environ.get("PG_HOST", "postgres")
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or f"postgresql://memory:{password}@{host}:5432/memory",
        embed_url=os.environ.get("EMBED_URL", "http://embedder:8421").rstrip("/"),
        tokens_file=Path(os.environ.get("TOKENS_FILE", "/data/tokens.json")),
        near_duplicate=float(os.environ.get("NEAR_DUPLICATE", "0.92")),
        related=float(os.environ.get("RELATED", "0.70")),
        candidates=int(os.environ.get("CANDIDATES", "50")),
        live_viewers=frozenset(v.strip() for v in os.environ.get("LIVE_VIEWERS", "").split(",") if v.strip()),
    )
