# Architecture decision records

Each record states the context, the decision, the alternatives that were
genuinely considered, and the consequences, including the ones that hurt.
Where a decision has a shelf life, it names the trigger that should cause a
revisit.

| ADR | Decision | Trigger to revisit |
|---|---|---|
| [001](ADR-001-python.md) | Python end to end |, |
| [002](ADR-002-official-mcp-sdk.md) | Official MCP SDK, low-level `Server` API | SDK major version changes handler signatures |
| [003](ADR-003-fastapi.md) | FastAPI for both gateways |, |
| [004](ADR-004-sqlite.md) | On-disk SQLite with WAL and `BEGIN IMMEDIATE` | A second replica; write contention; >50k chunks/tenant |
| [005](ADR-005-streaming-buffer.md) | Bounded look-behind buffer for streaming redaction | PII patterns longer than the window matter |
| [006](ADR-006-token-rate-limiter.md) | Sliding window, check and insert in one transaction | The moment a second replica is deployed |
| [007](ADR-007-provider-abstraction.md) | One streaming provider interface |, |
| [008](ADR-008-mock-provider.md) | Deterministic mock provider as the CI default |, |
| [009](ADR-009-ollama.md) | Ollama as the optional local model path | Production local inference is required |
| [010](ADR-010-fallback-strategy.md) | First-token deadline, retryable-only failover | A provider stalls mid-stream often enough to need an idle timeout |
| [011](ADR-011-status-code-contract.md) | HTTP status semantics differ between the gateways |, |
| [012](ADR-012-vector-store.md) | Brute-force vector search in SQLite | Retrieval p95 > 50 ms, or >50k chunks/tenant |

## Format

```markdown
# ADR-NNN: Title

**Status:** Proposed | Accepted | Superseded by ADR-XXX
**Date:** YYYY-MM-DD

## Context        What forced a decision
## Decision       What was chosen
## Alternatives considered   What else, and why not (the most useful section)
## Consequences   What this makes easy, and what it makes hard
## Security impact
## Cost impact
## Operational impact
## Trigger to revisit   (where applicable)
```

A decision recorded without its alternatives is an assertion, not a decision.
