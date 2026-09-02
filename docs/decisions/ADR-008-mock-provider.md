# ADR-008, A deterministic mock provider as the CI default

**Status:** Accepted · **Date:** 2026-09-02

## Context

The test suite must verify behaviour that depends on provider responses:
streaming chunk boundaries, a 429 failover, a 3-second first-token timeout,
mid-stream failure, and PII appearing in generated text. It must also run on
every push without cost, credentials, or a GPU.

## Decision

`MockProvider` is the default for every test and for local development
(`PRIMARY_PROVIDER=mock`). It streams a fixed script in configurable chunk
sizes with optional delays. `ScriptedFailureProvider` and `HangingProvider`
cover the failure shapes.

## Alternatives considered

**A real provider in CI.** The most realistic. Rejected on four grounds: it
costs money per push; it makes CI flaky when the vendor is slow; it requires a
secret in the CI environment; and, decisively, it cannot deterministically
produce a 429 or a first-token stall, which are exactly the behaviours Task 4
requires testing.

**Recorded fixtures (VCR-style).** Real payloads, replayed offline. Rejected:
recordings drift from the live API, and they still cannot produce a timeout on
demand.

**A local model (Ollama) in CI.** Free at the margin. Rejected: a multi-gigabyte
model pull per run, non-deterministic output, and a daemon that can fail for
reasons unrelated to the change under test. Kept as an *optional local* path.

## Consequences

- The full suite runs offline in about half a minute with zero marginal cost.
- Every failure mode is exercised on demand instead of hoped for.
- **The mock's text is not model output.** It cannot catch prompt-formatting
  bugs, tokenizer edge cases, or how a real model responds to an injected
  instruction. That is what the optional Ollama path and shadow-mode rollout
  are for (FDE-DELIVERY.md).
- The default script intentionally contains an email, an SSN and a Luhn-valid
  card, so the guardrail is exercised end to end by the plainest possible
  request.

## Security impact

No API keys exist in CI, so none can leak from it. The mock's PII-bearing
script means guardrail regressions surface in the default test run rather than
only under a specific test.

## Cost impact

The entire point: $0 inference in CI, forever, regardless of push volume.

## Operational impact

`PRIMARY_PROVIDER=mock` must never reach production. `Settings` does not police
this, the production credential check covers tokens and the pepper, not the
provider selection, so it is a line item in the deployment checklist in
OPERATIONS.md. The shipped `docker-compose.yml` on purpose uses `mock`: it is
a local demonstration of the request path, not a production manifest, and it
says so.
