"""Pure rules: validation, scopes, redaction, ranking. No I/O, so it is unit-tested directly."""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

KINDS = ("core", "fact", "episode", "procedure", "doc_chunk")
MAX_CHARS = {"core": 4000, "fact": 500, "episode": 1200, "procedure": 2000, "doc_chunk": 4000}
MAX_FACTS_PER_CALL = 20
SCOPE_RE = re.compile(r"^(global|(project|agent|session):[a-z0-9][a-z0-9._-]{0,63})$")


class RuleError(ValueError):
    """Input the caller can fix; the message is shown to the agent as-is."""


@dataclass(frozen=True)
class Agent:
    id: str
    scopes: tuple[str, ...]  # fnmatch patterns this agent may read and write

    def can_access(self, scope: str) -> bool:
        return any(fnmatch.fnmatchcase(scope, p) for p in self.scopes)

    @property
    def like_patterns(self) -> list[str]:
        return [to_like(p) for p in self.scopes]


def default_scopes(agent_id: str) -> tuple[str, ...]:
    return ("global", "project:*", "session:*", f"agent:{agent_id}")


def to_like(pattern: str) -> str:
    """fnmatch pattern -> SQL LIKE pattern (only '*' is used in scope patterns)."""
    escaped = pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return escaped.replace("*", "%")


def check_scope(scope: str) -> str:
    scope = scope.strip().lower()
    if not SCOPE_RE.match(scope):
        raise RuleError(
            f"Invalid scope {scope!r}. Use 'global', 'project:<name>', 'agent:<name>' or 'session:<name>' "
            "(lowercase letters, digits, '.', '_' or '-')."
        )
    return scope


def check_kind(kind: str) -> str:
    kind = kind.strip().lower()
    if kind not in KINDS:
        raise RuleError(f"Invalid kind {kind!r}. Use one of: {', '.join(KINDS)}.")
    return kind


def clean_facts(facts: list[str], kind: str) -> list[str]:
    if not facts:
        raise RuleError("Pass at least one fact.")
    if len(facts) > MAX_FACTS_PER_CALL:
        raise RuleError(f"At most {MAX_FACTS_PER_CALL} facts per call; split the rest into another call.")
    limit = MAX_CHARS[kind]
    cleaned: list[str] = []
    for i, fact in enumerate(facts, 1):
        text = " ".join(str(fact).split())
        if not text:
            raise RuleError(f"Fact {i} is empty.")
        if len(text) > limit:
            raise RuleError(
                f"Fact {i} has {len(text)} characters; a {kind} may have {limit}. "
                "Split it into shorter standalone statements."
            )
        cleaned.append(text)
    return cleaned


def domain_name(requested: str | None) -> str | None:
    """'domain:study' -> 'study'; None for any other scope request."""
    value = (requested or "").strip().lower()
    return value.split(":", 1)[1] if value.startswith("domain:") else None


def domain_filter(agent: Agent, name: str, present: list[str], domains: Any = None) -> list[str]:
    """LIKE patterns for one branch: the existing scopes in that domain, plus global and the agent's own notes
    (as a project scope search does). `present` is every scope that has memories or library items; `domains` is
    the store's overlay of domains.json with the branches agents set (domains.json alone by default)."""
    if domains is None:
        from .domains import DOMAINS as domains

    scopes = domains.members(name, present)
    if scopes is None:
        raise RuleError(f"Unknown domain {name!r}. Use one of: {', '.join(domains.names)}.")
    return [to_like(s) for s in scopes] + ["global", to_like(f"agent:{agent.id}")]


def read_filter(agent: Agent, requested: str | None) -> list[str]:
    """LIKE patterns for the scopes a recall may search, before the agent's own permissions apply.

    No scope (or 'all') searches everything the agent can see. A project or session scope
    narrows the search to that scope plus global and the agent's private notes.
    """
    if requested is None or requested.strip().lower() in ("", "all"):
        return ["%"]
    scope = check_scope(requested)
    if scope == "global" or scope.startswith("agent:"):
        return [to_like(scope)]
    return [to_like(scope), "global", to_like(f"agent:{agent.id}")]


# --- redaction -----------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("api-key", re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("password", re.compile(r"(?i)\b(pass(?:word)?|passwd|pwd|secret|token)\s*[:=]\s*\S+")),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("account-number", re.compile(r"(?<![\d.:-])\d{10,16}(?![\d.:-])")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Replace secrets and account-like numbers with [REDACTED:<type>]. Returns the text and the types found."""
    found: list[str] = []
    for name, pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{name}]", text)
        found.extend([name] * n)
    return text, found


# --- ranking -------------------------------------------------------------------------------

STOPWORDS = frozenset(
    """a an and are as at be by do does did for from how i in is it my of on or that the this to was
    were what which who why with you your we our me
    ada aku akan apa atau bisa dan dari dengan di itu ini juga ke kamu saya sudah tidak untuk yang
    apakah bagaimana berapa bukan buat deh dong gak gimana harus jadi kapan kenapa kok lagi mana
    mengapa mau nggak perlu pakai pake sering sih siapa ya
    again already alr also am any can check could did do done get got help just let lets like make maybe
    no not now ok okay please pls really should so still sure thanks thx then there they too try very
    want will would yes yeah yep work working works
    aja bang banget belum bro coba cuma dulu gitu kalau kalo kan lanjut masih nih oke sama tapi terus
    tolong udah""".split()
)
# The second block is conversational filler. Measured 30 Sep 2026: vague prompts such as
# "is this alr working or no?" scored 0.47 similarity, as high as real questions, and "no" matched
# "no CUDA GPU". A keyword match is only evidence of relevance once filler cannot produce one.
RRF_K = 60
# Keyword hits exist to catch exact identifiers (ports, names, paths). On natural-language questions
# they are noisier than meaning, so they count half. Seen 30 Sep 2026: "kenapa server sering freeze?"
# ranked a Minecraft note ("server" twice) above the freeze fact; prefix matching and Indonesian
# stopwords fix the miss, and this weight keeps a noisy word match from outranking meaning.
KEYWORD_WEIGHT = 0.5
VECTOR_WEIGHT = 1.0


def rrf(*ranked_lists: list[int], weights: tuple[float, ...] | None = None, k: int = RRF_K) -> dict[int, float]:
    """Weighted Reciprocal Rank Fusion: score(id) = sum over lists of weight / (k + rank), rank starting at 1."""
    weights = weights or (1.0,) * len(ranked_lists)
    scores: dict[int, float] = {}
    for ranked, weight in zip(ranked_lists, weights, strict=True):
        for rank, item in enumerate(ranked, 1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    return scores


# --- memory recall score ---------------------------------------------------------------------
# Since 13 Nov 2026 memory recall no longer fuses ranks. At 2,626 memories the keyword list was full of common-word
# matches, and RRF puts anything found in both lists above the best meaning-only hit: hit@1 was 0.48 against 0.61
# for meaning alone. The score is now cosine similarity plus the share of the query's word rarity a memory
# contains (0 to 1), which gave hit@1 0.72, hit@5 0.95 and MRR 0.81 on the same 102 questions, flat for weights
# 0.10 to 0.30. library_search still uses rrf above; it was not measured.
KEYWORD_SHARE_WEIGHT = 0.15
# Nudges on the cosine scale, where neighbouring results sit about 0.01 apart: they only reorder near-ties.
VERIFIED_BOOST = 0.003
KIND_BOOST = {"core": 0.0015, "procedure": 0.00075}
# A memory from the project the agent is working in (the hook names it from its cwd) moves up among neighbours.
SCOPE_BOOST = 0.02


def word_idf(corpus: int, df: int) -> float:
    """How rare a word is: BM25's idf for a word found in df of corpus memories."""
    return math.log(1 + (corpus - df + 0.5) / (df + 0.5))


def keyword_share(matched: Iterable[str], idf: dict[str, float]) -> float:
    """The share of the query's rarity that the matched words carry: 1.0 when a memory has every query word,
    near 0 when it has only the common ones. idf holds every query word found in some memory."""
    total = sum(idf.values())
    return sum(idf.get(w, 0.0) for w in set(matched)) / total if total else 0.0


def final_score(similarity: float, share: float, kind: str, verified: bool, preferred: bool = False) -> float:
    return (similarity + KEYWORD_SHARE_WEIGHT * share + (VERIFIED_BOOST if verified else 0.0)
            + KIND_BOOST.get(kind, 0.0) + (SCOPE_BOOST if preferred else 0.0))
