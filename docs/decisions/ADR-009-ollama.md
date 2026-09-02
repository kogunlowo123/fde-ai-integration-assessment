# ADR-009, Ollama as the optional local model path

**Status:** Accepted · **Date:** 2026-09-02

## Context

The mock provider is right for CI but cannot show how the system behaves with
real tokenisation, real chunk boundaries and real latency. Some verification, does the guardrail cope with how a model actually emits an email address?, needs
a real model. It must not cost money, need a key, or become a CI dependency.

## Context beyond development

The same capability answers a recurring customer question: "can this run
without our data leaving the building?" A working local-inference path turns
that from a promise into a demonstration.

## Decision

`OllamaProvider` (chat) and `OllamaEmbeddingProvider` (embeddings), selected by
`PRIMARY_PROVIDER=ollama` / `RAG_EMBEDDING_PROVIDER=ollama`. Never used in CI;
tests that would need it are marked `@pytest.mark.ollama` and deselected by
default. Suggested models: `qwen2.5:3b` for chat, `nomic-embed-text` for
embeddings, both small enough for a laptop.

## Alternatives considered

**llama.cpp directly.** Fewer moving parts, no daemon. Rejected: model
management, quantisation choice and an HTTP layer all become our problem.

**A hosted free tier.** Zero local setup. Rejected: still a key, still a
network dependency, still a rate limit, and the terms change.

**Transformers in-process.** Full control. Rejected: heavy dependency, slow
cold start, and CUDA/CPU divergence between machines.

**No local option at all.** Rejected: it would leave "runs entirely on your
infrastructure" as an untested claim.

## Consequences

- Real-model verification is a configuration change, not a code change.
- A credible answer for air-gapped and data-residency-constrained customers.
- Requires `ollama pull` before first use; the provider surfaces a clean
  `MODEL_PROVIDER_UNAVAILABLE` when the daemon is absent rather than an
  unhandled connection error.
- Ollama's embedding dimension differs per model, so `OllamaEmbeddingProvider`
  learns its width on the first successful call. Mixing embedders within one
  corpus produces meaningless similarities, re-ingest after switching.

## Security impact

Positive: prompts and documents never leave the machine. The base URL is
configuration, never request-derived. Response lines are length-capped and
malformed lines become a protocol error rather than an exception carrying
vendor text.

## Cost impact

$0 marginal inference cost. For high-volume, low-complexity work (classification,
extraction, routing) a local small model can be the production answer, not just
the development one.

## Operational impact

Not part of CI, so it cannot make the pipeline flaky. If used in production it
becomes a service to run, with its own capacity and model-version management.
