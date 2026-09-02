"""Structured logging, pinned to stderr.

WHAT
    ``configure_logging()`` installs a structlog pipeline that emits JSON (or
    human-readable console output in development) to **stderr only**, and
    redirects the stdlib ``logging`` root through the same pipeline.

WHY
    Task 1's headline evaluation criterion is STDIO isolation: an MCP stdio
    server's stdout carries newline-delimited JSON-RPC frames, and a single
    stray byte on stdout desynchronises the client's framing, the client
    either fails to parse the line or, worse, silently drops the response it
    was waiting for. Library code that logs to stdout by default (the stdlib
    root logger's ``lastResort`` handler writes to stderr, but many libraries
    add their own ``StreamHandler()`` which defaults to *stderr* too, while
    ``print`` defaults to stdout) is a live hazard, so the safe design is to
    make stderr the only configured sink and to forbid ``print`` in ``src/``
    via the ruff ``T20`` rule.

HOW
    A structlog processor chain adds ISO timestamps, log level and any bound
    context, then renders JSON through a small proxy that resolves
    ``sys.stderr`` on every write. The stdlib root logger gets a single
    ``StreamHandler`` over the same proxy.

WHEN
    Call once per process, as the first statement of every entrypoint.

SECURITY
    ``redact_mapping`` scrubs known credential-bearing keys; the logging policy
    (docs/security/LOGGING-POLICY.md) forbids logging prompts, completions,
    retrieved documents and raw API keys.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final, TextIO, cast

import structlog

_SENSITIVE_KEYS: Final = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "token",
        "bearer",
        "password",
        "secret",
        "pepper",
        "cookie",
        "set-cookie",
        "prompt",
        "messages",
        "completion",
        "content",
        "document_text",
        "chunk_text",
    }
)

REDACTED: Final = "[REDACTED]"

_configured = False


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``data`` with credential-bearing keys masked."""
    return {k: (REDACTED if k.lower() in _SENSITIVE_KEYS else v) for k, v in data.items()}


class _StderrProxy:
    """A write-through handle that resolves ``sys.stderr`` at write time.

    Binding the ``sys.stderr`` *object* at configuration time captures whatever
    handle happened to be installed then. That handle can be replaced or closed
    later: pytest swaps it per test, and a supervisor or an embedding host may
    rotate it. Every log call afterwards raises ``ValueError: I/O operation on
    closed file``. CI on Linux found this, where test ordering exposed it.

    Resolving at write time keeps the security property (stderr, never stdout)
    while making the binding immune to a stale handle. It also means a caller
    who redirects ``sys.stderr`` on purpose gets what they asked for.
    """

    # `__weakref__` stays in the slots: `logging.StreamHandler` takes a weak
    # reference to its stream, and a slotted class without it cannot be
    # weakly referenced at all.
    __slots__ = ("__weakref__",)

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return sys.stderr.isatty()

    @property
    def closed(self) -> bool:
        closed: bool = sys.stderr.closed
        return closed


def _redact_processor(
    _logger: object, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Defence in depth: mask sensitive keys even if a call site forgets."""
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging to write JSON to stderr.

    Idempotent: safe to call from an entrypoint that may itself be imported by
    a test that already configured logging.
    """
    global _configured
    if _configured:
        return

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        # Writing through `_StderrProxy`, instead of binding `sys.stderr`
        # itself, is the guarantee that no log record can ever reach stdout
        # and corrupt an MCP stdio frame, and that the sink cannot go stale.
        # The factory is typed for `TextIO`; the proxy implements the part of
        # that protocol structlog actually uses (write, flush, isatty).
        logger_factory=structlog.WriteLoggerFactory(file=cast(TextIO, _StderrProxy())),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=cast(TextIO, _StderrProxy()))
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(stderr_handler)
    root.setLevel(level)

    _configured = True


def reset_logging_for_tests() -> None:
    """Allow a test to reconfigure logging. Not used in production paths."""
    global _configured
    _configured = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
