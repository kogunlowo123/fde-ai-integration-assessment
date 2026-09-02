# Security

What is implemented, how it is verified, and, equally important, what is not
claimed.

---

## Controls implemented

### Identity

| Control | Where | Verified by |
|---|---|---|
| Bearer token → role, constant-time, full-table comparison | `mcp_gateway/auth.py` | `tests/security/test_mcp_gateway_auth.py` |
| API key → tenant, constant-time | `llm_gateway/auth.py` | `tests/e2e/test_llm_gateway.py` |
| Raw credentials never stored on a principal or in a log | `common/models.py:fingerprint` | `test_principal_never_carries_the_raw_token` |
| API keys stored only as HMAC-SHA256 with a configured pepper | `common/models.py` | `tests/unit/test_config.py` |
| Missing, malformed and unknown credentials are indistinguishable | `auth.py` (identical 401, same work) | parametrised 401 tests |

**HMAC rather than a bare digest.** Bearer tokens and API keys are low-entropy
enough that an unsalted `sha256` of a leaked database column is reversible with
a dictionary. The pepper lives in configuration, not in the database, so
stealing the table alone does not recover the keys.

### Authorization

| Control | Where | Verified by |
|---|---|---|
| `admin_`-prefixed tools require the `admin` role | `mcp_gateway/policy.py` | `test_every_admin_prefixed_name_is_gated` |
| Role is taken from the credential, never the request body | `mcp_gateway/authorization.py` | `test_body_supplied_role_is_ignored` |
| Denied calls never reach the downstream server | `mcp_gateway/app.py` | `test_downstream_is_never_invoked_for_a_denied_call` (asserts `call_count == 0`) |
| Methods are allowlisted; unknown methods are rejected | `policy.py:METHOD_POLICY` | `test_unknown_method_is_rejected_at_the_gateway` |
| Unreadable tool names are denied, not forwarded | `authorization.py` | `test_non_string_tool_name_is_denied` |
| Retrieval is scoped by a SQL predicate, not a prompt | `rag/vector_store.py` | `tests/security/test_rag_isolation.py` |

### Input handling

Every entry point validates with Pydantic using `extra="forbid"`, so an
unexpected field is a rejection rather than something silently forwarded.

- Customer identifiers must match `CUST-[0-9]{5}` exactly, anchored, so a
  trailing newline does not slip through, and ASCII-only, because `\d`
  matches full-width Unicode digits (found by adversarial testing; see
  FINAL-REVIEW.md F-1).
- Refund amounts must be JSON numbers (not numeric strings, not booleans),
  positive, finite, and within a ceiling. NaN and Infinity are rejected
  explicitly because they break every downstream comparison they touch.
- Refund reasons need ten non-whitespace characters: ten spaces satisfies a
  naive `min_length` and carries no audit value.
- Request bodies are capped by declared `Content-Length` *and* by streamed
  size, because a chunked request can omit or lie about the former.
- `top_k`, context characters, prompt length and `max_tokens` are all bounded.

### Output handling

- Client-facing error messages come from a fixed table keyed by error code, never from `str(exc)`. Leaking is structurally impossible rather than
  merely avoided.
- Debug context lives in `internal_detail`, which is logged and never
  serialised.
- Upstream failures are normalised: `tests/integration/test_mcp_gateway_proxy.py`
  feeds the proxy a stack trace, an internal IP, a connection-refused errno and
  an HTML error page, and asserts none of them appear in the response.
- Model output passes the PII guardrail before reaching the client.

### Logging

Structured JSON to stderr only. A processor masks credential-bearing keys even
if a call site forgets, and prompts, completions and retrieved passages are
never logged. Full policy: [docs/security/LOGGING-POLICY.md](docs/security/LOGGING-POLICY.md).

### Transport and process posture

- Services bind `127.0.0.1` by default; the container sets `BIND_HOST=0.0.0.0`
  explicitly. A service does not expose itself on every interface just because
  someone ran it.
- The container runs as UID 10001 with a read-only root filesystem, all
  capabilities dropped and `no-new-privileges`.
- The downstream MCP server sits on an `internal: true` Docker network with no
  route off it.
- `follow_redirects=False` on the proxy client: a downstream 302 must not be
  able to walk the gateway to another host.
- The downstream URL is configuration, never request-derived, the SSRF
  control.

### Supply chain

`ruff` (including bandit-family `S` rules), `mypy --strict`, `bandit`,
`pip-audit` and `gitleaks` run in CI on every change, and the dependency audit
also runs weekly, because a dependency can become vulnerable without the code
changing.

---

## Verified results

Run on this machine at the time of writing; CI runs the same commands.

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 117 files already formatted |
| `mypy src` (strict) | Success: no issues found in 51 source files |
| `bandit -r src -c pyproject.toml` | 0 issues (0 high, 0 medium, 0 low) |
| `pip-audit` | No known vulnerabilities (audits the installed dependencies; `fde-assessment` itself is skipped as it is not on PyPI) |
| `pytest -q` | 472 passed |
| GitHub Actions (Linux, 3.12 and 3.13) | quality, package, Docker and security jobs green |

One environment note, so "clean" is not overstated: on this Windows machine an
Application Control policy blocks mypy's compiled `mypyc` extension, so mypy
was installed with `--no-binary mypy` and reports `compiled: no`. That is a
machine constraint, not a code one; CI installs the standard wheel.

---

## Not claimed

Stated plainly, because a security document that only lists strengths is not
useful.

**Authentication is a mock.** A configuration-driven token table, suitable for
an assessment and local development. Production needs OIDC/JWT validation
against the customer's identity provider: signature verification against JWKS,
`iss`/`aud`/`exp`/`nbf` checks, and a group or scope claim mapped to a role.
Nothing downstream consumes a token, only a `GatewayPrincipal`, so the swap
touches one function.

**There is no token revocation, rotation or replay protection.** A leaked
development token is valid until configuration changes. Real deployments need
short-lived tokens and a revocation path.

**PII detection is three patterns.** Emails, US SSNs and Luhn-valid card
numbers. Not detected: names, postal addresses, phone numbers, passport and
national-insurance numbers, IBANs, API keys, dates of birth, and free-text
medical or financial detail. Obfuscation, `j o h n @ e x a m p l e . c o m`,
base64, homoglyphs, "at"/"dot" spellings, defeats regex entirely. This raises
the cost of accidental disclosure; it does not stop a model that is
on purpose encoding data.

**The guardrail has a bounded blind spot.** A match longer than the carry
window (128 characters by default) can be split across the emit boundary and
partially escape. The window is configurable; the trade is latency and memory
against recall on pathological inputs.

**Prompt injection is mitigated, not solved.** Structural separation and
delimiter neutralisation raise the bar. A sufficiently persuasive passage can
still steer an answer. The claim that *is* made: injected text cannot escalate
privilege, cross a tenant boundary, or reach a tool the caller could not
already call.

**Rate limiting is per node.** Two gateway replicas each admit up to the limit.
SQLite cannot coordinate across processes on different machines.

**No compliance certification of any kind.** Not SOC 2, not HIPAA, not
PCI DSS, not FedRAMP. The healthcare and financial-services material in
[FDE-DELIVERY.md](FDE-DELIVERY.md) describes architectural patterns that
support such programmes; it does not assert any of them have been achieved.

**No production deployment.** Everything here was tested locally and in CI.
There are no availability, latency or throughput guarantees.

**TLS terminates elsewhere.** The services speak HTTP; a load balancer or
service mesh is expected to terminate TLS and enforce mTLS between hops.

---

## Reporting a vulnerability

This is an assessment repository, not a deployed service. If you find an issue
in the code, open an issue describing the class of problem and the affected
module. Please do not include a working exploit.
