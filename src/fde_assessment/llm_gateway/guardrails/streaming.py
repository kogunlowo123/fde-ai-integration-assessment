"""Task 3, streaming guardrail with a bounded carry buffer.

WHAT
    ``StreamingRedactor`` consumes provider chunks and emits redacted text,
    holding back only the minimum tail that could still grow into a match.

WHY
    The assessment requires the stream to stay responsive "without accumulating
    the full response in memory, minimising TTFT". Two failure modes bracket
    the design:

    * Emit each chunk as it arrives -> ``john.smith@`` / ``example.`` / ``com``
      passes through unredacted. The guardrail is defeated by chunk boundaries.
    * Buffer the whole response, redact once, emit -> correct, but TTFT becomes
      total generation time and memory grows without bound.

    The middle path is a look-behind window: emit everything that provably
    cannot be part of a longer match, hold the rest.

HOW
    Per chunk:

    1. Append the chunk to the carry buffer.
    2. Redact every *complete* match, treating a match that ends at the buffer
       edge as possibly still growing.
    3. Ask :func:`pii.partial_match_start` where a still-growing match could
       have begun. Emit up to that point; carry the remainder.
    4. On ``flush`` (end of stream) redact and emit whatever is left.

    Step 3 is what keeps latency near zero for ordinary prose: a tail of
    ``". The answer is "`` cannot start an email, SSN or card, so nothing is
    held. Text is only delayed while it *looks* like the start of PII.

BUFFER SIZING (why 128 characters)
    The window must exceed the longest match the scanner can complete:

    * SSN with separators: 11 characters.
    * Card, 19 digits with separators: 37 characters.
    * Email: unbounded in principle; 128 covers the overwhelming majority of
      real addresses (RFC 5321 allows 254, but p99 of observed addresses is
      well under 64).

    128 characters is therefore ~3x the longest fixed-length pattern with
    headroom for emails, and costs at most 128 characters of memory per active
    stream plus, in the worst case, one chunk of added latency.

    **Documented limitation:** a match longer than the window can be split
    across the emit boundary and partially escape redaction. The window is
    configurable (``PII_CARRY_BUFFER_CHARS``); raising it trades latency and
    memory for recall on pathological inputs.

WHEN
    One instance per stream. Not thread-safe and not reusable across streams,
    the carry buffer is per-response state.

SECURITY
    The carry buffer holds *unscanned* text, so the failure mode of not
    flushing is data loss, never disclosure. On a client disconnect
    ``guard_stream`` therefore discards the carry rather than trying to emit it
    while the generator is being closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fde_assessment.common.models import StreamEvent
from fde_assessment.llm_gateway.guardrails.pii import (
    MAX_MATCH_LENGTH,
    partial_match_start,
    scan,
)
from fde_assessment.observability.metrics import METRICS, PII_REDACTIONS_TOTAL


class StreamingRedactor:
    """Incremental PII redaction over a chunked text stream."""

    __slots__ = ("_buffer", "_window", "counts")

    def __init__(self, window: int = MAX_MATCH_LENGTH) -> None:
        if window < 16:
            raise ValueError("carry window must be at least 16 characters")
        self._buffer = ""
        self._window = window
        self.counts: dict[str, int] = {}

    @property
    def buffered_chars(self) -> int:
        """Current carry size. Asserted by the bounded-memory tests."""
        return len(self._buffer)

    @property
    def redaction_count(self) -> int:
        return sum(self.counts.values())

    def _tally(self, counts: dict[str, int]) -> None:
        for kind, n in counts.items():
            self.counts[kind] = self.counts.get(kind, 0) + n
            METRICS.increment(PII_REDACTIONS_TOTAL, n, kind=kind)

    def process(self, chunk: str) -> str:
        """Feed one chunk; return the text that is safe to emit now."""
        if not chunk:
            return ""

        self._buffer += chunk

        # 1. Redact matches that are certainly complete.
        prefix, consumed, counts = scan(self._buffer, allow_trailing_match=False)
        self._tally(counts)
        remainder = self._buffer[consumed:]

        # 2. Hold back only a tail that could still become a match.
        hold_from = partial_match_start(remainder, self._window)
        emit_tail = remainder[:hold_from]
        self._buffer = remainder[hold_from:]

        # 3. Hard cap: never let the carry exceed the configured window, even
        #    if the tail still looks like a growing match. This is the bound
        #    that makes memory per stream O(window) rather than O(response).
        if len(self._buffer) > self._window:
            overflow = len(self._buffer) - self._window
            flushed, flushed_consumed, overflow_counts = scan(
                self._buffer[:overflow], allow_trailing_match=True
            )
            self._tally(overflow_counts)
            emit_tail += flushed + self._buffer[:overflow][flushed_consumed:]
            self._buffer = self._buffer[overflow:]

        return prefix + emit_tail

    def flush(self) -> str:
        """Redact and return everything still held. Idempotent."""
        if not self._buffer:
            return ""
        prefix, consumed, counts = scan(self._buffer, allow_trailing_match=True)
        self._tally(counts)
        tail = prefix + self._buffer[consumed:]
        self._buffer = ""
        return tail


async def guard_stream(
    source: AsyncIterator[StreamEvent], window: int = MAX_MATCH_LENGTH
) -> AsyncIterator[StreamEvent]:
    """Wrap a provider stream with redaction.

    Bounded state only: one carry buffer, no accumulation of the response.

    The tail is flushed on normal completion. It is deliberately *not* flushed
    from a ``finally`` block: an async generator that yields while unwinding a
    ``GeneratorExit`` raises ``RuntimeError``, so a client disconnect would
    turn into a crash. Dropping the carry on disconnect is also the safer
    outcome, unemitted text is unscanned text, and nobody is listening.
    """
    redactor = StreamingRedactor(window)
    completion_tokens = 0

    async for event in source:
        if event.completion_tokens:
            completion_tokens += event.completion_tokens
        if event.done:
            continue
        emitted = redactor.process(event.text)
        if emitted:
            yield StreamEvent(text=emitted, completion_tokens=event.completion_tokens)

    tail = redactor.flush()
    if tail:
        yield StreamEvent(text=tail)
    yield StreamEvent(done=True, completion_tokens=completion_tokens)
