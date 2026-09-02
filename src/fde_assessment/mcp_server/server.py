"""Task 1, runnable MCP server over stdio, using the official Python SDK.

WHAT
    Binds the transport-independent :mod:`~fde_assessment.mcp_server.registry`
    to the official ``mcp`` SDK's low-level ``Server`` and serves it over the
    stdio transport.

WHY
    The low-level ``Server`` (rather than the higher-level ``MCPServer``
    decorator API) is used deliberately: it lets a handler raise ``MCPError``
    with an explicit JSON-RPC code, which is exactly what the assessment scores
    -- "reject invalid formats with standard MCP JSON-RPC error codes".

HOW
    * ``on_list_tools`` renders the registry catalogue.
    * ``on_call_tool`` validates and dispatches; ``InvalidParamsError`` becomes
      ``-32602``, an unknown tool becomes ``-32601``, and anything unexpected
      becomes ``-32603`` with a fixed message.
    * ``main()`` configures logging **to stderr** before touching stdout.

WHEN
    ``python -m fde_assessment.mcp_server`` (or the ``fde-mcp-server`` console
    script) is the client-facing entrypoint an MCP client spawns.

SECURITY
    Two boundaries meet here. Argument validation stops malformed or hostile
    tool input, and the error mapping stops internal detail escaping: no
    exception text, path, or environment value is ever placed in a JSON-RPC
    error message.

STDIO ISOLATION
    stdout carries newline-delimited JSON-RPC frames and nothing else. Three
    independent controls enforce it:

    1. ``configure_logging`` sends structlog *and* the stdlib root logger to
       stderr (:mod:`fde_assessment.common.logging`).
    2. ruff's ``T20`` rule fails the build on any ``print`` under ``src/``.
    3. ``tests/integration/test_stdio_isolation.py`` spawns the real server as
       a subprocess and asserts every stdout line parses as JSON-RPC.

    A stray byte on stdout desynchronises the client's line framing: the client
    either fails to parse that line or drops the response it was waiting on,
    and the session appears to hang. That failure is silent and intermittent,
    which is why it is worth three controls rather than one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from fde_assessment import __version__
from fde_assessment.common.config import Settings, get_settings
from fde_assessment.common.errors import (
    JSONRPC_INTERNAL_ERROR,
    GatewayError,
    InvalidParamsError,
    MethodNotFoundError,
)
from fde_assessment.common.logging import configure_logging, get_logger
from fde_assessment.mcp_server.registry import ServerDeps, ToolDispatcher, build_dispatcher

log = get_logger(__name__)

SERVER_NAME = "fde-assessment-mcp-server"


def _to_mcp_error(exc: GatewayError) -> MCPError:
    """Map an internal error onto a JSON-RPC error with a safe message."""
    return MCPError(code=exc.jsonrpc_code, message=exc.message)


def build_server(dispatcher: ToolDispatcher) -> Server[Any]:
    """Wire ``dispatcher`` into an official-SDK ``Server`` instance."""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name=descriptor["name"],
                title=descriptor["title"],
                description=descriptor["description"],
                input_schema=descriptor["inputSchema"],
                annotations=types.ToolAnnotations(
                    read_only_hint=descriptor["annotations"]["readOnlyHint"],
                    destructive_hint=descriptor["annotations"]["destructiveHint"],
                    idempotent_hint=descriptor["annotations"]["idempotentHint"],
                ),
            )
            for descriptor in dispatcher.list_tools()
        ]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        try:
            outcome = await dispatcher.call(params.name, params.arguments)
        except (InvalidParamsError, MethodNotFoundError) as exc:
            raise _to_mcp_error(exc) from None
        except GatewayError as exc:  # pragma: no cover - defensive
            raise _to_mcp_error(exc) from None
        except Exception:  # pragma: no cover - defensive
            log.exception("call_tool_unhandled", tool=params.name)
            raise MCPError(code=JSONRPC_INTERNAL_ERROR, message="Internal server error") from None

        text = json.dumps(outcome.payload, separators=(",", ":"), sort_keys=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=outcome.payload,
            is_error=outcome.is_error,
        )

    return Server(
        SERVER_NAME,
        version=__version__,
        title="FDE Assessment MCP Server",
        instructions=(
            "Customer support tools. Identifiers use the CUST-XXXXX form. "
            "Refunds require a positive amount and a written reason."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_default_deps(settings: Settings) -> ServerDeps:
    """Construct handler dependencies, wiring the RAG tool when a corpus exists.

    The knowledge tool is a Production Enhancement and is only registered when
    ``MCP_KNOWLEDGE_CORPUS`` points at a readable directory, so the assessment
    tools remain the server's whole surface by default.
    """
    corpus_env = os.environ.get("MCP_KNOWLEDGE_CORPUS", "").strip()
    if not corpus_env:
        return ServerDeps()

    corpus_dir = Path(corpus_env)
    if not corpus_dir.is_dir():
        log.warning("knowledge_corpus_missing", path=str(corpus_dir))
        return ServerDeps()

    # Imported lazily: the assessment-critical path must not depend on RAG.
    from fde_assessment.rag.service import build_mcp_knowledge_search

    tenant_id = os.environ.get("MCP_TENANT_ID", "tenant-a").strip() or "tenant-a"
    return ServerDeps(
        knowledge_search=build_mcp_knowledge_search(
            settings=settings, corpus_dir=corpus_dir, tenant_id=tenant_id
        )
    )


async def run_stdio(settings: Settings | None = None) -> None:
    """Serve the MCP protocol on stdin/stdout until the client disconnects."""
    resolved = settings or get_settings()
    dispatcher = build_dispatcher(build_default_deps(resolved))
    server = build_server(dispatcher)

    log.info(
        "mcp_server_starting",
        transport="stdio",
        tools=list(dispatcher.names),
        version=__version__,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(NotificationOptions()),
        )


def main() -> None:
    """Console entrypoint.

    Logging is configured first and pinned to stderr; nothing in this process
    writes to stdout except the SDK's own transport writer.
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    try:
        asyncio.run(run_stdio(settings))
    except KeyboardInterrupt:  # pragma: no cover - operator action
        sys.stderr.write("mcp server interrupted\n")


if __name__ == "__main__":  # pragma: no cover
    main()
