"""Tasks 3 + 4, the LLM gateway.

WHAT
    ``POST /v1/chat/completions``: an OpenAI-shaped endpoint that
    authenticates the tenant, enforces a token budget, optionally augments the
    prompt with retrieved context, routes to a provider with fallback, and
    streams the answer back through the PII guardrail.

WHY
    Every control in the pipeline exists because the naive version, proxy
    the request, stream the bytes back, fails a real deployment in a
    specific way: no tenant isolation, no cost ceiling, no defence against the
    model emitting data it was given, and a vendor incident that becomes your
    outage.

HOW, the request pipeline
    ::

        bound body
          -> authenticate tenant
          -> validate request
          -> estimate tokens
          -> rate limit (fail closed)
          -> [optional] retrieve context
          -> route (primary, fallback on 429/timeout)
          -> guardrail (streaming redaction)
          -> SSE to client
          -> reconcile token accounting

    The order is load-bearing: authentication before parsing keeps the parser
    off an anonymous path, and rate limiting before routing means a
    tenant over budget never costs a provider call.

WHEN
    ``python -m fde_assessment.llm_gateway`` or ``docker compose up``.

SECURITY
    Errors leave through one function, ``_error_response``, which serialises
    the standardised envelope and nothing else. Prompts, completions and
    retrieved text are never logged.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from fde_assessment.common.config import Settings, get_settings
from fde_assessment.common.errors import (
    GatewayError,
    InvalidParamsError,
    ParseError,
    PayloadTooLargeError,
    RateLimitedError,
    to_gateway_error,
)
from fde_assessment.common.logging import configure_logging, get_logger
from fde_assessment.common.models import ChatCompletionRequest, StreamEvent, TenantPrincipal
from fde_assessment.llm_gateway.auth import authenticate_tenant
from fde_assessment.llm_gateway.guardrails.streaming import guard_stream
from fde_assessment.llm_gateway.providers import build_provider, estimate_tokens
from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter
from fde_assessment.llm_gateway.routing.router import ModelRouter, RouteOutcome
from fde_assessment.observability.metrics import (
    GATEWAY_LATENCY_MS,
    METRICS,
    REQUESTS_FAILED_TOTAL,
    REQUESTS_TOTAL,
    TOKENS_GENERATED,
    TOKENS_REQUESTED,
    Timer,
)
from fde_assessment.persistence.sqlite import Database

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    cleaned = "".join(c for c in supplied if c.isalnum() or c in "-_")[:64]
    return cleaned or f"llmgw-{uuid.uuid4().hex[:16]}"


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(internal_detail=f"content-length {declared} > {limit}")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(internal_detail="streamed body exceeded limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _chunk_frame(request_id: str, model: str, created: int, text: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _final_frame(request_id: str, model: str, created: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def create_app(
    settings: Settings | None = None,
    *,
    router: ModelRouter | None = None,
    limiter: TokenRateLimiter | None = None,
    database: Database | None = None,
    rag_service: Any | None = None,
) -> FastAPI:
    """Build the LLM gateway.

    Every collaborator is injectable so tests can drive real failure modes,
    a provider that returns 429, one that never emits a token, without
    monkeypatching.
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, fmt=resolved.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = database or Database(resolved.database_path, resolved.rate_limit_busy_timeout_ms)
        await db.initialize()

        app.state.settings = resolved
        app.state.database = db
        app.state.limiter = limiter or TokenRateLimiter(
            db,
            limit_tokens=resolved.rate_limit_tokens,
            window_seconds=resolved.rate_limit_window_seconds,
        )
        app.state.router = router or ModelRouter(
            primary=build_provider(
                resolved.primary_provider, resolved.primary_model, resolved.primary_model, resolved
            ),
            secondary=build_provider(
                resolved.secondary_provider,
                resolved.secondary_model,
                resolved.secondary_model,
                resolved,
            ),
            primary_timeout_s=resolved.primary_timeout_s,
            secondary_timeout_s=resolved.secondary_timeout_s,
        )
        app.state.rag = rag_service

        log.info(
            "llm_gateway_starting",
            primary=resolved.primary_model,
            secondary=resolved.secondary_model,
            rate_limit_tokens=resolved.rate_limit_tokens,
            window_seconds=resolved.rate_limit_window_seconds,
            rag_enabled=rag_service is not None,
        )
        try:
            yield
        finally:
            if database is None:
                await db.close()

    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        description="Streaming LLM gateway with PII guardrails, token rate limiting and fallback.",
        lifespan=lifespan,
    )
    api = APIRouter()

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/metrics")
    async def metrics() -> Response:
        return PlainTextResponse(METRICS.render_prometheus(), media_type="text/plain")

    @api.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        request_id = _request_id(request)
        timer = Timer(METRICS, GATEWAY_LATENCY_MS, surface="llm_gateway")
        timer.__enter__()

        try:
            raw = await _read_bounded_body(request, resolved.llm_gateway_max_body_bytes)
            principal = authenticate_tenant(
                request.headers.get("authorization"), request.headers.get("x-api-key"), resolved
            )

            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise ParseError(internal_detail="body was not valid JSON") from exc

            try:
                chat_request = ChatCompletionRequest.model_validate(payload)
            except ValidationError as exc:
                raise InvalidParamsError(
                    internal_detail=f"{exc.error_count()} field violations"
                ) from exc

            if len(chat_request.prompt_text) > resolved.max_prompt_chars:
                raise InvalidParamsError(internal_detail="prompt exceeds configured maximum")

            sources: list[dict[str, Any]] = []
            if chat_request.rag is not None and chat_request.rag.enabled:
                service = request.app.state.rag
                if service is None:
                    raise InvalidParamsError(internal_detail="retrieval is not enabled")
                chat_request, sources = await service.augment(
                    chat_request, principal.tenant_id, request_id
                )

            # Charge the worst case up front: a limiter that bills after
            # generation cannot prevent the burst it is meant to prevent.
            max_output = chat_request.max_tokens or resolved.max_output_tokens
            prompt_tokens = estimate_tokens(chat_request.prompt_text)
            estimated = prompt_tokens + max_output
            METRICS.increment(TOKENS_REQUESTED, estimated, tenant=principal.tenant_id)

            limiter_ref: TokenRateLimiter = request.app.state.limiter
            decision = await limiter_ref.check_and_consume(
                principal.tenant_id, principal.api_key_hash, estimated, request_id
            )
            if not decision.allowed:
                raise RateLimitedError(
                    internal_detail=f"used {decision.used_tokens}/{decision.limit}",
                    headers={
                        "retry-after": str(decision.retry_after_seconds),
                        "x-ratelimit-limit-tokens": str(decision.limit),
                        "x-ratelimit-remaining-tokens": str(decision.remaining),
                    },
                )

            model_router: ModelRouter = request.app.state.router
            outcome = RouteOutcome()
            guarded = guard_stream(
                model_router.stream(chat_request, outcome),
                window=resolved.pii_carry_buffer_chars,
            )

            if chat_request.stream:
                return StreamingResponse(
                    _sse_body(
                        guarded,
                        outcome=outcome,
                        request_id=request_id,
                        model=chat_request.model,
                        principal=principal,
                        limiter=limiter_ref,
                        estimated=estimated,
                        prompt_tokens=prompt_tokens,
                        timer=timer,
                        sources=sources,
                    ),
                    media_type="text/event-stream",
                    headers={
                        REQUEST_ID_HEADER: request_id,
                        "cache-control": "no-store",
                        "x-accel-buffering": "no",
                    },
                )

            body = await _collect(
                guarded,
                outcome=outcome,
                request_id=request_id,
                model=chat_request.model,
                principal=principal,
                limiter=limiter_ref,
                estimated=estimated,
                prompt_tokens=prompt_tokens,
                sources=sources,
            )
            timer.__exit__(None, None, None)
            METRICS.increment(REQUESTS_TOTAL, surface="llm_gateway", provider=outcome.provider_name)
            return JSONResponse(body, headers={REQUEST_ID_HEADER: request_id})

        except GatewayError as exc:
            timer.__exit__(None, None, None)
            return _error_response(exc, request_id)
        except Exception:  # pragma: no cover - defensive
            timer.__exit__(None, None, None)
            log.exception("llm_gateway_unhandled", request_id=request_id)
            return _error_response(GatewayError(), request_id)

    app.include_router(api)
    return app


async def _sse_body(
    guarded: AsyncIterator[StreamEvent],
    *,
    outcome: RouteOutcome,
    request_id: str,
    model: str,
    principal: TenantPrincipal,
    limiter: TokenRateLimiter,
    estimated: int,
    prompt_tokens: int,
    timer: Timer,
    sources: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """Server-sent events body.

    Errors raised after the response has started cannot change the status
    code, so they are delivered as a terminal SSE frame carrying the same
    standardised envelope a non-streaming caller would have received.
    """
    created = int(time.time())
    generated = 0
    try:
        if sources:
            yield _sse({"id": request_id, "object": "rag.sources", "sources": sources})
        async for event in guarded:
            if event.done:
                generated = event.completion_tokens
                continue
            yield _sse(_chunk_frame(request_id, model, created, event.text))
        yield _sse(_final_frame(request_id, model, created))
        yield "data: [DONE]\n\n"
        METRICS.increment(REQUESTS_TOTAL, surface="llm_gateway", provider=outcome.provider_name)
    except GatewayError as exc:
        METRICS.increment(REQUESTS_FAILED_TOTAL, surface="llm_gateway", code=exc.code)
        log.warning("llm_gateway_stream_failed", request_id=request_id, code=exc.code)
        yield _sse(to_gateway_error(exc, request_id))
        yield "data: [DONE]\n\n"
    except Exception:  # pragma: no cover - defensive
        log.exception("llm_gateway_stream_unhandled", request_id=request_id)
        yield _sse(to_gateway_error(GatewayError(), request_id))
        yield "data: [DONE]\n\n"
    finally:
        timer.__exit__(None, None, None)
        await _reconcile(limiter, principal, prompt_tokens, generated, estimated, request_id)
        METRICS.increment(TOKENS_GENERATED, generated, tenant=principal.tenant_id)
        log.info(
            "llm_gateway_completed",
            request_id=request_id,
            tenant=principal.tenant_id,
            provider=outcome.provider_name,
            fell_back=outcome.fell_back,
            ttft_ms=round(outcome.ttft_ms, 3),
            latency_ms=round(timer.elapsed_ms, 3),
            estimated_tokens=estimated,
            generated_tokens=generated,
            streamed=True,
        )


async def _collect(
    guarded: AsyncIterator[StreamEvent],
    *,
    outcome: RouteOutcome,
    request_id: str,
    model: str,
    principal: TenantPrincipal,
    limiter: TokenRateLimiter,
    estimated: int,
    prompt_tokens: int,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Non-streaming response. Same pipeline, joined at the end."""
    parts: list[str] = []
    generated = 0
    async for event in guarded:
        if event.done:
            generated = event.completion_tokens
            continue
        parts.append(event.text)

    await _reconcile(limiter, principal, prompt_tokens, generated, estimated, request_id)
    METRICS.increment(TOKENS_GENERATED, generated, tenant=principal.tenant_id)
    log.info(
        "llm_gateway_completed",
        request_id=request_id,
        tenant=principal.tenant_id,
        provider=outcome.provider_name,
        fell_back=outcome.fell_back,
        ttft_ms=round(outcome.ttft_ms, 3),
        estimated_tokens=estimated,
        generated_tokens=generated,
        streamed=False,
    )

    body: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(parts)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": generated,
            "total_tokens": prompt_tokens + generated,
        },
        "x_gateway": {
            "provider": outcome.provider_name,
            "fell_back": outcome.fell_back,
            "request_id": request_id,
        },
    }
    if sources:
        body["sources"] = sources
    return body


async def _reconcile(
    limiter: TokenRateLimiter,
    principal: TenantPrincipal,
    prompt_tokens: int,
    generated: int,
    estimated: int,
    request_id: str,
) -> None:
    """Correct the up-front estimate once the real usage is known."""
    actual = prompt_tokens + generated
    delta = actual - estimated
    if delta:
        await limiter.reconcile(principal.tenant_id, principal.api_key_hash, delta, request_id)


def _error_response(exc: GatewayError, request_id: str) -> JSONResponse:
    METRICS.increment(REQUESTS_FAILED_TOTAL, surface="llm_gateway", code=exc.code)
    log.warning(
        "llm_gateway_error",
        request_id=request_id,
        code=exc.code,
        detail=exc.internal_detail,
    )
    headers = {REQUEST_ID_HEADER: request_id, **exc.headers}
    return JSONResponse(
        to_gateway_error(exc, request_id), status_code=exc.http_status, headers=headers
    )
