"""FDE / AI Integration Engineer assessment implementation.

Package layout mirrors the assessment tasks:

* ``mcp_server``   -- Task 1: MCP server over stdio with strict validation.
* ``mcp_gateway``  -- Task 2: MCP security gateway (auth + tool authorization).
* ``llm_gateway``  -- Task 3 + 4: streaming PII guardrails, token-aware rate
  limiting, and resilient model routing with fallback.
* ``rag``          -- Production Enhancement: retrieval-augmented generation.
* ``common``       -- configuration, error taxonomy, logging, shared models.
* ``persistence``  -- on-disk SQLite access layer.
* ``observability``-- in-process metrics registry.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
