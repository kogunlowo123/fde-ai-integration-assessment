"""Transport-independent tool registry and dispatcher.

WHAT
    The MCP server's business core: the tool catalogue, argument validation,
    and dispatch to handlers. Contains no MCP SDK imports.

WHY
    Two reasons, both about testability and correctness.

    1. Keeping dispatch free of the transport means the validation matrix
       (Task 1's scored criterion) can be exercised as fast, deterministic
       unit tests with no subprocess and no protocol handshake.
    2. It draws an explicit line between *protocol* failures, which must
       become JSON-RPC errors, and *domain* outcomes, which must not. A
       refund refused because the customer is suspended is a successful tool
       invocation with a negative answer; reporting it as a JSON-RPC error
       would tell the client the call never happened.

HOW
    ``ToolSpec`` binds a name to a Pydantic input model and an async handler.
    ``ToolDispatcher.call`` validates, dispatches, and returns a
    ``ToolOutcome``. Validation failures raise ``InvalidParamsError``; unknown
    tools raise ``MethodNotFoundError``; the transport maps both onto JSON-RPC
    codes.

WHEN
    Register a tool here; ``server.py`` picks it up automatically.

SECURITY
    Handlers never see unvalidated input. Handler exceptions are caught and
    collapsed into a generic failure so an implementation detail (a driver
    message, a path) cannot reach the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from fde_assessment.common.errors import InvalidParamsError, MethodNotFoundError
from fde_assessment.common.logging import get_logger
from fde_assessment.mcp_server.schemas import (
    GetCustomerRecordInput,
    SearchKnowledgeBaseInput,
    TriggerRefundInput,
)
from fde_assessment.mcp_server.tools.customer import InMemoryCustomerRepository
from fde_assessment.mcp_server.tools.refund import InMemoryRefundLedger, RefundRejected
from fde_assessment.observability.metrics import MCP_TOOL_CALLS_TOTAL, METRICS

log = get_logger(__name__)

Handler = Callable[[BaseModel], Awaitable["ToolOutcome"]]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The result of a dispatched tool call.

    ``is_error`` marks a *domain* refusal, surfaced to the client as an MCP
    tool result with ``isError: true`` rather than as a JSON-RPC error.
    """

    payload: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A registered tool: contract plus implementation."""

    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    read_only: bool = True
    destructive: bool = False

    def json_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def descriptor(self) -> dict[str, Any]:
        """The catalogue entry advertised by ``tools/list``."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.json_schema(),
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.read_only,
            },
        }


class ToolDispatcher:
    """Validates and dispatches tool calls."""

    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool registration: {tool.name}")
        self._tools[tool.name] = tool

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        return [tool.descriptor() for tool in self._tools.values()]

    async def call(self, name: str, arguments: object) -> ToolOutcome:
        """Validate ``arguments`` against ``name``'s schema and dispatch.

        Raises:
            MethodNotFoundError: no such tool.
            InvalidParamsError: arguments failed schema validation.
        """
        tool = self._tools.get(name)
        if tool is None:
            METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool="unknown", outcome="not_found")
            raise MethodNotFoundError(internal_detail=f"unknown tool {name!r}")

        # A missing `arguments` object is equivalent to `{}`; anything that is
        # not an object is a protocol violation, not an empty argument set.
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool=name, outcome="invalid_params")
            raise InvalidParamsError(internal_detail="arguments must be a JSON object")

        try:
            validated = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool=name, outcome="invalid_params")
            # `exc.errors()` carries the offending values; only the field
            # locations and the count are safe to log.
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            log.info(
                "tool_validation_failed", tool=name, fields=fields, violations=exc.error_count()
            )
            raise InvalidParamsError(
                internal_detail=f"{exc.error_count()} violations on {', '.join(fields) or 'input'}"
            ) from exc

        try:
            outcome = await tool.handler(validated)
        except RefundRejected as exc:
            METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool=name, outcome="domain_refusal")
            return ToolOutcome({"error": exc.reason_code, "message": exc.message}, is_error=True)
        except Exception:
            # Never let a handler's exception text reach the client.
            METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool=name, outcome="handler_error")
            log.exception("tool_handler_failed", tool=name)
            return ToolOutcome(
                {"error": "internal_error", "message": "The tool failed to complete."},
                is_error=True,
            )

        METRICS.increment(MCP_TOOL_CALLS_TOTAL, tool=name, outcome="ok")
        return outcome


@dataclass
class ServerDeps:
    """Everything the tool handlers need. Constructed once per process."""

    customers: InMemoryCustomerRepository = field(default_factory=InMemoryCustomerRepository)
    refunds: InMemoryRefundLedger | None = None
    knowledge_search: Callable[[str, int, str | None], Awaitable[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        if self.refunds is None:
            self.refunds = InMemoryRefundLedger(self.customers)


def build_dispatcher(deps: ServerDeps | None = None) -> ToolDispatcher:
    """Construct the dispatcher with the two assessment tools (plus optional RAG)."""
    resolved = deps or ServerDeps()
    ledger = resolved.refunds or InMemoryRefundLedger(resolved.customers)

    async def get_customer_record(payload: BaseModel) -> ToolOutcome:
        # The dispatcher validated `payload` against this tool's input model
        # before calling us, so the cast states the contract rather than
        # re-checking it at runtime.
        args = cast(GetCustomerRecordInput, payload)
        record = resolved.customers.get(args.customer_id)
        if record is None:
            return ToolOutcome(
                {"error": "customer_not_found", "message": "No such customer."},
                is_error=True,
            )
        return ToolOutcome(record.to_dict())

    async def trigger_refund(payload: BaseModel) -> ToolOutcome:
        args = cast(TriggerRefundInput, payload)
        receipt = ledger.record(args.customer_id, args.amount, args.reason)
        return ToolOutcome(receipt.to_dict())

    tools = [
        ToolSpec(
            name="get_customer_record",
            title="Get customer record",
            description=(
                "Fetch the customer record for a CUST-XXXXX identifier. "
                "Read-only; returns account status, tier and contact details."
            ),
            input_model=GetCustomerRecordInput,
            handler=get_customer_record,
            read_only=True,
        ),
        ToolSpec(
            name="trigger_refund",
            title="Trigger refund",
            description=(
                "Issue a refund to a customer. Requires a positive finite amount and a "
                "reason of at least 10 characters for the audit trail."
            ),
            input_model=TriggerRefundInput,
            handler=trigger_refund,
            read_only=False,
            destructive=True,
        ),
    ]

    if resolved.knowledge_search is not None:
        search = resolved.knowledge_search

        async def search_knowledge_base(payload: BaseModel) -> ToolOutcome:
            args = cast(SearchKnowledgeBaseInput, payload)
            result = await search(args.query, args.top_k, args.document_type)
            return ToolOutcome(result)

        tools.append(
            ToolSpec(
                name="search_knowledge_base",
                title="Search knowledge base",
                description=(
                    "Search the tenant's approved knowledge base and return matching "
                    "passages with citations. Scoped to this server's tenant; cannot "
                    "read arbitrary files or URLs."
                ),
                input_model=SearchKnowledgeBaseInput,
                handler=search_knowledge_base,
                read_only=True,
            )
        )

    return ToolDispatcher(tools)
