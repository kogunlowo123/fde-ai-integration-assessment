"""Task 2, tool access policy.

WHAT
    The rules that decide whether a principal may invoke a named tool, and
    which JSON-RPC methods the gateway is willing to forward at all.

WHY
    Policy is separated from the transport so it can be reviewed, unit-tested
    and changed by a security engineer without touching proxy code. It is also
    the file a customer's security team will read first.

HOW
    ``required_role(tool_name)`` returns the minimum role. Today the rule is
    the assessment's: names beginning with ``admin_`` require ``admin``.
    ``METHOD_POLICY`` classifies JSON-RPC methods into forward / inspect /
    reject rather than defaulting to "forward anything".

WHEN
    Extend ``required_role`` when a customer introduces finer-grained scopes
    (per-tool ACLs, attribute-based rules, delegated consent).

SECURITY
    Fails closed in three places: an unknown method is rejected, an
    unparseable tool name is rejected, and an unknown role is treated as
    insufficient rather than as a wildcard.
"""

from __future__ import annotations

from enum import StrEnum

from fde_assessment.common.config import Role, Settings

# Roles ordered by privilege. `viewer` is the floor.
_ROLE_RANK: dict[Role, int] = {Role.viewer: 0, Role.admin: 10}


class MethodDisposition(StrEnum):
    """What the gateway does with a JSON-RPC method."""

    forward = "forward"
    """Proxy transparently; no per-tool policy applies."""

    inspect = "inspect"
    """Apply tool-level authorization before deciding."""

    reject = "reject"
    """Refuse at the gateway. Never reaches the downstream server."""


# An allowlist, not a denylist: a method the gateway has never heard of is
# rejected rather than tunnelled through to the downstream server.
METHOD_POLICY: dict[str, MethodDisposition] = {
    "initialize": MethodDisposition.forward,
    "notifications/initialized": MethodDisposition.forward,
    "ping": MethodDisposition.forward,
    "tools/list": MethodDisposition.forward,
    "tools/call": MethodDisposition.inspect,
    "prompts/list": MethodDisposition.forward,
    "resources/list": MethodDisposition.forward,
    "resources/read": MethodDisposition.forward,
}


def disposition(method: str) -> MethodDisposition:
    """Classify ``method``. Unknown methods are rejected (fail closed)."""
    return METHOD_POLICY.get(method, MethodDisposition.reject)


def required_role(tool_name: str, settings: Settings) -> Role:
    """Minimum role needed to call ``tool_name``.

    The assessment's rule: an ``admin_``-prefixed tool is an administrative
    action and requires the ``admin`` role. Everything else requires an
    authenticated caller, i.e. ``viewer``.
    """
    if tool_name.startswith(settings.admin_tool_prefix):
        return Role.admin
    return Role.viewer


def role_satisfies(actual: Role, needed: Role) -> bool:
    """True when ``actual`` is at least as privileged as ``needed``."""
    return _ROLE_RANK.get(actual, -1) >= _ROLE_RANK[needed]
