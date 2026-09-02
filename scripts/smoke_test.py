#!/usr/bin/env python
"""End-to-end smoke test: every assessment requirement, demonstrated once.

Runs entirely in-process (plus one real MCP subprocess), uses the mock
provider, needs no network, no API key and no GPU. Each check prints PASS or
FAIL with the observed evidence; the exit code is non-zero if anything fails.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fde_assessment.common.config import AppEnv, Settings  # noqa: E402
from fde_assessment.common.errors import UpstreamRateLimitedError  # noqa: E402
from fde_assessment.llm_gateway.app import create_app as create_llm_app  # noqa: E402
from fde_assessment.llm_gateway.providers.mock import (  # noqa: E402
    HangingProvider,
    MockProvider,
    ScriptedFailureProvider,
)
from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter  # noqa: E402
from fde_assessment.llm_gateway.routing.router import ModelRouter  # noqa: E402
from fde_assessment.mcp_gateway.app import create_app as create_mcp_gateway  # noqa: E402
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient  # noqa: E402
from fde_assessment.mcp_server.http_mock import build_mock_downstream_app  # noqa: E402
from fde_assessment.persistence.sqlite import Database  # noqa: E402

GREEN = "\033[32m" if os.environ.get("TERM") else ""
RED = "\033[31m" if os.environ.get("TERM") else ""
RESET = "\033[0m" if os.environ.get("TERM") else ""

PII_SCRIPT = (
    "Your contact is john.smith@example.com, the identifier on file is "
    "123-45-6789, and the saved card is 4111 1111 1111 1111. Anything else?"
)


@dataclass
class Results:
    passed: int = 0
    failed: int = 0

    def check(self, number: int, name: str, ok: bool, evidence: str) -> None:
        status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{number:>2}. [{status}] {name}\n       {evidence}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1


# ---------------------------------------------------------------------------
# Task 1, MCP server over a real stdio subprocess
# ---------------------------------------------------------------------------


class StdioClient:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fde_assessment.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        self._id = 0
        self.stdout_lines: list[str] = []

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
            + "\n"
        )
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.stdout_lines.append(line.strip())
        return json.loads(line)

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> tuple[str, str]:
        # `communicate` closes stdin itself; closing it here first makes its
        # internal flush raise on CPython 3.12.
        out, err = self.proc.communicate(timeout=30)
        return out, err


def run_mcp_server_checks(results: Results) -> None:
    client = StdioClient()
    try:
        handshake = client.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1.0"},
            },
        )
        client.notify("notifications/initialized")
        results.check(
            1,
            "MCP server starts and completes the handshake over stdio",
            handshake.get("result", {}).get("serverInfo", {}).get("name")
            == "fde-assessment-mcp-server",
            f"serverInfo={handshake.get('result', {}).get('serverInfo')}",
        )

        ok = client.send(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}},
        )
        record = ok.get("result", {}).get("structuredContent", {})
        results.check(
            2,
            "Valid customer lookup returns the record",
            record.get("customer_id") == "CUST-12345",
            f"name={record.get('name')!r} tier={record.get('tier')!r}",
        )

        bad = client.send(
            "tools/call",
            {"name": "get_customer_record", "arguments": {"customer_id": "CUST-123"}},
        )
        results.check(
            3,
            "Invalid customer id is rejected with JSON-RPC -32602",
            bad.get("error", {}).get("code") == -32602,
            f"error={bad.get('error')}",
        )

        refund = client.send(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": 25.50,
                    "reason": "Customer requested refund",
                },
            },
        )
        receipt = refund.get("result", {}).get("structuredContent", {})
        results.check(
            4,
            "Valid refund is accepted and returns a receipt",
            receipt.get("status") == "accepted" and receipt.get("amount") == 25.50,
            f"refund_id={receipt.get('refund_id')} amount={receipt.get('amount')}",
        )

        bad_refund = client.send(
            "tools/call",
            {
                "name": "trigger_refund",
                "arguments": {"customer_id": "CUST-12345", "amount": -5, "reason": "short"},
            },
        )
        results.check(
            5,
            "Invalid refund (negative amount, short reason) is rejected",
            bad_refund.get("error", {}).get("code") == -32602,
            f"error={bad_refund.get('error')}",
        )
    finally:
        remaining_stdout, stderr = client.close()

    all_stdout = [line for line in client.stdout_lines + remaining_stdout.splitlines() if line]
    clean = all(json.loads(line).get("jsonrpc") == "2.0" for line in all_stdout)
    results.check(
        15,
        "STDIO isolation: every stdout line is a JSON-RPC frame",
        clean and "mcp_server_starting" in stderr,
        f"{len(all_stdout)} stdout frames parsed; diagnostics went to stderr",
    )


# ---------------------------------------------------------------------------
# Task 2, MCP security gateway
# ---------------------------------------------------------------------------


def run_mcp_gateway_checks(results: Results, settings: Settings) -> None:
    mock_app = build_mock_downstream_app()
    downstream = DownstreamMcpClient(
        settings.model_copy(update={"mcp_downstream_url": "http://downstream/rpc"}),
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app), base_url="http://downstream"
        ),
    )
    app = create_mcp_gateway(
        settings.model_copy(update={"mcp_downstream_url": "http://downstream/rpc"}),
        downstream=downstream,
    )

    def call(tool: str, token: str) -> httpx.Response:
        return client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "smoke",
                "method": "tools/call",
                "params": {"name": tool, "arguments": {}},
            },
            headers={"authorization": f"Bearer {token}"},
        )

    with TestClient(app) as client:
        before = downstream.call_count
        denied = call("admin_reset_key", "dev-viewer-token").json()
        after = downstream.call_count
        results.check(
            6,
            "Viewer calling an admin_ tool is denied with -32001 and no downstream call",
            denied.get("error", {}).get("code") == -32001
            and denied["error"]["message"] == "Unauthorized Tool Call"
            and after == before,
            f"error={denied.get('error')} downstream_calls_delta={after - before}",
        )

        allowed = call("admin_reset_key", "dev-admin-token").json()
        results.check(
            7,
            "Admin calling the same tool is forwarded and succeeds",
            allowed.get("result", {}).get("structuredContent", {}).get("rotated") is True,
            f"result={allowed.get('result', {}).get('structuredContent')}",
        )


# ---------------------------------------------------------------------------
# Tasks 3 + 4, LLM gateway
# ---------------------------------------------------------------------------


@contextmanager
def llm_client(
    settings: Settings, primary: Any, secondary: Any = None, timeout_s: float = 3.0
) -> Iterator[TestClient]:
    """A gateway wired to the given providers, closed cleanly on exit.

    The database is closed explicitly because it is injected: the app only
    owns (and closes) a connection it created itself, so an injected one would
    otherwise stay open and, on Windows, keep the file locked.
    """
    database = Database(settings.database_path)
    app = create_llm_app(
        settings,
        router=ModelRouter(
            primary,
            secondary or MockProvider("mock-secondary", script="secondary answer"),
            primary_timeout_s=timeout_s,
        ),
        limiter=TokenRateLimiter(
            database, settings.rate_limit_tokens, settings.rate_limit_window_seconds
        ),
        database=database,
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        asyncio.run(database.close())


def stream_text(raw: str) -> str:
    out = []
    for line in raw.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            frame = json.loads(line[6:])
            for choice in frame.get("choices", []):
                out.append(choice.get("delta", {}).get("content", ""))
    return "".join(out)


def run_llm_gateway_checks(results: Results, settings: Settings) -> None:
    body = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "summarise the account"}],
        "stream": True,
    }
    headers = {"authorization": "Bearer dev-tenant-a-key"}

    # 8-10: PII redaction across chunk boundaries (chunk_size=3 splits every value).
    with llm_client(
        settings, MockProvider("mock-primary", script=PII_SCRIPT, chunk_size=3)
    ) as client:
        text = stream_text(client.post("/v1/chat/completions", json=body, headers=headers).text)

    results.check(
        8,
        "Email split across stream chunks is redacted",
        "john.smith@example.com" not in text and "[REDACTED]" in text,
        f"output: {text[:80]}...",
    )
    results.check(
        9,
        "SSN split across stream chunks is redacted",
        "123-45-6789" not in text,
        f"redaction count: {text.count('[REDACTED]')}",
    )
    results.check(
        10,
        "Credit card split across stream chunks is redacted (Luhn-validated)",
        "4111 1111 1111 1111" not in text and "4111" not in text,
        f"tail: ...{text[-60:]}",
    )

    # 11: rate limiting.
    tight = settings.model_copy(
        update={
            "rate_limit_tokens": 100,
            "max_output_tokens": 50,
            "database_path": settings.database_path.with_name("ratelimit.db"),
        }
    )
    with llm_client(tight, MockProvider("mock-primary")) as client:
        first = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers=headers,
        )
        second = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers=headers,
        )
    results.check(
        11,
        "Token budget is enforced per tenant (429 once the window is spent)",
        first.status_code == 200 and second.status_code == 429,
        f"first={first.status_code} second={second.status_code} "
        f"retry_after={second.headers.get('retry-after')}",
    )

    # 12: 429 failover.
    fallback_settings = settings.model_copy(
        update={"database_path": settings.database_path.with_name("fallback429.db")}
    )
    with llm_client(
        fallback_settings,
        ScriptedFailureProvider(UpstreamRateLimitedError("429"), name="mock-primary"),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers=headers,
        ).json()
    results.check(
        12,
        "Primary returning 429 fails over to the secondary provider",
        response["x_gateway"]["fell_back"] is True
        and response["choices"][0]["message"]["content"] == "secondary answer",
        f"provider={response['x_gateway']['provider']}",
    )

    # 13: timeout failover.
    timeout_settings = settings.model_copy(
        update={"database_path": settings.database_path.with_name("fallbacktimeout.db")}
    )
    with llm_client(
        timeout_settings, HangingProvider("mock-primary", hang_s=30), timeout_s=0.2
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers=headers,
        ).json()
    results.check(
        13,
        "Primary exceeding the first-token deadline fails over to the secondary",
        response["x_gateway"]["provider"] == "mock-secondary",
        f"fell_back={response['x_gateway']['fell_back']}",
    )

    # 14: upstream error sanitisation.
    from fde_assessment.common.errors import UpstreamUnavailableError

    leaky = UpstreamUnavailableError("psycopg2.OperationalError host=10.0.0.9 password=hunter2")
    error_settings = settings.model_copy(
        update={"database_path": settings.database_path.with_name("errors.db")}
    )
    with llm_client(
        error_settings,
        ScriptedFailureProvider(UpstreamRateLimitedError(), name="mock-primary"),
        secondary=ScriptedFailureProvider(leaky, name="mock-secondary"),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers=headers,
        )
    raw = response.text
    results.check(
        14,
        "Upstream failure is normalised: no stack trace, host or credential leaks",
        response.status_code == 502
        and "psycopg2" not in raw
        and "10.0.0.9" not in raw
        and "hunter2" not in raw
        and response.json()["error"]["code"] == "MODEL_PROVIDER_UNAVAILABLE",
        f"body={raw[:120]}",
    )


def main() -> int:
    results = Results()
    print("FDE assessment smoke test -- mock provider, no network, no API key\n")

    with tempfile.TemporaryDirectory(prefix="fde-smoke-") as tmp:
        settings = Settings(
            app_env=AppEnv.test,
            log_level="ERROR",
            database_path=Path(tmp) / "smoke.db",
        )
        run_mcp_server_checks(results)
        print()
        run_mcp_gateway_checks(results, settings)
        print()
        run_llm_gateway_checks(results, settings)

    print(f"\n{results.passed} passed, {results.failed} failed")
    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
