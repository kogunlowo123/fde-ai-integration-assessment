"""Ollama provider, optional local inference.

WHAT
    Streams from a local Ollama daemon's ``/api/chat`` endpoint.

WHY
    It gives a real model in the loop, real tokenisation, real chunk
    boundaries, real latency, at zero marginal cost and with no data leaving
    the machine. That combination is what makes it the right default for
    developing against, and for a customer whose data cannot leave their
    perimeter during a pilot.

HOW
    NDJSON streaming over httpx. Each line is a JSON object whose
    ``message.content`` carries the delta and whose ``done`` flag terminates
    the stream.

WHEN
    Local runs only. **Never in CI**: the tests that use it are marked
    ``@pytest.mark.ollama`` and deselected by default, because a CI job that
    depends on a model daemon is a flaky job.

    Requires ``ollama pull qwen2.5:3b`` (or another small instruct model).

SECURITY
    The base URL is configuration, never request-derived. Response lines are
    length-capped, and a malformed line is a protocol error rather than an
    exception with vendor text in it.

COST
    Local compute only; no per-token billing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import (
    UpstreamProtocolError,
    UpstreamRateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent
from fde_assessment.llm_gateway.providers.base import LLMProvider, estimate_tokens

MAX_LINE_BYTES = 64 * 1024


class OllamaProvider(LLMProvider):
    """Streaming client for a local Ollama daemon."""

    def __init__(
        self,
        settings: Settings,
        name: str = "ollama",
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._settings = settings
        self._model = model or settings.ollama_model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(settings.secondary_timeout_s),
            follow_redirects=False,
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamEvent]:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens or self._settings.max_output_tokens,
            },
        }

        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code == 429:
                    raise UpstreamRateLimitedError(internal_detail="ollama returned 429")
                if response.status_code >= 400:
                    raise UpstreamUnavailableError(
                        internal_detail=f"ollama status {response.status_code}"
                    )

                total = 0
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    if len(line) > MAX_LINE_BYTES:
                        raise UpstreamProtocolError(internal_detail="ollama line exceeds cap")
                    try:
                        frame = json.loads(line)
                    except ValueError as exc:
                        raise UpstreamProtocolError(
                            internal_detail="ollama emitted a non-JSON line"
                        ) from exc
                    if not isinstance(frame, dict):
                        raise UpstreamProtocolError(internal_detail="ollama frame not an object")

                    delta = ""
                    message = frame.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            delta = content

                    if delta:
                        tokens = estimate_tokens(delta)
                        total += tokens
                        yield StreamEvent(text=delta, completion_tokens=tokens)

                    if frame.get("done") is True:
                        break

                yield StreamEvent(done=True, completion_tokens=0)

        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(internal_detail=type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(internal_detail=type(exc).__name__) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
