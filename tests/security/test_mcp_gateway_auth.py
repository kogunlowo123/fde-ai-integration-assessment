"""Task 2, authentication and tool authorization at the MCP gateway.

The scored requirement: a viewer calling an ``admin_`` tool is intercepted with
JSON-RPC ``-32001`` and the downstream server is never invoked. Both halves are
asserted, the payload *and* the downstream call count.
"""

from __future__ import annotations

import pytest

from fde_assessment.common.config import Role, Settings
from fde_assessment.common.errors import UnauthenticatedError
from fde_assessment.common.jsonrpc import JsonRpcRequest
from fde_assessment.common.models import GatewayPrincipal
from fde_assessment.mcp_gateway.auth import authenticate, parse_bearer
from fde_assessment.mcp_gateway.authorization import authorize
from tests.conftest import GatewayStack

UNAUTHORIZED_TOOL_CALL = -32001
METHOD_NOT_FOUND = -32601

ADMIN = "dev-admin-token"
VIEWER = "dev-viewer-token"


def call(tool: str, arguments: object = None, request_id: str = "req-1") -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments if arguments is not None else {}},
    }


class TestBearerParsing:
    def test_extracts_the_token(self) -> None:
        assert parse_bearer("Bearer abc123") == "abc123"

    @pytest.mark.parametrize(
        "header",
        [None, "", "abc123", "Basic abc123", "bearer abc123", "Bearer ", "Bearer    "],
        ids=["missing", "empty", "no-scheme", "wrong-scheme", "lowercase", "empty-token", "spaces"],
    )
    def test_rejects_malformed_headers(self, header: str | None) -> None:
        with pytest.raises(UnauthenticatedError):
            parse_bearer(header)

    def test_unknown_token_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(UnauthenticatedError):
            authenticate("Bearer not-a-real-token", settings)

    def test_principal_never_carries_the_raw_token(self, settings: Settings) -> None:
        principal = authenticate(f"Bearer {ADMIN}", settings)
        assert ADMIN not in principal.subject
        assert ADMIN not in principal.token_fingerprint
        assert principal.role is Role.admin


class TestAuthorizationUnit:
    def test_body_supplied_role_is_ignored(self, settings: Settings) -> None:
        """A viewer that claims to be an admin in the payload stays a viewer."""
        viewer = GatewayPrincipal("token:aaaa", Role.viewer, "ffff")
        request = JsonRpcRequest.model_validate(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "admin_reset_key", "role": "admin", "arguments": {}},
            }
        )
        decision = authorize(request, viewer, settings)
        assert decision.allowed is False
        assert decision.reason == "insufficient_role"

    def test_non_string_tool_name_is_denied(self, settings: Settings) -> None:
        viewer = GatewayPrincipal("token:aaaa", Role.viewer, "ffff")
        request = JsonRpcRequest.model_validate(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": {"$ne": None}}}
        )
        decision = authorize(request, viewer, settings)
        assert decision.allowed is False
        assert decision.reason == "unreadable_tool_name"

    def test_unknown_method_is_denied(self, settings: Settings) -> None:
        admin = GatewayPrincipal("token:aaaa", Role.admin, "ffff")
        request = JsonRpcRequest.model_validate(
            {"jsonrpc": "2.0", "id": 1, "method": "server/shutdown"}
        )
        decision = authorize(request, admin, settings)
        assert decision.allowed is False
        assert decision.reason == "method_not_allowed"


class TestUnauthenticated:
    def test_missing_token_is_401(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("get_customer_record"), token=None)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert gateway_stack.downstream_calls == 0

    def test_invalid_token_is_401(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("get_customer_record"), token="forged-token")
        assert response.status_code == 401
        assert gateway_stack.downstream_calls == 0

    def test_tampered_token_is_401(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("get_customer_record"), token=ADMIN + "x")
        assert response.status_code == 401
        assert gateway_stack.downstream_calls == 0

    def test_401_body_is_jsonrpc_shaped_and_leaks_nothing(
        self, gateway_stack: GatewayStack
    ) -> None:
        body = gateway_stack.rpc(call("get_customer_record"), token=None).json()
        assert body["jsonrpc"] == "2.0"
        assert set(body["error"]) == {"code", "message"}
        assert "token" not in body["error"]["message"].lower()


class TestToolAuthorization:
    def test_viewer_calling_admin_tool_is_intercepted(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("admin_reset_key", request_id="abc"), token=VIEWER)
        assert response.status_code == 200
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": "abc",
            "error": {"code": UNAUTHORIZED_TOOL_CALL, "message": "Unauthorized Tool Call"},
        }

    def test_downstream_is_never_invoked_for_a_denied_call(
        self, gateway_stack: GatewayStack
    ) -> None:
        for _ in range(5):
            gateway_stack.rpc(call("admin_reset_key"), token=VIEWER)
        assert gateway_stack.downstream_calls == 0
        assert gateway_stack.downstream_invocations == []

    def test_admin_calling_admin_tool_is_forwarded(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("admin_reset_key", {"key_id": "primary"}), token=ADMIN)
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["structuredContent"]["rotated"] is True
        assert gateway_stack.downstream_calls == 1

    def test_viewer_may_call_a_non_admin_tool(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            call("get_customer_record", {"customer_id": "CUST-12345"}), token=VIEWER
        )
        assert response.json()["result"]["structuredContent"]["tier"] == "gold"
        assert gateway_stack.downstream_calls == 1

    @pytest.mark.parametrize(
        "tool",
        ["admin_reset_key", "admin_", "admin_delete_everything", "admin_x"],
    )
    def test_every_admin_prefixed_name_is_gated(
        self, gateway_stack: GatewayStack, tool: str
    ) -> None:
        response = gateway_stack.rpc(call(tool), token=VIEWER)
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert gateway_stack.downstream_calls == 0

    @pytest.mark.parametrize(
        "tool",
        ["Admin_reset_key", "ADMIN_reset_key", " admin_reset_key", "x_admin_reset_key"],
    )
    def test_prefix_check_is_exact_and_case_sensitive(
        self, gateway_stack: GatewayStack, tool: str
    ) -> None:
        """Names that only *look* administrative are forwarded, then rejected
        downstream as unknown tools, the gateway does not guess."""
        response = gateway_stack.rpc(call(tool), token=VIEWER)
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND
        assert gateway_stack.downstream_calls == 1

    def test_tools_list_is_forwarded_transparently(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}, token=VIEWER
        )
        body = response.json()
        assert body["id"] == 7
        names = {tool["name"] for tool in body["result"]["tools"]}
        assert "admin_reset_key" in names  # listing is transparent; calling is not
        assert gateway_stack.downstream_calls == 1
