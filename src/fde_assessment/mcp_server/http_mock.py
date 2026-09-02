"""Mock downstream MCP server over HTTP/JSON-RPC (Task 2's proxy target).

WHAT
    A small FastAPI application that speaks the same JSON-RPC methods as the
    stdio server and additionally exposes ``admin_reset_key``, the
    privileged tool the gateway must protect.

WHY
    Task 2 asks for a gateway "sitting between an AI agent client and a
    downstream mock MCP server". Keeping the mock in ``src/`` rather than in
    the test tree means the end-to-end path is runnable by hand
    (``docker compose up``) and not only under pytest.

HOW
    Reuses the transport-independent ``ToolDispatcher`` so the mock cannot
    drift from the real tool contracts, and records every invocation in
    ``app.state.invocations`` so tests can assert what actually reached it.

WHEN
    Local development, integration tests, and the compose demo. It is not a
    production component: it has **no authentication of its own**, which is
    exactly the deployment assumption the threat model calls out, the
    downstream MCP server must not be reachable from the agent network.

SECURITY
    Intentionally trusting. Its whole purpose is to demonstrate that the
    gateway, not the tool server, is the enforcement point.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fde_assessment.common.errors import GatewayError
from fde_assessment.common.jsonrpc import error_from_exception, parse_request, success_response
from fde_assessment.mcp_server.registry import (
    ServerDeps,
    ToolOutcome,
    ToolSpec,
    build_dispatcher,
)


class AdminResetKeyInput(BaseModel):
    """Arguments for the privileged ``admin_reset_key`` tool."""

    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=1, max_length=64, default="primary")


async def _admin_reset_key(payload: BaseModel) -> ToolOutcome:
    # The dispatcher validated `payload` against this tool's input model, so
    # the cast is a statement about the contract rather than a runtime check.
    payload = cast(AdminResetKeyInput, payload)
    return ToolOutcome(
        {
            "key_id": payload.key_id,
            "rotated": True,
            "new_key_fingerprint": uuid.uuid4().hex[:12],
        }
    )


ADMIN_TOOL = ToolSpec(
    name="admin_reset_key",
    title="Reset API key",
    description="Rotate a service credential. Administrative; must never be reachable by a viewer.",
    input_model=AdminResetKeyInput,
    handler=_admin_reset_key,
    read_only=False,
    destructive=True,
)


def build_mock_downstream_app(deps: ServerDeps | None = None) -> FastAPI:
    """Build the mock downstream MCP server."""
    dispatcher = build_dispatcher(deps or ServerDeps())
    dispatcher.register(ADMIN_TOOL)

    app = FastAPI(title="Mock downstream MCP server", version="0.1.0")
    app.state.invocations = []

    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/rpc")
    async def rpc(request: Request) -> JSONResponse:
        payload: Any = await request.json()
        try:
            rpc_request = parse_request(payload)
        except GatewayError as exc:
            return JSONResponse(error_from_exception(exc, None), status_code=200)

        request.app.state.invocations.append(
            {"method": rpc_request.method, "tool": rpc_request.tool_name}
        )

        if rpc_request.method == "initialize":
            return JSONResponse(
                success_response(
                    rpc_request.id,
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mock-downstream-mcp", "version": "0.1.0"},
                    },
                )
            )

        if rpc_request.method == "ping":
            return JSONResponse(success_response(rpc_request.id, {}))

        if rpc_request.method == "tools/list":
            return JSONResponse(
                success_response(rpc_request.id, {"tools": dispatcher.list_tools()})
            )

        if rpc_request.method == "tools/call":
            params = rpc_request.params or {}
            try:
                outcome = await dispatcher.call(str(params.get("name")), params.get("arguments"))
            except GatewayError as exc:
                return JSONResponse(error_from_exception(exc, rpc_request.id), status_code=200)
            return JSONResponse(
                success_response(
                    rpc_request.id,
                    {
                        "content": [{"type": "text", "text": str(outcome.payload)}],
                        "structuredContent": outcome.payload,
                        "isError": outcome.is_error,
                    },
                )
            )

        from fde_assessment.common.errors import MethodNotFoundError

        return JSONResponse(
            error_from_exception(MethodNotFoundError(), rpc_request.id), status_code=200
        )

    app.include_router(router)
    return app
