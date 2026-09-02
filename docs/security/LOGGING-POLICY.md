# Logging policy

Logs are a security surface. They are copied, shipped, indexed, retained for
years and read by people who never had access to the original data. This policy
defines what may be written and how it is enforced.

---

## Rules

### Never logged

| Category | Examples |
|---|---|
| Credentials | `Authorization` headers, bearer tokens, API keys, the pepper, cookies |
| Model input | Prompts, message content, system prompts containing customer data |
| Model output | Completions, streamed deltas |
| Retrieved content | Document text, chunk text, the retrieval query itself |
| Direct PII | Emails, SSNs, card numbers, names, addresses |
| Upstream bodies | Provider or downstream response payloads (they may contain any of the above) |

### Always logged

| Field | Purpose |
|---|---|
| `request_id` | Correlation across services |
| `event` | Stable machine-readable name (`mcp_gateway_audit`, `rate_limit_rejected`) |
| `level`, `timestamp` | ISO-8601 UTC |
| Outcome | `ok`, `denied`, `forwarded`, error code |
| Latency | `elapsed_ms`, `ttft_ms` |

### Logged as identifiers, never as content

| Instead of | Log |
|---|---|
| The bearer token | `subject: "token:a1b2c3d4"` (truncated HMAC) |
| The API key | `tenant: "tenant-a"` |
| The prompt | `estimated_tokens: 1024` |
| The completion | `generated_tokens: 187` |
| Retrieved passages | `document_ids: ["refund-policy"]`, `hits: 3`, `top_score: 0.71` |
| The failing value | `fields: ["customer_id"]`, `violations: 1` |
| The upstream error body | `code: "MODEL_PROVIDER_TIMEOUT"`, `error_type: "ReadTimeout"` |

The pattern throughout: log the *shape* of what happened, never the payload.

---

## Enforcement

1. **Sink.** `common/logging.py` binds structlog to
   `WriteLoggerFactory(sys.stderr)` and replaces the stdlib root handlers with
   a single stderr handler. Nothing can reach stdout, which is also the MCP
   transport (see ARCHITECTURE.md).
2. **Redaction processor.** `_redact_processor` masks any event key whose name
   matches a sensitive list (`authorization`, `api_key`, `token`, `prompt`,
   `messages`, `content`, `chunk_text`, …), so a call site that forgets is
   still safe.
3. **Structured only.** `log.info("event_name", field=value)`. No f-strings
   interpolating data into a message, which is how PII usually reaches a log.
4. **Correlation ids are sanitised.** A caller-supplied `x-request-id` is
   filtered to `[A-Za-z0-9-_]` and truncated to 64 characters, an id
   containing newlines is a log-injection primitive.
5. **`internal_detail` is engineered to be loggable.** It carries an exception
   *class name* and a short reason, never the exception's message, because an
   upstream message may embed a credential or a customer record. It is never
   serialised into a response.
6. **Tests.** `tests/integration/test_stdio_isolation.py` asserts stdout purity
   with a negative control; `tests/security/` asserts that stack traces, hosts
   and credentials do not appear in responses.

---

## Levels

| Level | Use |
|---|---|
| `DEBUG` | Local development only. Not for production, it widens what is written |
| `INFO` | Normal operations: request completed, audit decision, ingestion result |
| `WARNING` | Handled failures: upstream timeout, denial, validation failure |
| `ERROR` | Unexpected failures needing attention: all providers down, unhandled exception |

Audit events are `INFO`. They must survive log-level tuning, so they are never
`DEBUG`.

---

## Audit events

One event per security-relevant decision. `mcp_gateway_audit` carries:

```json
{
  "event": "mcp_gateway_audit",
  "request_id": "mcpgw-3f9a2b7c1d4e5f60",
  "principal_subject": "token:a1b2c3d4",
  "role": "viewer",
  "method": "tools/call",
  "tool": "admin_reset_key",
  "outcome": "denied",
  "reason": "insufficient_role",
  "downstream_called": false,
  "elapsed_ms": 0.42,
  "level": "info",
  "timestamp": "2026-09-02T17:05:07.353572Z"
}
```

`downstream_called` is deliberate: it makes "the denied call never reached the
tool server" auditable after the fact, not merely testable before it.

---

## Retention and shipping

Not implemented here, the services write to stderr and the platform decides.
For production:

- Ship to append-only storage; audit logs must not be editable by the roles
  they record (THREAT-MODEL.md, repudiation).
- Retain audit events per the customer's obligation, commonly 12-24 months,
  longer in regulated sectors.
- Operational logs can expire far sooner; they are for debugging, not evidence.
- Access to logs is access to metadata about customer activity. Scope it.

---

## If PII reaches a log

Treat it as a data incident, not a bug:

1. Identify the affected log streams and time range by `event` name.
2. Purge from the log store and from downstream indices and backups.
3. Fix the call site *and* add the offending key to the redaction list, so the
   class of mistake cannot recur.
4. Add a test asserting the field is not present.
5. Notify per the customer's incident process.
