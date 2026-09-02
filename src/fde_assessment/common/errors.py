"""Centralised error taxonomy and sanitisation.

WHAT
    One exception hierarchy for the whole system, plus the only two functions
    allowed to turn an exception into something a client can see:
    ``to_gateway_error`` (HTTP/JSON) and ``to_jsonrpc_error`` (MCP).

WHY
    Task 4 requires "a standardized gateway error payload without leaking raw
    upstream stack traces or internal implementation details". The reliable way
    to guarantee that is to make leaking structurally impossible: client-facing
    messages come from a fixed table keyed by error code, never from
    ``str(exc)`` of an arbitrary upstream failure.

HOW
    Every ``GatewayError`` carries a stable ``code`` (machine-readable), a
    ``type`` (coarse class), an HTTP status, and a JSON-RPC code. Detail useful
    for debugging lives in ``internal_detail``, which is logged and never
    serialised. Unknown exceptions collapse to ``INTERNAL_ERROR``.

WHEN
    Raise these from any layer. Handlers/middleware call the ``to_*``
    converters exactly once, at the process boundary.

SECURITY
    Protects against information disclosure (STRIDE-I): stack traces, file
    paths, upstream hostnames, driver messages and credentials never reach a
    response body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# JSON-RPC 2.0 reserved codes.
JSONRPC_PARSE_ERROR: Final = -32700
JSONRPC_INVALID_REQUEST: Final = -32600
JSONRPC_METHOD_NOT_FOUND: Final = -32601
JSONRPC_INVALID_PARAMS: Final = -32602
JSONRPC_INTERNAL_ERROR: Final = -32603
# Implementation-defined server error range (-32000..-32099).
# -32001 is mandated by the assessment for unauthorized tool calls.
JSONRPC_UNAUTHORIZED_TOOL_CALL: Final = -32001
JSONRPC_FORBIDDEN: Final = -32002
JSONRPC_RATE_LIMITED: Final = -32003
JSONRPC_UPSTREAM_TIMEOUT: Final = -32004
JSONRPC_UPSTREAM_FAILURE: Final = -32005


@dataclass(slots=True)
class GatewayError(Exception):
    """Base class for every error that may reach a client.

    Attributes:
        code: Stable machine-readable identifier (``MODEL_PROVIDER_UNAVAILABLE``).
        error_type: Coarse category used by clients for retry decisions.
        message: Safe, human-readable text. Never interpolates upstream output.
        http_status: Status code for HTTP surfaces.
        jsonrpc_code: Code for JSON-RPC surfaces.
        internal_detail: Debug context. Logged, never serialised to a client.
        headers: Extra response headers (e.g. ``Retry-After``).
    """

    code: str = "INTERNAL_ERROR"
    error_type: str = "internal_error"
    message: str = "An internal error occurred."
    http_status: int = 500
    jsonrpc_code: int = JSONRPC_INTERNAL_ERROR
    internal_detail: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _err(
    code: str,
    error_type: str,
    message: str,
    http_status: int,
    jsonrpc_code: int,
) -> type[GatewayError]:
    """Build a concrete ``GatewayError`` subclass with fixed public wording."""

    class _Concrete(GatewayError):
        def __init__(
            self,
            internal_detail: str | None = None,
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            super().__init__(
                code=code,
                error_type=error_type,
                message=message,
                http_status=http_status,
                jsonrpc_code=jsonrpc_code,
                internal_detail=internal_detail,
                headers=headers or {},
            )

    _Concrete.__name__ = code.title().replace("_", "") + "Error"
    _Concrete.__qualname__ = _Concrete.__name__
    return _Concrete


# --- Client-caused ---------------------------------------------------------
InvalidRequestError = _err(
    "INVALID_REQUEST",
    "invalid_request",
    "The request payload is not a valid JSON-RPC request.",
    400,
    JSONRPC_INVALID_REQUEST,
)
ParseError = _err(
    "PARSE_ERROR",
    "invalid_request",
    "The request body is not valid JSON.",
    400,
    JSONRPC_PARSE_ERROR,
)
InvalidParamsError = _err(
    "INVALID_PARAMS",
    "invalid_request",
    "One or more parameters failed validation.",
    422,
    JSONRPC_INVALID_PARAMS,
)
MethodNotFoundError = _err(
    "METHOD_NOT_FOUND",
    "invalid_request",
    "The requested method is not supported.",
    404,
    JSONRPC_METHOD_NOT_FOUND,
)
PayloadTooLargeError = _err(
    "PAYLOAD_TOO_LARGE",
    "invalid_request",
    "The request body exceeds the configured maximum size.",
    413,
    JSONRPC_INVALID_REQUEST,
)

# --- Identity and policy ---------------------------------------------------
UnauthenticatedError = _err(
    "UNAUTHENTICATED",
    "unauthenticated",
    "Missing or invalid credentials.",
    401,
    JSONRPC_UNAUTHORIZED_TOOL_CALL,
)
UnauthorizedToolCallError = _err(
    "UNAUTHORIZED_TOOL_CALL",
    "unauthorized",
    "Unauthorized Tool Call",
    403,
    JSONRPC_UNAUTHORIZED_TOOL_CALL,
)
ForbiddenError = _err(
    "FORBIDDEN",
    "forbidden",
    "The caller is not permitted to perform this operation.",
    403,
    JSONRPC_FORBIDDEN,
)

# --- Quota -----------------------------------------------------------------
RateLimitedError = _err(
    "RATE_LIMIT_EXCEEDED",
    "rate_limited",
    "Token rate limit exceeded for this tenant. Retry after the window resets.",
    429,
    JSONRPC_RATE_LIMITED,
)

# --- Upstream --------------------------------------------------------------
UpstreamTimeoutError = _err(
    "MODEL_PROVIDER_TIMEOUT",
    "upstream_timeout",
    "The model service did not respond in time.",
    504,
    JSONRPC_UPSTREAM_TIMEOUT,
)
UpstreamUnavailableError = _err(
    "MODEL_PROVIDER_UNAVAILABLE",
    "upstream_unavailable",
    "The model service is temporarily unavailable.",
    502,
    JSONRPC_UPSTREAM_FAILURE,
)
UpstreamRateLimitedError = _err(
    "MODEL_PROVIDER_RATE_LIMITED",
    "upstream_rate_limited",
    "The model service rejected the request with a rate limit.",
    429,
    JSONRPC_RATE_LIMITED,
)
UpstreamProtocolError = _err(
    "MODEL_PROVIDER_PROTOCOL_ERROR",
    "upstream_unavailable",
    "The model service returned a malformed response.",
    502,
    JSONRPC_UPSTREAM_FAILURE,
)

# --- Domain ----------------------------------------------------------------
NotFoundError = _err(
    "RESOURCE_NOT_FOUND",
    "not_found",
    "The requested resource does not exist.",
    404,
    JSONRPC_INVALID_PARAMS,
)
RetrievalError = _err(
    "RETRIEVAL_FAILED",
    "retrieval_failed",
    "The knowledge retrieval service could not answer the query.",
    502,
    JSONRPC_UPSTREAM_FAILURE,
)


def coerce(exc: BaseException) -> GatewayError:
    """Return ``exc`` as a ``GatewayError``, collapsing anything unknown.

    The ``internal_detail`` keeps the exception class name (not its message) so
    logs stay useful without risking that an upstream body, which may embed a
    credential or a customer record, is captured verbatim.
    """
    if isinstance(exc, GatewayError):
        return exc
    return GatewayError(internal_detail=f"unhandled {type(exc).__name__}")


def to_gateway_error(exc: BaseException, request_id: str) -> dict[str, Any]:
    """Serialise ``exc`` into the standardised HTTP error envelope."""
    err = coerce(exc)
    return {
        "error": {
            "type": err.error_type,
            "code": err.code,
            "message": err.message,
            "request_id": request_id,
        }
    }


def to_jsonrpc_error(exc: BaseException, request_id: Any) -> dict[str, Any]:
    """Serialise ``exc`` into a JSON-RPC 2.0 error response."""
    err = coerce(exc)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": err.jsonrpc_code, "message": err.message},
    }


__all__ = [
    "ForbiddenError",
    "GatewayError",
    "InvalidParamsError",
    "InvalidRequestError",
    "MethodNotFoundError",
    "NotFoundError",
    "ParseError",
    "PayloadTooLargeError",
    "RateLimitedError",
    "RetrievalError",
    "UnauthenticatedError",
    "UnauthorizedToolCallError",
    "UpstreamProtocolError",
    "UpstreamRateLimitedError",
    "UpstreamTimeoutError",
    "UpstreamUnavailableError",
    "coerce",
    "to_gateway_error",
    "to_jsonrpc_error",
]
