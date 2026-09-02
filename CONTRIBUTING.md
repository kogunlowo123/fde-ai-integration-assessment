# Contributing

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Or with `uv` (`uv.lock` is committed, so runtime dependencies resolve
identically):

```bash
uv sync --extra dev
```

Python 3.12 or 3.13. Both are exercised in CI.

## Before opening a pull request

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
bandit -r src -c pyproject.toml
pip-audit
python scripts/gen_env_example.py --check
python scripts/smoke_test.py
```

CI runs the same commands, plus a Docker build, a wheel install into a clean
environment, and a secret scan.

## Standards

**Typing.** `mypy --strict` over `src/`. No untyped definitions, no implicit
`Any`. Tests are not type-checked, so they stay readable.

**Documentation lives in the code.** Every module opens with WHAT / WHY / HOW /
WHEN, and security-relevant modules add SECURITY / COST / SCALE. Explain the
*why*, a reader can see the what.

**Comments earn their place.** Comment the non-obvious decision, not the
obvious line. `# increment the counter` is noise; `# full scan with
constant-time comparison: an early return would leak table position through
timing` is the reason the code looks strange.

**Async discipline.** No blocking I/O on a request path. Filesystem work in an
async function goes through `asyncio.to_thread`; ruff's `ASYNC` rules enforce
it.

**No `print` in `src/`.** ruff's `T20` rule fails the build. stdout belongs to
the MCP transport; diagnostics go to stderr via `common/logging.py`.

**No new dependency without a reason in the pull request.** Every dependency is
CVE surface, install time and a supply-chain relationship.

## Testing

New behaviour needs a test that would fail without it. Test names state the
property, not the mechanism: `test_downstream_is_never_invoked_for_a_denied_call`
says what must be true.

| Suite | Contains |
|---|---|
| `tests/unit` | Pure logic: validation, policy, PII patterns, limiter arithmetic |
| `tests/integration` | Real subprocesses and ASGI apps |
| `tests/security` | Authentication, authorization, isolation, leakage |
| `tests/streaming` | Chunk-boundary behaviour |
| `tests/concurrency` | The rate limiter under contention |
| `tests/e2e` | Client → gateway → provider → guardrail → client |
| `tests/rag` | Ingestion, chunking, retrieval quality, the MCP knowledge tool |

For security-relevant behaviour, assert the *absence* of the bad outcome as
well as the presence of the good one, "returned -32001" and "downstream call
count is zero" are two different facts.

Tests must not require the network, an API key, or a GPU. A test needing a
local model daemon is marked `@pytest.mark.ollama` and excluded by default.

## Configuration changes

Add the field to `Settings`, then regenerate the template:

```bash
python scripts/gen_env_example.py
```

`tests/unit/test_config.py` fails if `.env.example` and `Settings` disagree, `extra="ignore"` means a stale variable would otherwise be silently inert.

## Architecture decisions

A change to how a component works, what it depends on, or what it guarantees
needs an ADR in `docs/decisions/`: Context, Decision, Alternatives considered,
Consequences, Security impact, Cost impact, Operational impact. The point is
the alternatives, a decision without them is an assertion.

## Commits

Conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`).
The body explains why. Never commit `.env`, credentials, database files, or
build artefacts; `.gitignore` covers them and gitleaks checks.

## Dependency policy

Runtime dependencies are floors (`>=`) and pinned by `uv.lock`. Development
tools on purpose float, so a lint or type-checker release is caught here
rather than in someone's editor months later, the trade-off is that CI can go
red without a code change. If that becomes disruptive, pin the dev extras and
bump them on a schedule.
