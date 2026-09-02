"""RAG ingestion: idempotency, re-embedding avoidance, updates and deletes."""

from __future__ import annotations

from fde_assessment.rag.embeddings import MockEmbeddingProvider
from fde_assessment.rag.models import Document
from fde_assessment.rag.service import RagService
from tests.rag.conftest import CORPUS_DIR, TENANT_A


class CountingEmbedder(MockEmbeddingProvider):
    """Mock embedder that records how much work it was asked to do."""

    def __init__(self) -> None:
        super().__init__(dim=256)
        self.batches = 0
        self.texts = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batches += 1
        self.texts += len(texts)
        return await super().embed_batch(texts)


def doc(text: str, document_id: str = "policy-1", tenant: str = TENANT_A) -> Document:
    return Document(
        document_id=document_id,
        tenant_id=tenant,
        title="Policy",
        text=text,
        source="policy.md",
        document_type="policy",
    )


class TestIngestion:
    async def test_ingests_the_shipped_corpus(self, rag_service: RagService) -> None:
        report = await rag_service.ingestion_pipeline.ingest_directory(CORPUS_DIR, TENANT_A)
        assert report.documents_seen == 4
        assert report.documents_embedded == 4
        assert report.chunks_written >= 4
        assert report.errors == []

    async def test_empty_document_is_reported_not_stored(self, rag_service: RagService) -> None:
        report = await rag_service.ingestion_pipeline.ingest(doc("   \n\n  "))
        assert report.documents_embedded == 0
        assert report.errors

    async def test_chunks_carry_the_tenant(self, seeded_service: RagService) -> None:
        assert await seeded_service.store.count(TENANT_A) >= 4
        assert await seeded_service.store.count("tenant-b") == 0


class TestCostControl:
    async def test_unchanged_document_is_not_re_embedded(self, settings, rag_database) -> None:
        from fde_assessment.rag.service import build_rag_service

        embedder = CountingEmbedder()
        service = await build_rag_service(settings, rag_database, embedder)

        await service.ingestion_pipeline.ingest(doc("Refunds are available for 30 days."))
        after_first = embedder.batches
        assert after_first == 1

        report = await service.ingestion_pipeline.ingest(doc("Refunds are available for 30 days."))
        assert embedder.batches == after_first, "unchanged content must not be re-embedded"
        assert report.documents_skipped_unchanged == 1

    async def test_changed_document_is_re_embedded(self, settings, rag_database) -> None:
        from fde_assessment.rag.service import build_rag_service

        embedder = CountingEmbedder()
        service = await build_rag_service(settings, rag_database, embedder)

        await service.ingestion_pipeline.ingest(doc("Refunds are available for 30 days."))
        await service.ingestion_pipeline.ingest(doc("Refunds are available for 60 days."))
        assert embedder.batches == 2

    async def test_reformatting_alone_does_not_trigger_re_embedding(
        self, settings, rag_database
    ) -> None:
        """Normalisation happens before hashing, so whitespace churn is free."""
        from fde_assessment.rag.service import build_rag_service

        embedder = CountingEmbedder()
        service = await build_rag_service(settings, rag_database, embedder)

        await service.ingestion_pipeline.ingest(doc("Refunds are available for 30 days."))
        report = await service.ingestion_pipeline.ingest(
            doc("Refunds   are available    for 30 days.  ")
        )
        assert report.documents_skipped_unchanged == 1
        assert embedder.batches == 1


class TestUpdates:
    async def test_removed_content_stops_being_retrievable(self, rag_service: RagService) -> None:
        long_text = (
            "Section one covers refunds within thirty days.\n\n"
            "Section two covers the unicorn escalation procedure in detail."
        )
        await rag_service.ingestion_pipeline.ingest(doc(long_text))
        hits = await rag_service.search(TENANT_A, "unicorn escalation procedure", top_k=5)
        assert any("unicorn" in hit.chunk.text for hit in hits)

        await rag_service.ingestion_pipeline.ingest(
            doc("Section one covers refunds within thirty days.")
        )
        hits_after = await rag_service.search(TENANT_A, "unicorn escalation procedure", top_k=5)
        assert not any("unicorn" in hit.chunk.text for hit in hits_after)
