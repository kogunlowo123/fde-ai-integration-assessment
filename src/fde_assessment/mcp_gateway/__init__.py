"""Task 2: MCP security gateway, authentication, tool policy, proxying."""

from fde_assessment.mcp_gateway.app import create_app
from fde_assessment.mcp_gateway.authorization import Decision, authorize
from fde_assessment.mcp_gateway.proxy import DownstreamMcpClient

__all__ = ["Decision", "DownstreamMcpClient", "authorize", "create_app"]
