"""RAG domain models (Production Enhancement).

WHAT
    The types that move through the retrieval pipeline: documents, chunks,
    retrieval hits and the filter object that carries the authorization
    predicate.

WHY
    ``RetrievalFilter`` exists as a type rather than a bag of keyword
    arguments because tenant isolation is the security property of this whole
    subsystem. Making it a required constructor argument means a retrieval
    query that forgets the tenant does not compile, let alone run.

WHEN
    Shared by the ingestion pipeline, the vector store and the retriever.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Document:
    """A source document before chunking."""

    document_id: str
    tenant_id: str
    title: str
    text: str
    source: str = "unknown"
    document_type: str = "general"
    classification: str = "internal"

    @property
    def content_hash(self) -> str:
        """SHA-256 of the content, used to skip re-embedding unchanged docs."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """One retrievable passage."""

    chunk_id: str
    document_id: str
    tenant_id: str
    chunk_index: int
    text: str
    title: str = ""
    source: str = ""
    document_type: str = "general"
    classification: str = "internal"
    embedding: tuple[float, ...] = ()

    def citation(self) -> dict[str, Any]:
        """The metadata returned to the caller as a source attribution."""
        return {
            "document_id": self.document_id,
            "title": self.title,
            "chunk_id": self.chunk_id,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """A chunk plus its similarity score."""

    chunk: DocumentChunk
    score: float


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """The authorization predicate applied *inside* the store query.

    ``tenant_id`` has no default: retrieval without a tenant is not a
    supported operation.
    """

    tenant_id: str
    document_type: str | None = None
    classification: str | None = None


@dataclass(slots=True)
class IngestionReport:
    """What an ingestion run did. Used by tests and the cost documentation."""

    documents_seen: int = 0
    documents_embedded: int = 0
    documents_skipped_unchanged: int = 0
    chunks_written: int = 0
    errors: list[str] = field(default_factory=list)
