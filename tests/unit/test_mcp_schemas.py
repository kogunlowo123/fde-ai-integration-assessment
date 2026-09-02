"""Task 1, input validation matrix for the MCP tool schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fde_assessment.mcp_server.schemas import (
    GetCustomerRecordInput,
    SearchKnowledgeBaseInput,
    TriggerRefundInput,
)


class TestCustomerId:
    @pytest.mark.parametrize("value", ["CUST-12345", "CUST-00000", "CUST-99999"])
    def test_accepts_well_formed_ids(self, value: str) -> None:
        assert GetCustomerRecordInput(customer_id=value).customer_id == value

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("CUST-123", "too few digits"),
            ("CUST-123456", "too many digits"),
            ("customer-12345", "wrong prefix and case"),
            ("CUST12345", "missing separator"),
            ("cust-12345", "lowercase prefix"),
            ("CUST-1234A", "non-digit in the numeric part"),
            ("", "empty string"),
            (" CUST-12345", "leading whitespace"),
            ("CUST-12345 ", "trailing whitespace"),
            ("CUST-12345\n", "trailing newline defeats a non-anchored regex"),
            ("CUST-12345; DROP TABLE customers", "injection suffix"),
        ],
    )
    def test_rejects_malformed_strings(self, value: str, why: str) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput(customer_id=value)

    @pytest.mark.parametrize(
        "value",
        [None, 12345, 12.5, True, ["CUST-12345"], {"id": "CUST-12345"}],
        ids=["null", "int", "float", "bool", "list", "object"],
    )
    def test_rejects_wrong_json_types(self, value: object) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput.model_validate({"customer_id": value})

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput.model_validate({})

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            GetCustomerRecordInput.model_validate({"customer_id": "CUST-12345", "role": "admin"})


VALID_REFUND = {"customer_id": "CUST-12345", "amount": 25.50, "reason": "Customer requested refund"}


class TestRefund:
    def test_accepts_the_assessment_example(self) -> None:
        parsed = TriggerRefundInput.model_validate(VALID_REFUND)
        assert parsed.amount == pytest.approx(25.50)

    def test_accepts_integer_amount(self) -> None:
        parsed = TriggerRefundInput.model_validate({**VALID_REFUND, "amount": 25})
        assert parsed.amount == pytest.approx(25.0)

    @pytest.mark.parametrize(
        ("amount", "why"),
        [
            (0, "zero is not positive"),
            (-1.0, "negative"),
            (-0.01, "negative fraction"),
            (float("nan"), "NaN"),
            (float("inf"), "Infinity"),
            (float("-inf"), "-Infinity"),
            (1_000_000.01, "above the configured ceiling"),
        ],
    )
    def test_rejects_out_of_range_amounts(self, amount: float, why: str) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput.model_validate({**VALID_REFUND, "amount": amount})

    @pytest.mark.parametrize(
        "amount",
        ["25.50", None, True, [25.5], {"value": 25.5}],
        ids=["numeric-string", "null", "bool", "list", "object"],
    )
    def test_rejects_wrong_amount_types(self, amount: object) -> None:
        # Lax pydantic coercion would accept "25.50" and True; a money field
        # must not silently repair a caller's type error.
        with pytest.raises(ValidationError):
            TriggerRefundInput.model_validate({**VALID_REFUND, "amount": amount})

    @pytest.mark.parametrize(
        ("reason", "why"),
        [
            ("too short", "9 characters"),
            ("", "empty"),
            ("         ", "whitespace only, under the limit"),
            ("          ", "exactly 10 whitespace characters carries no audit value"),
            ("x" * 513, "above the maximum length"),
        ],
    )
    def test_rejects_bad_reasons(self, reason: str, why: str) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput.model_validate({**VALID_REFUND, "reason": reason})

    def test_accepts_exactly_ten_characters(self) -> None:
        parsed = TriggerRefundInput.model_validate({**VALID_REFUND, "reason": "0123456789"})
        assert parsed.reason == "0123456789"

    @pytest.mark.parametrize("field", ["customer_id", "amount", "reason"])
    def test_rejects_missing_required_field(self, field: str) -> None:
        payload = {k: v for k, v in VALID_REFUND.items() if k != field}
        with pytest.raises(ValidationError):
            TriggerRefundInput.model_validate(payload)

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            TriggerRefundInput.model_validate({**VALID_REFUND, "approved_by": "self"})


class TestKnowledgeSearchInput:
    def test_defaults_top_k(self) -> None:
        assert SearchKnowledgeBaseInput(query="refund policy").top_k == 5

    @pytest.mark.parametrize("top_k", [0, -1, 26, 1000])
    def test_rejects_out_of_range_top_k(self, top_k: int) -> None:
        with pytest.raises(ValidationError):
            SearchKnowledgeBaseInput.model_validate({"query": "policy", "top_k": top_k})

    @pytest.mark.parametrize("query", ["", None, 5, True, ["policy"]])
    def test_rejects_bad_queries(self, query: object) -> None:
        with pytest.raises(ValidationError):
            SearchKnowledgeBaseInput.model_validate({"query": query})
