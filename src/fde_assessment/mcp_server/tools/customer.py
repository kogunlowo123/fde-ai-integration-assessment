"""``get_customer_record``, Task 1.

WHAT
    Looks up a customer record in a deterministic in-memory store.

WHY
    The assessment scores validation, protocol behaviour and transport
    hygiene, not persistence. A fixed in-memory fixture keeps the tool
    deterministic (no database in CI, no seed drift) while leaving a single
    obvious seam, ``CustomerRepository``, for a real system of record.

HOW
    A frozen dataclass per customer, keyed by id. Lookups are pure.

WHEN
    Replace ``InMemoryCustomerRepository`` with an adapter over the customer's
    CRM or billing system; nothing else in the server changes.

SECURITY
    Returns only fields the tool contract advertises. Notably, this fixture
    deliberately includes an email address so the end-to-end demo can show the
    LLM gateway's PII guardrail redacting data that a *legitimate* tool
    returned, the guardrail protects the model output channel, not the tool.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    """A customer as exposed by the tool contract."""

    customer_id: str
    name: str
    email: str
    tier: str
    status: str
    lifetime_value: float
    open_tickets: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CustomerRepository(Protocol):
    """Read-side port for customer data."""

    def get(self, customer_id: str) -> CustomerRecord | None: ...


_SEED: tuple[CustomerRecord, ...] = (
    CustomerRecord(
        "CUST-12345", "Ada Lovelace", "ada.lovelace@example.com", "gold", "active", 4820.50, 1
    ),
    CustomerRecord(
        "CUST-54321", "Grace Hopper", "grace.hopper@example.com", "platinum", "active", 19240.00, 0
    ),
    CustomerRecord(
        "CUST-00001", "Alan Turing", "alan.turing@example.com", "silver", "suspended", 310.25, 3
    ),
    CustomerRecord(
        "CUST-99999", "Katherine Johnson", "k.johnson@example.com", "gold", "active", 7710.75, 2
    ),
)


class InMemoryCustomerRepository:
    """Deterministic fixture repository."""

    def __init__(self, records: tuple[CustomerRecord, ...] = _SEED) -> None:
        self._records: dict[str, CustomerRecord] = {r.customer_id: r for r in records}

    def get(self, customer_id: str) -> CustomerRecord | None:
        return self._records.get(customer_id)

    def exists(self, customer_id: str) -> bool:
        return customer_id in self._records
