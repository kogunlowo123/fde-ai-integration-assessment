"""RAG service wiring (Production Enhancement).

WHAT
    ``RagService`` composes embedder, store, retriever and prompt builder, and
    exposes the two entry points the rest of the system uses:

    * ``augment``, rewrite an LLM gateway request with retrieved context.
    * ``search``, the tenant-scoped search behind the ``search_knowledge_base``
      MCP tool.

WHY one service
    Both entry points must apply the same tenant scope, the same caps and the
    same trust boundary. Two code paths would eventually disagree, and the
    disagreement would be a security bug.

WHEN
    Constructed at gateway startup, or lazily by the MCP server when a corpus
    directory is configured.

SECURITY
    ``tenant_id`` is always supplied by the caller's authenticated identity,
    never by the request body, and never by the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import RetrievalError
from fde_assessment.common.logging import get_logger
from fde_assessment.common.models import ChatCompletionRequest, ChatMessage
from fde_assessment.observability.metrics import (
    METRICS,
    RAG_CONTEXT_CHARS,
    RAG_RETRIEVAL_ERRORS_TOTAL,
)
from fde_assessment.persistence.sqlite import Database
from fde_assessment.rag.embeddings import EmbeddingProvider, build_embedding_provider
from fde_assessment.rag.ingestion import IngestionPipeline
from fde_assessment.rag.models import RetrievalHit
from fde_assessment.rag.pipeline import SYSTEM_PROMPT, build_context_block, citations
from fde_assessment.rag.retriever import Retriever
from fde_assessment.rag.vector_store import SqliteVectorStore

log = get_logger(__name__)


class RagService:
    """Retrieval-augmented generation for the gateway and the MCP tool."""

    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        ingestion: IngestionPipeline,
        store: SqliteVectorStore,
    ) -> None:
        self._settings = settings
        self._retriever = retriever
        self._ingestion = ingestion
        self._store = store

    @property
    def ingestion_pipeline(self) -> IngestionPipeline:
        return self._ingestion

    @property
    def store(self) -> SqliteVectorStore:
        return self._store

    async def search(
        self,
        tenant_id: str,
        query: str,
        top_k: int | None = None,
        document_type: str | None = None,
    ) -> list[RetrievalHit]:
        return await self._retriever.search(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k or self._settings.rag_default_top_k,
            document_type=document_type,
        )

    async def augment(
        self, request: ChatCompletionRequest, tenant_id: str, request_id: str
    ) -> tuple[ChatCompletionRequest, list[dict[str, Any]]]:
        """Return ``(rewritten request, citations)``.

        The retrieval query defaults to the last user message. A retrieval
        failure is surfaced rather than silently answered from the model's own
        knowledge: a RAG answer with no retrieval is a different, less
        trustworthy product than the caller asked for.
        """
        options = request.rag
        if options is None:
            raise RetrievalError(internal_detail="augment called without rag options")

        query = options.query or _last_user_message(request)
        if not query:
            raise RetrievalError(internal_detail="no retrieval query available")

        try:
            hits = await self._retriever.search(
                tenant_id=tenant_id,
                query=query,
                top_k=options.top_k or self._settings.rag_default_top_k,
                document_type=options.document_type,
                classification=options.classification,
            )
        except RetrievalError:
            METRICS.increment(RAG_RETRIEVAL_ERRORS_TOTAL, tenant=tenant_id)
            raise
        except Exception as exc:
            METRICS.increment(RAG_RETRIEVAL_ERRORS_TOTAL, tenant=tenant_id)
            raise RetrievalError(internal_detail=type(exc).__name__) from exc

        context = build_context_block(hits, self._settings.rag_max_context_chars)
        METRICS.increment(RAG_CONTEXT_CHARS, len(context), tenant=tenant_id)

        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="system", content=context),
            *request.messages,
        ]
        rewritten = request.model_copy(update={"messages": messages})

        log.info(
            "rag_augmented",
            request_id=request_id,
            tenant=tenant_id,
            hits=len(hits),
            context_chars=len(context),
        )
        return rewritten, citations(hits, context)


def _last_user_message(request: ChatCompletionRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content
    return ""


async def build_rag_service(
    settings: Settings,
    database: Database,
    embedder: EmbeddingProvider | None = None,
) -> RagService:
    """Construct a ready-to-use service against ``database``."""
    await database.initialize()
    resolved_embedder = embedder or build_embedding_provider(settings)
    store = SqliteVectorStore(database)
    retriever = Retriever(
        store,
        resolved_embedder,
        max_top_k=settings.rag_max_top_k,
        max_context_chars=settings.rag_max_context_chars,
    )
    ingestion = IngestionPipeline(
        database,
        store,
        resolved_embedder,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )
    return RagService(settings, retriever, ingestion, store)


def build_mcp_knowledge_search(
    settings: Settings, corpus_dir: Path, tenant_id: str
) -> Callable[[str, int, str | None], Awaitable[dict[str, Any]]]:
    """Build the handler behind the ``search_knowledge_base`` MCP tool.

    The tenant is bound at process start, not taken from the tool arguments.
    An MCP stdio server has one client and one identity for its lifetime, so
    binding the tenant to the process is the isolation model: one server
    process per tenant, with the gateway deciding who may reach which process.
    A ``tenant_id`` argument on the tool would be a parameter the model could
    choose, which is precisely what must not exist.
    """
    lock = asyncio.Lock()
    state: dict[str, Any] = {"service": None, "database": None}

    async def ensure_service() -> RagService:
        async with lock:
            if state["service"] is None:
                database = Database(settings.database_path)
                service = await build_rag_service(settings, database)
                await service.ingestion_pipeline.ingest_directory(corpus_dir, tenant_id)
                state["database"] = database
                state["service"] = service
            return state["service"]  # type: ignore[no-any-return]

    async def search(query: str, top_k: int, document_type: str | None) -> dict[str, Any]:
        service = await ensure_service()
        hits = await service.search(tenant_id, query, top_k, document_type)
        return {
            "query_echo": query[:200],
            "result_count": len(hits),
            "results": [
                {
                    **hit.chunk.citation(),
                    "score": round(hit.score, 4),
                    "text": hit.chunk.text,
                }
                for hit in hits
            ],
        }

    return search
