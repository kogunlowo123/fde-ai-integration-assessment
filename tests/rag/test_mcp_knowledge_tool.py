"""The ``search_knowledge_base`` MCP tool (Production Enhancement).

Shows how retrieval is exposed to an agent without handing it a "search
anything" primitive: the tenant is bound to the process, breadth is capped in
the schema, and there is no filesystem or URL parameter to abuse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import InvalidParamsError
from fde_assessment.mcp_server.registry import ServerDeps, build_dispatcher
from fde_assessment.rag.service import build_mcp_knowledge_search
from tests.conftest import StdioMcpClient
from tests.rag.conftest import CORPUS_DIR, TENANT_A, TENANT_B


@pytest.fixture
def knowledge_settings(tmp_path: Path) -> Settings:
    from fde_assessment.common.config import AppEnv

    return Settings(app_env=AppEnv.test, database_path=tmp_path / "kb.db", log_level="DEBUG")


@pytest.fixture
def knowledge_dispatcher(knowledge_settings: Settings):
    search = build_mcp_knowledge_search(knowledge_settings, CORPUS_DIR, TENANT_A)
    return build_dispatcher(ServerDeps(knowledge_search=search))


class TestToolContract:
    def test_the_tool_is_registered_when_a_corpus_is_configured(self, knowledge_dispatcher) -> None:
        assert "search_knowledge_base" in knowledge_dispatcher.names

    def test_the_tool_is_absent_by_default(self) -> None:
        assert "search_knowledge_base" not in build_dispatcher(ServerDeps()).names

    def test_there_is_no_path_or_url_argument(self, knowledge_dispatcher) -> None:
        """No arbitrary filesystem or network reach, by construction."""
        descriptor = next(
            t for t in knowledge_dispatcher.list_tools() if t["name"] == "search_knowledge_base"
        )
        properties = set(descriptor["inputSchema"]["properties"])
        assert properties == {"query", "top_k", "document_type"}
        assert descriptor["inputSchema"]["additionalProperties"] is False

    def test_there_is_no_tenant_argument(self, knowledge_dispatcher) -> None:
        """The tenant is process-bound, never a parameter the model can pick."""
        descriptor = next(
            t for t in knowledge_dispatcher.list_tools() if t["name"] == "search_knowledge_base"
        )
        assert "tenant_id" not in descriptor["inputSchema"]["properties"]


class TestToolBehaviour:
    async def test_returns_relevant_passages_with_citations(self, knowledge_dispatcher) -> None:
        outcome = await knowledge_dispatcher.call(
            "search_knowledge_base", {"query": "How long do I have to request a refund?"}
        )
        assert outcome.is_error is False
        assert outcome.payload["result_count"] >= 1
        first = outcome.payload["results"][0]
        assert set(first) >= {"document_id", "title", "chunk_id", "source", "score", "text"}
        assert first["document_id"] == "refund-policy"

    async def test_irrelevant_query_returns_an_empty_result_not_an_error(
        self, knowledge_dispatcher
    ) -> None:
        outcome = await knowledge_dispatcher.call(
            "search_knowledge_base", {"query": "zzzz qqqq xxxx unrelated gibberish"}
        )
        assert outcome.is_error is False
        assert outcome.payload["result_count"] <= 1

    @pytest.mark.parametrize("top_k", [0, -5, 26, 10_000])
    async def test_out_of_range_top_k_is_rejected_by_the_schema(
        self, knowledge_dispatcher, top_k: int
    ) -> None:
        with pytest.raises(InvalidParamsError):
            await knowledge_dispatcher.call(
                "search_knowledge_base", {"query": "refunds", "top_k": top_k}
            )

    @pytest.mark.parametrize("query", ["", None, 42, {"$ne": None}])
    async def test_malformed_queries_are_rejected(self, knowledge_dispatcher, query) -> None:
        with pytest.raises(InvalidParamsError):
            await knowledge_dispatcher.call("search_knowledge_base", {"query": query})

    async def test_unknown_arguments_are_rejected(self, knowledge_dispatcher) -> None:
        with pytest.raises(InvalidParamsError):
            await knowledge_dispatcher.call(
                "search_knowledge_base", {"query": "refunds", "tenant_id": TENANT_B}
            )

    async def test_a_document_type_filter_narrows_results(self, knowledge_dispatcher) -> None:
        outcome = await knowledge_dispatcher.call(
            "search_knowledge_base", {"query": "refunds", "document_type": "does-not-exist"}
        )
        assert outcome.payload["result_count"] == 0


class TestOverStdio:
    """The tool as an agent would actually reach it."""

    def test_tool_appears_and_answers_over_the_wire(self, tmp_path: Path) -> None:
        client = StdioMcpClient(
            env={
                "MCP_KNOWLEDGE_CORPUS": str(CORPUS_DIR),
                "MCP_TENANT_ID": TENANT_A,
                "DATABASE_PATH": str(tmp_path / "stdio-kb.db"),
            }
        )
        try:
            client.initialize()
            names = {t["name"] for t in client.request("tools/list")["result"]["tools"]}
            assert "search_knowledge_base" in names

            response = client.call_tool(
                "search_knowledge_base", {"query": "How often are API keys rotated?"}
            )
            payload = response["result"]["structuredContent"]
            assert payload["result_count"] >= 1
            assert payload["results"][0]["document_id"] == "account-security"
        finally:
            remaining_stdout, _ = client.close()
            for line in remaining_stdout.splitlines():
                if line.strip():
                    json.loads(line)  # stdout stayed pure even with RAG enabled
