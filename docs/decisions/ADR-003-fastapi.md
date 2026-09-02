# ADR-003, FastAPI for both gateways

**Status:** Accepted · **Date:** 2026-09-02

## Context

Both gateways are HTTP services. One streams server-sent events with strict
latency requirements; the other proxies JSON-RPC with strict authorization
requirements. Both need request-scoped dependency injection, testability
without a live socket, and lifespan management for a database connection and an
HTTP client pool.

## Decision

FastAPI (on Starlette) with `uvicorn`, and `httpx` for outbound calls.

## Alternatives considered

**Starlette alone.** Fewer layers, and FastAPI's automatic request-model
binding is unused here, both gateways read the raw body to enforce a size cap
before parsing. Rejected narrowly: FastAPI's lifespan, dependency wiring and
`TestClient` ergonomics are worth the thin layer, and the OpenAPI surface is
useful for a customer integrating against the LLM gateway.

**Flask + gunicorn.** Rejected: no native async, and streaming SSE from a WSGI
worker fights the framework.

**aiohttp.** Capable and fast. Rejected: smaller ecosystem for the middleware
and testing patterns used here.

## Consequences

- `TestClient` runs the real ASGI app including lifespan, so tests exercise the
  actual request pipeline rather than calling handler functions directly.
- `httpx.ASGITransport` lets the MCP gateway talk to an in-process mock
  downstream over the real client code path, which is what makes the
  "downstream was never called" assertion meaningful.
- `StreamingResponse` over an async generator gives true incremental streaming;
  nothing accumulates the response.
- Bodies are read manually via `request.stream()` to enforce the size cap, so
  FastAPI's automatic model binding is deliberately bypassed on those routes.

## Security impact

Neutral in itself; the controls are ours. FastAPI does supply well-tested
request parsing and header handling, which is a meaningful amount of code not
written here.

## Cost impact

Measured gateway overhead is ~1.29 ms per forwarded call
([BENCHMARKS.md](../testing/BENCHMARKS.md)), negligible against provider
latency.

## Operational impact

Standard ASGI deployment. `uvicorn` with `--workers` scales on one host,
subject to the single-writer SQLite constraint (ADR-004).
