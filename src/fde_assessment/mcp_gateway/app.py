"""Task 2, the MCP security gateway (FastAPI).

WHAT
    An HTTP/JSON-RPC reverse proxy that sits between an AI agent and a
    downstream MCP server, applying authentication, method policy, tool-level
    authorization, request bounds, audit logging and timeouts.

WHY
    An MCP server speaks a powerful protocol: ``tools/call`` is remote code
    execution by design. Putting a policy enforcement point in front of it is
    what makes it deployable into an enterprise where the agent, the tools and
    the data have different owners and different blast radii.

HOW
    One route, ``POST /rpc``. The pipeline is deliberately linear and each
    stage can fail closed::

        bound body -> authenticate -> parse JSON-RPC -> authorize -> forward

WHEN
    Point every agent at the gateway, never at the MCP server directly; the
    MCP server should not be routable from the agent network (see
    THREAT-MODEL.md, "network posture").

SECURITY / status-code contract
    A failure that stopped a valid JSON-RPC exchange from starting carries an
    HTTP status: 401 unauthenticated, 413 oversized, 400 unparseable or
    malformed envelope.

    Everything after that point is a JSON-RPC outcome and travels as HTTP 200
    with an error object: a denied tool call (``-32001``, as the assessment
    specifies verbatim), a method outside policy (``-32601``), and an upstream
    failure (``-32004`` timeout, ``-32005`` unavailable). See
    ``TRANSPORT_LEVEL_STATUS`` below and docs/decisions/ADR-011.

    The LLM gateway deliberately does the opposite, it is an OpenAI-shaped
    REST API, so it answers 429, 502 and 504, because its clients are HTTP
    clients rather than JSON-RPC clients. ADR-011 records why the two surfaces
    differ on purpose.

COST
    Rejecting an unauthorized ``admin_*`` call at the gateway costs one
    dictionary lookup and never opens a downstream connection.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from fde_assessment.common.config import Settings, get_settings
from fde_assessment.common.errors import (
    GatewayError,
    ParseError,
    PayloadTooLargeError,
    UnauthenticatedError,
    UnauthorizedToolCallError,
)
from fde_assessment.common.jsonrpc import error_from_exception, parse_request
from fde_assessment.common.logging import configure_logging, get_logger
from fde_assessment.mcp_gateway.auth import authenticate
from fde_assessment.mcp_gateway.authorization import authorize
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient
from fde_assessment.observability.metrics import (
    GATEWAY_LATENCY_MS,
    METRICS,
    REQUESTS_FAILED_TOTAL,
    REQUESTS_TOTAL,
    UNAUTHORIZED_TOOL_CALLS_TOTAL,
    Timer,
)

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"


def _request_id(request: Request) -> str:
    """Reuse a caller-supplied correlation id when it is safe, else mint one.

    A caller-controlled value is echoed into logs, so it is length-capped and
    filtered to an unambiguous character set: an id containing newlines is a
    log-injection primitive.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    cleaned = "".join(c for c in supplied if c.isalnum() or c in "-_")[:64]
    return cleaned or f"mcpgw-{uuid.uuid4().hex[:16]}"


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Read the body, refusing anything over ``limit`` bytes.

    Both the declared ``Content-Length`` and the actual streamed size are
    checked: a chunked request can lie about (or omit) the former.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(internal_detail=f"content-length {declared} > {limit}")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(internal_detail="streamed body exceeded limit")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: Settings | None = None,
    downstream: DownstreamMcpClient | None = None,
) -> FastAPI:
    """Build the gateway application.

    ``downstream`` is injectable so tests can supply a client wired to an
    in-process mock and assert on its invocation count.
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, fmt=resolved.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.downstream = downstream or DownstreamMcpClient(resolved)
        app.state.settings = resolved
        log.info(
            "mcp_gateway_starting",
            downstream=resolved.mcp_downstream_url,
            max_body_bytes=resolved.mcp_gateway_max_body_bytes,
        )
        try:
            yield
        finally:
            await app.state.downstream.aclose()

    app = FastAPI(
        title="MCP Security Gateway",
        version="0.1.0",
        description="Authenticating, authorizing reverse proxy for MCP JSON-RPC.",
        lifespan=lifespan,
    )
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/metrics")
    async def metrics() -> Response:
        return PlainTextResponse(METRICS.render_prometheus(), media_type="text/plain")

    @router.post("/rpc")
    async def rpc(request: Request) -> Response:
        request_id = _request_id(request)
        started = time.perf_counter()
        method = "unknown"
        tool_name: str | None = None

        with Timer(METRICS, GATEWAY_LATENCY_MS, surface="mcp_gateway"):
            try:
                raw = await _read_bounded_body(request, resolved.mcp_gateway_max_body_bytes)

                # Authentication precedes parsing: an unauthenticated caller
                # should not be able to exercise the parser at all.
                principal = authenticate(request.headers.get("authorization"), resolved)

                try:
                    payload = _loads(raw)
                except ValueError as exc:
                    raise ParseError(internal_detail="body was not valid JSON") from exc

                rpc_request = parse_request(payload)
                method = rpc_request.method

                decision = authorize(rpc_request, principal, resolved)
                tool_name = decision.tool_name

                if not decision.allowed:
                    # `authorize` always attaches an error to a denial; falling
                    # back to a generic denial keeps this fail-closed even if a
                    # future policy branch forgets to.
                    denial = decision.error or UnauthorizedToolCallError(
                        internal_detail="denied without an explicit error"
                    )
                    METRICS.increment(
                        UNAUTHORIZED_TOOL_CALLS_TOTAL,
                        tool=tool_name or "unknown",
                        reason=decision.reason,
                    )
                    _audit(
                        request_id=request_id,
                        principal_subject=principal.subject,
                        role=str(principal.role),
                        method=method,
                        tool=tool_name,
                        outcome="denied",
                        reason=decision.reason,
                        downstream_called=False,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                    )
                    # Protocol-level denial: HTTP 200 with a JSON-RPC error.
                    return JSONResponse(
                        error_from_exception(denial, rpc_request.id),
                        status_code=200,
                        headers={REQUEST_ID_HEADER: request_id},
                    )

                client: DownstreamMcpClient = request.app.state.downstream
                body = await client.forward(payload, request_id)

                METRICS.increment(REQUESTS_TOTAL, surface="mcp_gateway", method=method)
                _audit(
                    request_id=request_id,
                    principal_subject=principal.subject,
                    role=str(principal.role),
                    method=method,
                    tool=tool_name,
                    outcome="forwarded",
                    reason=decision.reason,
                    downstream_called=True,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
                return JSONResponse(body, status_code=200, headers={REQUEST_ID_HEADER: request_id})

            except GatewayError as exc:
                return _error_response(exc, request_id, method, tool_name, started)
            except Exception:  # pragma: no cover - defensive
                log.exception("mcp_gateway_unhandled", request_id=request_id)
                return _error_response(GatewayError(), request_id, method, tool_name, started)

    app.include_router(router)
    return app


def _loads(raw: bytes) -> Any:
    import json

    return json.loads(raw.decode("utf-8"))


# The status-code contract, in one place (ADR-011).
#
# A failure that prevented a valid JSON-RPC exchange from ever starting is an
# HTTP-level failure and carries an HTTP status: the caller could not be
# authenticated, the body was too large to read, or it was not a parseable
# JSON-RPC request.
#
# Everything after that point is a JSON-RPC outcome and travels as HTTP 200
# with an error object, including a denied tool call (-32001, as the
# assessment specifies) and an upstream failure (-32004 / -32005). A JSON-RPC
# client is required to read the error object; many will not read a body at
# all on a 5xx, so a downstream timeout reported as HTTP 504 would reach the
# agent as "gateway broken" rather than "that call did not work".
TRANSPORT_LEVEL_STATUS: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "PAYLOAD_TOO_LARGE": 413,
    "PARSE_ERROR": 400,
    "INVALID_REQUEST": 400,
}


def http_status_for(exc: GatewayError) -> int:
    """HTTP status for ``exc`` under the contract above."""
    return TRANSPORT_LEVEL_STATUS.get(exc.code, 200)


def _error_response(
    exc: GatewayError,
    request_id: str,
    method: str,
    tool_name: str | None,
    started: float,
) -> JSONResponse:
    """Render a ``GatewayError`` as a JSON-RPC error with the right status."""
    METRICS.increment(REQUESTS_FAILED_TOTAL, surface="mcp_gateway", code=exc.code)
    log.warning(
        "mcp_gateway_error",
        request_id=request_id,
        method=method,
        tool=tool_name,
        code=exc.code,
        # `internal_detail` is engineered to be safe to log and is never
        # serialised into the response body.
        detail=exc.internal_detail,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    status = http_status_for(exc)
    headers = {REQUEST_ID_HEADER: request_id, **exc.headers}
    if isinstance(exc, UnauthenticatedError):
        headers["www-authenticate"] = "Bearer"
    return JSONResponse(error_from_exception(exc, None), status_code=status, headers=headers)


def _audit(**fields: object) -> None:
    """Emit one structured audit event per gateway decision.

    Deliberately records *who* (fingerprinted subject), *what* (method, tool),
    *the decision*, and *whether the downstream was invoked*, the last field
    is what makes "denied calls never reached the tool server" auditable after
    the fact, not just testable.
    """
    log.info("mcp_gateway_audit", **fields)


app = create_app  # convenient alias for `uvicorn ... --factory`
