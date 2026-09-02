"""Tenant authentication for the LLM gateway (Tasks 3 + 4).

WHAT
    Resolves an API key to a ``TenantPrincipal`` carrying the tenant id and an
    HMAC fingerprint of the key.

WHY
    The rate limit, the retrieval scope and every metric label are keyed on
    tenant identity, so identity has to be established before anything else
    happens. Hashing the key rather than storing it means the rate-limit table
    is not a credential store.

HOW
    Configuration maps ``api key -> tenant id``. Comparison is constant-time
    across the whole table.

WHEN
    First step of every request to ``/v1/chat/completions``.

PRODUCTION
    Replace with the customer's key-management service or OIDC client
    credentials. The rest of the gateway consumes ``TenantPrincipal`` only.
"""

from __future__ import annotations

import secrets

from fde_assessment.common.config import Settings
from fde_assessment.common.errors import UnauthenticatedError
from fde_assessment.common.models import TenantPrincipal, fingerprint

BEARER_PREFIX = "Bearer "


def extract_api_key(authorization: str | None, x_api_key: str | None) -> str:
    """Take the key from ``Authorization: Bearer`` or ``X-API-Key``."""
    if authorization and authorization.startswith(BEARER_PREFIX):
        token = authorization[len(BEARER_PREFIX) :].strip()
        if token:
            return token
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    raise UnauthenticatedError(internal_detail="no API key presented")


def authenticate_tenant(
    authorization: str | None, x_api_key: str | None, settings: Settings
) -> TenantPrincipal:
    """Resolve credentials to a tenant, or raise ``UnauthenticatedError``."""
    api_key = extract_api_key(authorization, x_api_key)

    matched_key: str | None = None
    matched_tenant: str | None = None
    for known_key, tenant_id in settings.tenant_keys.items():
        if secrets.compare_digest(api_key, known_key):
            matched_key = known_key
            matched_tenant = tenant_id

    if matched_key is None or matched_tenant is None:
        raise UnauthenticatedError(internal_detail="API key not recognised")

    return TenantPrincipal(
        tenant_id=matched_tenant,
        api_key_hash=fingerprint(
            matched_key, settings.api_key_pepper.get_secret_value(), length=32
        ),
    )
