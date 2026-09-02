"""Task 1, dispatcher behaviour: protocol errors vs domain outcomes."""

from __future__ import annotations

import pytest

from fde_assessment.common.errors import InvalidParamsError, MethodNotFoundError
from fde_assessment.mcp_server.registry import ServerDeps, build_dispatcher
from fde_assessment.observability.metrics import MCP_TOOL_CALLS_TOTAL, METRICS


@pytest.fixture
def dispatcher():
    return build_dispatcher(ServerDeps())


class TestCatalogue:
    def test_exposes_exactly_the_two_assessment_tools_by_default(self, dispatcher) -> None:
        assert dispatcher.names == ("get_customer_record", "trigger_refund")

    def test_descriptors_carry_a_json_schema(self, dispatcher) -> None:
        by_name = {t["name"]: t for t in dispatcher.list_tools()}
        schema = by_name["trigger_refund"]["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"customer_id", "amount", "reason"}
        assert schema["additionalProperties"] is False

    def test_refund_is_flagged_destructive(self, dispatcher) -> None:
        by_name = {t["name"]: t for t in dispatcher.list_tools()}
        assert by_name["trigger_refund"]["annotations"]["destructiveHint"] is True
        assert by_name["get_customer_record"]["annotations"]["readOnlyHint"] is True


class TestDispatch:
    async def test_valid_lookup_returns_the_record(self, dispatcher) -> None:
        outcome = await dispatcher.call("get_customer_record", {"customer_id": "CUST-12345"})
        assert outcome.is_error is False
        assert outcome.payload["name"] == "Ada Lovelace"

    async def test_unknown_tool_raises_method_not_found(self, dispatcher) -> None:
        with pytest.raises(MethodNotFoundError):
            await dispatcher.call("admin_reset_key", {})

    async def test_invalid_arguments_raise_invalid_params(self, dispatcher) -> None:
        with pytest.raises(InvalidParamsError):
            await dispatcher.call("get_customer_record", {"customer_id": "nope"})

    @pytest.mark.parametrize("arguments", ["a string", 42, ["CUST-12345"]])
    async def test_non_object_arguments_raise_invalid_params(self, dispatcher, arguments) -> None:
        with pytest.raises(InvalidParamsError):
            await dispatcher.call("get_customer_record", arguments)

    async def test_missing_arguments_are_treated_as_empty_object(self, dispatcher) -> None:
        # `{}` fails the schema (customer_id is required), the point is that
        # a missing `arguments` key is a validation error, not a crash.
        with pytest.raises(InvalidParamsError):
            await dispatcher.call("get_customer_record", None)

    async def test_unknown_customer_is_a_domain_outcome_not_a_protocol_error(
        self, dispatcher
    ) -> None:
        outcome = await dispatcher.call("get_customer_record", {"customer_id": "CUST-77777"})
        assert outcome.is_error is True
        assert outcome.payload["error"] == "customer_not_found"

    async def test_refund_succeeds_and_returns_a_receipt(self, dispatcher) -> None:
        outcome = await dispatcher.call(
            "trigger_refund",
            {"customer_id": "CUST-12345", "amount": 25.50, "reason": "Customer requested refund"},
        )
        assert outcome.is_error is False
        assert outcome.payload["refund_id"].startswith("REF-")
        assert outcome.payload["amount"] == 25.50
        assert outcome.payload["status"] == "accepted"

    async def test_refund_for_a_suspended_customer_is_refused(self, dispatcher) -> None:
        outcome = await dispatcher.call(
            "trigger_refund",
            {"customer_id": "CUST-00001", "amount": 5.0, "reason": "Suspended account test"},
        )
        assert outcome.is_error is True
        assert outcome.payload["error"] == "customer_not_active"

    async def test_refund_for_an_unknown_customer_is_refused(self, dispatcher) -> None:
        outcome = await dispatcher.call(
            "trigger_refund",
            {"customer_id": "CUST-77777", "amount": 5.0, "reason": "Unknown customer test"},
        )
        assert outcome.is_error is True
        assert outcome.payload["error"] == "customer_not_found"

    async def test_handler_exceptions_are_sanitised(self) -> None:
        async def exploding_search(_q: str, _k: int, _t: str | None) -> dict[str, object]:
            raise RuntimeError("connection to postgres://user:pw@10.0.0.5/kb failed")

        dispatcher = build_dispatcher(ServerDeps(knowledge_search=exploding_search))
        outcome = await dispatcher.call("search_knowledge_base", {"query": "refund policy"})
        assert outcome.is_error is True
        assert outcome.payload == {
            "error": "internal_error",
            "message": "The tool failed to complete.",
        }
        assert "postgres" not in str(outcome.payload)


class TestMetrics:
    async def test_records_tool_call_outcomes(self, dispatcher) -> None:
        await dispatcher.call("get_customer_record", {"customer_id": "CUST-12345"})
        with pytest.raises(InvalidParamsError):
            await dispatcher.call("get_customer_record", {"customer_id": "bad"})
        assert METRICS.counter(MCP_TOOL_CALLS_TOTAL, tool="get_customer_record", outcome="ok") == 1
        assert (
            METRICS.counter(
                MCP_TOOL_CALLS_TOTAL, tool="get_customer_record", outcome="invalid_params"
            )
            == 1
        )
