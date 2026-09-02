"""Task 1, strict tool input schemas.

WHAT
    Pydantic models for every tool argument object, plus the customer-id
    pattern shared by the tools.

WHY
    The MCP server is the last hop before a business action (reading a customer
    record, moving money). Validation here is a security control, not a
    convenience: a malformed ``customer_id`` that reaches a downstream system
    is an injection vector, and an unbounded ``amount`` is a financial one.

HOW
    ``extra="forbid"`` rejects unknown keys instead of ignoring them (an
    ignored key is how a caller smuggles a field a future version might honour).
    ``mode="before"`` validators reject wrong JSON types explicitly, because
    pydantic's default lax coercion would otherwise accept ``"25.50"`` for a
    float and ``True`` for a number.

WHEN
    Every tool argument object passes through these before any handler runs.

SECURITY
    Blocks type confusion, oversized input, NaN/Infinity amounts (which break
    downstream arithmetic and comparisons), and unknown-field smuggling.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The assessment specifies `CUST-XXXXX`. Every example uses five digits
# (CUST-12345), so `X` is read as a decimal digit and the length is exact.
# Documented in docs/testing/TEST-MATRIX.md; widening to [A-Z0-9] would be a
# one-character change here and nowhere else.
# `[0-9]` rather than `\d`: Python's `\d` matches every Unicode decimal
# digit, so an identifier written with full-width digits would satisfy
# `\d{5}` while being a different string from CUST-12345 to every
# downstream system that receives it. Found by adversarial testing; the
# case is covered in tests/security/test_adversarial.py.
CUSTOMER_ID_PATTERN: Final = re.compile(r"^CUST-[0-9]{5}$")
CUSTOMER_ID_DESCRIPTION: Final = "Customer identifier in the form CUST-XXXXX (five digits)."

MIN_REASON_LENGTH: Final = 10
MAX_REASON_LENGTH: Final = 512
MAX_REFUND_AMOUNT: Final = 1_000_000.0


def _validate_customer_id(value: Any) -> str:
    """Reject anything that is not a well-formed customer id string."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("customer_id must be a string")
    if not CUSTOMER_ID_PATTERN.fullmatch(value):
        raise ValueError("customer_id must match CUST-XXXXX where X is a digit")
    return value


class GetCustomerRecordInput(BaseModel):
    """Arguments for ``get_customer_record``."""

    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, Field(description=CUSTOMER_ID_DESCRIPTION)]

    @field_validator("customer_id", mode="before")
    @classmethod
    def _check_customer_id(cls, value: Any) -> str:
        return _validate_customer_id(value)


class TriggerRefundInput(BaseModel):
    """Arguments for ``trigger_refund``."""

    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, Field(description=CUSTOMER_ID_DESCRIPTION)]
    amount: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_REFUND_AMOUNT,
            allow_inf_nan=False,
            description="Refund amount in major currency units. Must be finite and positive.",
        ),
    ]
    reason: Annotated[
        str,
        Field(
            min_length=MIN_REASON_LENGTH,
            max_length=MAX_REASON_LENGTH,
            description=f"Human-written justification, at least {MIN_REASON_LENGTH} characters.",
        ),
    ]

    @field_validator("customer_id", mode="before")
    @classmethod
    def _check_customer_id(cls, value: Any) -> str:
        return _validate_customer_id(value)

    @field_validator("amount", mode="before")
    @classmethod
    def _check_amount_is_a_json_number(cls, value: Any) -> Any:
        # `bool` is a subclass of `int`, and lax mode would coerce "25.50" and
        # True alike. An amount arriving as anything but a JSON number is a
        # client bug or an attack, never something to silently repair.
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise ValueError("amount must be a JSON number")
        as_float = float(value)
        if math.isnan(as_float) or math.isinf(as_float):
            raise ValueError("amount must be finite")
        return as_float

    @field_validator("reason", mode="before")
    @classmethod
    def _check_reason_type(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError("reason must be a string")
        return value

    @field_validator("reason", mode="after")
    @classmethod
    def _check_reason_is_substantive(cls, value: str) -> str:
        # A reason of ten spaces satisfies min_length but carries no audit
        # value; the field exists to make refunds explainable after the fact.
        if len(value.strip()) < MIN_REASON_LENGTH:
            raise ValueError(
                f"reason must contain at least {MIN_REASON_LENGTH} non-whitespace characters"
            )
        return value


class SearchKnowledgeBaseInput(BaseModel):
    """Arguments for ``search_knowledge_base`` (Production Enhancement).

    ``top_k`` is capped in the schema itself so an agent cannot use retrieval
    breadth as a denial-of-service or context-flooding lever; the pipeline caps
    it again against configuration (defence in depth).
    """

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=2_000)]
    top_k: Annotated[int, Field(ge=1, le=25)] = 5
    document_type: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @field_validator("query", mode="before")
    @classmethod
    def _check_query_type(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError("query must be a string")
        return value

    @field_validator("top_k", mode="before")
    @classmethod
    def _check_top_k_type(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("top_k must be an integer")
        return value
