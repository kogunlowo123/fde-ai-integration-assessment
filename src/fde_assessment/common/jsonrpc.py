"""JSON-RPC 2.0 wire helpers used by the MCP security gateway (Task 2).

WHAT
    Strict parsing of an incoming JSON-RPC request envelope, plus constructors
    for success and error responses.

WHY
    The gateway must reason about ``method`` and ``params.name`` *before*
    deciding whether to forward. Parsing with an explicit model rather than ad
    hoc ``dict.get`` chains means a hostile payload (``params`` as a list, a
    ``name`` that is an integer, a missing ``jsonrpc`` field) is rejected at the
    boundary with a correct JSON-RPC error instead of raising a ``TypeError``
    deeper in the proxy.

HOW
    ``JsonRpcRequest`` validates the envelope. ``parse_request`` maps any
    validation failure onto ``InvalidRequestError`` so the caller never sees a
    pydantic ``ValidationError``.

WHEN
    Use at the gateway boundary. The MCP server itself uses the official SDK,
    which owns its own wire handling.

SECURITY
    Guards against protocol injection and type-confusion attacks on the
    authorization decision: the tool name used for the policy check is the same
    validated string that is forwarded downstream.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fde_assessment.common.errors import GatewayError, InvalidRequestError

# JSON-RPC ids may be a string, a number, or null (for notifications).
JsonRpcId = str | int | float | None


class JsonRpcRequest(BaseModel):
    """A single JSON-RPC 2.0 request envelope."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]
    method: Annotated[str, Field(min_length=1, max_length=256)]
    id: JsonRpcId = None
    params: dict[str, Any] | None = None

    @property
    def tool_name(self) -> str | None:
        """``params.name`` when it is a string, else ``None``.

        Returning ``None`` for a non-string keeps the authorization check
        total: callers treat "no parseable tool name" as "not an admin tool
        call I can validate", and reject rather than forward.
        """
        if not self.params:
            return None
        name = self.params.get("name")
        return name if isinstance(name, str) else None


def parse_request(payload: object) -> JsonRpcRequest:
    """Validate ``payload`` as a JSON-RPC request or raise ``InvalidRequestError``."""
    try:
        return JsonRpcRequest.model_validate(payload)
    except ValidationError as exc:
        raise InvalidRequestError(
            internal_detail=f"{exc.error_count()} envelope violations"
        ) from exc


def error_response(request_id: JsonRpcId, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response with a fixed, safe message."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def error_from_exception(exc: GatewayError, request_id: JsonRpcId) -> dict[str, Any]:
    """Build a JSON-RPC error response from a ``GatewayError``."""
    return error_response(request_id, exc.jsonrpc_code, exc.message)


def success_response(request_id: JsonRpcId, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
