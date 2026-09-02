# Benchmarks

Every number here was measured by `python scripts/benchmark.py`. Nothing is
estimated, extrapolated or rounded up.

Reproduce:

```bash
python scripts/benchmark.py --json benchmark-results/local.json
```

---

## Environment

| | |
|---|---|
| Python | 3.12.13 (CPython) |
| OS | Windows 11, AMD64 |
| Logical CPUs | 22 |
| Provider | `MockProvider`, no network, no model inference |
| Storage | Local NVMe; SQLite in a temporary directory |
| Measured | 2026-09-02T17:56:55Z |
| Conditions | Single process, warm cache, no network, no other load controlled for |

`p95` is the upper edge of the bucket containing the 95th percentile
(fixed-bucket histogram), so it is an upper bound rather than an interpolated
quantile.

---

## What these numbers are, and are not

**They are** the cost of the code in this repository: dispatch, policy
evaluation, redaction, transaction handling, retrieval.

**They are not** a capacity model. Provider latency dominates any real
deployment, a real model's time to first token is measured in hundreds of
milliseconds, against roughly 1 ms of gateway overhead. That ratio is the
useful reading, not the absolute figures.

Measurements exclude provider latency on purpose. Including a real model
would measure the vendor, not the gateway, and would not be reproducible.

---

## MCP tool dispatch (in-process)

| Operation | n | mean | p95 |
|---|---|---|---|
| Valid tool call | 2,000 | 0.0080 ms | 0.0140 ms |
| Rejected tool call (invalid `customer_id`) | 2,000 | 0.0073 ms | 0.0076 ms |

Rejection is marginally *cheaper* than success: validation fails before any
handler runs. Strict input validation costs nothing at this scale.

---

## MCP security gateway

Full ASGI request through the real pipeline, with an in-process downstream.

| Operation | n | mean | p95 |
|---|---|---|---|
| Forwarded `tools/call` | 300 | 1.2928 ms | 1.6947 ms |
| **Denied** `admin_reset_key` (short circuit) | 300 | 0.5919 ms | 0.7882 ms |

A denial costs **less than half** a forwarded call, because it opens no
downstream connection. Security here is a latency *saving* on the rejected
path.

The ~1.3 ms for a forwarded call includes authentication, JSON parsing,
envelope validation, policy evaluation, the downstream round trip through the
real `httpx` client stack, and audit logging.

---

## Streaming PII guardrail

| Operation | n | mean | p95 | Throughput |
|---|---|---|---|---|
| 4 KB prose, 8-char chunks | 500 | 1.4872 ms | 2.3387 ms | ~2.6 KB/ms |
| 4 KB PII-dense, 8-char chunks | 500 | 2.1126 ms | 3.8323 ms | ~1.9 KB/ms |
| Single chunk, prose tail | 5,000 | 0.0034 ms | 0.0063 ms |, |
| Single chunk, PII-prefix tail | 5,000 | 0.0037 ms | 0.0067 ms |, |

Per-chunk cost is **~3.5 µs**, far below the inter-token interval of any real
model, so the guardrail adds no measurable time to first token. PII-dense text
costs ~42% more than prose because more matches are actually redacted.

Memory is bounded by construction: `test_carry_never_exceeds_the_window`
asserts the carry stays ≤ the configured window (128 chars) under adversarial
input, and `test_long_stream_does_not_accumulate` pushes 2,000 chunks through
while asserting the same bound.

---

## Token rate limiter (on-disk SQLite)

| Operation | n | mean | p95 |
|---|---|---|---|
| Sequential admission | 500 | 0.6541 ms | 1.2827 ms |
| 200 concurrent admissions | 200 | 107.0 ms total | 0.5352 ms per request |

Each admission is a full `BEGIN IMMEDIATE` transaction: evict expired rows, sum
the window, insert, commit, with a real `fsync` policy (`synchronous=NORMAL`
under WAL).

The contended figure is the important one: 200 concurrent requests serialise
through one writer at ~0.54 ms each with no failures and no `database is
locked`. Per-request cost does not degrade under contention, which is what the
`BEGIN IMMEDIATE` + lock design is for.

Implied ceiling: roughly **1,500-1,800 admissions/second** on a single node.
Above that, the limiter is the bottleneck, and that is the trigger for Redis
(ADR-006).

---

## Model routing

| Operation | n | mean | p95 |
|---|---|---|---|
| Time to first token, primary healthy | 300 | 0.0076 ms | 0.0103 ms |
| Time to first token, after a 429 failover | 300 | 0.0063 ms | 0.0082 ms |

Failover overhead measured at **−0.0013 ms**, i.e. indistinguishable from
zero, and within run-to-run noise.

This measures the *router's* overhead, not failover's real-world cost. With the
mock provider the primary fails instantly; against a real provider, a 429
failover costs the primary's rejection round trip, and a timeout failover costs
the full `PRIMARY_TIMEOUT_MS` (3,000 ms by default) before the secondary
starts. The number here says the routing logic itself is free; the deadline is
what the user waits for.

---

## RAG (Production Enhancement)

Corpus: 4 documents → 8 chunks, `MockEmbeddingProvider(dim=256)` (lexical
hashing).

| Operation | Result |
|---|---|
| Full ingest (4 documents) | 8.161 ms |
| Re-ingest, unchanged | 2.543 ms, **4 documents skipped, 0 re-embedded** |
| Search | mean 0.6218 ms, p95 0.9803 ms (n=300) |

Content-hash skipping makes re-ingestion ~3× faster here and, more importantly,
performs **zero embedding calls**. Against a hosted embedding API that is the
difference between a nightly job that costs money every night and one that
costs nothing until content changes.

### Retrieval quality (measured, 8-question evaluation set)

| Metric | Value |
|---|---|
| Recall@1 | **0.625** |
| Recall@3 | **1.000** |
| Recall@5 | **1.000** |
| MRR@5 | **0.792** |

The correct document is always in the top 3, and is first for 5 of 8 questions.

**These are the mock embedder's numbers, and they are lexical.** Hashed
bag-of-terms similarity captures word overlap, not meaning: "cancel my
subscription" will not retrieve a passage that only says "terminate your plan".
The test thresholds are set below these measured values (≥0.60 / ≥0.85 / ≥0.60)
so they catch regressions rather than encoding aspiration. With Ollama
embeddings the semantic cases improve, that is not asserted here because the
default suite cannot verify it offline.

---

## Reading these numbers together

| Component | Cost per request |
|---|---|
| MCP tool dispatch | ~0.008 ms |
| MCP gateway (forwarded) | ~1.3 ms |
| MCP gateway (denied) | ~0.6 ms |
| Rate-limiter admission | ~0.65 ms |
| Guardrail, per chunk | ~0.0035 ms |
| RAG search | ~0.62 ms |
| **Total gateway overhead, RAG-augmented completion** | **~2 ms** |

Against a model whose time to first token is 300-800 ms, the entire control
plane, authentication, authorization, quota, retrieval, guardrails, routing,
audit, costs well under 1% of the request. The interesting engineering
question was never whether these controls are affordable; it was whether they
are correct under concurrency and at chunk boundaries.

---

## Limitations

- One machine, one run, warm cache. No repetition across boots, no control for
  background load.
- Windows. Linux SQLite `fsync` behaviour differs; expect different limiter
  numbers on other platforms and filesystems.
- Mock provider throughout: no network, no TLS handshake, no real tokenisation.
- Latency only. No sustained-throughput or memory-over-time measurement; a soak
  test is future work.
- p95 values come from fixed histogram buckets and are upper bounds.
- Run-to-run variance on the sub-millisecond measurements is roughly ±25%
  on this machine. Repeated runs of the RAG search, for example, produced
  means between 0.50 ms and 0.62 ms. Treat single-run figures as orders of
  magnitude, not as regression thresholds.
