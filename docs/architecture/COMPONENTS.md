# Component reference

WHAT / WHY / HOW / WHEN, plus SECURITY, COST and SCALE, for every major
component. The same structure opens each module's source, so this page is a
map, not a second source of truth.

---

## MCP server (`mcp_server/`)

| | |
|---|---|
| **What** | Exposes `get_customer_record` and `trigger_refund` over the MCP stdio transport using the official Python SDK. |
| **Why** | Tools are how an agent acts on the world. The value is not the two functions, it is that hostile input cannot reach them and that the transport stays uncorrupted. |
| **How** | A transport-independent `ToolDispatcher` validates against Pydantic models and dispatches; `server.py` binds it to the SDK's low-level `Server` and maps failures to JSON-RPC codes. |
| **When** | An MCP client spawns `python -m fde_assessment.mcp_server`. One process per client session. |
| **Security** | Anchored pattern validation, `extra="forbid"`, finite-number enforcement, error messages from a fixed table. Protocol errors and domain outcomes are by design different things. |
| **Cost** | ~0.008 ms per dispatch. No I/O beyond the transport. |
| **Scale** | One process per session; replace the in-memory repositories with adapters over real systems of record. Nothing else changes. |

---

## Tool registry (`mcp_server/registry.py`)

| | |
|---|---|
| **What** | The tool catalogue, argument validation and dispatch. Imports nothing from the MCP SDK. |
| **Why** | Keeps the validation matrix testable in microseconds, and draws the line between a protocol failure (`-32602`) and a domain outcome (`isError: true`). Reporting a refusal as a protocol error would tell the client the call never happened. |
| **How** | `ToolSpec` binds a name to an input model and an async handler; `ToolDispatcher.call` validates then dispatches. |
| **When** | Register a tool here; the stdio server and the HTTP mock both pick it up. |
| **Security** | Handlers never see unvalidated input; handler exceptions are caught and collapsed so no implementation detail escapes. |
| **Cost** | Pure CPU. |
| **Scale** | Add tools without touching transport code. |

---

## MCP security gateway (`mcp_gateway/`)

| | |
|---|---|
| **What** | Authenticating, authorizing reverse proxy for MCP JSON-RPC. |
| **Why** | `tools/call` is remote code execution by design. A policy enforcement point is what makes an MCP server deployable where the agent, the tools and the data have different owners. |
| **How** | Linear pipeline: bound body → authenticate → parse → authorize → forward. Each stage fails closed. |
| **When** | Every agent points here; the MCP server is not routable from the agent network. |
| **Security** | Identity from the header only; allowlisted methods; `admin_` gating; audit event per decision recording whether the downstream was invoked. |
| **Cost** | ~1.29 ms per forwarded call, ~0.59 ms for a denial (which opens no downstream connection). |
| **Scale** | Stateless; scale horizontally behind a load balancer. |

---

## LLM gateway (`llm_gateway/app.py`)

| | |
|---|---|
| **What** | `POST /v1/chat/completions` with tenant auth, token budget, optional retrieval, routing with fallback, and streaming redaction. |
| **Why** | Proxying a model API is the easy half. The half that decides whether it survives production is the tenancy, the cost ceiling, the guardrail and the vendor-incident behaviour. |
| **How** | Authenticate → validate → estimate → rate limit → [retrieve] → route → guard → stream → reconcile. |
| **When** | Every application call to a model goes through it. |
| **Security** | Errors leave through one function; prompts and completions are never logged; failures after the response has started become a terminal SSE frame carrying the same envelope. |
| **Cost** | Rate limiting rejects before any provider call. Failover is metered so a silent spend shift is visible. |
| **Scale** | Stateless except for SQLite. Per-node rate limiting until Redis (ADR-006). |

---

## Streaming guardrail (`llm_gateway/guardrails/`)

| | |
|---|---|
| **What** | Redacts emails, US SSNs and Luhn-valid card numbers from a chunked stream. |
| **Why** | The gateway is the last place model output can be stopped before it reaches a client, a log or a browser cache. |
| **How** | Carry only text that could still become a match; emit everything else immediately; hard-cap the carry (ADR-005). |
| **When** | Wraps every provider stream, streaming and non-streaming alike. |
| **Security** | Closes the chunk-boundary bypass. Recall limits are documented, not implied. |
| **Cost** | ~1.49 ms per 4 KB; ~3.4 µs per chunk. |
| **Scale** | O(window) memory per stream, independent of response length. |

---

## Rate limiter (`llm_gateway/rate_limit/`)

| | |
|---|---|
| **What** | Sliding-window token budget per tenant API key, on on-disk SQLite. |
| **Why** | Request counts do not describe LLM cost. A fixed window permits a 2× burst across the boundary. |
| **How** | Check and insert in one `BEGIN IMMEDIATE` transaction behind an `asyncio.Lock`; eviction happens in the same transaction. |
| **When** | Once per completion request, before any provider is contacted. |
| **Security** | Only HMAC fingerprints stored; fails closed on database errors. |
| **Cost** | The mechanism that bounds spend per tenant. ~0.65 ms per admission. |
| **Scale** | Per node. Redis when a second replica appears. |

---

## Model router (`llm_gateway/routing/`)

| | |
|---|---|
| **What** | Primary provider with fallback to a secondary on 429 or a first-token deadline. |
| **Why** | Converts a vendor incident into added latency rather than an outage, but only for failures that are actually transient. |
| **How** | `asyncio.timeout` around the first `anext`, then `aclose()` on the abandoned generator so the upstream is in fact released. |
| **When** | Every completion. |
| **Security** | Both providers' failures are normalised; the caller cannot tell which upstream failed. |
| **Cost** | Failover doubles the request cost; `fallback_total` makes that visible. |
| **Scale** | Extend to cost-based model selection without touching the providers. |

---

## Providers (`llm_gateway/providers/`)

| | |
|---|---|
| **What** | `LLMProvider` plus mock, scripted-failure, hanging and Ollama implementations. |
| **Why** | Provider independence is the core LLMOps property: $0 CI, deterministic failure injection, and a vendor change that is a configuration change. |
| **How** | One async-generator method; non-streaming is the degenerate case. |
| **When** | Mock in CI and development; Ollama locally; a real vendor in production. |
| **Security** | Vendor payloads never propagate past the adapter. The factory is explicit, so a provider cannot be added by configuration alone. |
| **Cost** | $0 in CI, by construction. |
| **Scale** | One file per vendor. |

---

## RAG service (`rag/`), Production Enhancement

| | |
|---|---|
| **What** | Ingestion, chunking, embeddings, vector store, retrieval and prompt assembly. |
| **Why** | Enterprise knowledge is where the value is, and where the tenancy and injection risks are. |
| **How** | Filter in SQL, score in Python, budget the context, place passages in a labelled untrusted region. |
| **When** | Opt-in per request (`rag.enabled`), or via the `search_knowledge_base` MCP tool when a corpus is configured. |
| **Security** | Tenant isolation is a SQL predicate; delimiter lookalikes are neutralised; citations name only what actually entered the prompt. |
| **Cost** | Content hashing means unchanged documents are never re-embedded; `top_k` and context characters cap retrieval's share of every prompt. |
| **Scale** | Brute-force cosine to roughly tens of thousands of chunks per tenant; then pgvector (ADR-012). |

---

## Persistence (`persistence/sqlite.py`)

| | |
|---|---|
| **What** | One `aiosqlite` connection with WAL, `busy_timeout`, `synchronous=NORMAL`, foreign keys, and a write-transaction context manager. |
| **Why** | SQLite is correct at this scale *if* its concurrency model is respected. Default settings fail under concurrency. |
| **How** | `BEGIN IMMEDIATE` inside an `asyncio.Lock`; rollback on any exception. |
| **When** | One instance per process. |
| **Security** | Parameterised statements only; no credentials stored. |
| **Cost** | $0 infrastructure. |
| **Scale** | Single writer. PostgreSQL/Redis beyond one node (ADR-004). |

---

## Observability (`observability/metrics.py`)

| | |
|---|---|
| **What** | Thread-safe counters and fixed-bucket latency histograms with Prometheus text rendering. |
| **Why** | LLMOps needs four questions answered continuously: how much traffic, how much failed, how slow, how much did it cost. |
| **How** | Label sets flattened to a sorted key; histograms keep buckets plus sum and count. |
| **When** | Increment from request paths; never with a high-cardinality label. |
| **Security** | Label values are enumerations and tenant ids, never secrets or free text, which would make the registry itself a leak. |
| **Cost** | In-process, no exporter. |
| **Scale** | Swap for `prometheus_client` or OTLP; call sites do not change. |

---

## Configuration (`common/config.py`)

| | |
|---|---|
| **What** | One validated `Settings` object built at startup. |
| **Why** | Configuration is a security boundary. Validating once turns a class of runtime failures into a loud crash before the first request. |
| **How** | `pydantic-settings` over `.env` and the environment; `.env.example` is generated from the model and parity is asserted in tests. |
| **When** | Read at entrypoints; passed down explicitly. |
| **Security** | Refuses to start in `production` with published development credentials; binds loopback by default. |
| **Cost** | Every spend-multiplying knob is bounded here rather than at call sites. |
| **Scale** | The seam for a secret manager. |
