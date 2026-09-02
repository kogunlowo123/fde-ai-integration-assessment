"""Retrieval (Production Enhancement).

WHAT
    ``Retriever.search`` embeds a query and returns tenant-scoped, filtered,
    context-budgeted hits.

WHY the retriever owns authorization
    The alternative, retrieve broadly, then ask the model to only use what
    the user is allowed to see, is not a control. A model instruction is a
    suggestion; a ``WHERE tenant_id = ?`` is not. Filtering happens in the
    store query, before any text enters the process, so unauthorised content
    is never a candidate for the prompt.

HOW
    Embed -> store search with the filter -> drop hits below the score floor
    -> de-duplicate by document -> truncate to the character budget.

WHEN
    Called by the RAG pipeline and by the ``search_knowledge_base`` MCP tool.
    Both go through this one path.

COST
    ``top_k`` and the character budget are the two knobs that decide how much
    of the prompt (and therefore of the bill and the latency) retrieval
    consumes. Both are capped by configuration, not by the caller.
"""

from __future__ import annotations

import time

from fde_assessment.common.logging import get_logger
from fde_assessment.observability.metrics import (
    METRICS,
    RAG_DOCUMENTS_RETRIEVED,
    RAG_EMBEDDING_LATENCY_MS,
    RAG_QUERIES_TOTAL,
    RAG_RETRIEVAL_EMPTY_TOTAL,
    RAG_RETRIEVAL_LATENCY_MS,
)
from fde_assessment.rag.embeddings import EmbeddingProvider
from fde_assessment.rag.models import RetrievalFilter, RetrievalHit
from fde_assessment.rag.vector_store import VectorStore

log = get_logger(__name__)

#: Hits below this cosine score are noise rather than weak evidence. Returning
#: them would spend context budget and widen the injection surface for nothing.
MIN_SCORE = 0.05


class Retriever:
    """Query-time retrieval with mandatory tenant scoping."""

    def __init__(
        self,
        store: VectorStore,
        embedder: EmbeddingProvider,
        max_top_k: int = 10,
        max_context_chars: int = 6_000,
        min_score: float = MIN_SCORE,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._max_top_k = max_top_k
        self._max_context_chars = max_context_chars
        self._min_score = min_score

    async def search(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 4,
        document_type: str | None = None,
        classification: str | None = None,
    ) -> list[RetrievalHit]:
        """Return ranked, filtered, budgeted hits for ``query``."""
        if not tenant_id:
            # Defence in depth: the type system already requires a tenant, and
            # so does this.
            raise ValueError("tenant_id is required for retrieval")

        effective_k = max(1, min(top_k, self._max_top_k))
        METRICS.increment(RAG_QUERIES_TOTAL, tenant=tenant_id)

        embed_started = time.perf_counter()
        embedding = await self._embedder.embed(query)
        METRICS.observe(
            RAG_EMBEDDING_LATENCY_MS,
            (time.perf_counter() - embed_started) * 1000.0,
            provider=self._embedder.name,
        )

        search_started = time.perf_counter()
        filters = RetrievalFilter(
            tenant_id=tenant_id, document_type=document_type, classification=classification
        )
        # Over-fetch a little so de-duplication does not starve the result set.
        raw_hits = await self._store.search(embedding, filters, effective_k * 3)
        METRICS.observe(RAG_RETRIEVAL_LATENCY_MS, (time.perf_counter() - search_started) * 1000.0)

        selected: list[RetrievalHit] = []
        seen_documents: set[str] = set()
        budget = self._max_context_chars

        for hit in raw_hits:
            if hit.score < self._min_score:
                continue
            if hit.chunk.document_id in seen_documents and len(selected) >= effective_k // 2:
                # Prefer breadth: several documents beat several chunks of one.
                continue
            if len(hit.chunk.text) > budget:
                continue
            selected.append(hit)
            seen_documents.add(hit.chunk.document_id)
            budget -= len(hit.chunk.text)
            if len(selected) >= effective_k:
                break

        METRICS.increment(RAG_DOCUMENTS_RETRIEVED, len(selected), tenant=tenant_id)
        if not selected:
            METRICS.increment(RAG_RETRIEVAL_EMPTY_TOTAL, tenant=tenant_id)

        log.info(
            "rag_retrieval",
            tenant=tenant_id,
            requested_top_k=top_k,
            effective_top_k=effective_k,
            hits=len(selected),
            # Identifiers and scores only, never the query or the passages.
            document_ids=[hit.chunk.document_id for hit in selected],
            top_score=round(selected[0].score, 4) if selected else 0.0,
        )
        return selected
