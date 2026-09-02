# ADR-004, On-disk SQLite for durable state

**Status:** Accepted · **Date:** 2026-09-02

## Context

Task 4 states: "Use on disk sqlite for database." The state to hold is
rate-limit accounting (high write rate, short retention) and, for the RAG
enhancement, document chunks and embeddings (low write rate, long retention).

The requirement is explicit, so the decision is not *whether* to use SQLite but
how to use it correctly under concurrency, the naive configuration fails with
`database is locked` the first time two coroutines write.

## Decision

One on-disk SQLite database via `aiosqlite`, configured with:

| Setting | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers do not block the writer, and the writer does not block readers |
| `busy_timeout` | 5000 ms | A blocked writer waits instead of failing immediately |
| `synchronous` | `NORMAL` | With WAL this is durable across application crashes; only a machine crash can lose the last transactions |
| `foreign_keys` | `ON` | The chunk→document relationship is enforced, not assumed |
| Writes | `BEGIN IMMEDIATE` + `asyncio.Lock` | Makes read-check-write atomic (ADR-006) |

## Alternatives considered

**In-memory dictionary.** Fastest and simplest. Rejected: the requirement says
on-disk, and quota state that resets on every restart is a quota bypass.

**PostgreSQL.** The right answer above one node. Rejected here: it contradicts
the brief, and adds a service to run, back up and secure for a workload that
one file handles.

**Redis.** The right answer for distributed rate limiting specifically.
Rejected for the same reasons, and recorded as the evolution in ADR-006.

**Default SQLite settings.** Rejected empirically: without WAL and
`busy_timeout`, the concurrency tests in `tests/concurrency/` fail with
`database is locked`.

## Consequences

- Zero infrastructure; the test suite creates a real database per temporary
  directory and deletes it.
- Genuinely transactional, which is what makes the limiter correct.
- **One writer at a time, machine-wide.** Fine for a single node; a bottleneck
  above roughly a few thousand writes per second.
- **No horizontal scaling.** Two replicas cannot share the file safely over a
  network filesystem. Rate limiting is therefore per node.

Measured: ~0.65 ms mean per admission sequentially, and 200 concurrent
admissions complete in ~107 ms (~0.54 ms each), [BENCHMARKS.md](../testing/BENCHMARKS.md).

## Security impact

No credentials are stored, only HMAC fingerprints. Every statement is
parameterised. The file is the only writable path the container needs, which
keeps the root filesystem read-only.

## Cost impact

$0 of managed infrastructure. A managed PostgreSQL plus Redis is roughly
$150-400/month at small scale before any traffic.

## Operational impact

Back up the file if quota continuity across restarts matters; otherwise it is
disposable, the corpus can be re-ingested and quota state self-heals within
one window. **Do not** place it on NFS or a shared volume with multiple
writers.

## Trigger to revisit

Any of: a second gateway replica, sustained write contention visible as
`database is locked` in logs, or a corpus beyond ~50k chunks per tenant.
