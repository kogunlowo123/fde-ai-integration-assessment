"""Task 4, concurrency: the read-calculate-write race must not exist.

The naive limiter reads the window sum, decides, and then inserts. Under
concurrency two requests both read 49,000, both decide they fit, and both
insert, admitting 51,000 tokens against a 50,000 limit. These tests fail
against that implementation and pass against the transactional one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter
from fde_assessment.persistence.sqlite import Database

KEY_A = "hash-tenant-a"
KEY_B = "hash-tenant-b"


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "concurrent.db")
    await db.initialize()
    yield db
    await db.close()


class TestSingleConnectionConcurrency:
    async def test_exact_budget_is_never_exceeded(self, database: Database) -> None:
        limiter = TokenRateLimiter(database, limit_tokens=50_000, window_seconds=60)

        results = await asyncio.gather(
            *[limiter.check_and_consume("tenant-a", KEY_A, 1_000, f"req-{i}") for i in range(100)]
        )

        admitted = [r for r in results if r.allowed]
        assert len(admitted) == 50, "exactly 50 x 1,000 tokens fit in a 50,000 budget"
        assert await limiter.used_tokens(KEY_A) == 50_000

    async def test_no_overshoot_with_uneven_request_sizes(self, database: Database) -> None:
        limiter = TokenRateLimiter(database, limit_tokens=10_000, window_seconds=60)
        sizes = [700, 1_300, 250, 4_000, 900, 2_500, 3_100, 150, 8_000, 600] * 5

        await asyncio.gather(
            *[
                limiter.check_and_consume("tenant-a", KEY_A, size, f"req-{i}")
                for i, size in enumerate(sizes)
            ]
        )
        assert await limiter.used_tokens(KEY_A) <= 10_000

    async def test_concurrent_tenants_do_not_interfere(self, database: Database) -> None:
        limiter = TokenRateLimiter(database, limit_tokens=10_000, window_seconds=60)

        async def hammer(tenant: str, key: str) -> int:
            results = await asyncio.gather(
                *[limiter.check_and_consume(tenant, key, 1_000, f"{tenant}-{i}") for i in range(20)]
            )
            return sum(1 for r in results if r.allowed)

        admitted_a, admitted_b = await asyncio.gather(
            hammer("tenant-a", KEY_A), hammer("tenant-b", KEY_B)
        )
        assert admitted_a == 10
        assert admitted_b == 10
        assert await limiter.used_tokens(KEY_A) == 10_000
        assert await limiter.used_tokens(KEY_B) == 10_000

    async def test_high_contention_burst(self, database: Database) -> None:
        limiter = TokenRateLimiter(database, limit_tokens=1_000, window_seconds=60)
        results = await asyncio.gather(
            *[limiter.check_and_consume("tenant-a", KEY_A, 1, f"r{i}") for i in range(500)]
        )
        # 500 single-token requests all fit inside a 1,000-token budget, so
        # every one of them must be admitted, contention must not cause
        # spurious rejections either.
        assert sum(1 for r in results if r.allowed) == 500
        assert await limiter.used_tokens(KEY_A) == 500


class TestMultiConnectionConcurrency:
    """Two connections to one file: the cross-process case in miniature."""

    async def test_two_connections_share_one_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "shared.db"
        first = Database(path, busy_timeout_ms=10_000)
        second = Database(path, busy_timeout_ms=10_000)
        await first.initialize()
        await second.initialize()
        try:
            limiter_one = TokenRateLimiter(first, limit_tokens=10_000, window_seconds=60)
            limiter_two = TokenRateLimiter(second, limit_tokens=10_000, window_seconds=60)

            results = await asyncio.gather(
                *[
                    (limiter_one if i % 2 == 0 else limiter_two).check_and_consume(
                        "tenant-a", KEY_A, 1_000, f"req-{i}"
                    )
                    for i in range(20)
                ]
            )

            admitted = sum(1 for r in results if r.allowed)
            # BEGIN IMMEDIATE + busy_timeout serialise the two writers, so the
            # shared budget is respected across connections.
            assert admitted == 10
            assert await limiter_one.used_tokens(KEY_A) == 10_000
        finally:
            await first.close()
            await second.close()


class TestNoDeadlock:
    async def test_mixed_reads_and_writes_complete(self, database: Database) -> None:
        limiter = TokenRateLimiter(database, limit_tokens=100_000, window_seconds=60)

        async def writer(i: int) -> None:
            await limiter.check_and_consume("tenant-a", KEY_A, 10, f"w{i}")

        async def reader() -> int:
            return await limiter.used_tokens(KEY_A)

        # WAL mode is what lets readers proceed while the writer holds the
        # write lock; without it this gathers into "database is locked".
        await asyncio.wait_for(
            asyncio.gather(*[writer(i) for i in range(50)], *[reader() for _ in range(50)]),
            timeout=30,
        )
        assert await limiter.used_tokens(KEY_A) == 500
