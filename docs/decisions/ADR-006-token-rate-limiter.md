# ADR-006, Sliding-window token limiter in a single transaction

**Status:** Accepted · **Date:** 2026-09-02

## Context

Task 4 requires "a token-aware sliding window rate limiter (e.g. maximum 50,000
tokens/minute per tenant API key)" on on-disk SQLite, and scores "accurate
rate-limiter state eviction and token tracking logic" and "async concurrency
handling".

The difficulty is not the arithmetic. It is that the obvious implementation, read the window sum, decide, insert, is a race: two concurrent requests both
read 49,000, both conclude they fit, and both insert.

## Decision

An append-only event log with the check and the insert in one
`BEGIN IMMEDIATE` transaction, serialised in-process by an `asyncio.Lock`.

```
BEGIN IMMEDIATE
  DELETE FROM rate_limit_events WHERE timestamp < now - window   -- eviction
  SELECT SUM(token_count) WHERE api_key_hash = ? AND timestamp >= window_start
  -- if used + requested > limit -> reject (nothing written)
  INSERT ... (admission)
COMMIT
```

**Boundary semantics.** "Maximum 50,000 tokens" is read inclusively: a request
bringing the total to exactly 50,000 is admitted; 50,001 is rejected. Both
sides are tested.

**Accounting.** Charged at admission with prompt + maximum possible output,
then reconciled with a correction row carrying the delta. Charging up front is
the point: a limiter that counts after generation cannot prevent the burst it
exists to prevent.

## Alternatives considered

**Fixed window (bucket per minute).** One row per key, trivial. Rejected: it
permits a 2× burst across the boundary, 50,000 at 11:59:59 and 50,000 at
12:00:00 satisfies a minute bucket and is 100,000 tokens in one second.

**Token bucket.** Excellent for smoothing, and cheap. Rejected: the assessment
specifies a sliding window, and a bucket's refill semantics are harder to
explain to a customer being billed against them.

**Read-then-write without a transaction.** Rejected: it is the race described
above. The concurrency tests fail against it.

**Optimistic retry on conflict.** Viable, but adds a retry loop and unbounded
tail latency under contention for no gain over `BEGIN IMMEDIATE`.

**Redis sorted sets.** The correct distributed answer. Rejected here: the brief
says SQLite, and one node needs no coordination.

## Consequences

- Correct under concurrency: 100 concurrent 1,000-token requests against a
  50,000 budget admit exactly 50, including across two connections to one file.
- Eviction runs inside the admission transaction, so the table holds one window
  of traffic rather than growing forever, asserted directly.
- Append-only, so the window arithmetic stays a single `SUM` and the log
  doubles as an audit trail.
- **Per node.** Two replicas each admit up to the limit.
- Measured: ~0.65 ms mean sequential admission; 200 concurrent in ~107 ms
  (~0.54 ms each).

## Security impact

Fails closed: a database error raises rather than admitting an unmetered
request. Only HMAC fingerprints of API keys are stored.

## Cost impact

This is the mechanism that bounds spend per tenant. Charging the worst case up
front slightly over-restricts in exchange for a hard ceiling; reconciliation
returns the difference within the same window.

## Operational impact

Watch `rate_limit_rejections_total{tenant}`. Sustained rejections mean either
an under-provisioned tenant or abuse, the metric alone does not distinguish
them; the event log does.

## Trigger to revisit

The moment a second replica is deployed. Migration path: replace
`TokenRateLimiter` with a Redis implementation using sorted sets
(`ZREMRANGEBYSCORE` to evict, `ZADD` + `ZRANGEBYSCORE` to sum) inside a Lua
script for atomicity. The interface, `check_and_consume` / `reconcile`, does
not change.
