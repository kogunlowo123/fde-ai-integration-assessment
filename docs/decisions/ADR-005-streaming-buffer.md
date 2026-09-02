# ADR-005, Bounded look-behind buffer for streaming redaction

**Status:** Accepted · **Date:** 2026-09-02

## Context

Task 3 requires redacting PII from a streamed response "without accumulating
the full response in memory, minimising Time To First Token". Those two
requirements pull against correctness: PII arrives split across chunks
(`john.smith@` / `example.` / `com`), and a matcher that only sees one chunk
cannot detect it.

## Decision

A carry buffer holding only text that could still become a match:

1. Append the chunk to the buffer.
2. Redact every match that is certainly complete, a match ending exactly at
   the buffer edge is treated as possibly still growing.
3. Ask where a still-growing match could have begun
   (`pii.partial_match_start`). Emit up to that point; carry the rest.
4. Cap the carry at `PII_CARRY_BUFFER_CHARS` (128) regardless, so memory is
   O(window) per stream rather than O(response).
5. On end of stream, redact and emit the remainder.

The key property of step 3: text that *cannot* start a match is emitted
immediately. `". The answer is "` cannot begin an email, SSN or card, so
ordinary prose is never delayed.

## Alternatives considered

**Redact each chunk independently.** Zero added latency, zero memory. Rejected:
it fails the assessment's own example, any value split across a boundary
escapes.

**Buffer the entire response, redact once, emit.** Simple and completely
correct. Rejected: TTFT becomes total generation time, and memory grows with
the answer. It defeats both stated requirements.

**Fixed N-character look-behind, always held.** Simple and bounded. Rejected as
the primary mechanism because it delays *all* text by N characters even when
the tail is plainly `". "`. The fixed cap is kept as the safety bound, not the
mechanism.

**Sentence-boundary buffering.** Natural for prose. Rejected: a sentence is
unbounded, so memory is unbounded, and code and log output have no sentences.

## Why 128 characters

| Pattern | Longest form |
|---|---|
| SSN with separators | 11 characters |
| Card, 19 digits with separators | 37 characters |
| Email | unbounded in principle; RFC 5321 allows 254, but observed addresses are overwhelmingly under 64 |

128 is roughly 3× the longest fixed-length pattern with headroom for realistic
emails. Cost: at most 128 characters of memory per active stream, and at most
one chunk of added latency when the tail looks like PII.

## Consequences

- Chunk-boundary correctness is verified exhaustively: every two-way split of
  every fixture, plus character-by-character streaming
  (`tests/streaming/test_chunk_boundaries.py`).
- Measured: ~1.49 ms for 4 KB of prose, ~3.4 µs for a single chunk, carry
  provably ≤ window under adversarial input.
- **Documented limitation:** a match longer than the window can be split across
  the emit boundary and partially escape. Asserted in a test rather than
  hidden.
- The window is configurable; raising it trades latency and memory for recall
  on pathological inputs.

## Security impact

Closes the chunk-boundary bypass, which is the most likely way a naive
guardrail leaks. Does not change the underlying detection recall, see
SECURITY.md, "Not claimed".

## Cost impact

Pure CPU, microseconds per chunk. No model call, no network.

## Operational impact

`pii_redactions_total{kind}` shows guardrail activity; a spike means upstream
content changed. The carry buffer is per stream, so memory scales with
concurrent streams, not with response length.
