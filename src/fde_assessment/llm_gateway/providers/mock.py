"""Deterministic mock provider.

WHAT
    An in-process ``LLMProvider`` that streams scripted text and can be told to
    fail in specific, realistic ways: HTTP 429, a slow first token, a mid-stream
    error, a malformed response.

WHY
    Every default test runs against this provider, which is what makes the test
    suite cost nothing to run, need no API key or GPU, and give the same answer
    on every machine. It is also the only honest way to test a 3-second timeout
    or a 429 failover without depending on a vendor to misbehave on cue.

HOW
    ``MockProvider`` streams a fixed script word by word with a configurable
    per-chunk delay. ``ScriptedFailureProvider`` raises a chosen error.

WHEN
    Default provider for CI and local development
    (``PRIMARY_PROVIDER=mock``). Never for production traffic.

COST
    $0 per token, by construction.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from fde_assessment.common.errors import GatewayError
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent
from fde_assessment.llm_gateway.providers.base import LLMProvider, estimate_tokens

DEFAULT_SCRIPT = (
    "Here is the summary you asked for. "
    "The account contact is john.smith@example.com, "
    "the national identifier on file is 123-45-6789, "
    "and the saved card is 4111 1111 1111 1111. "
    "Let me know if you need anything else."
)


class MockProvider(LLMProvider):
    """Streams a fixed script in small chunks."""

    def __init__(
        self,
        name: str = "mock-primary",
        script: str | None = None,
        chunk_size: int = 7,
        first_token_delay_s: float = 0.0,
        per_chunk_delay_s: float = 0.0,
        chunks: Sequence[str] | None = None,
    ) -> None:
        self.name = name
        self._script = DEFAULT_SCRIPT if script is None else script
        self._chunk_size = chunk_size
        self._first_token_delay_s = first_token_delay_s
        self._per_chunk_delay_s = per_chunk_delay_s
        self._chunks = list(chunks) if chunks is not None else None
        self.call_count = 0

    def _iter_chunks(self) -> list[str]:
        if self._chunks is not None:
            return self._chunks
        text = self._script
        size = max(1, self._chunk_size)
        return [text[i : i + size] for i in range(0, len(text), size)]

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        if self._first_token_delay_s:
            await asyncio.sleep(self._first_token_delay_s)

        total = 0
        for chunk in self._iter_chunks():
            if self._per_chunk_delay_s:
                await asyncio.sleep(self._per_chunk_delay_s)
            tokens = estimate_tokens(chunk)
            total += tokens
            yield StreamEvent(text=chunk, completion_tokens=tokens)

        yield StreamEvent(done=True, completion_tokens=0)
        del total  # accounting is done by the caller from per-chunk counts


class ScriptedFailureProvider(LLMProvider):
    """Raises a chosen ``GatewayError``, optionally after streaming a prefix.

    Two shapes matter for Task 4: failing *before* the first token (a clean
    failover) and failing *after* it (bytes already on the wire, so failover is
    no longer transparent).
    """

    def __init__(
        self,
        error: GatewayError,
        name: str = "mock-failing",
        prefix_chunks: Sequence[str] = (),
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self._error = error
        self._prefix = list(prefix_chunks)
        self._delay_s = delay_s
        self.call_count = 0

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        for chunk in self._prefix:
            yield StreamEvent(text=chunk, completion_tokens=estimate_tokens(chunk))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        raise self._error


class HangingProvider(LLMProvider):
    """Never produces a first token. Used to exercise the timeout path.

    Records ``cancelled`` so a test can assert the router actually cancelled
    the attempt rather than leaving it running in the background.
    """

    def __init__(self, name: str = "mock-hanging", hang_s: float = 30.0) -> None:
        self.name = name
        self._hang_s = hang_s
        self.call_count = 0
        self.cancelled = False

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        try:
            await asyncio.sleep(self._hang_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield StreamEvent(text="never reached", done=False)
