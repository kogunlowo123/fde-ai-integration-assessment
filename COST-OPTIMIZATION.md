# Cost optimization

AI systems fail on cost quietly: nothing breaks, the bill just arrives. The
levers below are ordered by how much they actually move it, and each one is
either implemented here or documented as the next step.

---

## 1. Development and CI cost: $0, by construction

The default test suite makes **zero paid inference calls**. Not "few", zero.

| Requirement | How |
|---|---|
| No paid API | `MockProvider` and `MockEmbeddingProvider` are the defaults |
| No API key | Nothing reads a provider credential on the default path |
| No GPU | Everything is CPU-bound Python |
| No network | ASGI transports in-process; the MCP server runs as a subprocess |
| Deterministic | Fixed scripts and hashed embeddings, the same answer every run |

472 tests run in about half a minute at no marginal cost. The alternative, a
suite that calls a real model, costs money on every push, is flaky when the
vendor is slow, and cannot deterministically produce a 429 or a 3-second
timeout, which are precisely the behaviours Task 4 requires testing.

**Local realism without cost:** Ollama provides real tokenisation, real chunk
boundaries and real latency on local hardware. It is never required, tests
that would use it are marked `@pytest.mark.ollama` and deselected by default.

Estimated saving: a CI suite making even 50 small calls per run, at 30 runs a
day, is roughly $15-60/month in inference plus the engineering time lost to
flakes. The mock provider removes both.

---

## 2. Token controls (the largest production lever)

Implemented:

| Control | Setting | Effect |
|---|---|---|
| Output ceiling | `MAX_OUTPUT_TOKENS` (1024) | Caps the expensive half of every request |
| Prompt ceiling | `MAX_PROMPT_CHARS` (100k) | Stops a runaway context |
| Body cap | `LLM_GATEWAY_MAX_BODY_BYTES` | Rejects oversized payloads before parsing |
| Token budget | `RATE_LIMIT_TOKENS` per key per window | Hard ceiling on spend per tenant |
| Retrieval breadth | `RAG_MAX_TOP_K`, `RAG_MAX_CONTEXT_CHARS` | Caps retrieval's share of the prompt |

**Charged up front, reconciled after.** Admission charges prompt + maximum
possible output, then a correction row records the difference. A limiter that
only counts tokens after generation cannot prevent a burst, because by the time
it knows the cost, the cost has been incurred.

**Token estimation is a heuristic** (~4 characters per token). It runs low for
code and non-Latin scripts and high for prose with long words, so a tenant's
effective quota varies with content by roughly ±20%. Production should use the
model's tokenizer (`tiktoken`, or the provider's usage response) and reconcile
against reported usage. The heuristic is used here because it needs no model
artefact and keeps rate-limiter tests deterministic.

---

## 3. Model routing

The largest structural saving is not compressing prompts, it is not sending
the expensive model a request a cheap one could answer.

| Task shape | Model class | Typical saving |
|---|---|---|
| Classification, routing, extraction, tagging | Small/local | 10-50× per call |
| Summarisation of short documents | Mid | 3-10× |
| Multi-step reasoning, code generation | Large |, |

The `LLMProvider` abstraction and `ModelRouter` are the seam. Today the router
implements the assessment's primary/secondary resilience policy; extending
`ModelRouter` with a per-request class selection is a contained change, and the
provider adapters do not move.

**Fallback has a cost signature worth alerting on.** A failover doubles the
cost of the affected request. A sustained rise in `fallback_total` means the
primary is degraded *and* spend has quietly shifted to the secondary, which
may be the more expensive vendor. OPERATIONS.md sets the alert.

---

## 4. Retrieval cost

Implemented:

| Control | Effect |
|---|---|
| **Content-hash skipping** | An unchanged document is never re-embedded |
| Chunk size / overlap | Overlap is duplicated storage and duplicated embedding cost |
| `top_k` cap | Each hit is prompt tokens on every request that retrieves it |
| Score floor | Noise hits are dropped rather than paid for |
| Context character budget | Hard ceiling on retrieval's share of the prompt |
| Local embeddings | `MockEmbeddingProvider` (free) or Ollama (local) |

**Measured** (`scripts/benchmark.py`, four documents, eight chunks): a full
ingest takes ~8.2 ms; re-ingesting the unchanged corpus takes ~2.5 ms, skips
all four documents and performs zero embedding calls. On a hosted embedding API, that is the difference between
a nightly re-ingestion job that costs money every night and one that costs
nothing until content actually changes.

The relationship worth internalising:

```
more retrieved context
   ├── possibly better recall
   ├── higher token cost      (linear in context size, every request)
   ├── higher latency         (prefill grows with context)
   └── larger injection surface
```

Optimise for the smallest context that reliably answers the question, which is
why the evaluation set exists: without measurement, `top_k` only ever goes up.

---

## 5. Caching

Not implemented, by design. Where it pays and where it is unsafe:

| Kind | When it pays | When it is unsafe |
|---|---|---|
| **Exact response cache** | High-volume identical prompts (FAQ, classification of repeated inputs) | Any prompt containing tenant data, a cache key collision is a cross-tenant leak. Must include the tenant in the key and never be shared |
| **Prompt/prefix cache** | Long stable system prompts (where the provider supports server-side prompt caching) | Rarely unsafe; the main risk is a stale cached instruction after a policy change |
| **Semantic cache** | Paraphrased repeat questions | Dangerous by default: "similar" is not "same", and returning a near-neighbour's answer to a different question is a correctness *and* confidentiality bug |
| **Embedding cache** | Repeated ingestion | Already covered by content hashing |

If a response cache is added, the key must include tenant, model, and the full
prompt hash, entries must be tenant-partitioned, and anything derived from
retrieved documents must be invalidated when the corpus changes.

---

## 6. Infrastructure

| Lever | Applies here |
|---|---|
| Scale to zero | Both gateways are stateless apart from the SQLite file; on Cloud Run or Container Apps they idle at zero cost. The SQLite file must move to a mounted volume or a managed database first |
| Right-sizing | Guardrail and gateway work is measured in single-digit milliseconds ([BENCHMARKS.md](docs/testing/BENCHMARKS.md)); these are small instances, not large ones |
| No infrastructure until needed | SQLite instead of PostgreSQL + Redis + a vector database. Three managed services would cost more per month than this workload consumes in a year |
| Slim images | `python:3.12-slim` multi-stage; the runtime layer has no build toolchain and no pip cache |
| Local development | The full path runs on a laptop with `docker compose up` |

**The cost of unnecessary infrastructure.** A managed Redis, a managed
PostgreSQL and a managed vector database is roughly $150-400/month at small
scale, plus the operational burden of three more things that can page someone.
For a single-node gateway, SQLite is not a compromise, it is the cheaper *and*
simpler answer, and ADR-004/006/012 record the specific triggers that should
change that.

---

## 7. What to measure

Cost is only controllable if it is visible. The metrics that matter:

| Metric | Question it answers |
|---|---|
| `tokens_requested{tenant}` | Who is spending, before generation |
| `tokens_generated{tenant}` | Who is spending, after |
| `rate_limit_rejections_total{tenant}` | Is a tenant sized wrongly, or abusing? |
| `fallback_total{primary,reason}` | Has spend silently moved to another vendor? |
| `rag_context_chars{tenant}` | Is retrieval inflating every prompt? |
| `rag_embeddings_skipped_total` | Is content-hash skipping working? |
| `time_to_first_token_ms{provider}` | Is the expensive provider actually faster? |

All are exported on `/metrics` in Prometheus text format.
