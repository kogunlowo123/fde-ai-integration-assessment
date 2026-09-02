"""LLM provider adapters and the factory that selects one from configuration."""

from __future__ import annotations

from fde_assessment.common.config import Settings
from fde_assessment.llm_gateway.providers.base import LLMProvider, estimate_tokens
from fde_assessment.llm_gateway.providers.mock import (
    HangingProvider,
    MockProvider,
    ScriptedFailureProvider,
)
from fde_assessment.llm_gateway.providers.ollama import OllamaProvider

__all__ = [
    "HangingProvider",
    "LLMProvider",
    "MockProvider",
    "OllamaProvider",
    "ScriptedFailureProvider",
    "build_provider",
    "estimate_tokens",
]


def build_provider(kind: str, name: str, model: str, settings: Settings) -> LLMProvider:
    """Construct a provider from configuration.

    Deliberately explicit rather than plugin-discovered: a gateway that can be
    pointed at an arbitrary provider by configuration alone is an exfiltration
    channel. Adding a provider is a code change and therefore a review.
    """
    if kind == "mock":
        return MockProvider(name=name)
    if kind == "ollama":
        return OllamaProvider(settings, name=name, model=model)
    raise ValueError(f"unsupported provider kind: {kind!r}")
