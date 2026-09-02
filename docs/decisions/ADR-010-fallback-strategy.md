# ADR-010, First-token deadline and retryable-only failover

**Status:** Accepted · **Date:** 2026-09-02

## Context

Task 4: "If the primary model endpoint returns a 429 Too Many Requests status
or times out after 3000ms, automatically failover to a secondary backup model
provider." Two questions the requirement does not settle: what the 3000 ms
measures, and what else should fail over.

## Decision

**The deadline covers time to first token, not the whole stream.**

**Failover happens only for actually transient upstream failures:**

| Code | Failover |
|---|---|
| `MODEL_PROVIDER_RATE_LIMITED` (429) | yes |
| `MODEL_PROVIDER_TIMEOUT` (no first token in budget) | yes |
| `MODEL_PROVIDER_UNAVAILABLE` (connection refused, 5xx) | yes |
| `MODEL_PROVIDER_PROTOCOL_ERROR` (malformed response) | yes |
| Invalid request, auth, authorization, policy, unsupported model | **no** |

**After the first token, no failover.** The client already holds a partial
answer; a second provider would restart the response mid-sentence. The router
surfaces a normalised error instead.

**The abandoned attempt is cancelled.** `asyncio.timeout` cancels the pending
`anext`, and the router then awaits `aclose()` on the provider generator.

## Alternatives considered

**Whole-stream deadline.** Simplest reading of the requirement. Rejected: a
legitimate 4,000 ms answer would be killed and restarted, and the user would
see a truncated response rather than a slow one.

**Retry on any exception.** Rejected: retrying an invalid request wastes the
secondary's quota, doubles the cost of a bad request, and converts a
deterministic 4xx into a slow 4xx. It also masks client bugs.

**Buffer the first N tokens so mid-stream failover stays transparent.**
Genuinely better UX. Rejected: it reintroduces the latency the guardrail design
works to avoid, and the failure it covers is rare compared with a first-token
stall.

**Circuit breaker.** The right next control. Not implemented: it adds state and
a tuning surface, and the assessment's requirement is per request. Recorded as
future work, today a hard-down primary is retried on every request, spending
the full timeout each time.

**Hedged requests** (start both, take the first). Halves tail latency.
Rejected: it doubles cost on every request, which is the wrong default for a
gateway whose job includes cost control.

## Consequences

- Measured failover overhead is under a millisecond with the mock provider; in
  production it is bounded by the deadline itself.
- The abandoned upstream is really released, asserted by
  `test_the_hung_primary_is_actually_cancelled`.
- `RouteOutcome` records the provider used, whether failover occurred, and the
  failure codes, so the decision is visible in logs and metrics.
- No idle-timeout between tokens: a provider that stalls after the first token
  is not caught. Called out as future work.

## Security impact

Both providers' failures are normalised before leaving the router; a caller
cannot tell which upstream failed or why beyond the gateway's vocabulary.

## Cost impact

A failover doubles the cost of the affected request. `fallback_total` is the
metric to alert on: a sustained rise means the primary is degraded *and* spend
has shifted, possibly to a more expensive vendor.

## Operational impact

`PRIMARY_TIMEOUT_MS` is the tuning knob. Set it from observed
`time_to_first_token_ms` p99 plus headroom: too low causes needless failover
and doubled cost; too high delays recovery.
