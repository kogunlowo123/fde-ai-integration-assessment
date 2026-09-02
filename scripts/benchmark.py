#!/usr/bin/env python
"""Measure local performance. Every number printed here was measured.

    python scripts/benchmark.py
    python scripts/benchmark.py --json benchmark-results/local.json

WHAT IS MEASURED
    MCP tool dispatch, MCP gateway overhead, PII guardrail throughput and
    added latency, rate-limiter admission latency (sequential and contended),
    gateway time-to-first-token, fallback cost, and RAG retrieval quality and
    latency.

HONESTY NOTES
    * All measurements use the mock provider, so provider latency is excluded
      by design, what is measured is the *gateway's own* cost, which is the
      number a deployment decision actually depends on.
    * Single machine, single process, warm cache, no network. These are not
      capacity figures for a production deployment; they are the cost of the
      code in this repository.
    * Timings come from ``time.perf_counter`` around whole operations,
      including the async event-loop overhead a real request would also pay.
    * The environment block is emitted with the results so a number can never
      be quoted without the machine it came from.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fde_assessment.common.config import AppEnv, Settings  # noqa: E402
from fde_assessment.common.errors import (  # noqa: E402
    InvalidParamsError,
    UpstreamRateLimitedError,
)
from fde_assessment.common.logging import configure_logging  # noqa: E402
from fde_assessment.common.models import ChatCompletionRequest  # noqa: E402
from fde_assessment.llm_gateway.guardrails.streaming import StreamingRedactor  # noqa: E402
from fde_assessment.llm_gateway.providers.mock import (  # noqa: E402
    MockProvider,
    ScriptedFailureProvider,
)
from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter  # noqa: E402
from fde_assessment.llm_gateway.routing.router import ModelRouter, RouteOutcome  # noqa: E402
from fde_assessment.mcp_gateway.app import create_app as create_mcp_gateway  # noqa: E402
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient  # noqa: E402
from fde_assessment.mcp_server.http_mock import build_mock_downstream_app  # noqa: E402
from fde_assessment.mcp_server.registry import ServerDeps, build_dispatcher  # noqa: E402
from fde_assessment.persistence.sqlite import Database  # noqa: E402
from fde_assessment.rag.embeddings import MockEmbeddingProvider  # noqa: E402
from fde_assessment.rag.service import build_rag_service  # noqa: E402

CORPUS = REPO_ROOT / "corpus"
TENANT = "tenant-a"

PROSE = (
    "The account was reviewed on Tuesday and the balance is correct. "
    "No further action is required at this time. "
)
PII_TEXT = (
    "Contact john.smith@example.com, identifier 123-45-6789, "
    "card 4111 1111 1111 1111, and that is everything. "
)

EVAL_SET: list[tuple[str, str]] = [
    ("How long do I have to request a refund?", "refund-policy"),
    ("Can a suspended account get a refund?", "refund-policy"),
    ("When is express shipping dispatched?", "shipping-policy"),
    ("What happens if my shipment is lost?", "shipping-policy"),
    ("How often are API keys rotated?", "account-security"),
    ("How many failed sign-in attempts suspend an account?", "account-security"),
    ("How long are billing records kept?", "data-retention"),
    ("How long are support transcripts retained?", "data-retention"),
]


def summarise(samples: Iterable[float]) -> dict[str, float]:
    values = sorted(samples)
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 4),
        "p50_ms": round(statistics.median(values), 4),
        "p95_ms": round(values[max(0, int(len(values) * 0.95) - 1)], 4),
        "max_ms": round(values[-1], 4),
    }


def time_sync(operation: Callable[[], Any], iterations: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000.0)
    return summarise(samples)


async def time_async(operation: Callable[[], Any], iterations: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        await operation()
        samples.append((time.perf_counter() - start) * 1000.0)
    return summarise(samples)


def environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "MockProvider (no network, no model inference)",
        "note": "single process, warm cache, no network; gateway cost only",
    }


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


async def bench_mcp_dispatch() -> dict[str, Any]:
    dispatcher = build_dispatcher(ServerDeps())

    async def lookup() -> None:
        await dispatcher.call("get_customer_record", {"customer_id": "CUST-12345"})

    async def rejected() -> None:
        # The rejection is the measurement: this is the cost of refusing a
        # malformed tool call, which should be cheaper than serving a good one.
        with suppress(InvalidParamsError):
            await dispatcher.call("get_customer_record", {"customer_id": "nope"})

    return {
        "valid_tool_call": await time_async(lookup, 2_000),
        "rejected_tool_call": await time_async(rejected, 2_000),
    }


def bench_mcp_gateway(settings: Settings) -> dict[str, Any]:
    tuned = settings.model_copy(update={"mcp_downstream_url": "http://downstream/rpc"})
    mock_app = build_mock_downstream_app()
    downstream = DownstreamMcpClient(
        tuned,
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app), base_url="http://downstream"
        ),
    )
    app = create_mcp_gateway(tuned, downstream=downstream)

    forwarded = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}},
    }
    denied = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "admin_reset_key", "arguments": {}},
    }

    with TestClient(app) as client:
        viewer = {"authorization": "Bearer dev-viewer-token"}
        return {
            "forwarded_call": time_sync(
                lambda: client.post("/rpc", json=forwarded, headers=viewer), 300
            ),
            "denied_call_short_circuit": time_sync(
                lambda: client.post("/rpc", json=denied, headers=viewer), 300
            ),
        }


def bench_guardrail() -> dict[str, Any]:
    def run(text: str, chunk: int) -> Callable[[], None]:
        chunks = [text[i : i + chunk] for i in range(0, len(text), chunk)]

        def operation() -> None:
            redactor = StreamingRedactor()
            for piece in chunks:
                redactor.process(piece)
            redactor.flush()

        return operation

    prose = PROSE * 40  # ~4 KB
    pii = PII_TEXT * 40

    prose_stats = time_sync(run(prose, 8), 500)
    pii_stats = time_sync(run(pii, 8), 500)

    # Added latency for the first emitted token: prose that cannot start a
    # match is released immediately, which is the whole point of the design.
    def first_chunk_prose() -> None:
        StreamingRedactor().process("The answer is ")

    def first_chunk_pii() -> None:
        StreamingRedactor().process("Mail john.smith@")

    return {
        "prose_4kb_stream": {
            **prose_stats,
            "kb_per_ms": round(len(prose) / 1024 / prose_stats["mean_ms"], 3),
        },
        "pii_heavy_4kb_stream": {
            **pii_stats,
            "kb_per_ms": round(len(pii) / 1024 / pii_stats["mean_ms"], 3),
        },
        "single_chunk_prose": time_sync(first_chunk_prose, 5_000),
        "single_chunk_pii_prefix": time_sync(first_chunk_pii, 5_000),
    }


async def bench_rate_limiter(tmp: Path) -> dict[str, Any]:
    database = Database(tmp / "bench-rate.db")
    await database.initialize()
    try:
        limiter = TokenRateLimiter(database, limit_tokens=10_000_000, window_seconds=60)
        counter = {"n": 0}

        async def admit() -> None:
            counter["n"] += 1
            await limiter.check_and_consume("bench", "hash-bench", 10, f"r{counter['n']}")

        sequential = await time_async(admit, 500)

        start = time.perf_counter()
        await asyncio.gather(
            *[limiter.check_and_consume("bench", "hash-bench", 10, f"c{i}") for i in range(200)]
        )
        contended_total_ms = (time.perf_counter() - start) * 1000.0

        return {
            "sequential_admission": sequential,
            "contended_200_concurrent": {
                "total_ms": round(contended_total_ms, 3),
                "per_request_ms": round(contended_total_ms / 200, 4),
            },
        }
    finally:
        await database.close()


async def bench_routing() -> dict[str, Any]:
    request = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    )
    healthy = ModelRouter(MockProvider("primary"), MockProvider("secondary"))
    failing = ModelRouter(
        ScriptedFailureProvider(UpstreamRateLimitedError("429"), name="primary"),
        MockProvider("secondary"),
    )

    async def ttft(router: ModelRouter) -> float:
        outcome = RouteOutcome()
        stream = router.stream(request, outcome)
        await anext(stream)
        await stream.aclose()
        return outcome.ttft_ms

    direct = [await ttft(healthy) for _ in range(300)]
    failover = [await ttft(failing) for _ in range(300)]

    return {
        "time_to_first_token_primary": summarise(direct),
        "time_to_first_token_after_429_failover": summarise(failover),
        "failover_overhead_mean_ms": round(
            statistics.fmean(failover) - statistics.fmean(direct), 4
        ),
    }


async def bench_rag(tmp: Path) -> dict[str, Any]:
    database = Database(tmp / "bench-rag.db")
    await database.initialize()
    try:
        service = await build_rag_service(
            Settings(app_env=AppEnv.test, database_path=tmp / "bench-rag.db"),
            database,
            MockEmbeddingProvider(dim=256),
        )

        start = time.perf_counter()
        report = await service.ingestion_pipeline.ingest_directory(CORPUS, TENANT)
        ingest_ms = (time.perf_counter() - start) * 1000.0

        start = time.perf_counter()
        reingest = await service.ingestion_pipeline.ingest_directory(CORPUS, TENANT)
        reingest_ms = (time.perf_counter() - start) * 1000.0

        async def search() -> None:
            await service.search(TENANT, "How long do I have to request a refund?", top_k=4)

        latency = await time_async(search, 300)

        # Retrieval quality, measured on the same evaluation set the tests use.
        quality: dict[str, float] = {}
        for k in (1, 3, 5):
            hits = 0
            for question, expected in EVAL_SET:
                results = await service.search(TENANT, question, top_k=k)
                if any(hit.chunk.document_id == expected for hit in results):
                    hits += 1
            quality[f"recall_at_{k}"] = round(hits / len(EVAL_SET), 4)

        reciprocal = 0.0
        for question, expected in EVAL_SET:
            for rank, hit in enumerate(await service.search(TENANT, question, top_k=5), start=1):
                if hit.chunk.document_id == expected:
                    reciprocal += 1.0 / rank
                    break
        quality["mrr_at_5"] = round(reciprocal / len(EVAL_SET), 4)

        return {
            "corpus": {
                "documents": report.documents_seen,
                "chunks": report.chunks_written,
                "embedder": "MockEmbeddingProvider(dim=256), lexical hashing",
            },
            "ingest_ms": round(ingest_ms, 3),
            "reingest_unchanged_ms": round(reingest_ms, 3),
            "reingest_skipped": reingest.documents_skipped_unchanged,
            "search_latency": latency,
            "quality_eval_set_size": len(EVAL_SET),
            "quality": quality,
        }
    finally:
        await database.close()


async def run_all() -> dict[str, Any]:
    # Benchmarks are noisy enough without per-call logs competing for the
    # same CPU; measure the code, not the logger.
    configure_logging(level="ERROR", fmt="json")
    with tempfile.TemporaryDirectory(prefix="fde-bench-") as tmp_name:
        tmp = Path(tmp_name)
        settings = Settings(app_env=AppEnv.test, log_level="ERROR", database_path=tmp / "bench.db")
        results = {
            "environment": environment(),
            "mcp_tool_dispatch": await bench_mcp_dispatch(),
            "mcp_gateway": bench_mcp_gateway(settings),
            "pii_guardrail": bench_guardrail(),
            "rate_limiter": await bench_rate_limiter(tmp),
            "model_routing": await bench_routing(),
            "rag": await bench_rag(tmp),
        }
    return results


def render(results: dict[str, Any]) -> str:
    lines: list[str] = ["FDE assessment benchmark", "=" * 64, ""]
    env = results["environment"]
    lines.append(
        f"Python {env['python']} ({env['implementation']}) on {env['os']} / "
        f"{env['machine']}, {env['cpu_count']} logical CPUs"
    )
    lines.append(f"Provider: {env['provider']}")
    lines.append(f"Measured: {env['timestamp_utc']}")
    lines.append("")

    def table(title: str, rows: dict[str, Any]) -> None:
        lines.append(title)
        lines.append("-" * len(title))
        for name, stats in rows.items():
            if isinstance(stats, dict) and "mean_ms" in stats:
                lines.append(
                    f"  {name:<38} n={stats['n']:<6} mean={stats['mean_ms']:>9.4f} ms  "
                    f"p95={stats['p95_ms']:>9.4f} ms"
                )
            else:
                lines.append(f"  {name:<38} {stats}")
        lines.append("")

    table("MCP tool dispatch (in-process)", results["mcp_tool_dispatch"])
    table("MCP security gateway (ASGI, in-process downstream)", results["mcp_gateway"])
    table("Streaming PII guardrail", results["pii_guardrail"])
    table("Token rate limiter (on-disk SQLite)", results["rate_limiter"])
    table("Model routing", results["model_routing"])
    table("RAG", results["rag"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="also write raw results to this path")
    args = parser.parse_args()

    results = asyncio.run(run_all())
    sys.stdout.write(render(results) + "\n")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        sys.stdout.write(f"raw results written to {args.json}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
