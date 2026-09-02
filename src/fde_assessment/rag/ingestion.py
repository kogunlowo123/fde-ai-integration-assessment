"""Document ingestion (Production Enhancement).

WHAT
    ``Document -> normalise -> chunk -> hash -> embed -> store``, with
    content-hash-based skipping of unchanged documents.

WHY the content hash
    Embedding is the expensive step of any RAG system, and most re-ingestion
    runs re-process a corpus that has barely changed. Hashing the normalised
    text and comparing against the stored hash makes a no-op re-ingestion
    genuinely free, no embedding calls, no writes. On a hosted embedding API
    that is the difference between a nightly job that costs money and one that
    costs nothing.

HOW
    ``rag_documents`` stores ``content_hash``. Ingestion compares, skips when
    equal, and otherwise deletes the document's old chunks before writing new
    ones so a shrinking document does not leave orphaned passages behind.

WHEN
    Batch, out of band, never on the request path.

SECURITY
    ``tenant_id`` is stamped on the document and copied onto every chunk. A
    document loaded from a customer's share cannot become retrievable by
    another tenant without a code change.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fde_assessment.common.logging import get_logger
from fde_assessment.observability.metrics import METRICS, RAG_EMBEDDINGS_SKIPPED_TOTAL
from fde_assessment.persistence.sqlite import Database
from fde_assessment.rag.chunking import chunk_text, normalize
from fde_assessment.rag.embeddings import EmbeddingProvider
from fde_assessment.rag.models import Document, DocumentChunk, IngestionReport
from fde_assessment.rag.vector_store import SqliteVectorStore

log = get_logger(__name__)

SUPPORTED_SUFFIXES = {".md", ".txt", ".markdown"}


class IngestionPipeline:
    """Turns documents into stored, embedded chunks."""

    def __init__(
        self,
        database: Database,
        store: SqliteVectorStore,
        embedder: EmbeddingProvider,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
    ) -> None:
        self._db = database
        self._store = store
        self._embedder = embedder
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    async def _stored_hash(self, document_id: str, tenant_id: str) -> str | None:
        row = await self._db.fetch_one(
            "SELECT content_hash FROM rag_documents WHERE document_id = ? AND tenant_id = ?",
            (document_id, tenant_id),
        )
        return str(row["content_hash"]) if row is not None else None

    async def ingest(
        self, document: Document, report: IngestionReport | None = None
    ) -> IngestionReport:
        """Ingest one document. Idempotent for unchanged content."""
        result = report or IngestionReport()
        result.documents_seen += 1

        normalized = normalize(document.text)
        if not normalized:
            result.errors.append(f"{document.document_id}: empty after normalisation")
            return result

        canonical = Document(
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            title=document.title,
            text=normalized,
            source=document.source,
            document_type=document.document_type,
            classification=document.classification,
        )

        existing = await self._stored_hash(canonical.document_id, canonical.tenant_id)
        if existing == canonical.content_hash:
            result.documents_skipped_unchanged += 1
            METRICS.increment(RAG_EMBEDDINGS_SKIPPED_TOTAL, tenant=canonical.tenant_id)
            log.info(
                "rag_ingest_skipped_unchanged",
                document_id=canonical.document_id,
                tenant=canonical.tenant_id,
            )
            return result

        pieces = chunk_text(canonical.text, self._chunk_size, self._chunk_overlap)
        if not pieces:
            result.errors.append(f"{canonical.document_id}: produced no chunks")
            return result

        vectors = await self._embedder.embed_batch(pieces)
        chunks = [
            DocumentChunk(
                chunk_id=f"{canonical.document_id}#{index:04d}",
                document_id=canonical.document_id,
                tenant_id=canonical.tenant_id,
                chunk_index=index,
                text=text,
                title=canonical.title,
                source=canonical.source,
                document_type=canonical.document_type,
                classification=canonical.classification,
                embedding=tuple(vector),
            )
            for index, (text, vector) in enumerate(zip(pieces, vectors, strict=True))
        ]

        # The document row is written first: rag_chunks has a foreign key
        # onto it, and the chunk write would otherwise fail the constraint.
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO rag_documents (
                    document_id, tenant_id, title, source, document_type,
                    classification, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title = excluded.title,
                    source = excluded.source,
                    document_type = excluded.document_type,
                    classification = excluded.classification,
                    content_hash = excluded.content_hash
                """,
                (
                    canonical.document_id,
                    canonical.tenant_id,
                    canonical.title,
                    canonical.source,
                    canonical.document_type,
                    canonical.classification,
                    canonical.content_hash,
                    time.time(),
                ),
            )

        # Replace rather than merge: a document that lost a section must not
        # keep serving the removed passage.
        await self._store.delete_document(canonical.tenant_id, canonical.document_id)
        await self._store.upsert(chunks)

        result.documents_embedded += 1
        result.chunks_written += len(chunks)
        log.info(
            "rag_ingest_completed",
            document_id=canonical.document_id,
            tenant=canonical.tenant_id,
            chunks=len(chunks),
        )
        return result

    async def ingest_directory(
        self,
        directory: Path,
        tenant_id: str,
        document_type: str = "general",
        classification: str = "internal",
    ) -> IngestionReport:
        """Ingest every supported file in ``directory`` (non-recursive).

        Ingestion is a batch operation, never on a request path, so the
        blocking filesystem reads here are moved onto a worker thread rather
        than being allowed to stall the event loop.
        """
        report = IngestionReport()
        paths = await asyncio.to_thread(_list_documents, directory)
        for path in paths:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            title = text.splitlines()[0].lstrip("# ").strip() if text.strip() else path.stem
            await self.ingest(
                Document(
                    document_id=path.stem,
                    tenant_id=tenant_id,
                    title=title or path.stem,
                    text=text,
                    source=path.name,
                    document_type=document_type,
                    classification=classification,
                ),
                report,
            )
        return report


def _list_documents(directory: Path) -> list[Path]:
    """Supported files in ``directory``, sorted for deterministic ingestion."""
    return [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]
