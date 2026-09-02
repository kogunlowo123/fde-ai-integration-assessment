# Runbook

Procedures for the incidents these services actually produce. Each one is
keyed by symptom, because that is what an on-call engineer has.

---

## 1. MCP client hangs after connecting

**Symptom.** The client connects, sends `initialize`, and never receives a
response, or receives responses one request behind.

**Cause.** Something wrote to stdout. On an MCP stdio server, stdout is the
wire; one stray line desynchronises the framing.

**Diagnose.**

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}\n' \
  | python -m fde_assessment.mcp_server 2>/dev/null | head -1 | python -m json.tool
```

If that fails to parse, the first stdout line is not JSON-RPC.

**Fix.**

1. `ruff check src`, the `T20` rule finds `print`.
2. `pytest tests/integration/test_stdio_isolation.py -q`.
3. If a third-party library is the culprit, ensure `configure_logging()` runs
   before it is imported, and check for a library adding its own
   `StreamHandler(sys.stdout)`.

**Prevent.** The three controls in ARCHITECTURE.md §3 stay in place; do not
relax the `T20` rule.

---

## 2. Gateway returns 401 for every request

**Diagnose.**

```bash
docker exec fde-mcp-gateway printenv MCP_GATEWAY_TOKENS   # or the process env
curl -i localhost:8000/rpc -H 'authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

Check the logs for `event: "mcp_gateway_error"` with `code: "UNAUTHENTICATED"`
and `detail: "token not recognised"` (the token is not in the table) versus
`"missing Authorization header"` (the client is not sending one).

**Common causes.**

| Cause | Fix |
|---|---|
| Token missing from `MCP_GATEWAY_TOKENS` | Add it; the format is `token:role,token:role` |
| Whitespace inside the value | Strip it; `parse_pairs` trims, but a shell may have injected a newline |
| Client sending `Basic` or a bare token | Must be `Authorization: Bearer <token>` |
| Secret manager returned an empty value | Startup would have failed, check for a restart loop instead |

**Do not** disable authentication to restore service. Add the correct token.

---

## 3. A legitimate tenant is being rate limited

**Diagnose.**

```bash
curl -s localhost:8001/metrics | grep rate_limit_rejections_total
sqlite3 "$DATABASE_PATH" \
  "SELECT kind, COUNT(*), SUM(token_count) FROM rate_limit_events GROUP BY kind;"
```

**Decide which it is.**

| Evidence | Meaning | Action |
|---|---|---|
| Steady usage near the ceiling | Under-provisioned | Raise `RATE_LIMIT_TOKENS`, or move the tenant to a larger tier |
| Sudden spike from one tenant | Runaway client or abuse | Contact the tenant; consider a temporary lower ceiling |
| Rejections with a nearly empty table | Estimation is running high | Compare `tokens_requested` against `tokens_generated`; the up-front estimate charges maximum output |
| Rejections across all tenants at once | Clock or window misconfiguration | Check `RATE_LIMIT_WINDOW_SECONDS` and host time |

**Emergency relief.** Raising `RATE_LIMIT_TOKENS` takes effect on restart and
does not require a code change. Deleting rows from `rate_limit_events` clears
the window immediately but removes audit history, prefer waiting one window.

---

## 4. Constant failover to the secondary provider

**Symptom.** `fallback_total` rising; spend shifting to the secondary.

**Diagnose.**

```bash
curl -s localhost:8001/metrics | grep -E 'fallback_total|time_to_first_token'
```

| `reason` label | Meaning | Action |
|---|---|---|
| `MODEL_PROVIDER_RATE_LIMITED` | The primary is throttling us | Request a quota increase; reduce concurrency |
| `MODEL_PROVIDER_TIMEOUT` | No first token within `PRIMARY_TIMEOUT_MS` | Compare against `time_to_first_token_ms{provider}` p99; raise the deadline if the provider is simply slower than 3 s |
| `MODEL_PROVIDER_UNAVAILABLE` | Connection or 5xx | Check the provider's status page |
| `MODEL_PROVIDER_PROTOCOL_ERROR` | Malformed responses | Likely an API version change |

**Cost note.** Every failover doubles the cost of that request. If the primary
is down for an extended period, consider promoting the secondary to primary by
configuration rather than paying the failed attempt on every request, there is
no circuit breaker (ADR-010).

---

## 5. Suspected PII leak in a response

**Treat as a data incident.**

1. **Contain.** Identify the affected tenant and time window from
   `llm_gateway_completed` events by `request_id`. If the leak is systemic,
   consider disabling the affected route.
2. **Reproduce.** Recreate the exact text through the guardrail:
   ```bash
   python -c "from fde_assessment.llm_gateway.guardrails.pii import redact; \
              print(redact(open('sample.txt').read()).text)"
   ```
3. **Classify.**
   - *Pattern not implemented* (a name, an address, a phone number), expected;
     see SECURITY.md "Not claimed". Escalate to a guardrail scope decision.
   - *Pattern implemented but missed*, a bug. Add the case to
     `tests/unit/test_pii.py`, fix, ship.
   - *Split across a boundary beyond the window*, raise
     `PII_CARRY_BUFFER_CHARS` and re-measure (ADR-005).
4. **Check the logs.** Confirm the leaked value did not also reach the logs
   (it should not: prompts and completions are never logged).
5. **Notify** per the customer's incident process.

---

## 6. Suspected prompt injection via the knowledge base

**Symptom.** A model answer contradicts policy, reveals instructions, or
attempts an unexpected tool call.

**Diagnose.**

```bash
# Which documents were retrieved for that request?
grep '"event":"rag_retrieval"' gateway.log | grep '<request_id>'
```

The log records `document_ids`, never the passages, so inspect the named
documents directly.

**Contain.**

```bash
sqlite3 "$DATABASE_PATH" \
  "DELETE FROM rag_chunks WHERE document_id = '<id>' AND tenant_id = '<tenant>';
   DELETE FROM rag_documents WHERE document_id = '<id>' AND tenant_id = '<tenant>';"
```

**Then.**

1. Confirm the blast radius: an injected instruction cannot escalate privilege
   or cross a tenant (THREAT-MODEL.md §3). Verify no privileged tool call
   succeeded, check `unauthorized_tool_calls_total` and the MCP audit events.
2. Find how the document entered the corpus. Corpus write access is the actual
   control.
3. Add the document's text to the injection test fixtures.
4. Re-ingest the cleaned document.

---

## 7. `database is locked`

**Cause.** WAL or `busy_timeout` is not in effect, usually because the file is
on a network filesystem, or two processes are writing.

**Diagnose.**

```bash
sqlite3 "$DATABASE_PATH" "PRAGMA journal_mode; PRAGMA busy_timeout;"
# expect: wal / 5000
```

**Fix.**

1. Move the file to local disk. SQLite's locking is unreliable on NFS/SMB.
2. Confirm exactly one process writes to it. Two gateway replicas sharing a
   file is unsupported (ADR-004).
3. If contention is genuine, raise `RATE_LIMIT_BUSY_TIMEOUT_MS`; if it recurs,
   that is the trigger to move to Redis (ADR-006).

---

## 8. Retrieval returns nothing

```bash
sqlite3 "$DATABASE_PATH" "SELECT tenant_id, COUNT(*) FROM rag_chunks GROUP BY 1;"
```

| Result | Cause | Fix |
|---|---|---|
| No rows | Corpus never ingested | `python scripts/seed_db.py --tenant <id>` |
| Rows under a different tenant | Ingested under the wrong tenant | Re-ingest with the right `--tenant` |
| Rows present, still no hits | Embedder changed since ingestion | Re-ingest; mixed embedders produce meaningless similarities |
| Rows present, hits below the floor | Query vocabulary does not overlap the corpus | Expected with the lexical mock embedder; switch to Ollama embeddings for semantic matching |

---

## 9. Restore or reset the database

The SQLite file holds quota accounting and the vector store. Neither is a
system of record.

```bash
# Reset quota accounting only (keeps the knowledge base)
sqlite3 "$DATABASE_PATH" "DELETE FROM rate_limit_events;"

# Rebuild the knowledge base from source documents
sqlite3 "$DATABASE_PATH" "DELETE FROM rag_chunks; DELETE FROM rag_documents;"
python scripts/seed_db.py --tenant <id>

# Full reset
rm -f "$DATABASE_PATH" "$DATABASE_PATH"-wal "$DATABASE_PATH"-shm
python scripts/seed_db.py
```

Deleting quota rows resets every tenant's window to zero, effectively granting
a full budget immediately. Prefer waiting one window unless the outage is worse
than the over-admission.

---

## 10. Verifying a deployment

```bash
curl -fsS localhost:8000/healthz && curl -fsS localhost:8001/healthz

# Denial path (expect -32001, and no downstream invocation in the audit log)
curl -s localhost:8000/rpc -H 'authorization: Bearer <viewer-token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'

# Guardrail path (expect [REDACTED] in the stream)
curl -N -s localhost:8001/v1/chat/completions -H 'authorization: Bearer <tenant-key>' \
  -d '{"model":"mock-primary","messages":[{"role":"user","content":"summarise"}],"stream":true}'
```

Then confirm in the logs that `downstream_called: false` appears on the denial
event, the response being right and the action not happening are two separate
facts.
