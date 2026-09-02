"""Task 4, resilient model routing with fallback.

WHAT
    ``ModelRouter`` tries the primary provider and fails over to the secondary
    when the primary returns 429 or fails to produce a first token within the
    configured timeout (3000 ms by default).

WHY
    A gateway that forwards a provider's bad day to every caller is not adding
    much. Failover converts a vendor incident into added latency instead of an
    outage, but only for failures that are actually transient, which is why
    the retry policy is explicit rather than "retry on any exception".

HOW, where the timeout is applied
    The 3000 ms budget covers **time to first token**, not the whole
    generation. That distinction matters:

    * A whole-stream deadline would kill legitimate long completions, and the
      client would see a truncated answer rather than a slow one.
    * Once the first token has been forwarded, failover is no longer
      transparent: bytes are already on the wire and a second provider would
      restart the answer mid-sentence. So after the first token the router
      stops failing over and surfaces a clean error instead.

    An idle-timeout between tokens is the natural next control; it is called
    out as future work in ADR-010 rather than half-implemented here.

HOW, cancellation
    ``asyncio.timeout`` cancels the pending ``anext``, and the router then
    awaits ``aclose()`` on the provider generator. That is what actually
    releases the upstream socket; without it the abandoned request keeps
    consuming a connection and the provider keeps generating tokens the
    customer still pays for.

RETRY POLICY
    Failover happens **only** for:

    * ``MODEL_PROVIDER_RATE_LIMITED`` (HTTP 429 upstream),
    * ``MODEL_PROVIDER_TIMEOUT`` (no first token in the budget),
    * ``MODEL_PROVIDER_UNAVAILABLE`` (connection refused, 5xx),
    * ``MODEL_PROVIDER_PROTOCOL_ERROR`` (malformed upstream response).

    It never happens for client-caused failures, invalid requests,
    authentication or authorization errors, policy refusals, unsupported
    models. Retrying those wastes the second provider's quota, doubles the
    cost of a bad request, and turns a deterministic 4xx into a slow 4xx.

WHEN
    One router per gateway process, constructed from configuration.

SECURITY
    Both providers' failures are normalised before they leave the router; a
    caller cannot distinguish which upstream failed, or why, beyond the
    gateway's own error vocabulary.

COST
    Failover doubles the cost of the affected request. ``fallback_total`` is a
    metric worth alerting on: a sustained rise means the primary is degraded
    and spend has silently shifted to the secondary.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fde_assessment.common.errors import (
    GatewayError,
    UpstreamProtocolError,
    UpstreamRateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from fde_assessment.common.logging import get_logger
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent
from fde_assessment.llm_gateway.providers.base import LLMProvider
from fde_assessment.observability.metrics import (
    FALLBACK_TOTAL,
    METRICS,
    PROVIDER_LATENCY_MS,
    TIME_TO_FIRST_TOKEN_MS,
)

log = get_logger(__name__)

#: Error codes that justify trying the secondary provider.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "MODEL_PROVIDER_RATE_LIMITED",
        "MODEL_PROVIDER_TIMEOUT",
        "MODEL_PROVIDER_UNAVAILABLE",
        "MODEL_PROVIDER_PROTOCOL_ERROR",
    }
)


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a transient upstream failure worth failing over."""
    return isinstance(exc, GatewayError) and exc.code in RETRYABLE_CODES


@dataclass
class RouteOutcome:
    """Mutable record of what the router actually did, for logging/metrics."""

    provider_name: str = ""
    fell_back: bool = False
    attempts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    ttft_ms: float = 0.0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A provider generator that has already produced its first event."""

    generator: AsyncIterator[StreamEvent]
    first: StreamEvent
    ttft_ms: float


class ModelRouter:
    """Primary/secondary router with a first-token deadline."""

    def __init__(
        self,
        primary: LLMProvider,
        secondary: LLMProvider,
        primary_timeout_s: float = 3.0,
        secondary_timeout_s: float = 10.0,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._primary_timeout_s = primary_timeout_s
        self._secondary_timeout_s = secondary_timeout_s

    async def _open(
        self, provider: LLMProvider, request: ChatCompletionRequest, timeout_s: float
    ) -> _Attempt:
        """Start a stream and wait for its first event within ``timeout_s``."""
        started = time.perf_counter()
        generator = provider.stream(request)
        try:
            async with asyncio.timeout(timeout_s):
                first = await anext(generator)
        except TimeoutError as exc:
            # Release the upstream: without aclose the provider keeps
            # generating (and billing) into a stream nobody reads.
            await _safe_close(generator)
            raise UpstreamTimeoutError(
                internal_detail=f"{provider.name} produced no first token in {timeout_s}s"
            ) from exc
        except StopAsyncIteration as exc:
            await _safe_close(generator)
            raise UpstreamProtocolError(
                internal_detail=f"{provider.name} produced an empty stream"
            ) from exc
        except GatewayError:
            await _safe_close(generator)
            raise
        except Exception as exc:
            await _safe_close(generator)
            raise UpstreamUnavailableError(
                internal_detail=f"{provider.name} raised {type(exc).__name__}"
            ) from exc

        ttft_ms = (time.perf_counter() - started) * 1000.0
        METRICS.observe(TIME_TO_FIRST_TOKEN_MS, ttft_ms, provider=provider.name)
        return _Attempt(generator=generator, first=first, ttft_ms=ttft_ms)

    async def stream(
        self, request: ChatCompletionRequest, outcome: RouteOutcome | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Stream ``request``, failing over to the secondary when warranted."""
        record = outcome if outcome is not None else RouteOutcome()

        attempt: _Attempt | None = None
        provider = self._primary

        record.attempts.append(self._primary.name)
        try:
            attempt = await self._open(self._primary, request, self._primary_timeout_s)
        except GatewayError as primary_error:
            record.failures.append(primary_error.code)
            if not is_retryable(primary_error):
                # A non-retryable failure is the caller's problem, not the
                # secondary's. Surface it immediately.
                log.warning(
                    "primary_failed_no_fallback",
                    provider=self._primary.name,
                    code=primary_error.code,
                )
                raise

            log.warning(
                "failing_over",
                primary=self._primary.name,
                secondary=self._secondary.name,
                code=primary_error.code,
            )
            METRICS.increment(FALLBACK_TOTAL, primary=self._primary.name, reason=primary_error.code)
            record.fell_back = True
            record.attempts.append(self._secondary.name)
            provider = self._secondary
            try:
                attempt = await self._open(self._secondary, request, self._secondary_timeout_s)
            except GatewayError as secondary_error:
                record.failures.append(secondary_error.code)
                log.error(
                    "all_providers_failed",
                    primary_code=primary_error.code,
                    secondary_code=secondary_error.code,
                )
                raise

        record.provider_name = provider.name
        record.ttft_ms = attempt.ttft_ms

        started = time.perf_counter()
        tokens = 0
        try:
            if not attempt.first.done:
                tokens += attempt.first.completion_tokens
                yield attempt.first
            async for event in attempt.generator:
                if event.done:
                    break
                tokens += event.completion_tokens
                yield event
        except GatewayError:
            # Past the first token there is no transparent failover: the client
            # already holds a partial answer. Surface a normalised error.
            log.warning("provider_failed_mid_stream", provider=provider.name)
            raise
        finally:
            record.completion_tokens = tokens
            METRICS.observe(
                PROVIDER_LATENCY_MS,
                (time.perf_counter() - started) * 1000.0,
                provider=provider.name,
            )

        yield StreamEvent(done=True, completion_tokens=tokens)


async def _safe_close(generator: AsyncIterator[StreamEvent]) -> None:
    """Close a provider generator, ignoring errors raised during teardown."""
    aclose = getattr(generator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # pragma: no cover - teardown must never mask the cause
        log.debug("provider_close_failed")


__all__ = [
    "RETRYABLE_CODES",
    "ModelRouter",
    "RouteOutcome",
    "UpstreamRateLimitedError",
    "is_retryable",
]
