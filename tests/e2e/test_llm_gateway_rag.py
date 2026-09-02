"""End to end: LLM gateway with retrieval enabled (Production Enhancement).

Client -> LLM Gateway -> RAG -> Provider -> Guardrail -> Client, including the
tenant boundary: the same request from two tenants must not see the same
context.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fde_assessment.common.config import AppEnv, Settings
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent
from fde_assessment.llm_gateway.app import create_app
from fde_assessment.llm_gateway.providers.base import LLMProvider
from fde_assessment.llm_gateway.providers.mock import MockProvider
from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter
from fde_assessment.llm_gateway.routing.router import ModelRouter
from fde_assessment.persistence.sqlite import Database
from fde_assessment.rag.embeddings import MockEmbeddingProvider
from fde_assessment.rag.models import Document
from fde_assessment.rag.service import build_rag_service
from tests.rag.conftest import CORPUS_DIR, TENANT_A, TENANT_B


class RecordingProvider(LLMProvider):
    """Captures the prompt it was handed so the test can inspect it."""

    name = "mock-primary"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        self.prompts.append(request.prompt_text)
        yield StreamEvent(text="Refunds are available for 30 days.", completion_tokens=8)
        yield StreamEvent(done=True)


@pytest.fixture
def rag_stack(tmp_path: Path):
    """Gateway wired to a RAG service seeded for tenant-a only."""
    settings = Settings(
        app_env=AppEnv.test, database_path=tmp_path / "rag-gateway.db", log_level="DEBUG"
    )
    database = Database(settings.database_path)
    provider = RecordingProvider()

    async def prepare():
        service = await build_rag_service(settings, database, MockEmbeddingProvider(dim=256))
        await service.ingestion_pipeline.ingest_directory(CORPUS_DIR, TENANT_A)
        await service.ingestion_pipeline.ingest(
            Document(
                "b-only",
                TENANT_B,
                "Tenant B handbook",
                "Tenant B customers may request a refund within 90 days of the purchase date.",
            )
        )
        return service

    service = asyncio.run(prepare())
    app = create_app(
        settings,
        router=ModelRouter(provider, MockProvider("mock-secondary")),
        limiter=TokenRateLimiter(database, settings.rate_limit_tokens, 60),
        database=database,
        rag_service=service,
    )
    with TestClient(app) as client:
        yield client, provider
    asyncio.run(database.close())


def ask(client: TestClient, key: str, question: str, **rag_options):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-primary",
            "messages": [{"role": "user", "content": question}],
            "rag": {"enabled": True, **rag_options},
        },
        headers={"authorization": f"Bearer {key}"},
    )


class TestAugmentation:
    def test_context_is_injected_and_cited(self, rag_stack) -> None:
        client, provider = rag_stack
        body = ask(client, "dev-tenant-a-key", "How long do I have to request a refund?").json()

        assert body["sources"], "an augmented answer must carry citations"
        assert body["sources"][0]["document_id"] == "refund-policy"
        assert "<retrieved_context>" in provider.prompts[0]
        assert "30 days" in provider.prompts[0]

    def test_the_user_question_survives_augmentation(self, rag_stack) -> None:
        client, provider = rag_stack
        ask(client, "dev-tenant-a-key", "How long do I have to request a refund?")
        assert provider.prompts[0].rstrip().endswith("How long do I have to request a refund?")

    def test_retrieval_is_off_unless_requested(self, rag_stack) -> None:
        client, provider = rag_stack
        body = client.post(
            "/v1/chat/completions",
            json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "Bearer dev-tenant-a-key"},
        ).json()
        assert "sources" not in body
        assert "<retrieved_context>" not in provider.prompts[0]

    def test_citations_are_never_fabricated(self, rag_stack) -> None:
        """Every citation must name a passage that really entered the prompt.

        Note the honest limitation: the mock (lexical) embedder has weak
        precision, so an unrelated query can still surface a low-scoring
        passage. What must never happen, and is what this asserts, is a
        citation for a document that was not retrieved.
        """
        client, provider = rag_stack
        body = ask(client, "dev-tenant-a-key", "zzzz qqqq totally unrelated gibberish").json()
        prompt = provider.prompts[-1]
        for source in body.get("sources", []):
            assert source["chunk_id"] in prompt
            assert source["document_id"] in {
                "refund-policy",
                "shipping-policy",
                "account-security",
                "data-retention",
            }


class TestTenantBoundaryOverHttp:
    def test_each_tenant_gets_only_its_own_context(self, rag_stack) -> None:
        client, provider = rag_stack

        ask(client, "dev-tenant-a-key", "How long do I have to request a refund?")
        tenant_a_prompt = provider.prompts[-1]

        ask(client, "dev-tenant-b-key", "How long do I have to request a refund?")
        tenant_b_prompt = provider.prompts[-1]

        assert "30 days" in tenant_a_prompt
        assert "90 days" not in tenant_a_prompt
        assert "90 days" in tenant_b_prompt
        assert "30 days" not in tenant_b_prompt

    def test_tenant_cannot_choose_its_own_scope(self, rag_stack) -> None:
        """There is no tenant field on the request; identity comes from the key."""
        client, _ = rag_stack
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-primary",
                "messages": [{"role": "user", "content": "refunds"}],
                "rag": {"enabled": True, "tenant_id": TENANT_A},
            },
            headers={"authorization": "Bearer dev-tenant-b-key"},
        )
        assert response.status_code == 422


class TestStreamingWithRag:
    def test_sources_are_announced_before_the_first_delta(self, rag_stack) -> None:
        client, _ = rag_stack
        raw = client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-primary",
                "messages": [{"role": "user", "content": "refund window"}],
                "stream": True,
                "rag": {"enabled": True},
            },
            headers={"authorization": "Bearer dev-tenant-a-key"},
        ).text
        frames = [
            json.loads(line[6:])
            for line in raw.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert frames[0]["object"] == "rag.sources"
        assert frames[0]["sources"]
