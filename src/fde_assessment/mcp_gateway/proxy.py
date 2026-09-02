"""Task 2, downstream forwarding.

WHAT
    A thin async client that forwards an authorized JSON-RPC request to the
    downstream MCP server and normalises every failure mode.

WHY
    The proxy is where an unbounded set of upstream behaviours (timeouts, TLS
    failures, HTML error pages, 500s with stack traces in the body) meets a
    client that must only ever see the gateway's own error vocabulary. Doing
    that translation in one place is what makes "no raw upstream detail
    escapes" checkable.

HOW
    One shared ``httpx.AsyncClient`` per process with a hard timeout and a
    bounded connection pool. Non-JSON or non-object responses become
    ``UpstreamProtocolError``; timeouts become ``UpstreamTimeoutError``;
    transport errors become ``UpstreamUnavailableError``.

WHEN
    Only after ``authorization.authorize`` has allowed the request.

SECURITY
    * The downstream URL comes from configuration, never from the request:
      there is no user-controlled path that can redirect the proxy, which is
      the SSRF control.
    * ``follow_redirects=False``, a downstream 302 must not be able to walk
      the gateway to another host.
    * Response bodies are size-capped and never logged.
"""

from __future__ import annotations

from typing import Any

import httpx

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import (
    UpstreamProtocolError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from fde_assessment.common.logging import get_logger

log = get_logger(__name__)

MAX_DOWNSTREAM_BYTES = 4 * 1024 * 1024


class DownstreamMcpClient:
    """Forwards JSON-RPC payloads to the configured downstream MCP server."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.mcp_downstream_timeout_s),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self.call_count = 0
        """Number of downstream invocations. Asserted by the authorization tests."""

    async def forward(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Send ``payload`` downstream and return the parsed JSON-RPC response."""
        self.call_count += 1
        try:
            response = await self._client.post(
                self._settings.mcp_downstream_url,
                json=payload,
                headers={"content-type": "application/json", "x-request-id": request_id},
            )
        except httpx.TimeoutException as exc:
            log.warning("downstream_timeout", request_id=request_id, method=payload.get("method"))
            raise UpstreamTimeoutError(internal_detail=type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            log.warning(
                "downstream_transport_error",
                request_id=request_id,
                error_type=type(exc).__name__,
            )
            raise UpstreamUnavailableError(internal_detail=type(exc).__name__) from exc

        if response.status_code >= 500:
            log.warning(
                "downstream_server_error", request_id=request_id, status=response.status_code
            )
            raise UpstreamUnavailableError(internal_detail=f"status {response.status_code}")

        if len(response.content) > MAX_DOWNSTREAM_BYTES:
            raise UpstreamProtocolError(internal_detail="response body exceeds cap")

        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamProtocolError(internal_detail="response was not JSON") from exc

        if not isinstance(body, dict):
            raise UpstreamProtocolError(internal_detail="response was not a JSON object")

        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
