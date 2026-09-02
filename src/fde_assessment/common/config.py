"""Application configuration.

WHAT
    A single Pydantic ``Settings`` object validated at process startup and
    injected into every component. No module reads ``os.environ`` directly.

WHY
    Configuration is a security boundary. Validating it once, at startup, turns
    a class of runtime failures (missing downstream URL, empty token map,
    nonsensical timeout) into a loud crash before the first request is served,
    and gives every component a typed contract instead of stringly-typed
    environment lookups.

HOW
    ``pydantic-settings`` reads ``.env`` then the process environment.
    ``get_settings()`` memoizes one instance; tests build their own via
    ``Settings(...)``.

WHEN
    Import ``get_settings`` from application entrypoints only; pass the
    resulting object down explicitly (dependency injection) so tests can
    substitute a configuration without mutating global state.

SECURITY
    Refuses to boot in ``production`` while the published development
    credentials are still configured (fail closed).

COST
    Caps output tokens, prompt size, retrieval breadth and context size,
    every knob that multiplies spend is bounded here rather than per call site.

SCALE
    The same object is the seam for a future secret manager: replace the
    ``SecretStr`` sources without touching any consumer.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Credentials shipped for local development and deterministic tests. They are
# NOT secrets: they appear in .env.example and the README. Production
# deployments must override them; `_reject_dev_defaults` enforces that.
DEV_MCP_TOKENS = "dev-admin-token:admin,dev-viewer-token:viewer"
DEV_LLM_TENANTS = "dev-tenant-a-key:tenant-a,dev-tenant-b-key:tenant-b"
DEV_API_KEY_PEPPER = "dev-only-pepper-not-a-secret"


class AppEnv(StrEnum):
    """Deployment environment. Controls how strict startup validation is."""

    development = "development"
    test = "test"
    production = "production"


class Role(StrEnum):
    """Roles the MCP gateway understands. Deliberately tiny and closed."""

    admin = "admin"
    viewer = "viewer"


def parse_pairs(raw: str) -> dict[str, str]:
    """Parse ``a:b,c:d`` into ``{"a": "b", "c": "d"}``.

    Chosen over JSON because it survives shell quoting, Docker ``environment:``
    blocks and CI secret injection without escaping games.
    """
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        key, sep, value = entry.partition(":")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"malformed key:value entry {entry!r}")
        pairs[key.strip()] = value.strip()
    if not pairs:
        raise ValueError("no key:value entries parsed")
    return pairs


class Settings(BaseSettings):
    """Validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnv = AppEnv.development
    # Loopback by default: a service that binds every interface the moment
    # someone runs it locally is how internal APIs end up on the network.
    # The container image sets BIND_HOST=0.0.0.0 explicitly.
    bind_host: str = "127.0.0.1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # , Service ports -----------------------------------------------------
    mcp_gateway_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    llm_gateway_port: Annotated[int, Field(ge=1, le=65535)] = 8001

    # , Persistence -------------------------------------------------------
    # Task 4 requires on-disk SQLite.
    database_path: Path = Path("./data/gateway.db")

    # , MCP gateway (Task 2) ----------------------------------------------
    mcp_downstream_url: str = "http://127.0.0.1:9000/rpc"
    mcp_downstream_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 5_000
    mcp_gateway_max_body_bytes: Annotated[int, Field(ge=1_024, le=10_485_760)] = 262_144
    mcp_gateway_tokens: str = DEV_MCP_TOKENS
    admin_tool_prefix: str = "admin_"

    # , LLM gateway (Tasks 3 + 4) -----------------------------------------
    llm_gateway_tenants: str = DEV_LLM_TENANTS
    api_key_pepper: SecretStr = SecretStr(DEV_API_KEY_PEPPER)
    llm_gateway_max_body_bytes: Annotated[int, Field(ge=1_024, le=10_485_760)] = 1_048_576

    primary_model: str = "mock-primary"
    secondary_model: str = "mock-secondary"
    primary_provider: Literal["mock", "ollama"] = "mock"
    secondary_provider: Literal["mock", "ollama"] = "mock"
    primary_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 3_000
    secondary_timeout_ms: Annotated[int, Field(ge=100, le=120_000)] = 10_000

    max_output_tokens: Annotated[int, Field(ge=1, le=32_768)] = 1_024
    max_prompt_chars: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000

    # , Rate limiting (Task 4) --------------------------------------------
    rate_limit_tokens: Annotated[int, Field(ge=1)] = 50_000
    rate_limit_window_seconds: Annotated[int, Field(ge=1, le=86_400)] = 60
    rate_limit_busy_timeout_ms: Annotated[int, Field(ge=100, le=60_000)] = 5_000

    # , Streaming guardrail (Task 3) --------------------------------------
    # Look-behind window carried between chunks; bounds memory and added
    # latency per stream. Sizing argument: docs/decisions/ADR-005.
    pii_carry_buffer_chars: Annotated[int, Field(ge=32, le=4_096)] = 128

    # , Ollama (optional, local only) -------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_embedding_model: str = "nomic-embed-text"

    # , RAG (Production Enhancement) --------------------------------------
    rag_chunk_size: Annotated[int, Field(ge=64, le=8_192)] = 512
    rag_chunk_overlap: Annotated[int, Field(ge=0, le=4_096)] = 64
    rag_max_top_k: Annotated[int, Field(ge=1, le=100)] = 10
    rag_default_top_k: Annotated[int, Field(ge=1, le=100)] = 4
    rag_max_context_chars: Annotated[int, Field(ge=256, le=200_000)] = 6_000
    rag_embedding_dim: Annotated[int, Field(ge=8, le=4_096)] = 256
    rag_embedding_provider: Literal["mock", "ollama"] = "mock"

    @field_validator("mcp_gateway_tokens", "llm_gateway_tenants")
    @classmethod
    def _validate_pairs(cls, value: str) -> str:
        parse_pairs(value)
        return value

    @field_validator("mcp_downstream_url", "ollama_base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _check_ranges(self) -> Settings:
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("rag_chunk_overlap must be smaller than rag_chunk_size")
        if self.rag_default_top_k > self.rag_max_top_k:
            raise ValueError("rag_default_top_k must not exceed rag_max_top_k")
        return self

    @model_validator(mode="after")
    def _reject_dev_defaults(self) -> Settings:
        """Fail closed: never serve production traffic with published creds."""
        if self.app_env is not AppEnv.production:
            return self
        offenders = [
            name
            for name, default in (
                ("MCP_GATEWAY_TOKENS", DEV_MCP_TOKENS),
                ("LLM_GATEWAY_TENANTS", DEV_LLM_TENANTS),
            )
            if getattr(self, name.lower()) == default
        ]
        if self.api_key_pepper.get_secret_value() == DEV_API_KEY_PEPPER:
            offenders.append("API_KEY_PEPPER")
        if offenders:
            raise ValueError(
                "refusing to start in production with development credentials still set: "
                + ", ".join(sorted(offenders))
            )
        return self

    # , Derived views ------------------------------------------------------

    @property
    def token_roles(self) -> Mapping[str, Role]:
        """``{bearer token: role}`` for the MCP gateway's mock authenticator."""
        return {token: Role(role) for token, role in parse_pairs(self.mcp_gateway_tokens).items()}

    @property
    def tenant_keys(self) -> Mapping[str, str]:
        """``{tenant API key: tenant id}`` for the LLM gateway."""
        return parse_pairs(self.llm_gateway_tenants)

    @property
    def primary_timeout_s(self) -> float:
        return self.primary_timeout_ms / 1000.0

    @property
    def secondary_timeout_s(self) -> float:
        return self.secondary_timeout_ms / 1000.0

    @property
    def mcp_downstream_timeout_s(self) -> float:
        return self.mcp_downstream_timeout_ms / 1000.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructing them on first use.

    A configuration error is fatal by design: the process must not serve
    traffic with a half-valid security configuration. The message goes to
    stderr so it can never contaminate an MCP stdio session.
    """
    try:
        return Settings()
    except Exception as exc:  # pragma: no cover - exercised manually
        sys.stderr.write(f"FATAL: invalid configuration: {exc}\n")
        raise
