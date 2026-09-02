# ADR-001, Python end to end

**Status:** Accepted · **Date:** 2026-09-02

## Context

The assessment allows TypeScript or Python. The system spans an MCP stdio
server, two HTTP gateways, a streaming guardrail, a concurrent rate limiter and
a retrieval pipeline. One language for all of it, or the best language per
component.

## Decision

Python 3.12+ for everything. No TypeScript, JavaScript, Node.js or npm anywhere
in the repository.

## Alternatives considered

**TypeScript throughout.** Strong MCP SDK, excellent streaming ergonomics, and
a single-threaded event loop that makes some concurrency reasoning simpler.
Rejected because the surrounding ecosystem is weaker for the parts that carry
the most risk here: schema validation with Pydantic is stricter and more
introspectable than Zod for this use case, and the customer teams who will own
an AI platform are more often Python teams.

**Python services with a TypeScript MCP server.** Rejected outright: two
toolchains, two dependency trees, two CI paths and two security-scan surfaces,
for no capability gain.

**Rust or Go for the gateways.** Genuinely better on raw throughput and memory.
Rejected because the bottleneck in an LLM gateway is the provider, not the
proxy, the measured overhead here is ~1.3 ms against provider latencies in the
hundreds, and an FDE hands work to a customer team that must maintain it.

## Consequences

- One toolchain: `ruff`, `mypy`, `pytest`, `bandit`, `pip-audit`.
- The same language as the customer's data and ML code, so tools and retrieval
  logic live beside it.
- Python 3.12 minimum buys `asyncio.timeout`, `StrEnum` and modern generics, `asyncio.timeout` in particular is what makes the router's cancellation
  correct rather than approximate.
- GIL-bound CPU work. Acceptable: everything on the request path is I/O-bound,
  and the guardrail's regex work is measured in microseconds.

## Security impact

Mature static-analysis ecosystem (`bandit`, ruff's `S` rules) and a single
dependency tree to audit. Pydantic makes validation declarative and therefore
reviewable, a security reviewer reads a model, not a chain of `if` statements.

## Cost impact

Neutral at runtime. Positive operationally: one toolchain to maintain, one set
of CI images.

## Operational impact

One runtime to package, patch and monitor. Deployment is a single slim image.
