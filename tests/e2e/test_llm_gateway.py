"""Tasks 3 + 4 end to end, HTTP in, guarded SSE out.

Client -> LLM Gateway -> Primary -> (Fallback) -> Guardrail -> Client.
"""

from __future__ import annotations

import json

import pytest

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import UpstreamRateLimitedError
from fde_assessment.llm_gateway.providers.mock import (
    HangingProvider,
    MockProvider,
    ScriptedFailureProvider,
)
from tests.conftest import LlmStack, build_llm_stack

PII_SCRIPT = "Contact john.smith@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Done."


def sse_text(raw: str) -> str:
    """Concatenate the content deltas from an SSE body."""
    out: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        frame = json.loads(line[6:])
        for choice in frame.get("choices", []):
            out.append(choice.get("delta", {}).get("content", ""))
    return "".join(out)


def sse_frames(raw: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in raw.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


class TestAuthentication:
    def test_missing_key_is_401(self, llm_stack: LlmStack) -> None:
        response = llm_stack.completions(key=None)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_unknown_key_is_401(self, llm_stack: LlmStack) -> None:
        assert llm_stack.completions(key="nope").status_code == 401

    def test_x_api_key_header_is_accepted(self, llm_stack: LlmStack) -> None:
        response = llm_stack.completions(key=None, headers={"x-api-key": "dev-tenant-a-key"})
        assert response.status_code == 200


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"model": "m"},
            {"model": "m", "messages": []},
            {"model": "", "messages": [{"role": "user", "content": "hi"}]},
            {"model": "m", "messages": [{"role": "root", "content": "hi"}]},
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 9},
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 0},
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "unexpected": 1},
        ],
        ids=[
            "empty",
            "no-messages",
            "empty-messages",
            "empty-model",
            "bad-role",
            "temperature-out-of-range",
            "zero-max-tokens",
            "unknown-field",
        ],
    )
    def test_invalid_bodies_are_422(self, llm_stack: LlmStack, body: dict) -> None:
        response = llm_stack.completions(body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PARAMS"

    def test_oversized_body_is_413(self, llm_stack: LlmStack) -> None:
        body = {
            "model": "mock-primary",
            "messages": [{"role": "user", "content": "x" * 1_100_000}],
        }
        assert llm_stack.completions(body).status_code == 413


class TestNonStreaming:
    def test_returns_an_openai_shaped_body(self, llm_stack: LlmStack) -> None:
        body = llm_stack.completions().json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["usage"]["total_tokens"] > 0
        assert body["x_gateway"]["provider"] == "mock-primary"

    def test_pii_is_redacted_in_the_collected_answer(self, settings: Settings) -> None:
        provider = MockProvider("mock-primary", script=PII_SCRIPT, chunk_size=5)
        for stack in build_llm_stack(settings, primary=provider):
            content = stack.completions().json()["choices"][0]["message"]["content"]
            assert "john.smith@example.com" not in content
            assert "123-45-6789" not in content
            assert "4111" not in content
            assert content.count("[REDACTED]") == 3


class TestStreaming:
    def _stream(self, stack: LlmStack, body: dict | None = None) -> str:
        payload = body or {
            "model": "mock-primary",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        return stack.completions(payload).text

    def test_streams_sse_frames(self, llm_stack: LlmStack) -> None:
        raw = self._stream(llm_stack)
        assert raw.rstrip().endswith("data: [DONE]")
        assert sse_text(raw)

    def test_final_frame_carries_finish_reason(self, llm_stack: LlmStack) -> None:
        frames = sse_frames(self._stream(llm_stack))
        assert frames[-1]["choices"][0]["finish_reason"] == "stop"

    def test_pii_split_across_provider_chunks_is_redacted(self, settings: Settings) -> None:
        # chunk_size=3 guarantees the email, SSN and card are each split.
        provider = MockProvider("mock-primary", script=PII_SCRIPT, chunk_size=3)
        for stack in build_llm_stack(settings, primary=provider):
            text = sse_text(self._stream(stack))
            assert "john.smith@example.com" not in text
            assert "123-45-6789" not in text
            assert "4111 1111 1111 1111" not in text
            assert text.count("[REDACTED]") == 3
            assert text.endswith("Done.")

    def test_single_character_chunks_still_redact(self, settings: Settings) -> None:
        provider = MockProvider("mock-primary", script=PII_SCRIPT, chunk_size=1)
        for stack in build_llm_stack(settings, primary=provider):
            assert sse_text(self._stream(stack)).count("[REDACTED]") == 3


class TestRateLimiting:
    def test_over_budget_request_is_429(self, settings: Settings) -> None:
        tuned = settings.model_copy(update={"rate_limit_tokens": 100, "max_output_tokens": 50})
        for stack in build_llm_stack(tuned):
            assert stack.completions().status_code == 200
            second = stack.completions()
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            assert int(second.headers["retry-after"]) >= 1
            assert second.headers["x-ratelimit-limit-tokens"] == "100"

    def test_a_rejected_request_never_reaches_the_provider(self, settings: Settings) -> None:
        tuned = settings.model_copy(update={"rate_limit_tokens": 10, "max_output_tokens": 50})
        provider = MockProvider("mock-primary")
        for stack in build_llm_stack(tuned, primary=provider):
            assert stack.completions().status_code == 429
            assert provider.call_count == 0

    def test_tenants_are_isolated(self, settings: Settings) -> None:
        tuned = settings.model_copy(update={"rate_limit_tokens": 100, "max_output_tokens": 50})
        for stack in build_llm_stack(tuned):
            assert stack.completions(key="dev-tenant-a-key").status_code == 200
            assert stack.completions(key="dev-tenant-a-key").status_code == 429
            assert stack.completions(key="dev-tenant-b-key").status_code == 200


class TestFallback:
    def test_primary_429_fails_over(self, settings: Settings) -> None:
        primary = ScriptedFailureProvider(UpstreamRateLimitedError("429"), name="mock-primary")
        for stack in build_llm_stack(settings, primary=primary):
            body = stack.completions().json()
            assert body["choices"][0]["message"]["content"] == "secondary answer"
            assert body["x_gateway"]["fell_back"] is True

    def test_primary_timeout_fails_over(self, settings: Settings) -> None:
        primary = HangingProvider("mock-primary", hang_s=30)
        for stack in build_llm_stack(settings, primary=primary, primary_timeout_s=0.05):
            body = stack.completions().json()
            assert body["choices"][0]["message"]["content"] == "secondary answer"
            assert body["x_gateway"]["provider"] == "mock-secondary"

    def test_both_providers_down_returns_a_sanitised_502(self, settings: Settings) -> None:
        from fde_assessment.common.errors import UpstreamUnavailableError

        primary = ScriptedFailureProvider(UpstreamRateLimitedError(), name="mock-primary")
        secondary = ScriptedFailureProvider(
            UpstreamUnavailableError("psycopg2 host=10.0.0.9"), name="mock-secondary"
        )
        for stack in build_llm_stack(settings, primary=primary, secondary=secondary):
            response = stack.completions()
            assert response.status_code == 502
            body = response.json()
            assert body["error"] == {
                "type": "upstream_unavailable",
                "code": "MODEL_PROVIDER_UNAVAILABLE",
                "message": "The model service is temporarily unavailable.",
                "request_id": body["error"]["request_id"],
            }
            assert "10.0.0.9" not in response.text
            assert "psycopg2" not in response.text

    def test_stream_failure_is_delivered_as_a_terminal_sse_frame(self, settings: Settings) -> None:
        from fde_assessment.common.errors import UpstreamUnavailableError

        primary = ScriptedFailureProvider(
            UpstreamUnavailableError("boom"),
            name="mock-primary",
            prefix_chunks=["partial answer "],
        )
        for stack in build_llm_stack(settings, primary=primary):
            raw = stack.completions(
                {
                    "model": "mock-primary",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                }
            ).text
            assert "partial answer" in raw
            error_frames = [f for f in sse_frames(raw) if "error" in f]
            assert error_frames
            assert error_frames[0]["error"]["code"] == "MODEL_PROVIDER_UNAVAILABLE"
            assert "boom" not in raw


class TestOperations:
    def test_healthz(self, llm_stack: LlmStack) -> None:
        assert llm_stack.client.get("/healthz").json() == {"status": "ok"}

    def test_metrics_expose_counters(self, llm_stack: LlmStack) -> None:
        llm_stack.completions()
        text = llm_stack.client.get("/metrics").text
        assert "requests_total" in text
        assert "tokens_requested" in text

    def test_request_id_is_returned(self, llm_stack: LlmStack) -> None:
        response = llm_stack.completions(headers={"x-request-id": "trace-9"})
        assert response.headers["x-request-id"] == "trace-9"
