"""Task 2, proxy behaviour: framing, bounds, timeouts, error sanitisation."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_assessment.common.config import Settings
from fde_assessment.mcp_gateway.app import create_app
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient
from tests.conftest import GatewayStack

VIEWER = "dev-viewer-token"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601


class TestFraming:
    def test_response_id_matches_request_id(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": "corr-42", "method": "ping", "params": {}}
        )
        assert response.json()["id"] == "corr-42"

    @pytest.mark.parametrize(
        "payload",
        [
            {"id": 1, "method": "ping"},  # missing jsonrpc
            {"jsonrpc": "1.0", "id": 1, "method": "ping"},  # wrong version
            {"jsonrpc": "2.0", "id": 1},  # missing method
            {"jsonrpc": "2.0", "id": 1, "method": ""},  # empty method
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},  # params not an object
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1},  # unknown envelope field
            ["batch", "requests"],  # array payload
            42,  # valid JSON, not an object
        ],
        ids=[
            "no-version",
            "wrong-version",
            "no-method",
            "empty-method",
            "params-array",
            "extra-field",
            "array",
            "number",
        ],
    )
    def test_malformed_envelopes_are_rejected_before_forwarding(
        self, gateway_stack: GatewayStack, payload: object
    ) -> None:
        response = gateway_stack.rpc(payload)  # type: ignore[arg-type]
        assert response.json()["error"]["code"] == INVALID_REQUEST
        assert gateway_stack.downstream_calls == 0

    def test_non_json_body_is_a_parse_error(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc("{not json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == PARSE_ERROR
        assert gateway_stack.downstream_calls == 0

    def test_correlation_id_is_echoed(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"x-request-id": "trace-123"}
        )
        assert response.headers["x-request-id"] == "trace-123"

    def test_hostile_correlation_id_is_sanitised(self, gateway_stack: GatewayStack) -> None:
        # The id is echoed into structured logs, so anything outside
        # [A-Za-z0-9-_] is stripped: a caller must not be able to forge log
        # fields or smuggle control characters through it.
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"x-request-id": 'abc/../123 "level":"error"'},
        )
        assert response.headers["x-request-id"] == "abc123levelerror"

    def test_overlong_correlation_id_is_truncated(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"x-request-id": "a" * 500},
        )
        assert response.headers["x-request-id"] == "a" * 64

    def test_unknown_method_is_rejected_at_the_gateway(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc({"jsonrpc": "2.0", "id": 1, "method": "server/shutdown"})
        assert response.status_code == 200  # ADR-011: a JSON-RPC-level outcome
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND
        assert gateway_stack.downstream_calls == 0


class TestRequestBounds:
    def test_oversized_body_is_413(self, gateway_stack: GatewayStack) -> None:
        huge = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_customer_record", "arguments": {"pad": "x" * 400_000}},
        }
        response = gateway_stack.rpc(huge)
        assert response.status_code == 413
        assert gateway_stack.downstream_calls == 0

    def test_body_just_under_the_limit_is_accepted(self, gateway_stack: GatewayStack) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}},
        }
        assert gateway_stack.rpc(payload).status_code == 200


def _stack_with_downstream(settings: Settings, handler) -> tuple[TestClient, DownstreamMcpClient]:
    """Build a gateway whose downstream is an arbitrary httpx handler."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://downstream")
    tuned = settings.model_copy(update={"mcp_downstream_url": "http://downstream/rpc"})
    downstream = DownstreamMcpClient(tuned, client=http_client)
    return TestClient(create_app(tuned, downstream=downstream)), downstream


class TestUpstreamFailureSanitisation:
    def _post(self, client: TestClient) -> httpx.Response:
        return client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"authorization": f"Bearer {VIEWER}"},
        )

    def test_downstream_timeout_becomes_a_safe_error(self, settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out reading from 10.0.0.5:9000", request=request)

        client, _ = _stack_with_downstream(settings, handler)
        with client:
            response = self._post(client)
        body = response.json()
        # ADR-011: a well-formed request that failed downstream is a JSON-RPC
        # outcome, so it travels as HTTP 200 with an error object.
        assert response.status_code == 200
        assert body["error"]["code"] == -32004
        assert body["error"]["message"] == "The model service did not respond in time."
        assert "10.0.0.5" not in str(body)

    def test_downstream_500_becomes_a_safe_error(self, settings: Settings) -> None:
        leak = "Traceback (most recent call last): File '/srv/app/mcp.py', line 42"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=leak)

        client, _ = _stack_with_downstream(settings, handler)
        with client:
            response = self._post(client)
        body = response.json()
        assert response.status_code == 200
        assert "Traceback" not in str(body)
        assert "/srv/app" not in str(body)
        assert body["error"]["code"] == -32005

    def test_downstream_html_becomes_a_protocol_error(self, settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>nginx 502</body></html>")

        client, _ = _stack_with_downstream(settings, handler)
        with client:
            body = self._post(client).json()
        assert body["error"]["message"] == "The model service returned a malformed response."

    def test_downstream_json_array_becomes_a_protocol_error(self, settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        client, _ = _stack_with_downstream(settings, handler)
        with client:
            body = self._post(client).json()
        assert body["error"]["message"] == "The model service returned a malformed response."

    def test_connection_refused_becomes_a_safe_error(self, settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

        client, _ = _stack_with_downstream(settings, handler)
        with client:
            body = self._post(client).json()
        assert "Errno" not in str(body)
        assert body["error"]["message"] == "The model service is temporarily unavailable."


class TestOperationalEndpoints:
    def test_healthz(self, gateway_stack: GatewayStack) -> None:
        assert gateway_stack.client.get("/healthz").json() == {"status": "ok"}

    def test_metrics_render(self, gateway_stack: GatewayStack) -> None:
        gateway_stack.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        text = gateway_stack.client.get("/metrics").text
        assert "requests_total" in text


class TestDownstreamAppIsNotDirectlyExposed:
    def test_mock_downstream_has_no_authentication(self) -> None:
        """Documents the deployment assumption the threat model relies on."""
        from fde_assessment.mcp_server.http_mock import build_mock_downstream_app

        app: FastAPI = build_mock_downstream_app()
        with TestClient(app) as client:
            response = client.post(
                "/rpc",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "admin_reset_key", "arguments": {}},
                },
            )
        # No credential was presented and the call succeeded: the downstream
        # MCP server MUST NOT be routable from the agent network.
        assert response.json()["result"]["structuredContent"]["rotated"] is True


class TestStatusCodeContract:
    """ADR-011, asserted rather than described."""

    def test_transport_failures_carry_an_http_status(self, gateway_stack: GatewayStack) -> None:
        assert (
            gateway_stack.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None).status_code
            == 401
        )
        assert gateway_stack.rpc("{not json").status_code == 400
        assert gateway_stack.rpc({"method": "ping"}).status_code == 400

    def test_jsonrpc_outcomes_travel_as_200(self, gateway_stack: GatewayStack) -> None:
        denied = gateway_stack.rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "admin_reset_key", "arguments": {}},
            }
        )
        assert denied.status_code == 200
        assert denied.json()["error"]["code"] == -32001

    def test_the_mapping_is_declared_in_one_place(self) -> None:
        from fde_assessment.common.errors import (
            RateLimitedError,
            UnauthenticatedError,
            UpstreamTimeoutError,
        )
        from fde_assessment.mcp_gateway.app import http_status_for

        assert http_status_for(UnauthenticatedError()) == 401
        assert http_status_for(UpstreamTimeoutError()) == 200
        assert http_status_for(RateLimitedError()) == 200
