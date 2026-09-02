"""Task 2, authentication for the MCP security gateway.

WHAT
    Turns an ``Authorization: Bearer <token>`` header into a
    ``GatewayPrincipal`` (subject + role + safe fingerprint), or raises.

WHY
    Authorization decisions must be made against an identity the *gateway*
    established, never against anything the caller asserted in the request
    body. Keeping token parsing in one module makes that invariant auditable:
    there is exactly one place a role can come from.

HOW
    A deterministic, configuration-driven token table for the assessment.
    Comparison is constant-time and scans every entry so response timing does
    not reveal which prefix of a token was correct.

WHEN
    Called by the gateway's dependency chain on every request, before routing.

SECURITY
    * Missing, malformed or unknown tokens all produce the same 401 with the
      same message and the same amount of work, no oracle for enumeration.
    * The raw token never leaves this module: the principal carries only an
      HMAC fingerprint, so audit logs cannot leak a credential.

PRODUCTION
    **This mock authenticator is for the assessment and local testing only.**
    In production, replace ``authenticate`` with OIDC/JWT validation against
    the customer's identity provider: verify the signature against the JWKS
    endpoint, check ``iss``/``aud``/``exp``/``nbf``, and map a group or scope
    claim onto the role. The rest of the gateway is unchanged by that swap,
    it consumes ``GatewayPrincipal``, not a token. See docs/decisions and
    SECURITY.md.
"""

from __future__ import annotations

import secrets

from fde_assessment.common.config import Role, Settings
from fde_assessment.common.errors import UnauthenticatedError
from fde_assessment.common.models import GatewayPrincipal, fingerprint

BEARER_PREFIX = "Bearer "


def parse_bearer(header_value: str | None) -> str:
    """Extract the token from an ``Authorization`` header value."""
    if not header_value:
        raise UnauthenticatedError(internal_detail="missing Authorization header")
    if not header_value.startswith(BEARER_PREFIX):
        raise UnauthenticatedError(internal_detail="Authorization scheme is not Bearer")
    token = header_value[len(BEARER_PREFIX) :].strip()
    if not token:
        raise UnauthenticatedError(internal_detail="empty bearer token")
    return token


def authenticate(header_value: str | None, settings: Settings) -> GatewayPrincipal:
    """Resolve a bearer header to a principal, or raise ``UnauthenticatedError``."""
    token = parse_bearer(header_value)

    matched_subject: str | None = None
    matched_role: Role | None = None
    # Full scan with constant-time comparison: an early `return` on the first
    # match would make response time a function of table position.
    for known_token, role in settings.token_roles.items():
        if secrets.compare_digest(token, known_token):
            matched_subject = known_token
            matched_role = role

    if matched_subject is None or matched_role is None:
        raise UnauthenticatedError(internal_detail="token not recognised")

    pepper = settings.api_key_pepper.get_secret_value()
    return GatewayPrincipal(
        # The subject is a stable, non-reversible handle for the credential;
        # the raw token is deliberately not carried on the principal.
        subject=f"token:{fingerprint(matched_subject, pepper, length=8)}",
        role=matched_role,
        token_fingerprint=fingerprint(matched_subject, pepper),
    )
