# ADR-011, HTTP status semantics differ between the two gateways

**Status:** Accepted · **Date:** 2026-09-02

## Context

Both gateways return errors over HTTP, but they serve different kinds of
client. The MCP gateway carries JSON-RPC for MCP clients; the LLM gateway is an
OpenAI-shaped REST API for HTTP clients. Applying one rule to both makes one of
them wrong.

The assessment specifies the MCP denial payload verbatim:

```json
{"jsonrpc": "2.0", "id": "...", "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
```

It does not specify an HTTP status.

## Decision

**MCP gateway.** A failure that stopped a valid JSON-RPC exchange from starting
carries an HTTP status; everything after that is a JSON-RPC outcome at HTTP 200.

| Situation | HTTP | JSON-RPC |
|---|---|---|
| Missing/invalid credentials | 401 + `WWW-Authenticate` | -32001 |
| Body over the size limit | 413 | -32600 |
| Unparseable JSON | 400 | -32700 |
| Malformed envelope | 400 | -32600 |
| Unauthorized tool call | **200** | **-32001** |
| Method outside policy | 200 | -32601 |
| Downstream timeout | 200 | -32004 |
| Downstream failure | 200 | -32005 |

Implemented as one table, `TRANSPORT_LEVEL_STATUS` in `mcp_gateway/app.py`, so
the rule exists in exactly one place.

**LLM gateway.** Conventional REST statuses: 401, 413, 422, 429 (with
`Retry-After`), 502, 504. Errors raised after a stream has started, where the
status is already sent, are delivered as a terminal SSE frame carrying the
same envelope.

## Alternatives considered

**HTTP statuses everywhere, including MCP.** More conventional for an HTTP
proxy. Rejected: a JSON-RPC client is required to parse the error object, and
many HTTP clients discard the body on a 5xx. A downstream timeout reported as
504 reaches the agent as "the gateway is broken" rather than "that call did not
work", a materially worse diagnosis.

**HTTP 200 for everything on both gateways.** Consistent, and common in
JSON-RPC-over-HTTP implementations. Rejected for the LLM gateway: OpenAI-shaped
clients, proxies, retry middleware and dashboards all key off status codes, and
a 200 carrying an error breaks every one of them.

**403 for the unauthorized tool call.** Semantically precise. Rejected: the
assessment shows a JSON-RPC error object, and a JSON-RPC client that treats a
403 as a transport failure never reads the `-32001` it was told to expect.

## Consequences

- The two surfaces differ on purpose, and the difference is documented in both
  module docstrings and asserted in `TestStatusCodeContract`.
- Operators reading MCP gateway logs must use `requests_failed_total{code}`
  rather than HTTP status counts, since most failures are 200s. Called out in
  OPERATIONS.md.
- A single table means adding an error code cannot silently change the
  contract.

## Security impact

Neutral. Both paths use the same sanitised message table; only the envelope
differs.

## Cost impact

None.

## Operational impact

Load-balancer health and error-rate dashboards for the MCP gateway must be
built on the metrics endpoint, not on HTTP status codes.
