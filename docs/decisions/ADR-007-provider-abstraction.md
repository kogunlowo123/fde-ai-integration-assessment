# ADR-007, A single streaming provider interface

**Status:** Accepted · **Date:** 2026-09-02

## Context

The gateway must route to a primary provider, fail over to a secondary, run
deterministically in CI, and support a local model for development, without
the routing, guardrail or rate-limiting code knowing which vendor is in play.

## Decision

One abstract base class with a single method:

```python
class LLMProvider(ABC):
    name: str

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]: ...
```

Non-streaming responses are the degenerate case (join the events), so there is
one code path rather than two.

Implementations must be cancellation-safe, must raise `GatewayError` subclasses
rather than vendor exceptions, and must emit a terminal `StreamEvent(done=True)`.

## Alternatives considered

**Separate `complete` and `stream` methods.** Familiar from vendor SDKs.
Rejected: two paths means two implementations of guardrail application, token
accounting and error mapping, and the non-streaming one inevitably drifts.

**Return the vendor's response object.** Less mapping code. Rejected: it leaks
vendor shape into the router and the guardrail, and makes failover between
vendors a rewrite.

**A plugin system discovering providers from configuration.** Rejected on
security grounds: a gateway that can be pointed at an arbitrary provider by
configuration alone is an exfiltration channel. `build_provider` is an explicit
`if/elif`, so adding a provider is a code change and therefore a review.

**LangChain or LiteLLM.** Both solve this. Rejected for an assessment whose
purpose is to demonstrate understanding of the mechanism; also a large
dependency for one interface, and an extra hop to debug when a stream stalls.

## Consequences

- CI runs entirely on `MockProvider`: $0, deterministic, no key, no GPU.
- Failure modes that are hard to obtain from a real vendor on cue, a 429, a
  first-token stall, a malformed response, a mid-stream disconnect, are
  scripted directly (`ScriptedFailureProvider`, `HangingProvider`).
- Adding a vendor is one file plus one line in the factory.
- Token counting is per provider; the shared heuristic is documented as an
  estimate (COST-OPTIMIZATION.md).

## Security impact

Vendor payloads never propagate past the adapter, so a provider's error body, which may contain a credential or a customer record, cannot reach a client.
The explicit factory prevents configuration-only redirection of traffic.

## Cost impact

Provider independence is the precondition for cost-based routing: a cheap model
for classification, an expensive one for reasoning. The abstraction is what
makes that a routing change rather than a rewrite.

## Operational impact

`provider_latency_ms{provider}` and `time_to_first_token_ms{provider}` make a
vendor comparison an observation rather than an argument.
