"""Shared request/response models and identity types.

WHAT
    Pydantic models used across more than one component: the OpenAI-shaped chat
    completion request, the identities produced by authentication, and the
    normalised stream event emitted by every provider.

WHY
    Providers, the router, the guardrail and the rate limiter must agree on one
    vocabulary. Modelling it once keeps provider-specific wire formats confined
    to the provider adapters.

WHEN
    Import from anywhere. These types carry no I/O and no configuration.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from fde_assessment.common.config import Role


class ChatMessage(BaseModel):
    """A single conversation turn."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(min_length=0, max_length=200_000)]


class RagOptions(BaseModel):
    """Retrieval options (Production Enhancement, not part of Tasks 1-4)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    query: Annotated[str, Field(min_length=1, max_length=2_000)] | None = None
    top_k: Annotated[int, Field(ge=1, le=100)] | None = None
    document_type: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    classification: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class ChatCompletionRequest(BaseModel):
    """``POST /v1/chat/completions`` request body.

    Deliberately a strict subset of the OpenAI shape: ``extra="forbid"`` means
    an unexpected field is a 422 rather than something silently forwarded to a
    provider.
    """

    model_config = ConfigDict(extra="forbid")

    model: Annotated[str, Field(min_length=1, max_length=128)]
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=200)]
    stream: bool = False
    max_tokens: Annotated[int, Field(ge=1, le=32_768)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.0
    # Production Enhancement: opt-in retrieval augmentation.
    rag: RagOptions | None = None

    @property
    def prompt_text(self) -> str:
        """Flattened prompt text, used for token estimation and length caps."""
        return "\n".join(m.content for m in self.messages)


class Usage(BaseModel):
    """Token accounting for one completion."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One normalised chunk from a provider stream.

    ``text`` is the incremental delta. ``done`` marks the terminal event, which
    carries no text but may carry final usage.
    """

    text: str = ""
    done: bool = False
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class GatewayPrincipal:
    """The authenticated caller of the MCP gateway (Task 2).

    ``token_fingerprint`` is a truncated keyed hash, safe to log; the raw token
    is never stored on the principal so it cannot be logged by accident.
    """

    subject: str
    role: Role
    token_fingerprint: str


@dataclass(frozen=True, slots=True)
class TenantPrincipal:
    """The authenticated tenant of the LLM gateway (Tasks 3 + 4)."""

    tenant_id: str
    api_key_hash: str


def fingerprint(secret: str, pepper: str, *, length: int = 16) -> str:
    """Return a truncated HMAC-SHA256 of ``secret``.

    HMAC with a deployment-specific pepper rather than a bare digest: bearer
    tokens and tenant API keys are low-entropy enough that an unsalted
    ``sha256`` of a leaked database column is trivially reversible with a
    dictionary. The pepper lives in configuration, not in the database, so
    stealing the table alone does not recover the keys.
    """
    digest = hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:length]
