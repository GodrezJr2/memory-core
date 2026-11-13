from __future__ import annotations

import httpx

# Qwen3-Embedding is instruction-aware on the query side only; stored text is embedded as-is.
# The model card recommends English instructions even for multilingual data.
QUERY_TASK = "Given a question about a person's projects, servers or preferences, retrieve stored notes that answer it"
LIBRARY_TASK = "Given a task or question, retrieve the skill, code repository or project document that helps with it"


class Embedder:
    """Client for llama.cpp's OpenAI-compatible /v1/embeddings endpoint."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def query(self, text: str, task: str = QUERY_TASK) -> list[float]:
        return (await self._embed([f"Instruct: {task}\nQuery:{text}"]))[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post("/v1/embeddings", json={"input": texts, "model": "qwen3-embedding-0.6b"})
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
