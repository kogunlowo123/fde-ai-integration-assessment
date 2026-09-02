"""Task 4, sliding-window token limiter: boundaries, eviction, isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fde_assessment.llm_gateway.rate_limit.limiter import TokenRateLimiter
from fde_assessment.persistence.sqlite import Database

KEY_A = "hash-tenant-a"
KEY_B = "hash-tenant-b"


class FakeClock:
    """Controllable time so window behaviour is tested, not slept through."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "rate.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def limiter(database: Database, clock: FakeClock) -> TokenRateLimiter:
    return TokenRateLimiter(database, limit_tokens=50_000, window_seconds=60, clock=clock)


class TestAdmission:
    async def test_first_request_is_admitted(self, limiter: TokenRateLimiter) -> None:
        decision = await limiter.check_and_consume("tenant-a", KEY_A, 100, "req-1")
        assert decision.allowed is True
        assert decision.used_tokens == 100
        assert decision.remaining == 49_900

    async def test_exactly_the_limit_is_admitted(self, limiter: TokenRateLimiter) -> None:
        """'Maximum 50,000 tokens' is inclusive: 50,000 fits."""
        decision = await limiter.check_and_consume("tenant-a", KEY_A, 50_000, "req-1")
        assert decision.allowed is True
        assert decision.remaining == 0

    async def test_one_token_over_the_limit_is_rejected(self, limiter: TokenRateLimiter) -> None:
        decision = await limiter.check_and_consume("tenant-a", KEY_A, 50_001, "req-1")
        assert decision.allowed is False
        assert decision.used_tokens == 0  # nothing was charged

    async def test_the_boundary_across_two_requests(self, limiter: TokenRateLimiter) -> None:
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 49_999, "r1")).allowed is True
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1, "r2")).allowed is True
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1, "r3")).allowed is False

    async def test_a_rejected_request_is_not_charged(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 49_000, "r1")
        await limiter.check_and_consume("tenant-a", KEY_A, 5_000, "r2")  # rejected
        assert await limiter.used_tokens(KEY_A) == 49_000
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1_000, "r3")).allowed is True

    async def test_a_single_request_larger_than_the_limit_can_never_pass(
        self, limiter: TokenRateLimiter
    ) -> None:
        decision = await limiter.check_and_consume("tenant-a", KEY_A, 60_000, "r1")
        assert decision.allowed is False
        assert decision.retry_after_seconds >= 1

    async def test_zero_token_request_is_admitted(self, limiter: TokenRateLimiter) -> None:
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 0, "r1")).allowed is True

    async def test_negative_tokens_are_rejected_as_a_programming_error(
        self, limiter: TokenRateLimiter
    ) -> None:
        with pytest.raises(ValueError):
            await limiter.check_and_consume("tenant-a", KEY_A, -1, "r1")


class TestSlidingWindow:
    async def test_tokens_age_out_of_the_window(
        self, limiter: TokenRateLimiter, clock: FakeClock
    ) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 50_000, "r1")
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1, "r2")).allowed is False

        clock.advance(61)
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 50_000, "r3")).allowed is True

    async def test_window_slides_rather_than_resetting(
        self, limiter: TokenRateLimiter, clock: FakeClock
    ) -> None:
        """A fixed window would allow 2x the limit across the boundary."""
        await limiter.check_and_consume("tenant-a", KEY_A, 30_000, "r1")
        clock.advance(30)
        await limiter.check_and_consume("tenant-a", KEY_A, 20_000, "r2")
        clock.advance(20)  # 50s after r1: still inside the window
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1, "r3")).allowed is False

        clock.advance(11)  # 61s after r1: r1 has aged out, 20,000 remain
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 30_000, "r4")).allowed is True

    async def test_expired_rows_are_evicted(
        self, limiter: TokenRateLimiter, clock: FakeClock
    ) -> None:
        for i in range(20):
            await limiter.check_and_consume("tenant-a", KEY_A, 10, f"r{i}")
        assert await limiter.event_count() == 20

        clock.advance(120)
        await limiter.check_and_consume("tenant-a", KEY_A, 10, "later")
        # Eviction runs inside the admission transaction, so the table holds
        # one window of traffic rather than growing forever.
        assert await limiter.event_count() == 1

    async def test_retry_after_reflects_the_oldest_event(
        self, limiter: TokenRateLimiter, clock: FakeClock
    ) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 50_000, "r1")
        clock.advance(45)
        decision = await limiter.check_and_consume("tenant-a", KEY_A, 1, "r2")
        assert decision.allowed is False
        assert 10 <= decision.retry_after_seconds <= 17


class TestTenantIsolation:
    async def test_tenants_have_independent_budgets(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 50_000, "r1")
        assert (await limiter.check_and_consume("tenant-a", KEY_A, 1, "r2")).allowed is False
        assert (await limiter.check_and_consume("tenant-b", KEY_B, 50_000, "r3")).allowed is True

    async def test_usage_is_keyed_on_the_api_key_hash(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 1_000, "r1")
        assert await limiter.used_tokens(KEY_A) == 1_000
        assert await limiter.used_tokens(KEY_B) == 0


class TestReconciliation:
    async def test_over_estimate_is_refunded(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 1_100, "r1")
        await limiter.reconcile("tenant-a", KEY_A, -1_000, "r1")
        assert await limiter.used_tokens(KEY_A) == 100

    async def test_under_estimate_is_charged(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 100, "r1")
        await limiter.reconcile("tenant-a", KEY_A, 400, "r1")
        assert await limiter.used_tokens(KEY_A) == 500

    async def test_zero_delta_writes_nothing(self, limiter: TokenRateLimiter) -> None:
        await limiter.check_and_consume("tenant-a", KEY_A, 100, "r1")
        await limiter.reconcile("tenant-a", KEY_A, 0, "r1")
        assert await limiter.event_count() == 1


class TestDurability:
    async def test_state_survives_a_reconnect(self, tmp_path: Path) -> None:
        """On-disk, as Task 4 requires, not an in-memory dictionary."""
        path = tmp_path / "durable.db"
        clock = FakeClock()

        first = Database(path)
        await first.initialize()
        limiter_one = TokenRateLimiter(first, 50_000, 60, clock)
        await limiter_one.check_and_consume("tenant-a", KEY_A, 49_000, "r1")
        await first.close()

        second = Database(path)
        await second.initialize()
        limiter_two = TokenRateLimiter(second, 50_000, 60, clock)
        assert await limiter_two.used_tokens(KEY_A) == 49_000
        assert (
            await limiter_two.check_and_consume("tenant-a", KEY_A, 2_000, "r2")
        ).allowed is False
        await second.close()

    async def test_the_database_file_is_actually_created(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "gateway.db"
        db = Database(path)
        await db.initialize()
        assert path.exists()
        await db.close()
