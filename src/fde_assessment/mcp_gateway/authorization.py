"""Task 2, the authorization decision.

WHAT
    ``authorize(request, principal, settings)`` returns an ``Decision`` saying
    whether the gateway forwards the JSON-RPC request downstream.

WHY
    The scored requirement is that an unauthorized ``admin_*`` call is
    intercepted and answered with ``-32001`` *without invoking the downstream
    server*. Expressing that as a pure function over (request, principal)
    makes it directly testable and keeps the network code free of policy.

HOW
    Look up the method disposition, then, for ``tools/call``, the tool's
    required role. The role comes from ``principal``, which was established by
    ``auth.authenticate`` from the Authorization header; ``params`` is never
    consulted for identity.

WHEN
    Called once per request, before the proxy is touched.

SECURITY
    Fails closed on every ambiguity:

    * unknown method -> reject,
    * ``tools/call`` with a missing or non-string ``params.name`` -> reject
      (a name the gateway cannot read is a name it cannot police),
    * insufficient role -> reject.

    A role field in the request body is ignored by construction: it is never
    read. ``tests/security/test_mcp_gateway_auth.py`` asserts that a viewer
    token claiming ``"role": "admin"`` in ``params`` is still denied.
"""

from __future__ import annotations

from dataclasses import dataclass

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import (
    GatewayError,
    MethodNotFoundError,
    UnauthorizedToolCallError,
)
from fde_assessment.common.jsonrpc import JsonRpcRequest
from fde_assessment.common.models import GatewayPrincipal
from fde_assessment.mcp_gateway.policy import (
    MethodDisposition,
    disposition,
    required_role,
    role_satisfies,
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of the authorization check."""

    allowed: bool
    reason: str
    tool_name: str | None = None
    error: GatewayError | None = None


def authorize(request: JsonRpcRequest, principal: GatewayPrincipal, settings: Settings) -> Decision:
    """Decide whether ``request`` may be forwarded downstream."""
    method_disposition = disposition(request.method)

    if method_disposition is MethodDisposition.reject:
        return Decision(
            allowed=False,
            reason="method_not_allowed",
            error=MethodNotFoundError(internal_detail=f"method {request.method!r} not in policy"),
        )

    if method_disposition is MethodDisposition.forward:
        return Decision(allowed=True, reason="method_forwarded")

    # tools/call: the tool name drives the decision.
    tool_name = request.tool_name
    if tool_name is None:
        return Decision(
            allowed=False,
            reason="unreadable_tool_name",
            error=UnauthorizedToolCallError(internal_detail="params.name missing or not a string"),
        )

    needed = required_role(tool_name, settings)
    if not role_satisfies(principal.role, needed):
        return Decision(
            allowed=False,
            reason="insufficient_role",
            tool_name=tool_name,
            error=UnauthorizedToolCallError(
                internal_detail=f"role {principal.role} < {needed} for {tool_name}"
            ),
        )

    return Decision(allowed=True, reason="authorized", tool_name=tool_name)
