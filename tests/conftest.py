"""Shared pytest fixtures.

Every fixture here is deterministic and offline: the default test run must not
open a network socket, require an API key, or need a GPU (see COST-OPTIMIZATION.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fde_assessment.common.config import AppEnv, Settings
from fde_assessment.llm_gateway.app import create_app as create_llm_app
from fde_assessment.llm_gateway.providers.base import LLMProvider
from fde_assessment.llm_gateway.providers.mock import MockProvider
from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter
from fde_assessment.llm_gateway.routing.router import ModelRouter
from fde_assessment.mcp_gateway.app import create_app
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient
from fde_assessment.mcp_server.http_mock import build_mock_downstream_app
from fde_assessment.observability.metrics import METRICS
from fde_assessment.persistence.sqlite import Database

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    """Counters are process-wide; isolate every test from its neighbours."""
    METRICS.reset()
    yield
    METRICS.reset()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Test settings with an isolated on-disk SQLite file."""
    return Settings(
        app_env=AppEnv.test,
        log_level="DEBUG",
        database_path=tmp_path / "gateway.db",
        rate_limit_tokens=50_000,
        rate_limit_window_seconds=60,
        primary_timeout_ms=3_000,
    )


class StdioMcpClient:
    """A minimal MCP client that speaks raw JSON-RPC over a real subprocess.

    Deliberately does not use the SDK's client: the point of these tests is to
    verify what actually appears on the wire (framing, error codes, stdout
    purity), which an SDK client would abstract away.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        process_env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fde_assessment.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=process_env,
        )
        self._next_id = 0
        self.stdout_lines: list[str] = []

    # , wire helpers ------------------------------------------------------

    def _send(self, frame: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout before responding")
        self.stdout_lines.append(line.rstrip("\n"))
        parsed: dict[str, Any] = json.loads(line)
        return parsed

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )
        return self._read()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_raw(self, raw: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw if raw.endswith("\n") else raw + "\n")
        self.proc.stdin.flush()

    # , lifecycle ---------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> tuple[str, str]:
        """Shut the server down and return any remaining ``(stdout, stderr)``.

        ``communicate`` closes stdin itself. Closing it here first makes the
        flush inside ``communicate`` raise ``ValueError: I/O operation on
        closed file`` on CPython 3.12, which only catches ``BrokenPipeError``
        there. CI on Linux found this; 3.13 happens to tolerate it.
        """
        try:
            out, err = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung server
            self.proc.kill()
            out, err = self.proc.communicate()
        return out, err


@pytest.fixture
def mcp_client() -> Iterator[StdioMcpClient]:
    """A started-and-initialized MCP server subprocess."""
    client = StdioMcpClient()
    client.initialize()
    try:
        yield client
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Task 2, MCP security gateway wired to an in-process mock downstream.
# ---------------------------------------------------------------------------


@dataclass
class GatewayStack:
    """The gateway, its downstream client, and the mock server behind it."""

    client: TestClient
    downstream: DownstreamMcpClient
    mock_app: FastAPI
    settings: Settings

    @property
    def downstream_calls(self) -> int:
        """How many times the gateway opened a downstream request."""
        return self.downstream.call_count

    @property
    def downstream_invocations(self) -> list[dict[str, Any]]:
        """What the mock server actually received."""
        invocations: list[dict[str, Any]] = self.mock_app.state.invocations
        return invocations

    def rpc(
        self,
        payload: dict[str, Any] | str,
        token: str | None = "dev-viewer-token",
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["authorization"] = f"Bearer {token}"
        if isinstance(payload, str):
            request_headers.setdefault("content-type", "application/json")
            return self.client.post("/rpc", content=payload, headers=request_headers)
        return self.client.post("/rpc", json=payload, headers=request_headers)


@pytest.fixture
def gateway_stack(settings: Settings) -> Iterator[GatewayStack]:
    mock_app = build_mock_downstream_app()
    transport = httpx.ASGITransport(app=mock_app)
    downstream_http = httpx.AsyncClient(transport=transport, base_url="http://downstream")
    gateway_settings = settings.model_copy(update={"mcp_downstream_url": "http://downstream/rpc"})
    downstream = DownstreamMcpClient(gateway_settings, client=downstream_http)
    app = create_app(gateway_settings, downstream=downstream)
    with TestClient(app) as client:
        yield GatewayStack(
            client=client, downstream=downstream, mock_app=mock_app, settings=gateway_settings
        )


# ---------------------------------------------------------------------------
# Tasks 3 + 4, LLM gateway with injectable providers and limiter.
# ---------------------------------------------------------------------------


@dataclass
class LlmStack:
    """The LLM gateway plus the collaborators a test may want to assert on."""

    client: TestClient
    database: Database
    limiter: TokenRateLimiter
    router: ModelRouter
    settings: Settings

    def completions(
        self,
        body: dict[str, Any] | None = None,
        key: str | None = "dev-tenant-a-key",
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if key is not None:
            request_headers["authorization"] = f"Bearer {key}"
        payload = (
            body
            if body is not None
            else {
                "model": "mock-primary",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        return self.client.post("/v1/chat/completions", json=payload, headers=request_headers)


def build_llm_stack(
    settings: Settings,
    primary: LLMProvider | None = None,
    secondary: LLMProvider | None = None,
    primary_timeout_s: float = 3.0,
) -> Iterator[LlmStack]:
    database = Database(settings.database_path)
    limiter = TokenRateLimiter(
        database,
        limit_tokens=settings.rate_limit_tokens,
        window_seconds=settings.rate_limit_window_seconds,
    )
    router = ModelRouter(
        primary or MockProvider("mock-primary"),
        secondary or MockProvider("mock-secondary", script="secondary answer"),
        primary_timeout_s=primary_timeout_s,
    )
    app = create_llm_app(settings, router=router, limiter=limiter, database=database)
    with TestClient(app) as client:
        yield LlmStack(
            client=client,
            database=database,
            limiter=limiter,
            router=router,
            settings=settings,
        )
    asyncio.run(database.close())


@pytest.fixture
def llm_stack(settings: Settings) -> Iterator[LlmStack]:
    yield from build_llm_stack(settings)
