"""In-process metrics registry.

WHAT
    Counters and latency histograms with a Prometheus-compatible text
    rendering, plus a ``Timer`` context manager.

WHY
    LLMOps needs the four questions answered at all times: how much traffic,
    how much of it failed, how slow was it, and how much did it cost. A tiny
    dependency-free registry keeps that observable in the assessment without
    pulling in a metrics backend, while exposing exactly the surface a real
    ``prometheus_client`` swap-in would need.

HOW
    Label sets are flattened into a sorted key. Latency is stored as fixed
    buckets plus sum/count so p-quantiles can be approximated without
    unbounded memory.

WHEN
    Increment from request paths. Never put a high-cardinality value (a raw API
    key, a customer id, a document id) in a label, the registry would grow
    without bound and the labels would themselves become a PII leak.

SECURITY
    Label values are restricted by convention to enumerations (provider name,
    outcome, tool name) and tenant identifiers, never secrets or free text.

SCALE
    Replace with ``prometheus_client`` (or an OTLP exporter) by reimplementing
    ``MetricsRegistry``; call sites do not change.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType

# Buckets chosen for gateway-scale latencies: sub-millisecond guardrail work up
# to the 3 s primary-provider timeout and beyond.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    0.5,
    1,
    2,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1_000,
    3_000,
    10_000,
)


def _key(name: str, labels: Mapping[str, str]) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


@dataclass
class Histogram:
    """Fixed-bucket latency histogram in milliseconds."""

    buckets: tuple[float, ...] = DEFAULT_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    n: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value_ms: float) -> None:
        self.total += value_ms
        self.n += 1
        for i, edge in enumerate(self.buckets):
            if value_ms <= edge:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    @property
    def mean_ms(self) -> float:
        return self.total / self.n if self.n else 0.0

    def quantile(self, q: float) -> float:
        """Approximate quantile: the upper edge of the bucket containing it."""
        if self.n == 0:
            return 0.0
        target = q * self.n
        seen = 0
        for i, count in enumerate(self.counts):
            seen += count
            if seen >= target:
                return self.buckets[i] if i < len(self.buckets) else float("inf")
        return float("inf")


class MetricsRegistry:
    """Thread-safe counters and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._histograms: dict[str, Histogram] = {}

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value_ms: float, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self._histograms.setdefault(key, Histogram()).observe(value_ms)

    def counter(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get(_key(name, labels), 0.0)

    def histogram(self, name: str, **labels: str) -> Histogram | None:
        with self._lock:
            return self._histograms.get(_key(name, labels))

    def snapshot(self) -> dict[str, float]:
        """Flat view of every counter plus histogram count/mean/p95."""
        with self._lock:
            out: dict[str, float] = dict(self._counters)
            for key, hist in self._histograms.items():
                out[f"{key}#count"] = float(hist.n)
                out[f"{key}#mean_ms"] = hist.mean_ms
                out[f"{key}#p95_ms"] = hist.quantile(0.95)
            return out

    def render_prometheus(self) -> str:
        """Render the registry in Prometheus text exposition format."""
        lines: list[str] = []
        for key, value in sorted(self.snapshot().items()):
            lines.append(f"{key.replace('#', '_')} {value}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Clear all series. Test helper; never called on a serving path."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


class Timer:
    """Context manager recording elapsed wall time into a histogram."""

    __slots__ = ("_labels", "_name", "_registry", "_start", "elapsed_ms")

    def __init__(self, registry: MetricsRegistry, name: str, **labels: str) -> None:
        self._registry = registry
        self._name = name
        self._labels = labels
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._registry.observe(self._name, self.elapsed_ms, **self._labels)


@contextmanager
def timed(registry: MetricsRegistry, name: str, **labels: str) -> Iterator[Timer]:
    """Functional form of ``Timer``."""
    timer = Timer(registry, name, **labels)
    with timer:
        yield timer


# Process-wide registry. Injected explicitly where testability matters.
METRICS = MetricsRegistry()

# --- Metric names (single source of truth) ---------------------------------
REQUESTS_TOTAL = "requests_total"
REQUESTS_FAILED_TOTAL = "requests_failed_total"
FALLBACK_TOTAL = "fallback_total"
RATE_LIMIT_REJECTIONS_TOTAL = "rate_limit_rejections_total"
PII_REDACTIONS_TOTAL = "pii_redactions_total"
PROVIDER_LATENCY_MS = "provider_latency_ms"
GATEWAY_LATENCY_MS = "gateway_latency_ms"
TIME_TO_FIRST_TOKEN_MS = "time_to_first_token_ms"  # noqa: S105  # nosec B105 - metric name
TOKENS_REQUESTED = "tokens_requested"
TOKENS_GENERATED = "tokens_generated"
UNAUTHORIZED_TOOL_CALLS_TOTAL = "unauthorized_tool_calls_total"
MCP_TOOL_CALLS_TOTAL = "mcp_tool_calls_total"
RAG_QUERIES_TOTAL = "rag_queries_total"
RAG_RETRIEVAL_LATENCY_MS = "rag_retrieval_latency_ms"
RAG_EMBEDDING_LATENCY_MS = "rag_embedding_latency_ms"
RAG_DOCUMENTS_RETRIEVED = "rag_documents_retrieved"
RAG_CONTEXT_CHARS = "rag_context_chars"
RAG_RETRIEVAL_EMPTY_TOTAL = "rag_retrieval_empty_total"
RAG_RETRIEVAL_ERRORS_TOTAL = "rag_retrieval_errors_total"
RAG_EMBEDDINGS_SKIPPED_TOTAL = "rag_embeddings_skipped_total"
