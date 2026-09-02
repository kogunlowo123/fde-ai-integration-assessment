# Operations

Running these services: configuration, health, metrics, alerts, failure modes
and the checks to run when something is wrong.

---

## Services

| Service | Command | Port | State |
|---|---|---|---|
| MCP server | `python -m fde_assessment.mcp_server` | stdio | none |
| MCP gateway | `python -m fde_assessment.mcp_gateway` | 8000 | none |
| LLM gateway | `python -m fde_assessment.llm_gateway` | 8001 | SQLite at `DATABASE_PATH` |
| Mock MCP downstream | see README | 9000 | none |

Both gateways are stateless apart from the SQLite file, so they restart freely.
The file holds rate-limit accounting and the vector store; losing it resets
quotas to zero (fail-open for one window) and empties the knowledge base.

---

## Configuration

All configuration is environment variables, validated at startup by
`Settings`. A bad value is a crash before the first request, not a surprise
later. The complete list is in [.env.example](.env.example), generated from the
model, `python scripts/gen_env_example.py --check` fails if it drifts.

The settings that change behaviour most:

| Variable | Default | Effect of changing it |
|---|---|---|
| `RATE_LIMIT_TOKENS` | 50000 | Per-key budget per window |
| `RATE_LIMIT_WINDOW_SECONDS` | 60 | Longer smooths bursts; shorter is stricter |
| `PRIMARY_TIMEOUT_MS` | 3000 | First-token deadline. Too low causes needless failover; too high delays recovery |
| `PII_CARRY_BUFFER_CHARS` | 128 | Larger catches longer matches, at more memory and latency |
| `MCP_DOWNSTREAM_URL` | loopback | The only place the proxy target is set, never request-derived |
| `BIND_HOST` | 127.0.0.1 | Containers must set `0.0.0.0`; a bare process should not |
| `APP_ENV` | development | `production` refuses to start with development credentials |

---

## Health and metrics

```bash
curl localhost:8000/healthz     # {"status":"ok"}
curl localhost:8001/healthz
curl localhost:8001/metrics     # Prometheus text format
```

`/healthz` is a liveness check: the process is up and serving. It deliberately
does not check the downstream provider, a readiness probe that fails when a
third party is slow takes your own service out of rotation during someone
else's incident.

### Metrics worth a dashboard

| Metric | Labels | Read it as |
|---|---|---|
| `requests_total` | surface, provider, method | Traffic |
| `requests_failed_total` | surface, code | Failure mix by cause |
| `gateway_latency_ms` | surface | Your own overhead |
| `provider_latency_ms` | provider | Their latency |
| `time_to_first_token_ms` | provider | User-perceived responsiveness |
| `fallback_total` | primary, reason | Primary health **and** spend shift |
| `rate_limit_rejections_total` | tenant | Sizing or abuse |
| `pii_redactions_total` | kind | Guardrail activity; a spike means something changed upstream |
| `unauthorized_tool_calls_total` | tool, reason | Misconfigured agent, or an attack |
| `mcp_tool_calls_total` | tool, outcome | Tool usage and validation failure rate |
| `rag_*` | tenant | Retrieval volume, latency, empties, errors |

### Suggested alerts

| Alert | Condition | Why |
|---|---|---|
| Primary degraded | `fallback_total` rate > 5% of requests for 5 min | The primary is failing and spend has moved |
| Tenant throttled | `rate_limit_rejections_total{tenant}` > 0 for 15 min | Under-provisioned or abusive |
| Guardrail spike | `pii_redactions_total` > 3× the 7-day baseline | Upstream data or prompt changed |
| Unauthorized calls | `unauthorized_tool_calls_total` > 0 sustained | Misconfigured agent, or probing |
| Latency regression | p95 `gateway_latency_ms` > 50 ms | Our overhead, not the provider's |
| Empty retrieval | `rag_retrieval_empty_total` > 20% of `rag_queries_total` | Ingestion broken or the corpus is wrong |

Baselines from [docs/testing/BENCHMARKS.md](docs/testing/BENCHMARKS.md): gateway
overhead is ~1.3 ms per forwarded call and ~0.6 ms for a denial, so a p95 above
50 ms means something is wrong locally, not upstream.

---

## Logging

Structured JSON on **stderr only**. Correlate with `request_id`, which is
echoed in the `x-request-id` response header and accepted from the caller
(sanitised to `[A-Za-z0-9-_]`, truncated to 64 characters).

```bash
python -m fde_assessment.llm_gateway 2>&1 | jq 'select(.event=="llm_gateway_completed")'
```

Never logged: credentials, prompts, completions, retrieved passages, PII. Full
policy: [docs/security/LOGGING-POLICY.md](docs/security/LOGGING-POLICY.md).

---

## Failure modes

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| MCP client hangs after connecting | Something wrote to stdout | `python -m fde_assessment.mcp_server < frames.jsonl \| head -1`, is line 1 JSON? | Remove the write; `ruff check` catches `print` |
| Every request 401 | Token table mismatch | `MCP_GATEWAY_TOKENS` in the running environment | Align the token; check for whitespace in the value |
| Every request 429 immediately | Window carries stale usage, or budget too small | `SELECT SUM(token_count) FROM rate_limit_events WHERE api_key_hash=?` | Wait one window, or raise `RATE_LIMIT_TOKENS` |
| Constant failover | `PRIMARY_TIMEOUT_MS` below the provider's real TTFT | `time_to_first_token_ms{provider}` p95 | Raise the deadline to p99 + headroom |
| `database is locked` | WAL or busy_timeout not applied (e.g. a network filesystem) | `PRAGMA journal_mode` | Move the file to local disk; raise `RATE_LIMIT_BUSY_TIMEOUT_MS` |
| Retrieval returns nothing | Corpus not ingested, or wrong tenant | `SELECT tenant_id, COUNT(*) FROM rag_chunks GROUP BY 1` | `python scripts/seed_db.py --tenant <id>` |
| 502 on every completion | Both providers failing | `requests_failed_total{code}` | Check provider status; consider a third provider |
| Container restart loop | `APP_ENV=production` with development credentials | Container logs, the message names the offending variables | Set real credentials |

---

## Runbooks

Step-by-step procedures live in
[docs/operations/RUNBOOK.md](docs/operations/RUNBOOK.md):

- Gateway returning 401 for everyone
- Rate limiter rejecting a legitimate tenant
- Primary provider degraded
- Suspected PII leak in a response
- Suspected prompt injection via the knowledge base
- Restoring or resetting the SQLite database

---

## Deployment checklist

Before any environment carrying real traffic:

- [ ] `APP_ENV=production`, startup then refuses the published development
      credentials.
- [ ] `MCP_GATEWAY_TOKENS`, `LLM_GATEWAY_TENANTS`, `API_KEY_PEPPER` sourced
      from a secret manager, not a file.
- [ ] `PRIMARY_PROVIDER` / `SECONDARY_PROVIDER` set to a real provider.
      **Nothing in the code prevents `mock` from reaching production**, the
      credential check does not cover provider selection. The shipped
      `docker-compose.yml` uses `mock` on purpose; it is a local demonstration,
      not a production manifest.
- [ ] `BIND_HOST=0.0.0.0` only inside a container whose network is scoped.
- [ ] `DATABASE_PATH` on local disk, not a shared or network filesystem.
- [ ] Exactly one replica per SQLite file, or Redis in place first (ADR-006).
- [ ] The MCP tool server unreachable from the agent network.
- [ ] TLS terminated upstream; mTLS between hops where the environment expects it.
- [ ] Logs shipped off-host to append-only storage.

## Deployment notes

**Network posture is a control, not a detail.** The MCP tool server has no
authentication of its own. It must not be routable from the agent network. In
`docker-compose.yml` this is an `internal: true` network; in Kubernetes it is a
`NetworkPolicy` allowing ingress only from the gateway's pod selector. Without
it, the gateway is decoration.

**One writer per SQLite file.** Two gateway replicas sharing a file over a
network filesystem will corrupt it. Either run one replica per file, or move to
Redis/PostgreSQL (ADR-004, ADR-006) before scaling horizontally.

**Rate limiting is per node.** Two replicas each admit up to the limit, the
effective budget doubles. Confirm the replica count before promising a tenant a
number.

**TLS terminates upstream.** The services speak HTTP; a load balancer or mesh
is expected to terminate TLS and enforce mTLS between hops.

**Backups.** The SQLite file holds quota accounting and the vector store.
Neither is a system of record; the corpus can be re-ingested with
`scripts/seed_db.py`, and quota state self-heals within one window. Back it up
if quota continuity across restarts matters; otherwise it is disposable.

---

## Capacity

Measured locally with the mock provider (see BENCHMARKS.md for the machine):

| Operation | Mean | p95 |
|---|---|---|
| MCP tool dispatch | 0.008 ms | 0.014 ms |
| MCP gateway forwarded call | 1.29 ms | 1.69 ms |
| MCP gateway denial (short circuit) | 0.59 ms | 0.79 ms |
| Guardrail, 4 KB of prose | 1.49 ms | 2.34 ms |
| Rate-limiter admission (sequential) | 0.65 ms | 1.28 ms |
| RAG search (8 chunks) | 0.62 ms | 0.98 ms |

These are the cost of *this code*, not a capacity model: provider latency
dominates any real deployment, and the numbers come from one machine with a
warm cache and no network. The useful reading is the ratio, gateway overhead
is roughly 1 ms against provider latencies measured in hundreds.
