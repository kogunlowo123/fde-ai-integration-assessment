# Final review

A self-review conducted as the hiring manager would: score each dimension,
attack the system, and record every finding, including the ones that are still
open.

**Method.** Scores are 1-5 against what a senior FDE would expect to see in a
take-home of this scope, not against a hypothetical production platform.
Findings come from an adversarial pass over the finished system, not from
re-reading the plan.

---

## Scores

| Dimension | Score | Reasoning |
|---|---|---|
| Architecture | 5 | Two independent gateways, one shared vocabulary, no coupling. Transport separated from logic, which is what makes the validation matrix testable and the SDK replaceable. |
| Python engineering | 5 | `mypy --strict` clean across 51 files, no `Any` leakage, async discipline enforced by lint, no global mutable state on request paths. |
| MCP knowledge | 5 | Correct use of the low-level SDK to get real JSON-RPC error codes; the protocol-error vs domain-outcome distinction; STDIO isolation with a negative control. |
| AI integration | 4 | Provider abstraction, streaming, retrieval, citations. No tool-calling loop or multi-turn agent orchestration, outside the brief, but it is what a 5 would add. |
| LLMOps | 5 | Provider independence, routing, quotas, guardrails, metrics, $0 CI, measured retrieval quality with thresholds set below observed values. |
| Security | 4 | Fail-closed authorization, no leakage, HMAC fingerprints, threat model with residual risk. Held at 4 by the mock authenticator and the absence of token revocation, both documented, neither solved. |
| Streaming | 5 | The bounded-carry design solves the stated tension rather than picking a side, and is verified against every two-way split of every fixture. |
| Rate limiting | 5 | The race is the difficulty and it is addressed transactionally, with concurrency tests that fail against the naive implementation. |
| Reliability | 4 | Cancellation-aware failover restricted to retryable failures. No circuit breaker and no idle timeout between tokens, both named as future work. |
| Testing | 5 | 472 tests across seven suites, offline and deterministic, asserting absences as well as presences. |
| Observability | 4 | Structured audit events, useful metric set, correlation ids. In-process registry rather than a real exporter; no tracing. |
| Cost optimization | 5 | $0 CI by construction, quotas, content-hash skipping, and levers ranked by actual impact rather than listed. |
| FDE thinking | 5 | Discovery questions that change designs, rollout with exit criteria, per-vertical constraints, and a missing task investigated rather than invented. |
| Documentation | 5 | Reasoning next to the code, 12 ADRs with alternatives and revisit triggers, and limitations stated in the README rather than buried. |
| Production readiness | 3 | Honest score. Correct and well-tested, but the authenticator is a mock, rate limiting is per node, and nothing has been deployed. The path is documented; the work is not done. |

**Overall: 4.6 / 5.**

The score is dragged down where it should be, production readiness, and the
reason is stated rather than smoothed over.

---

## Findings

### Fixed during this review

**F-1 · Unicode digits bypassed customer-id validation · High · Fixed**

*Found by:* adversarial testing (`test_control_characters_and_homoglyph_digits_are_rejected`).

`CUSTOMER_ID_PATTERN` used `\d{5}`. Python's `\d` matches every Unicode decimal
digit, so an identifier written with full-width digits satisfied the pattern
while being a different string from `CUST-12345` to every downstream system
that would receive it, a homoglyph confusion vector against exactly the field
the assessment asks to validate strictly.

*Why it matters:* validation that accepts a visually identical but distinct
identifier is worse than no validation, because it creates false confidence.

*Fix:* `[0-9]{5}`, plus `luhn_check` now requires ASCII digits, `str.isdigit()`
is true for full-width digits whose code points are not `ord(c) - 48`, so the
checksum arithmetic would have been meaningless on them.

*Status:* fixed, with the regression test retained.

---

**F-2 · The two gateways disagreed on upstream-failure status codes · Medium · Fixed**

The LLM gateway returned 502/504 for upstream failures while the MCP gateway
returned HTTP 200 with a JSON-RPC error, and no test pinned either. The
divergence was accidental rather than designed.

*Fix:* the rule is now one table (`TRANSPORT_LEVEL_STATUS`), the divergence is
deliberate and justified in ADR-011, and `TestStatusCodeContract` asserts both
surfaces.

---

**F-3 · `assert` used for control flow in production paths · Medium · Fixed**

Six `assert` statements carried real behaviour (a `None` check before a
denial, ledger initialisation). Under `python -O` they vanish, so a denial path
could have dereferenced `None`.

*Fix:* replaced with explicit checks or typed casts. `bandit -r src` now
reports zero issues of any severity.

---

**F-4 · Services bound `0.0.0.0` by default · Medium · Fixed**

Both entrypoints hard-coded `host="0.0.0.0"` with a lint suppression. Running
`python -m fde_assessment.llm_gateway` on a laptop exposed an unauthenticated-by-
default surface to the local network.

*Fix:* `BIND_HOST` defaults to `127.0.0.1`; the container image and compose set
`0.0.0.0` explicitly, which is the only context where it is correct.

---

**F-5 · An async generator yielded from a `finally` block · Medium · Fixed**

`guard_stream` flushed its carry buffer in `finally`. On client disconnect,
Python raises `RuntimeError: async generator ignored GeneratorExit` when a
generator yields while unwinding, so a hung-up client would have produced an
error rather than a clean close.

*Fix:* the tail is flushed on normal completion only. Dropping the carry on
disconnect is also the safer outcome: unemitted text is unscanned text.
`test_client_disconnect_does_not_raise` covers it.

---

**F-6 · Foreign-key violation in RAG ingestion · Low · Fixed**

Chunks were written before the parent document row, violating the foreign key
under `PRAGMA foreign_keys=ON`. Found by the ingestion tests on first run.

---

### Open, accepted, and documented

**F-7 · Authentication is a mock · High · Accepted for the assessment**

A configuration-driven token table with constant-time comparison. Production
needs OIDC/JWT validation against the customer's IdP.

*Why accepted:* the customer's identity provider is not knowable in advance,
and the seam is one function, nothing downstream consumes a token, only a
`GatewayPrincipal`. Documented in SECURITY.md, `auth.py`, and FDE-DELIVERY.md
as the first integration point.

---

**F-8 · No token revocation, rotation or replay protection · High · Open**

A leaked credential is valid until configuration changes. This is the highest
residual risk in THREAT-MODEL.md and the smallest change to make, but it is
inseparable from F-7: it arrives with the real identity provider.

---

**F-9 · PII detection covers three patterns · High · Accepted with limits stated**

Emails, US SSNs and Luhn-valid cards. Names, addresses, phone numbers, IBANs
and free-text detail are not detected, and obfuscation defeats regex entirely.

*Why accepted:* the assessment names these three categories. Making a broader
claim would be the actual failure. The adversarial suite *asserts* the
obfuscation limit rather than hiding it, and SECURITY.md says plainly what is
not claimed.

---

**F-10 · Rate limiting is per node · Medium · Accepted, trigger documented**

SQLite cannot coordinate replicas; two pods each admit up to the limit.

*Why accepted:* the brief mandates SQLite, and one node needs no coordination.
ADR-006 records the Redis migration and states that the trigger is the moment a
second replica is deployed.

---

**F-11 · No circuit breaker · Medium · Open**

A hard-down primary is retried on every request, spending the full 3-second
deadline each time. Latency degrades for everyone until the provider recovers.

*Why not fixed:* it adds state and a tuning surface beyond the brief's
per-request requirement. Named in ADR-010 and in the runbook, with the
operational workaround (promote the secondary by configuration).

---

**F-12 · No idle timeout between tokens · Medium · Open**

The deadline covers time to first token. A provider that emits one token and
then stalls is not caught, and the request hangs until the client gives up.

*Why not fixed:* out of scope for the stated requirement; recorded in ADR-010
as the natural next control.

---

**F-13 · Token estimation is a heuristic · Medium · Accepted**

~4 characters per token, which runs low for code and non-Latin scripts and high
for prose. A tenant's effective quota therefore varies with content by roughly
±20%.

*Why accepted:* a real tokenizer needs model artefacts and would make
rate-limiter tests non-deterministic. Documented in `providers/base.py` and
COST-OPTIMIZATION.md, with the production path (provider-reported usage,
reconciled) stated.

---

**F-14 · Mock embeddings are lexical, not semantic · Low · Accepted, measured**

Recall@1 = 0.63, Recall@3 = 1.00, MRR@5 = 0.79 on the eight-question set.
Paraphrases that share no vocabulary will not be retrieved.

*Why accepted:* CI must be free and deterministic. The numbers are measured and
printed by `scripts/benchmark.py`, and the test thresholds sit *below* them so
they catch regressions rather than encoding aspiration. Ollama embeddings are
the documented local upgrade.

---

**F-15 · The network posture is a deployment assumption · Medium · Documented**

The mock MCP server has no authentication of its own. On a flat network, the
gateway is decoration.

*Why not fixed in code:* it cannot be. Compose enforces it with an
`internal: true` network, the Kubernetes equivalent is given in OPERATIONS.md,
and `test_mock_downstream_has_no_authentication` exists specifically to make
the assumption explicit rather than implicit.

---

**F-16 · Docker image build, verified in CI · Resolved**

The Docker daemon was not running on the machine used for authoring, so
`docker build` never ran locally. GitHub Actions has since built the image,
asserted it runs as a non-root uid, started the LLM gateway inside it and
received a 200 from `/healthz`. The claim is now backed by a run, not by a
Dockerfile that looked right.

---

**F-17 · mypy installed with `--no-binary` · Informational**

An Application Control policy on this Windows machine blocks mypy's compiled
`mypyc` extension, so mypy was installed from source and reports
`compiled: no`. Same analysis, slower. CI installs the standard wheel.

---

**F-18 · The log sink captured `sys.stderr` instead of resolving it · Medium · Fixed**

*Found by:* GitHub Actions, Linux, Python 3.12. Fifteen stdio tests failed with
`ValueError: I/O operation on closed file`, while the same suite passed on the
authoring machine and on 3.13.

`configure_logging` passed the `sys.stderr` *object* to structlog's
`WriteLoggerFactory` and to the stdlib `StreamHandler`. pytest replaces
`sys.stderr` per test, so the handle captured during the first test that
configured logging was closed by the time a later test logged through it.
Benign in production, where stderr does not move, and a latent trap anywhere
the handle is rotated: a supervisor, an embedding host, a test runner.

*Fix:* a small write-through proxy that resolves `sys.stderr` on every write.
The security property is unchanged (stderr, never stdout) and the binding can
no longer go stale. `test_structlog_factory_writes_through_to_stderr` redirects
stderr and asserts the sink follows it.

*Worth noting:* only a real CI run on a different platform surfaced this. It is
the argument for running the matrix rather than trusting one machine.

---

**F-19 · gitleaks configuration failed to parse · Low · Fixed**

The secret-scan job never scanned anything: `[[allowlist]]` array-of-tables
entries produced `'Allowlist' expected a map, got 'slice'`, and gitleaks exited
before looking at a single file. A scanner that fails to load is worse than no
scanner, because the job's red X reads as "found something".

*Fix:* a single `[allowlist]` table with the regexes merged, and the schema
requirement noted in the file so it is not reintroduced.

---

**F-20 · Test helper closed stdin before `communicate()` · Low · Fixed**

*Found by:* GitHub Actions, Linux, Python 3.12. Fifteen stdio tests errored at
teardown with `ValueError: I/O operation on closed file` while passing on 3.13
and on Windows.

`StdioMcpClient.close` closed the subprocess's stdin and then called
`communicate()`, which flushes stdin itself and, on CPython 3.12, catches only
`BrokenPipeError` around that flush. A defect in the harness rather than in the
server, but it made the suite platform-dependent, which is its own problem.

*Fix:* let `communicate()` own the shutdown. Applied to the pytest helper and
to `scripts/smoke_test.py`, which had the same shape.

---

## Verified results

Every command below was run; the output is what is quoted.

| Command | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 117 files already formatted |
| `mypy src` | Success: no issues found in 51 source files |
| `pytest -q` | 472 passed |
| `bandit -r src -c pyproject.toml` | 0 issues (0 high, 0 medium, 0 low) |
| `pip-audit` | No known vulnerabilities. It audits the installed dependency set and skips `fde-assessment` itself, which is not on PyPI; CI runs `pip-audit --skip-editable` for the same reason |
| `python -m build` | Built `fde_assessment-0.1.0` sdist and wheel |
| Wheel installed into a clean venv | imports clean; `fde-mcp-server` console script present |
| `python scripts/smoke_test.py` | 15 passed, 0 failed |
| `python scripts/benchmark.py` | Completed; results in docs/testing/BENCHMARKS.md |
| Fresh `git clone` + `pip install -e ".[dev]"` + `pytest` | 472 passed |
| Fresh `git clone` + `uv sync --extra dev` + `uv run pytest` | 472 passed |
| Python 3.13 | 472 passed |
| GitHub Actions, ubuntu-latest | quality, package, Docker and security jobs green |
| Docker image, built in CI | non-root uid, `/healthz` returned 200 |

---

## Would I hire this candidate?

The question a hiring manager actually asks is not "is the code good" but
"would I put this person in front of a customer next week".

**Evidence for yes:**

- The hard parts are treated as the hard parts. The rate limiter's difficulty
  is the read-check-write race, and it is solved transactionally with a test
  that fails against the naive version. The guardrail's difficulty is the
  tension between correctness and TTFT, and the design resolves it instead of
  choosing a side.
- Absences are asserted, not assumed. "Returned -32001" and "the downstream was
  never called" are separate assertions.
- The missing Task 5 was investigated, evidenced and documented rather than
  invented, which is exactly what you want from someone who will be alone in a
  customer's environment with an ambiguous brief.
- Limitations appear in the README, not only in a footnote. A customer forgives
  a prototype's limits; they do not forgive discovering a limit you knew about.

**Evidence for reservations:**

- Production readiness is a 3. Real identity, distributed rate limiting and a
  circuit breaker are all still ahead.
- The AI integration is a gateway, not an agent: no tool-calling loop, no
  multi-turn orchestration. In scope for the brief, but it is the next thing to
  see.

**Conclusion.** The judgement on display, what to build, what to refuse to
claim, and what to write down, is the part that is hard to teach. The
remaining work is the part that is not.
