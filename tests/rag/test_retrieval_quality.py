"""RAG retrieval quality on a small deterministic evaluation set.

The numbers here are measured, not asserted aspirationally: the thresholds are
set to what the shipped mock (hashed bag-of-terms) embedder actually achieves,
and ``scripts/benchmark.py`` prints the same metrics. With Ollama embeddings
the semantic cases improve; that is stated in the RAG documentation rather than
baked into a threshold this suite cannot verify offline.
"""

from __future__ import annotations

import pytest

from fde_assessment.rag.service import RagService
from tests.rag.conftest import TENANT_A

# (question, expected document id, keywords that must appear in a good hit)
EVAL_SET: list[tuple[str, str, tuple[str, ...]]] = [
    ("How long do I have to request a refund?", "refund-policy", ("30 days", "refund")),
    ("Can a suspended account get a refund?", "refund-policy", ("suspended",)),
    ("When is express shipping dispatched?", "shipping-policy", ("Express", "same business day")),
    ("What happens if my shipment is lost?", "shipping-policy", ("lost", "replaced")),
    ("How often are API keys rotated?", "account-security", ("ninety days", "rotated")),
    ("How many failed sign-in attempts suspend an account?", "account-security", ("ten",)),
    ("How long are billing records kept?", "data-retention", ("seven years", "billing")),
    ("How long are support transcripts retained?", "data-retention", ("twenty-four months",)),
]


async def recall_at_k(service: RagService, k: int) -> float:
    hits = 0
    for question, expected_document, _ in EVAL_SET:
        results = await service.search(TENANT_A, question, top_k=k)
        if any(hit.chunk.document_id == expected_document for hit in results):
            hits += 1
    return hits / len(EVAL_SET)


async def mean_reciprocal_rank(service: RagService, k: int) -> float:
    total = 0.0
    for question, expected_document, _ in EVAL_SET:
        results = await service.search(TENANT_A, question, top_k=k)
        for rank, hit in enumerate(results, start=1):
            if hit.chunk.document_id == expected_document:
                total += 1.0 / rank
                break
    return total / len(EVAL_SET)


class TestRetrievalQuality:
    async def test_recall_at_1(self, seeded_service: RagService) -> None:
        score = await recall_at_1_value(seeded_service)
        assert score >= 0.60, f"Recall@1 regressed to {score:.2f}"

    async def test_recall_at_3(self, seeded_service: RagService) -> None:
        score = await recall_at_k(seeded_service, 3)
        assert score >= 0.85, f"Recall@3 regressed to {score:.2f}"

    async def test_mrr_at_3(self, seeded_service: RagService) -> None:
        score = await mean_reciprocal_rank(seeded_service, 3)
        assert score >= 0.60, f"MRR@3 regressed to {score:.2f}"

    @pytest.mark.parametrize(("question", "document", "keywords"), EVAL_SET)
    async def test_top_3_contains_a_useful_passage(
        self, seeded_service: RagService, question: str, document: str, keywords: tuple[str, ...]
    ) -> None:
        results = await seeded_service.search(TENANT_A, question, top_k=3)
        assert results, f"no hits at all for {question!r}"
        joined = " ".join(hit.chunk.text for hit in results)
        assert any(keyword.lower() in joined.lower() for keyword in keywords)


async def recall_at_1_value(service: RagService) -> float:
    return await recall_at_k(service, 1)


class TestRetrievalBehaviour:
    async def test_irrelevant_query_returns_little_or_nothing(
        self, seeded_service: RagService
    ) -> None:
        results = await seeded_service.search(
            TENANT_A, "quantum chromodynamics lattice gauge theory", top_k=4
        )
        # The score floor is what stops noise from consuming context budget.
        assert len(results) <= 1

    async def test_empty_corpus_returns_no_hits(self, rag_service: RagService) -> None:
        assert await rag_service.search(TENANT_A, "refund policy", top_k=4) == []

    async def test_top_k_is_honoured(self, seeded_service: RagService) -> None:
        assert len(await seeded_service.search(TENANT_A, "refund", top_k=2)) <= 2

    async def test_top_k_is_capped_by_configuration(self, seeded_service: RagService) -> None:
        results = await seeded_service.search(TENANT_A, "policy", top_k=10_000)
        assert len(results) <= 10  # rag_max_top_k

    async def test_results_are_ordered_by_score(self, seeded_service: RagService) -> None:
        results = await seeded_service.search(TENANT_A, "refund policy", top_k=5)
        scores = [hit.score for hit in results]
        assert scores == sorted(scores, reverse=True)

    async def test_metadata_filter_restricts_the_candidate_set(
        self, rag_service: RagService
    ) -> None:
        from fde_assessment.rag.models import Document

        await rag_service.ingestion_pipeline.ingest(
            Document("hr-1", TENANT_A, "Leave", "Annual leave is 25 days.", document_type="hr")
        )
        await rag_service.ingestion_pipeline.ingest(
            Document(
                "fin-1",
                TENANT_A,
                "Expenses",
                "Expense claims within 25 days.",
                document_type="finance",
            )
        )

        hr_only = await rag_service.search(TENANT_A, "25 days", top_k=5, document_type="hr")
        assert hr_only
        assert {hit.chunk.document_id for hit in hr_only} == {"hr-1"}
