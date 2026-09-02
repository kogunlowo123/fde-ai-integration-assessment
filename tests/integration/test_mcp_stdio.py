"""Task 1, protocol behaviour over a real stdio subprocess."""

from __future__ import annotations

import json

from tests.conftest import StdioMcpClient

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


class TestHandshake:
    def test_initialize_returns_server_info(self, mcp_client: StdioMcpClient) -> None:
        # The fixture already completed the handshake; re-read the catalogue.
        result = mcp_client.request("tools/list")["result"]
        names = {tool["name"] for tool in result["tools"]}
        assert names == {"get_customer_record", "trigger_refund"}

    def test_tools_advertise_input_schemas_on_the_wire(self, mcp_client: StdioMcpClient) -> None:
        tools = mcp_client.request("tools/list")["result"]["tools"]
        refund = next(t for t in tools if t["name"] == "trigger_refund")
        assert refund["inputSchema"]["properties"]["amount"]["exclusiveMinimum"] == 0


class TestToolCalls:
    def test_valid_customer_lookup(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        assert response["result"]["isError"] is False
        assert response["result"]["structuredContent"]["customer_id"] == "CUST-12345"

    def test_valid_refund(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-54321", "amount": 10.0, "reason": "Damaged on arrival"},
        )
        payload = response["result"]["structuredContent"]
        assert payload["status"] == "accepted"
        assert payload["refund_id"].startswith("REF-")

    def test_content_block_mirrors_structured_content(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        result = response["result"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


class TestErrorCodes:
    def test_malformed_customer_id_is_invalid_params(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool("get_customer_record", {"customer_id": "CUST-123"})
        assert response["error"]["code"] == INVALID_PARAMS
        assert "result" not in response

    def test_negative_refund_is_invalid_params(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-12345", "amount": -50.0, "reason": "Trying my luck here"},
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_short_reason_is_invalid_params(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-12345", "amount": 5.0, "reason": "short"},
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_unknown_tool_is_method_not_found(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool("admin_reset_key", {})
        assert response["error"]["code"] == METHOD_NOT_FOUND

    def test_unknown_method_is_method_not_found(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.request("tools/destroy")
        assert response["error"]["code"] == METHOD_NOT_FOUND

    def test_error_messages_do_not_leak_internals(self, mcp_client: StdioMcpClient) -> None:
        response = mcp_client.call_tool("get_customer_record", {"customer_id": 12345})
        message = response["error"]["message"]
        for leak in ("Traceback", 'File "', "pydantic", "site-packages", ".py", "fde_assessment"):
            assert leak not in message

    def test_survives_a_malformed_frame_and_keeps_serving(self, mcp_client: StdioMcpClient) -> None:
        # A parse failure must not wedge the session: the next well-formed
        # request still gets an answer.
        mcp_client.send_raw("{not json at all")
        response = mcp_client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        assert response["result"]["isError"] is False
