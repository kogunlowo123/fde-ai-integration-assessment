"""Production Enhancement: retrieval-augmented generation.

Not part of the assessment's Tasks 1-4. Included to show how enterprise
knowledge is integrated without weakening tenant isolation, cost control or
the prompt-injection posture. See ARCHITECTURE.md and THREAT-MODEL.md.
"""

from fde_assessment.rag.embeddings import (
    EmbeddingProvider,
    MockEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from fde_assessment.rag.ingestion import IngestionPipeline
from fde_assessment.rag.models import Document, DocumentChunk, RetrievalFilter, RetrievalHit
from fde_assessment.rag.retriever import Retriever
from fde_assessment.rag.service import RagService, build_rag_service
from fde_assessment.rag.vector_store import SqliteVectorStore, VectorStore

__all__ = [
    "Document",
    "DocumentChunk",
    "EmbeddingProvider",
    "IngestionPipeline",
    "MockEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "RagService",
    "RetrievalFilter",
    "RetrievalHit",
    "Retriever",
    "SqliteVectorStore",
    "VectorStore",
    "build_rag_service",
]
