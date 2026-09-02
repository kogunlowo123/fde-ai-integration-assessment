# Task 5 was not supplied

## Finding

The assessment brief states that it consists of five tasks, but only four task
specifications are present in the document. **No Task 5 requirements were
provided, and none have been invented here.**

## Evidence from the supplied brief

Source: `FDE Assessment Questions - MCP & LLM Gateways.pdf` (85.4 KB, 3 pages).

The Overview says (page 1, verbatim):

> This assessment consists of 5 practical technical tasks designed to evaluate
> candidates on building MCP servers, implementing LLM & MCP security proxy
> gateways, handling stream guardrails, managing model routing resilience, and
> **troubleshooting zero-trust network deployments**.

The header block names the same five focus areas:

> **Core Focus Areas:** Model Context Protocol (MCP) Servers, MCP Gateways, LLM
> Gateways, Security Guardrails, & System Integration

The document then specifies exactly four tasks, each with a Problem Statement,
Requirements and Evaluation Criteria:

| Task | Title | Page |
|---|---|---|
| 1 | Build a Custom MCP Server with Strict Validation & Transport Handling | 1 |
| 2 | Implement an MCP Security Gateway Proxy (Tool Filtering & Auth) | 2 |
| 3 | Implement an LLM Gateway Streaming Guardrail (PII Redaction) | 2-3 |
| 4 | Build a Rate-Limiting & Model Fallback Router for LLM Gateways | 3 |

The document ends after Task 4's Evaluation Criteria. There is no Task 5
heading, no fifth Problem Statement, and no further Requirements section.

The one theme named in the Overview that no task body covers is
**"troubleshooting zero-trust network deployments"**, which suggests the
missing task concerned zero-trust deployment or network troubleshooting. That
is an inference from the Overview, not a requirement, and it has not been
treated as one.

## Where the search was performed

| Location | Result |
|---|---|
| `C:\Users\citad\OneDrive\Documents\Assessment task` (working directory) | Empty at the start of the engagement, no files of any kind |
| `FDE Assessment Questions - MCP & LLM Gateways.pdf` | The only supplied artefact; all 3 pages read in full |
| The PDF's text, including headings, footers and page breaks | Tasks 1-4 only |

No supplementary Markdown, text, PDF or hidden reference files were supplied
alongside the brief.

## Decision

Tasks 1-4 are implemented in full. Task 5 is **not** implemented, because
inventing requirements and then marking them complete would misrepresent both
the brief and the work.

## Related material already in this repository

Zero-trust content appears throughout as part of the *production enhancement*
scope, not as a claimed Task 5:

- [ARCHITECTURE.md](ARCHITECTURE.md), trust boundaries and the zero-trust
  request pipeline.
- [THREAT-MODEL.md](THREAT-MODEL.md), STRIDE analysis including the network
  posture the deployment depends on.
- [SECURITY.md](SECURITY.md), the controls that make every request
  authenticated, authorized, validated, bounded and audited.
- [docs/operations/RUNBOOK.md](docs/operations/RUNBOOK.md), diagnosing a
  gateway that is refusing traffic, including the network-path checks.

If the fifth task is supplied later, it can be implemented against the same
foundation; the gateways already carry the identity, policy and audit
machinery a zero-trust troubleshooting exercise would most likely build on.
