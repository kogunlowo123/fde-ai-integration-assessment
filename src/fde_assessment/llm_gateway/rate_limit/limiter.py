"""Task 4, token-aware sliding-window rate limiter.

WHAT
    Enforces "at most N tokens per window per tenant API key" (default 50,000
    per 60 seconds) using an event log in on-disk SQLite.

WHY
    Request-count limits do not describe LLM cost or capacity: one request can
    be 50 tokens or 50,000. A token-aware limit is the one that maps onto both
    the provider's quota and the customer's bill.

    A *sliding* window rather than a fixed one because fixed windows permit a
    2x burst across the boundary: 50,000 tokens at 11:59:59 and another 50,000
    at 12:00:00 is 100,000 tokens in one second while satisfying a fixed
    minute-bucket limit.

HOW
    Every admitted request appends ``(tenant, key hash, timestamp, tokens)``.
    Admission sums the last ``window`` seconds for that key and compares
    against the limit. Old rows are evicted inside the same transaction, so
    the table stays proportional to one window of traffic rather than growing
    forever.

    The check and the insert run in a single ``BEGIN IMMEDIATE`` transaction
    behind an ``asyncio.Lock``. Without that, two concurrent requests can both
    read 49,000 used, both conclude they fit, and both insert, the classic
    read-calculate-write race, and the failure the assessment explicitly asks
    about.

BOUNDARY SEMANTICS
    "Maximum 50,000 tokens/minute" is read inclusively: a request that brings
    the window total to exactly 50,000 is **admitted**; 50,001 is rejected.
    Both sides are tested.

ACCOUNTING MODEL
    Tokens are charged at admission using an estimate (prompt + the maximum
    output the request could produce), then reconciled after the stream with a
    correction row carrying the delta. Charging up front is deliberate: a
    limiter that only counts tokens after generation cannot prevent a burst,
    because by the time it knows the cost, the cost has been incurred.

WHEN
    Called once per completion request, before any provider is contacted.

SECURITY
    Raw API keys are never stored, only an HMAC fingerprint. Failing open on
    a database error is *not* an option: the limiter raises, and the gateway
    turns that into a 500 rather than an unmetered request.

SCALE
    Per-node enforcement. See ADR-006 for the Redis evolution and the
    correctness caveat when running more than one gateway replica.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from fde_assessment.common.logging import get_logger
from fde_assessment.observability.metrics import METRICS, RATE_LIMIT_REJECTIONS_TOTAL
from fde_assessment.persistence.sqlite import Database

log = get_logger(__name__)

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Outcome of an admission check."""

    allowed: bool
    used_tokens: int
    requested_tokens: int
    limit: int
    window_seconds: int
    retry_after_seconds: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used_tokens)


class TokenRateLimiter:
    """Sliding-window token limiter backed by SQLite."""

    def __init__(
        self,
        database: Database,
        limit_tokens: int = 50_000,
        window_seconds: int = 60,
        clock: Clock = time.time,
    ) -> None:
        self._db = database
        self._limit = limit_tokens
        self._window = window_seconds
        self._clock = clock

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window

    async def check_and_consume(
        self,
        tenant_id: str,
        api_key_hash: str,
        tokens: int,
        request_id: str,
    ) -> RateLimitDecision:
        """Atomically decide admission and, if admitted, charge ``tokens``."""
        if tokens < 0:
            raise ValueError("tokens must not be negative")

        now = self._clock()
        window_start = now - self._window

        async with self._db.write_transaction() as conn:
            # Eviction happens inside the same transaction as the check, so a
            # concurrent writer can never observe a half-evicted window.
            await conn.execute("DELETE FROM rate_limit_events WHERE timestamp < ?", (window_start,))

            async with conn.execute(
                """
                SELECT COALESCE(SUM(token_count), 0) AS used
                FROM rate_limit_events
                WHERE api_key_hash = ? AND timestamp >= ?
                """,
                (api_key_hash, window_start),
            ) as cursor:
                row = await cursor.fetchone()
            used = int(row["used"]) if row is not None else 0

            # Inclusive limit: reaching exactly `limit` is allowed.
            if used + tokens > self._limit:
                retry_after = await self._retry_after(conn, api_key_hash, window_start, now)
                METRICS.increment(RATE_LIMIT_REJECTIONS_TOTAL, tenant=tenant_id)
                log.info(
                    "rate_limit_rejected",
                    tenant=tenant_id,
                    request_id=request_id,
                    used_tokens=used,
                    requested_tokens=tokens,
                    limit=self._limit,
                )
                return RateLimitDecision(
                    allowed=False,
                    used_tokens=used,
                    requested_tokens=tokens,
                    limit=self._limit,
                    window_seconds=self._window,
                    retry_after_seconds=retry_after,
                )

            await conn.execute(
                """
                INSERT INTO rate_limit_events
                    (tenant_id, api_key_hash, timestamp, token_count, request_id, kind)
                VALUES (?, ?, ?, ?, ?, 'admission')
                """,
                (tenant_id, api_key_hash, now, tokens, request_id),
            )

        return RateLimitDecision(
            allowed=True,
            used_tokens=used + tokens,
            requested_tokens=tokens,
            limit=self._limit,
            window_seconds=self._window,
        )

    async def _retry_after(
        self, conn: object, api_key_hash: str, window_start: float, now: float
    ) -> int:
        """Seconds until the oldest in-window event ages out."""
        from typing import cast

        import aiosqlite

        connection = cast(aiosqlite.Connection, conn)
        async with connection.execute(
            """
            SELECT MIN(timestamp) AS oldest
            FROM rate_limit_events
            WHERE api_key_hash = ? AND timestamp >= ?
            """,
            (api_key_hash, window_start),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["oldest"] is None:
            return self._window
        expires_at = float(row["oldest"]) + self._window
        return max(1, int(expires_at - now) + 1)

    async def reconcile(
        self,
        tenant_id: str,
        api_key_hash: str,
        delta_tokens: int,
        request_id: str,
    ) -> None:
        """Record the difference between the estimate and the actual usage.

        A negative delta refunds an over-estimate. Reconciliation is a separate
        row rather than an update so the event log stays append-only and the
        window arithmetic stays a simple SUM.
        """
        if delta_tokens == 0:
            return
        now = self._clock()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO rate_limit_events
                    (tenant_id, api_key_hash, timestamp, token_count, request_id, kind)
                VALUES (?, ?, ?, ?, ?, 'reconciliation')
                """,
                (tenant_id, api_key_hash, now, delta_tokens, request_id),
            )

    async def used_tokens(self, api_key_hash: str) -> int:
        """Tokens charged to ``api_key_hash`` inside the current window."""
        window_start = self._clock() - self._window
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(SUM(token_count), 0) AS used
            FROM rate_limit_events
            WHERE api_key_hash = ? AND timestamp >= ?
            """,
            (api_key_hash, window_start),
        )
        return int(row["used"]) if row is not None else 0

    async def event_count(self) -> int:
        """Rows currently retained. Used to assert that eviction works."""
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM rate_limit_events")
        return int(row["n"]) if row is not None else 0
