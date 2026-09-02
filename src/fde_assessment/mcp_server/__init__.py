"""Task 1: MCP server with strict validation and stdio transport isolation."""

from fde_assessment.mcp_server.registry import (
    ServerDeps,
    ToolDispatcher,
    ToolOutcome,
    ToolSpec,
    build_dispatcher,
)

__all__ = ["ServerDeps", "ToolDispatcher", "ToolOutcome", "ToolSpec", "build_dispatcher"]
