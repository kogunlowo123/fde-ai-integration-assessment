"""Task 1, STDIO isolation: stdout carries JSON-RPC and nothing else.

This is the assessment's headline evaluation criterion for Task 1, so it is
tested three ways: against a real subprocess under the noisiest log level, at
the logging-configuration level, and statically against the source tree.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from fde_assessment.common.logging import configure_logging, reset_logging_for_tests
from tests.conftest import REPO_ROOT, StdioMcpClient

SRC_ROOT = REPO_ROOT / "src" / "fde_assessment"


class TestWireCleanliness:
    def test_every_stdout_line_is_a_jsonrpc_frame(self) -> None:
        client = StdioMcpClient(env={"LOG_LEVEL": "DEBUG", "APP_ENV": "development"})
        client.initialize()
        client.request("tools/list")
        client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        client.call_tool("get_customer_record", {"customer_id": "bad-id"})  # logs a warning
        client.call_tool(
            "trigger_refund", {"customer_id": "CUST-12345", "amount": -1, "reason": "x"}
        )
        remaining_stdout, stderr = client.close()

        all_stdout = client.stdout_lines + [
            line for line in remaining_stdout.splitlines() if line.strip()
        ]
        assert all_stdout, "server produced no output at all"
        for line in all_stdout:
            frame = json.loads(line)  # raises if anything non-JSON reached stdout
            assert frame["jsonrpc"] == "2.0", f"non-JSON-RPC object on stdout: {line[:120]}"
            assert ("result" in frame) or ("error" in frame) or ("method" in frame)

        # And prove the diagnostics actually happened, a clean stdout is only
        # meaningful if the process was logging at the time.
        assert "mcp_server_starting" in stderr
        assert "tool_validation_failed" in stderr

    def test_stderr_is_structured_json(self) -> None:
        client = StdioMcpClient(env={"LOG_LEVEL": "DEBUG"})
        client.initialize()
        _, stderr = client.close()
        events = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
        assert any(e.get("event") == "mcp_server_starting" for e in events)


class TestLoggingConfiguration:
    def test_configure_logging_writes_to_stderr_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reset_logging_for_tests()
        try:
            configure_logging(level="INFO", fmt="json")
            from fde_assessment.common.logging import get_logger

            get_logger("isolation-probe").info("hello_from_a_test")
            captured = capsys.readouterr()
            assert "hello_from_a_test" in captured.err
            assert captured.out == ""
        finally:
            reset_logging_for_tests()

    def test_structlog_factory_writes_through_to_stderr(self) -> None:
        """The sink must resolve stderr at write time, not capture it.

        Binding the `sys.stderr` object goes stale whenever the handle is
        replaced or closed. That is how this surfaced: pytest swaps stderr per
        test, so a later log call hit a closed buffer.
        """
        reset_logging_for_tests()
        try:
            configure_logging(level="INFO", fmt="json")
            import structlog

            factory = structlog.get_config()["logger_factory"]
            assert isinstance(factory, structlog.WriteLoggerFactory)
            sink = factory._file
            assert sink is not sys.stdout

            # Redirecting stderr must redirect the sink, which only holds if
            # the sink resolves it per write.
            probe = io.StringIO()
            original = sys.stderr
            sys.stderr = probe
            try:
                sink.write("probe-line\n")
            finally:
                sys.stderr = original
            assert probe.getvalue() == "probe-line\n"
        finally:
            reset_logging_for_tests()


class TestStaticGuarantees:
    def test_no_print_calls_in_the_source_tree(self) -> None:
        """`print` defaults to stdout; on an MCP stdio server that is a bug.

        ruff's T20 rule enforces this in CI. Asserting it here too means the
        guarantee survives someone relaxing the lint configuration.
        """
        offenders: list[str] = []
        for path in SRC_ROOT.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Word-boundary match: `fingerprint(` is not a print call.
                if re.search(r"(?<![\w.])print\s*\(", stripped):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        assert offenders == [], f"print() found in src/: {offenders}"

    def test_no_stdout_writes_in_the_source_tree(self) -> None:
        offenders: list[str] = []
        for path in SRC_ROOT.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "sys.stdout" in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        assert offenders == [], f"direct sys.stdout use in src/: {offenders}"


class TestContaminationIsDetectable:
    """A negative control: prove the test above would actually catch a leak."""

    def test_a_print_before_serving_would_corrupt_the_wire(self, tmp_path: Path) -> None:
        script = tmp_path / "contaminated.py"
        script.write_text(
            "import sys\n"
            "print('I am a stray debug line')\n"
            "sys.stdout.flush()\n"
            "from fde_assessment.mcp_server.server import main\n"
            "main()\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            input='{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n',
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
            check=False,
        )
        first_line = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else ""
        with pytest.raises(json.JSONDecodeError):
            json.loads(first_line)
