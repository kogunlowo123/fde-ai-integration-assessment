"""Embedding providers (Production Enhancement).

WHAT
    ``EmbeddingProvider`` plus two implementations: a deterministic local
    hashing embedder and an Ollama-backed one.

WHY a mock embedder at all
    Retrieval quality tests must be reproducible and free. A hashed
    bag-of-terms embedding is not semantically clever, it captures lexical
    overlap, not meaning, but it is deterministic, needs no model artefact,
    and is sufficient to test the parts this repository is actually
    responsible for: tenant isolation, filtering, ranking, context budgeting
    and citation correctness.

    Being explicit about that boundary matters more than pretending
    otherwise: with the mock embedder, "cancel my subscription" will not
    retrieve a passage that only says "terminate your plan". Ollama's
    ``nomic-embed-text`` will. The evaluation harness reports the mock's real
    Recall@K rather than an aspirational number.

HOW (mock)
    Tokenise to lowercase words and word bigrams, hash each term into one of
    ``dim`` buckets with a signed contribution, weight by sub-linear term
    frequency, then L2-normalise. Cosine similarity over these vectors
    approximates weighted term overlap.

WHEN
    ``RAG_EMBEDDING_PROVIDER=mock`` for CI and tests; ``ollama`` for local
    quality work.

COST
    Mock: microseconds, $0. Ollama: local compute, $0 marginal. Neither
    requires a paid API.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol, runtime_checkable

import httpx

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import RetrievalError

_TOKEN = re.compile(r"[a-z0-9]+")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into a dense vector."""

    name: str
    dim: int

    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _terms(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower())
    bigrams = [f"{a}_{b}" for a, b in itertools.pairwise(words)]
    return words + bigrams


class MockEmbeddingProvider:
    """Deterministic hashing embedder. No model, no network, no cost."""

    def __init__(self, dim: int = 256) -> None:
        self.name = "mock-embed"
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        counts: dict[str, int] = {}
        for term in _terms(text):
            counts[term] = counts.get(term, 0) + 1

        vector = [0.0] * self.dim
        for term, count in counts.items():
            digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            # Sub-linear term frequency: a word repeated ten times is not ten
            # times as informative.
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    async def embed(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class OllamaEmbeddingProvider:
    """Embeddings from a local Ollama daemon (``/api/embeddings``)."""

    def __init__(
        self,
        settings: Settings,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = "ollama-embed"
        self._model = model or settings.ollama_embedding_model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.ollama_base_url, timeout=httpx.Timeout(30.0)
        )
        # Filled in on the first successful call; Ollama models differ in width.
        self.dim = settings.rag_embedding_dim

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self._client.post(
                "/api/embeddings", json={"model": self._model, "prompt": text}
            )
        except httpx.HTTPError as exc:
            raise RetrievalError(internal_detail=f"ollama embeddings {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise RetrievalError(internal_detail=f"ollama embeddings status {response.status_code}")

        body = response.json()
        vector = body.get("embedding") if isinstance(body, dict) else None
        if not isinstance(vector, list) or not vector:
            raise RetrievalError(internal_detail="ollama returned no embedding")

        self.dim = len(vector)
        return [float(value) for value in vector]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def cosine_similarity(
    left: list[float] | tuple[float, ...], right: list[float] | tuple[float, ...]
) -> float:
    """Cosine similarity, tolerant of unnormalised inputs and length mismatch."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Select an embedding provider from configuration."""
    if settings.rag_embedding_provider == "ollama":
        return OllamaEmbeddingProvider(settings)
    return MockEmbeddingProvider(dim=settings.rag_embedding_dim)
