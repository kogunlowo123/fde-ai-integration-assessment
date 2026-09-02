"""Vector storage over SQLite (Production Enhancement).

WHAT
    ``VectorStore``: upsert chunks, and search them within a tenant scope.
    ``SqliteVectorStore`` implements it on the same on-disk database the rate
    limiter uses.

WHY SQLite
    The assessment already requires SQLite, and at assessment scale a brute
    force cosine scan over a few thousand chunks is genuinely faster than the
    operational cost of running a vector database. The interface is the
    important part: it is narrow enough that pgvector, OpenSearch or a managed
    vector DB slots in behind it without touching the pipeline.

HOW
    Embeddings are stored as packed float32 blobs. Search filters in SQL
    first, ``WHERE tenant_id = ?`` and any metadata predicate, and only
    then scores the surviving rows.

    That order is the security property, not an optimisation: another
    tenant's rows are never loaded into the process, so they cannot be ranked,
    logged, truncated into a prompt, or leaked by a later bug.

WHEN
    Swap the implementation when the corpus outgrows a linear scan, roughly
    tens of thousands of chunks per tenant on commodity hardware. ADR-012
    records the thresholds and the migration path.

SECURITY
    ``search`` takes a ``RetrievalFilter`` whose ``tenant_id`` is required.
    There is no API on this class that can return cross-tenant rows.
"""

from __future__ import annotations

import struct
import time
from typing import Protocol, runtime_checkable

from fde_assessment.common.logging import get_logger
from fde_assessment.persistence.sqlite import Database
from fde_assessment.rag.embeddings import cosine_similarity
from fde_assessment.rag.models import DocumentChunk, RetrievalFilter, RetrievalHit

log = get_logger(__name__)


def pack_embedding(values: list[float] | tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_embedding(blob: bytes) -> tuple[float, ...]:
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob[: count * 4])


@runtime_checkable
class VectorStore(Protocol):
    """Persistence port for chunk embeddings."""

    async def upsert(self, chunks: list[DocumentChunk]) -> None: ...

    async def search(
        self, embedding: list[float], filters: RetrievalFilter, top_k: int
    ) -> list[RetrievalHit]: ...


class SqliteVectorStore:
    """Brute-force cosine search over tenant-filtered rows."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert(self, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return
        now = time.time()
        async with self._db.write_transaction() as conn:
            for chunk in chunks:
                await conn.execute(
                    """
                    INSERT INTO rag_chunks (
                        chunk_id, document_id, tenant_id, chunk_index, text, embedding,
                        document_type, classification, title, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        text = excluded.text,
                        embedding = excluded.embedding,
                        document_type = excluded.document_type,
                        classification = excluded.classification,
                        title = excluded.title,
                        source = excluded.source
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.tenant_id,
                        chunk.chunk_index,
                        chunk.text,
                        pack_embedding(chunk.embedding),
                        chunk.document_type,
                        chunk.classification,
                        chunk.title,
                        chunk.source,
                        now,
                    ),
                )

    async def delete_document(self, tenant_id: str, document_id: str) -> None:
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "DELETE FROM rag_chunks WHERE tenant_id = ? AND document_id = ?",
                (tenant_id, document_id),
            )

    async def search(
        self, embedding: list[float], filters: RetrievalFilter, top_k: int
    ) -> list[RetrievalHit]:
        """Return the ``top_k`` most similar chunks *within the tenant scope*."""
        sql = [
            "SELECT chunk_id, document_id, tenant_id, chunk_index, text, embedding,",
            "       document_type, classification, title, source",
            "FROM rag_chunks WHERE tenant_id = ?",
        ]
        params: list[object] = [filters.tenant_id]
        if filters.document_type is not None:
            sql.append("AND document_type = ?")
            params.append(filters.document_type)
        if filters.classification is not None:
            sql.append("AND classification = ?")
            params.append(filters.classification)

        rows = await self._db.fetch_all(" ".join(sql), tuple(params))

        hits: list[RetrievalHit] = []
        for row in rows:
            chunk = DocumentChunk(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                tenant_id=row["tenant_id"],
                chunk_index=row["chunk_index"],
                text=row["text"],
                title=row["title"],
                source=row["source"],
                document_type=row["document_type"],
                classification=row["classification"],
                embedding=unpack_embedding(row["embedding"]),
            )
            hits.append(
                RetrievalHit(chunk=chunk, score=cosine_similarity(embedding, chunk.embedding))
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(0, top_k)]

    async def count(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM rag_chunks")
        else:
            row = await self._db.fetch_one(
                "SELECT COUNT(*) AS n FROM rag_chunks WHERE tenant_id = ?", (tenant_id,)
            )
        return int(row["n"]) if row is not None else 0
