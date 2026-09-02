"""Adversarial probes.

Attacks tried against the finished system rather than behaviours designed for.
Each test records what was attempted and what actually happened; where the
outcome is "allowed but harmless", that is stated rather than dressed up as a
defence.
"""

from __future__ import annotations

import json

import pytest

from fde_assessment.common.errors import InvalidParamsError
from fde_assessment.llm_gateway.guardrails.pii import redact
from fde_assessment.llm_gateway.guardrails.streaming import StreamingRedactor
from fde_assessment.mcp_server.registry import ServerDeps, build_dispatcher
from tests.conftest import GatewayStack, LlmStack

VIEWER = "dev-viewer-token"
ADMIN = "dev-admin-token"
UNAUTHORIZED_TOOL_CALL = -32001
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600


def call(tool: str, arguments: object = None) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "adv",
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments if arguments is not None else {}},
    }


class TestAuthorizationBypassAttempts:
    @pytest.mark.parametrize(
        "name",
        [
            "admin_reset_key ",  # trailing space
            "admin_reset_key\n",  # trailing newline
            "admin_reset_key\t",
            "admin_" + "\u200b" + "reset_key",  # zero-width space after the prefix
            "admin_../admin_reset_key",
        ],
    )
    def test_prefix_evasion_by_whitespace_or_padding_still_denies(
        self, gateway_stack: GatewayStack, name: str
    ) -> None:
        """These all still start with `admin_`, so the gate still closes."""
        response = gateway_stack.rpc(call(name), token=VIEWER)
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert gateway_stack.downstream_calls == 0

    def test_homoglyph_prefix_is_not_treated_as_admin(self, gateway_stack: GatewayStack) -> None:
        """A Cyrillic 'а' does not match the ASCII prefix.

        Documented outcome: the call is forwarded and the downstream rejects it
        as an unknown tool. That is safe *because* no tool with that name
        exists. A deployment that registered a homoglyph tool name downstream
        would have a real bypass, which is why tool registration is a code
        change and a review (see ADR-002 and THREAT-MODEL.md).
        """
        response = gateway_stack.rpc(call("\u0430dmin_reset_key"), token=VIEWER)
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND
        assert gateway_stack.downstream_calls == 1

    def test_duplicate_json_keys_do_not_confuse_the_policy(
        self, gateway_stack: GatewayStack
    ) -> None:
        """Last-value-wins parsing must not let a benign first name slip past."""
        raw = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"get_customer_record","name":"admin_reset_key","arguments":{}}}'
        )
        response = gateway_stack.rpc(raw, token=VIEWER)
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert gateway_stack.downstream_calls == 0

    def test_method_case_variation_is_not_a_policy_hole(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "TOOLS/CALL", "params": {"name": "admin_x"}},
            token=VIEWER,
        )
        # Unknown method -> rejected by the allowlist, never forwarded.
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND
        assert gateway_stack.downstream_calls == 0

    def test_second_authorization_header_does_not_upgrade_the_role(
        self, gateway_stack: GatewayStack
    ) -> None:
        """httpx joins repeated headers; the token must still not resolve."""
        response = gateway_stack.client.post(
            "/rpc",
            json=call("admin_reset_key"),
            headers=[("authorization", f"Bearer {VIEWER}"), ("authorization", f"Bearer {ADMIN}")],
        )
        assert response.status_code in (401, 200)
        if response.status_code == 200:
            assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert gateway_stack.downstream_calls == 0

    def test_null_byte_in_the_tool_name(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("admin_reset_key\x00get_customer_record"), token=VIEWER)
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
        assert gateway_stack.downstream_calls == 0


class TestParserAbuse:
    def test_deeply_nested_json_is_rejected_or_survived(self, gateway_stack: GatewayStack) -> None:
        """A nesting bomb must not crash the process."""
        depth = 2_000
        payload = "[" * depth + "]" * depth
        raw = f'{{"jsonrpc":"2.0","id":1,"method":"ping","params":{{"x":{payload}}}}}'
        response = gateway_stack.rpc(raw, token=VIEWER)
        # Either the parser refuses it or the envelope validator does; what
        # matters is a clean error and a still-serving gateway.
        assert response.status_code in (200, 400)
        assert gateway_stack.rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"}).status_code == 200

    def test_enormous_string_field_is_bounded(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc(call("x" * 300_000), token=VIEWER)
        assert response.status_code == 413
        assert gateway_stack.downstream_calls == 0

    def test_method_longer_than_the_schema_allows(self, gateway_stack: GatewayStack) -> None:
        response = gateway_stack.rpc({"jsonrpc": "2.0", "id": 1, "method": "a" * 500}, token=VIEWER)
        assert response.json()["error"]["code"] == INVALID_REQUEST
        assert gateway_stack.downstream_calls == 0

    def test_unicode_direction_override_in_a_tool_name(self, gateway_stack: GatewayStack) -> None:
        """RLO characters can make a name *display* as something else."""
        response = gateway_stack.rpc(call("admin_\u202ereset_key"), token=VIEWER)
        assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL


class TestToolInputAbuse:
    async def test_prototype_pollution_style_keys_are_rejected(self) -> None:
        dispatcher = build_dispatcher(ServerDeps())
        with pytest.raises(InvalidParamsError):
            await dispatcher.call(
                "get_customer_record",
                {"customer_id": "CUST-12345", "__proto__": {"role": "admin"}},
            )

    @pytest.mark.parametrize(
        "customer_id",
        [
            "CUST-12345\x00",
            "CUST-12345\u202e",
            "CUST-１２３４５",  # full-width digits
            "CUST-12345\r\nX-Injected: true",
        ],
    )
    async def test_control_characters_and_homoglyph_digits_are_rejected(
        self, customer_id: str
    ) -> None:
        dispatcher = build_dispatcher(ServerDeps())
        with pytest.raises(InvalidParamsError):
            await dispatcher.call("get_customer_record", {"customer_id": customer_id})

    @pytest.mark.parametrize("amount", [1e308, 1e400, 10**20, "1e5", "0x10"])
    async def test_extreme_refund_amounts_are_rejected(self, amount: object) -> None:
        dispatcher = build_dispatcher(ServerDeps())
        with pytest.raises(InvalidParamsError):
            await dispatcher.call(
                "trigger_refund",
                {"customer_id": "CUST-12345", "amount": amount, "reason": "Trying it on here"},
            )

    async def test_a_refund_reason_cannot_smuggle_a_huge_payload(self) -> None:
        dispatcher = build_dispatcher(ServerDeps())
        with pytest.raises(InvalidParamsError):
            await dispatcher.call(
                "trigger_refund",
                {"customer_id": "CUST-12345", "amount": 1.0, "reason": "x" * 100_000},
            )


class TestGuardrailEvasion:
    """What the regex guardrail does and does not catch, stated honestly."""

    @pytest.mark.parametrize(
        "text",
        [
            "mail: john.smith@example.com",
            "mail: JOHN.SMITH@EXAMPLE.COM",
            "mail:john.smith@example.com,next",
            "(john.smith@example.com)",
            "<john.smith@example.com>",
        ],
    )
    def test_formatting_variations_are_still_caught(self, text: str) -> None:
        assert "example.com" not in redact(text).text

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("j o h n @ e x a m p l e . c o m", "spaced out"),
            ("john.smith AT example DOT com", "spelled out"),
            ("am9obi5zbWl0aEBleGFtcGxlLmNvbQ==", "base64"),
            ("john.smith@example[.]com", "defanged"),
        ],
    )
    def test_obfuscation_defeats_the_regex_guardrail(self, text: str, why: str) -> None:
        """Asserted, not hidden: this is the documented recall limit.

        A model that deliberately encodes data is not stopped by pattern
        matching. See SECURITY.md, "Not claimed".
        """
        assert redact(text).text == text

    def test_interleaved_pii_across_many_tiny_chunks(self) -> None:
        text = "a@b.co 123-45-6789 4111111111111111"
        redactor = StreamingRedactor()
        out = "".join(redactor.process(c) for c in text) + redactor.flush()
        assert out.count("[REDACTED]") == 3
        assert "4111" not in out

    def test_pii_repeated_at_the_window_boundary(self) -> None:
        """A stream engineered so a match starts exactly at the emit cut."""
        redactor = StreamingRedactor(window=32)
        filler = "x" * 31
        out = redactor.process(filler + "john.smith@") + redactor.process("example.com ")
        out += redactor.flush()
        assert "john.smith@example.com" not in out


class TestQuotaAbuse:
    def test_a_tenant_cannot_exceed_its_budget_by_racing(self, llm_stack: LlmStack) -> None:
        """Sequential here; `tests/concurrency/` covers the parallel case."""
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}
        statuses = [llm_stack.completions(body).status_code for _ in range(5)]
        assert set(statuses) <= {200, 429}

    def test_max_tokens_above_the_schema_ceiling_is_rejected(self, llm_stack: LlmStack) -> None:
        response = llm_stack.completions(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 999_999}
        )
        assert response.status_code == 422

    def test_a_huge_message_list_is_rejected(self, llm_stack: LlmStack) -> None:
        response = llm_stack.completions(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}] * 500,
            }
        )
        assert response.status_code == 422


class TestErrorSurface:
    def test_no_response_anywhere_contains_a_file_path(
        self, gateway_stack: GatewayStack, llm_stack: LlmStack
    ) -> None:
        bodies = [
            gateway_stack.rpc(call("admin_reset_key"), token=VIEWER).text,
            gateway_stack.rpc("{bad", token=VIEWER).text,
            gateway_stack.rpc(call("get_customer_record", {"customer_id": "x"}), token=VIEWER).text,
            llm_stack.completions({"model": "", "messages": []}).text,
            llm_stack.completions(key="nope").text,
        ]
        for body in bodies:
            for leak in ("site-packages", "Traceback", ".py", "fde_assessment", "C:\\", "/usr/"):
                assert leak not in body, f"{leak!r} leaked in {body[:120]}"

    def test_error_bodies_are_small_and_structured(self, llm_stack: LlmStack) -> None:
        body = llm_stack.completions(key="nope")
        payload = json.loads(body.text)
        assert set(payload) == {"error"}
        assert set(payload["error"]) == {"type", "code", "message", "request_id"}
        assert len(body.text) < 300
