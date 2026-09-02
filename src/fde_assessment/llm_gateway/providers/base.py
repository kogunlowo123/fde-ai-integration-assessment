"""Provider abstraction.

WHAT
    ``LLMProvider``: the one interface the gateway knows about. Every concrete
    provider (mock, Ollama, an OpenAI-compatible endpoint) implements it.

WHY
    Provider independence is the core LLMOps property of a gateway. It is what
    makes it possible to fail over between vendors, to run CI at zero inference
    cost, to renegotiate a contract without a rewrite, and to satisfy a
    customer whose approved-model list differs from the one you developed
    against.

HOW
    A single async-generator method, ``stream``, yielding ``StreamEvent``.
    Non-streaming responses are the degenerate case (join the events), so
    there is one code path to test rather than two.

WHEN
    Add a provider by implementing this class; register it in
    ``providers/__init__.py``. Nothing else in the gateway changes.

SECURITY
    Providers translate vendor errors into the gateway's error vocabulary
    (``UpstreamRateLimitedError`` and friends). Raw vendor payloads never
    propagate past this layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from fde_assessment.common.models import ChatCompletionRequest, StreamEvent


class LLMProvider(ABC):
    """An upstream text-generation provider."""

    #: Stable identifier used in metrics, logs and routing configuration.
    name: str = "unnamed"

    @abstractmethod
    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        """Yield incremental events for ``request``.

        Implementations must:

        * be cancellation-safe, when the caller's timeout fires, the
          coroutine is cancelled and any socket must be released;
        * raise a ``GatewayError`` subclass rather than a vendor exception;
        * emit a final ``StreamEvent(done=True)``.
        """
        raise NotImplementedError

    async def complete(self, request: ChatCompletionRequest) -> str:
        """Collect a full response. Convenience for non-streaming callers."""
        parts: list[str] = []
        async for event in self.stream(request):
            if not event.done:
                parts.append(event.text)
        return "".join(parts)


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate.

    Uses the ~4-characters-per-token heuristic. It is an *estimate*: a real
    deployment would call the model's tokenizer (``tiktoken`` for OpenAI-style
    models, the Ollama ``/api/tokenize`` endpoint for local models) and
    reconcile against the provider's reported usage. The heuristic is used here
    because it needs no model artefacts, is identical across platforms, and
    keeps rate-limiter tests deterministic.

    Consequences, spelled out because they matter for a quota control:
    the estimate is low for code and non-Latin scripts and high for prose with
    long words, so a tenant's effective quota varies with content by roughly
    +/- 20%. Documented in COST-OPTIMIZATION.md.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
