"""Task 4, routing, timeout and fallback semantics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from fde_assessment.common.errors import (
    GatewayError,
    InvalidParamsError,
    UpstreamProtocolError,
    UpstreamRateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent
from fde_assessment.llm_gateway.providers.base import LLMProvider
from fde_assessment.llm_gateway.providers.mock import (
    HangingProvider,
    MockProvider,
    ScriptedFailureProvider,
)
from fde_assessment.llm_gateway.routing.router import ModelRouter, RouteOutcome, is_retryable
from fde_assessment.observability.metrics import FALLBACK_TOTAL, METRICS

REQUEST = ChatCompletionRequest.model_validate(
    {"model": "mock-primary", "messages": [{"role": "user", "content": "hello"}]}
)


async def collect(router: ModelRouter, outcome: RouteOutcome | None = None) -> str:
    return "".join([e.text async for e in router.stream(REQUEST, outcome) if not e.done])


class TestHappyPath:
    async def test_primary_is_used_when_healthy(self) -> None:
        primary = MockProvider("mock-primary", script="primary answer")
        secondary = MockProvider("mock-secondary", script="secondary answer")
        outcome = RouteOutcome()

        assert await collect(ModelRouter(primary, secondary), outcome) == "primary answer"
        assert outcome.provider_name == "mock-primary"
        assert outcome.fell_back is False
        assert secondary.call_count == 0

    async def test_token_totals_are_reported(self) -> None:
        router = ModelRouter(MockProvider(script="abcdefgh"), MockProvider("second"))
        outcome = RouteOutcome()
        events = [e async for e in router.stream(REQUEST, outcome)]
        assert events[-1].done is True
        assert events[-1].completion_tokens == outcome.completion_tokens > 0


class TestFallbackOn429:
    async def test_primary_429_fails_over(self) -> None:
        primary = ScriptedFailureProvider(UpstreamRateLimitedError("429"), name="mock-primary")
        secondary = MockProvider("mock-secondary", script="secondary answer")
        outcome = RouteOutcome()

        assert await collect(ModelRouter(primary, secondary), outcome) == "secondary answer"
        assert outcome.fell_back is True
        assert outcome.provider_name == "mock-secondary"
        assert outcome.failures == ["MODEL_PROVIDER_RATE_LIMITED"]
        assert (
            METRICS.counter(
                FALLBACK_TOTAL, primary="mock-primary", reason="MODEL_PROVIDER_RATE_LIMITED"
            )
            == 1
        )

    @pytest.mark.parametrize(
        "error",
        [
            UpstreamRateLimitedError("429"),
            UpstreamUnavailableError("connection refused"),
            UpstreamProtocolError("garbage"),
        ],
    )
    async def test_every_retryable_failure_fails_over(self, error: GatewayError) -> None:
        primary = ScriptedFailureProvider(error, name="mock-primary")
        secondary = MockProvider("mock-secondary", script="ok")
        outcome = RouteOutcome()
        assert await collect(ModelRouter(primary, secondary), outcome) == "ok"
        assert outcome.fell_back is True


class TestFallbackOnTimeout:
    async def test_primary_timeout_fails_over(self) -> None:
        primary = HangingProvider("mock-primary", hang_s=30)
        secondary = MockProvider("mock-secondary", script="secondary answer")
        router = ModelRouter(primary, secondary, primary_timeout_s=0.05)

        outcome = RouteOutcome()
        started = time.perf_counter()
        assert await collect(router, outcome) == "secondary answer"
        elapsed = time.perf_counter() - started

        assert outcome.fell_back is True
        assert elapsed < 5, "must not wait for the hung primary to finish"

    async def test_the_hung_primary_is_actually_cancelled(self) -> None:
        """Not leaving upstream work running is the point of the timeout."""
        primary = HangingProvider("mock-primary", hang_s=30)
        secondary = MockProvider("mock-secondary", script="ok")
        router = ModelRouter(primary, secondary, primary_timeout_s=0.05)

        await collect(router)
        await asyncio.sleep(0.05)
        assert primary.cancelled is True

    async def test_a_slow_but_responsive_primary_is_kept(self) -> None:
        primary = MockProvider("mock-primary", script="slow answer", first_token_delay_s=0.02)
        secondary = MockProvider("mock-secondary", script="secondary")
        outcome = RouteOutcome()
        assert await collect(ModelRouter(primary, secondary, primary_timeout_s=1.0), outcome) == (
            "slow answer"
        )
        assert outcome.fell_back is False

    async def test_the_deadline_covers_first_token_not_whole_generation(self) -> None:
        """A long answer that starts promptly must not be aborted."""
        primary = MockProvider(
            "mock-primary",
            chunks=["a", "b", "c", "d", "e"],
            per_chunk_delay_s=0.03,  # 150ms total, well past a 100ms deadline
        )
        secondary = MockProvider("mock-secondary", script="secondary")
        outcome = RouteOutcome()
        assert await collect(ModelRouter(primary, secondary, primary_timeout_s=0.1), outcome) == (
            "abcde"
        )
        assert outcome.fell_back is False


class TestRetryPolicy:
    @pytest.mark.parametrize(
        "error",
        [
            UpstreamRateLimitedError(),
            UpstreamTimeoutError(),
            UpstreamUnavailableError(),
            UpstreamProtocolError(),
        ],
    )
    def test_transient_failures_are_retryable(self, error: GatewayError) -> None:
        assert is_retryable(error) is True

    @pytest.mark.parametrize("error", [InvalidParamsError(), GatewayError(), ValueError("x")])
    def test_client_and_unknown_failures_are_not_retryable(self, error: BaseException) -> None:
        assert is_retryable(error) is False

    async def test_a_non_retryable_failure_does_not_touch_the_secondary(self) -> None:
        primary = ScriptedFailureProvider(InvalidParamsError("bad model"), name="mock-primary")
        secondary = MockProvider("mock-secondary")
        outcome = RouteOutcome()

        with pytest.raises(InvalidParamsError):
            await collect(ModelRouter(primary, secondary), outcome)

        assert secondary.call_count == 0
        assert outcome.fell_back is False
        assert METRICS.counter(FALLBACK_TOTAL, primary="mock-primary") == 0


class TestBothProvidersFail:
    async def test_secondary_failure_surfaces_a_sanitised_error(self) -> None:
        primary = ScriptedFailureProvider(UpstreamRateLimitedError("429"), name="mock-primary")
        secondary = ScriptedFailureProvider(UpstreamUnavailableError("boom"), name="mock-secondary")
        outcome = RouteOutcome()

        with pytest.raises(GatewayError) as excinfo:
            await collect(ModelRouter(primary, secondary), outcome)

        assert excinfo.value.code == "MODEL_PROVIDER_UNAVAILABLE"
        assert excinfo.value.message == "The model service is temporarily unavailable."
        assert outcome.failures == ["MODEL_PROVIDER_RATE_LIMITED", "MODEL_PROVIDER_UNAVAILABLE"]

    async def test_an_empty_primary_stream_is_a_protocol_error(self) -> None:
        class EmptyProvider(LLMProvider):
            name = "mock-empty"

            async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
                return
                yield  # pragma: no cover - makes this an async generator

        secondary = MockProvider("mock-secondary", script="rescued")
        outcome = RouteOutcome()
        assert await collect(ModelRouter(EmptyProvider(), secondary), outcome) == "rescued"
        assert outcome.failures == ["MODEL_PROVIDER_PROTOCOL_ERROR"]

    async def test_a_non_gateway_exception_is_normalised(self) -> None:
        class ExplodingProvider(LLMProvider):
            name = "mock-exploding"

            async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
                raise RuntimeError("psycopg2.OperationalError: host=10.0.0.9 password=hunter2")
                yield  # pragma: no cover

        secondary = MockProvider("mock-secondary", script="rescued")
        outcome = RouteOutcome()
        assert await collect(ModelRouter(ExplodingProvider(), secondary), outcome) == "rescued"
        assert outcome.failures == ["MODEL_PROVIDER_UNAVAILABLE"]


class TestMidStreamFailure:
    async def test_failure_after_the_first_token_does_not_fail_over(self) -> None:
        """Bytes are already on the wire; a second answer would corrupt it."""
        primary = ScriptedFailureProvider(
            UpstreamUnavailableError("died"),
            name="mock-primary",
            prefix_chunks=["partial ", "answer "],
        )
        secondary = MockProvider("mock-secondary", script="should not be used")
        outcome = RouteOutcome()

        collected: list[str] = []
        with pytest.raises(GatewayError):
            async for event in ModelRouter(primary, secondary).stream(REQUEST, outcome):
                if not event.done:
                    collected.append(event.text)

        assert "".join(collected) == "partial answer "
        assert secondary.call_count == 0
        assert outcome.fell_back is False
