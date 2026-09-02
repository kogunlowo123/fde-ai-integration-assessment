"""Fixtures for the RAG suite (Production Enhancement)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fde_assessment.common.config import Settings
from fde_assessment.persistence.sqlite import Database
from fde_assessment.rag.embeddings import MockEmbeddingProvider
from fde_assessment.rag.service import RagService, build_rag_service

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "corpus"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture
async def rag_database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "rag.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def rag_service(settings: Settings, rag_database: Database) -> RagService:
    return await build_rag_service(settings, rag_database, MockEmbeddingProvider(dim=256))


@pytest.fixture
async def seeded_service(rag_service: RagService) -> RagService:
    """The shipped corpus, ingested for tenant-a only."""
    await rag_service.ingestion_pipeline.ingest_directory(CORPUS_DIR, TENANT_A)
    return rag_service
