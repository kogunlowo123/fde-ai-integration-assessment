"""``trigger_refund``, Task 1.

WHAT
    Records a refund against a customer and returns a refund receipt.

WHY
    This is the tool that moves money, so it is the one an attacker wants. Its
    argument schema (``schemas.TriggerRefundInput``) is the primary control;
    this module adds the domain checks that a schema cannot express: the
    customer must exist and must be in a state that permits refunds.

HOW
    An append-only in-memory ledger. Each refund gets a UUID4 identifier and a
    UTC timestamp, both returned to the caller so the action is traceable.

WHEN
    Replace ``InMemoryRefundLedger`` with the customer's payments API. That
    adapter, not this module, owns retries and idempotency keys.

SECURITY
    Fails closed on unknown or suspended customers. Amount bounds live in the
    schema so they are enforced before any handler code runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fde_assessment.mcp_server.tools.customer import InMemoryCustomerRepository


@dataclass(frozen=True, slots=True)
class RefundReceipt:
    """The result of a successful refund."""

    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "refund_id": self.refund_id,
            "customer_id": self.customer_id,
            "amount": self.amount,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
        }


class RefundRejected(Exception):
    """A domain refusal (not a validation failure).

    Carries a ``reason_code`` so the transport layer can decide how to surface
    it without string-matching a message.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class InMemoryRefundLedger:
    """Append-only refund ledger backed by a list."""

    def __init__(self, customers: InMemoryCustomerRepository | None = None) -> None:
        self._customers = customers or InMemoryCustomerRepository()
        self._entries: list[RefundReceipt] = []

    def record(self, customer_id: str, amount: float, reason: str) -> RefundReceipt:
        customer = self._customers.get(customer_id)
        if customer is None:
            raise RefundRejected("customer_not_found", "No such customer.")
        if customer.status != "active":
            raise RefundRejected("customer_not_active", "Customer is not eligible for refunds.")

        receipt = RefundReceipt(
            refund_id=f"REF-{uuid.uuid4().hex[:12].upper()}",
            customer_id=customer_id,
            amount=round(amount, 2),
            reason=reason,
            status="accepted",
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._entries.append(receipt)
        return receipt

    @property
    def entries(self) -> tuple[RefundReceipt, ...]:
        return tuple(self._entries)
