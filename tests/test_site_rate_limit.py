"""Per-account site rate limits from a blueprint's ``rate_limit`` (ENG-14).

The Redis-backed paths run only when PLAIDIFY_TEST_REDIS_URL points at a
disposable Redis (the tests flush the keys they create).
"""

import asyncio
import os
import uuid

import pytest

from src.core.blueprint import RateLimitConfig
from src.core.site_rate_limit import SiteRateLimiter
from src.exceptions import RateLimitedError

REDIS_URL = os.environ.get("PLAIDIFY_TEST_REDIS_URL")


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestLocalLimiter:
    @pytest.mark.asyncio
    async def test_minimum_interval_between_connections(self):
        clock = _Clock()
        limiter = SiteRateLimiter(redis_factory=lambda: None, clock=clock)
        config = RateLimitConfig(max_requests_per_hour=100, min_interval_seconds=30)

        await limiter.acquire("bank", "alice", config)
        clock.now += 10
        with pytest.raises(RateLimitedError) as caught:
            await limiter.acquire("bank", "alice", config)
        assert caught.value.retry_after == 20
        clock.now += 20
        await limiter.acquire("bank", "alice", config)

    @pytest.mark.asyncio
    async def test_hourly_cap(self):
        clock = _Clock()
        limiter = SiteRateLimiter(redis_factory=lambda: None, clock=clock)
        config = RateLimitConfig(max_requests_per_hour=3, min_interval_seconds=0)

        for _ in range(3):
            await limiter.acquire("bank", "alice", config)
            clock.now += 1
        with pytest.raises(RateLimitedError):
            await limiter.acquire("bank", "alice", config)
        clock.now += 3600
        await limiter.acquire("bank", "alice", config)

    @pytest.mark.asyncio
    async def test_limits_are_per_account_and_per_site(self):
        limiter = SiteRateLimiter(redis_factory=lambda: None, clock=_Clock())
        config = RateLimitConfig(max_requests_per_hour=1, min_interval_seconds=60)
        await limiter.acquire("bank", "alice", config)
        await limiter.acquire("bank", "bob", config)
        await limiter.acquire("utility", "alice", config)
        with pytest.raises(RateLimitedError):
            await limiter.acquire("bank", "alice", config)

    @pytest.mark.asyncio
    async def test_no_rate_limit_means_no_limit(self):
        limiter = SiteRateLimiter(redis_factory=lambda: None)
        for _ in range(5):
            await limiter.acquire("bank", "alice", None)


@pytest.mark.skipif(not REDIS_URL, reason="set PLAIDIFY_TEST_REDIS_URL to run against a real Redis")
class TestRedisLimiter:
    @pytest.mark.asyncio
    async def test_window_is_shared_between_workers(self):
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.Redis.from_url(REDIS_URL, decode_responses=True)
        site = f"bank-{uuid.uuid4().hex[:8]}"
        try:
            worker_a = SiteRateLimiter(redis_factory=lambda: client)
            worker_b = SiteRateLimiter(redis_factory=lambda: client)
            config = RateLimitConfig(max_requests_per_hour=2, min_interval_seconds=0)
            await worker_a.acquire(site, "alice", config)
            await worker_b.acquire(site, "alice", config)
            with pytest.raises(RateLimitedError):
                await worker_a.acquire(site, "alice", config)

            spaced = RateLimitConfig(max_requests_per_hour=100, min_interval_seconds=30)
            await worker_a.acquire(site, "bob", spaced)
            with pytest.raises(RateLimitedError) as caught:
                await worker_b.acquire(site, "bob", spaced)
            assert 1 <= caught.value.retry_after <= 30
        finally:
            async for key in client.scan_iter(f"plaidify:site_rate:{site}:*"):
                await client.delete(key)
            await client.aclose()


@pytest.mark.skipif(not REDIS_URL, reason="set PLAIDIFY_TEST_REDIS_URL to run against a real Redis")
class TestRedisMFASessions:
    @pytest.mark.asyncio
    async def test_code_crosses_workers_through_real_redis(self):
        import redis.asyncio as redis_asyncio

        from src.core.mfa_manager import MFAManager

        client = redis_asyncio.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        session_id = f"real-{uuid.uuid4().hex[:8]}"
        try:
            engine_side = MFAManager(redis_factory=lambda: client)
            api_side = MFAManager(redis_factory=lambda: client)
            session = await engine_side.create_session(session_id, "bank", "otp", metadata={"mfa_type": "otp"})
            waiter = asyncio.create_task(session.wait_for_code(timeout=5))
            await asyncio.sleep(0.1)
            assert (await api_side.get_session(session_id)).awaiting_code
            assert await api_side.submit_code(session_id, "314159")
            assert await waiter == "314159"
            seen = await api_side.get_session(session_id)
            assert seen.consumed and seen.attempts == 1
            assert await client.get(f"plaidify:mfa_code:{session_id}") is None
            await engine_side.reopen_session(session_id, metadata={"mfa_error": "invalid_code"})
            reopened = await api_side.get_session(session_id)
            assert reopened.awaiting_code and reopened.metadata["mfa_error"] == "invalid_code"
        finally:
            await client.delete(f"plaidify:mfa_session:{session_id}", f"plaidify:mfa_code:{session_id}")
            await client.aclose()
