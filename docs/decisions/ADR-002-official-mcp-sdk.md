# ADR-002, Official MCP SDK, low-level `Server` API

**Status:** Accepted · **Date:** 2026-09-02

## Context

The assessment requires the official SDK (`mcp`). The installed version (2.1.1)
offers two server APIs: the high-level `MCPServer` decorator interface, and the
low-level `Server` with explicit `on_list_tools` / `on_call_tool` handlers.

The scored requirement is "reject invalid formats with standard MCP JSON-RPC
error codes", which makes the distinction load-bearing: the high-level API
converts handler exceptions into tool results with `isError: true`, while the
low-level API lets a handler raise `MCPError` with an explicit code that the
runner maps onto a JSON-RPC error response.

## Decision

Use the official SDK with the low-level `Server`, and keep all business logic
in a transport-independent `ToolDispatcher` that imports nothing from `mcp`.

## Alternatives considered

**High-level `MCPServer` decorators.** Less code and automatic schema
generation from type hints. Rejected: validation failures would surface as
`isError` tool results rather than `-32602`, which is the opposite of what the
assessment scores.

**Hand-rolled JSON-RPC.** Total control, and it would have avoided the SDK
version churn. Rejected: the assessment requires the official SDK, and
reimplementing a protocol is how subtle incompatibilities reach a customer.

**SDK types throughout the business logic.** Rejected: it would have coupled
the validation matrix, the most valuable tests here, to a protocol handshake
and a subprocess.

## Consequences

- Validation and dispatch are unit-testable in microseconds with no transport.
- Error classification is explicit: protocol failures raise `MCPError`; domain
  outcomes return `CallToolResult(isError=True)`.
- The same dispatcher backs the stdio server and the HTTP mock downstream, so
  the two cannot drift.
- More wiring code than the decorator API, and the SDK's handler signatures are
  a dependency to track across major versions.

## Security impact

The dispatcher never sees unvalidated input, and handler exceptions are caught
and collapsed before reaching the transport, so an implementation detail cannot
escape in an error message.

## Cost impact

None at runtime. Test cost is far lower: the validation matrix runs in-process
rather than spawning a subprocess per case.

## Operational impact

SDK major versions may change handler signatures. The blast radius is
`mcp_server/server.py`, roughly 150 lines, because nothing else imports the
SDK.
