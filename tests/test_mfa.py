"""
Tests for MFA session manager — creation, submission, expiry.
"""

import asyncio

import pytest

from src.core.mfa_manager import MFAManager


class FakeAsyncRedis:
    """The slice of redis.asyncio the MFA manager uses, shared between "workers"."""

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.ttls = {}
        self.fail_reads = False
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        if self.fail_reads:
            raise ConnectionError("redis timed out")
        return self.strings.get(key)

    async def set(self, key, value, ex=None):
        self.strings[key] = str(value)
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += int(self.strings.pop(key, None) is not None) + int(self.hashes.pop(key, None) is not None)
        return removed

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)
        return len(fields)

    async def hgetall(self, key):
        if self.fail_reads:
            raise ConnectionError("redis timed out")
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        if self.redis.fail_reads:
            raise ConnectionError("redis timed out")
        return [await getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.ops]


def _shared(redis):
    return lambda: redis


@pytest.fixture
def mfa_manager():
    """Create a fresh MFA manager for each test."""
    return MFAManager()


# ── Session Creation ──────────────────────────────────────────────────────────


class TestMFASessionCreation:
    @pytest.mark.asyncio
    async def test_create_session(self, mfa_manager):
        session = await mfa_manager.create_session(
            session_id="sess_1",
            site="internal_bank",
            mfa_type="otp",
        )
        assert session.session_id == "sess_1"
        assert session.site == "internal_bank"
        assert session.mfa_type == "otp"
        assert session.code is None
        assert not session.expired

    @pytest.mark.asyncio
    async def test_create_session_with_metadata(self, mfa_manager):
        session = await mfa_manager.create_session(
            session_id="sess_2",
            site="internal_bank",
            mfa_type="security_question",
            metadata={"question": "What is your pet's name?"},
        )
        assert session.metadata["question"] == "What is your pet's name?"

    @pytest.mark.asyncio
    async def test_active_count(self, mfa_manager):
        await mfa_manager.create_session("s1", "site1", "otp")
        await mfa_manager.create_session("s2", "site2", "otp")
        assert mfa_manager.active_count == 2


# ── Code Submission ───────────────────────────────────────────────────────────


class TestMFACodeSubmission:
    @pytest.mark.asyncio
    async def test_submit_code_success(self, mfa_manager):
        session = await mfa_manager.create_session("sess_3", "bank", "otp")

        # Submit code in background
        async def submit():
            await asyncio.sleep(0.1)
            result = await mfa_manager.submit_code("sess_3", "123456")
            assert result is True

        submit_task = asyncio.create_task(submit())
        code = await session.wait_for_code(timeout=5)
        assert code == "123456"
        await submit_task

    @pytest.mark.asyncio
    async def test_submit_code_nonexistent_session(self, mfa_manager):
        result = await mfa_manager.submit_code("nonexistent", "000000")
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_code_timeout(self, mfa_manager):
        session = await mfa_manager.create_session("sess_4", "bank", "otp", ttl=1)
        code = await session.wait_for_code(timeout=0.2)
        assert code is None

    @pytest.mark.asyncio
    async def test_recreate_session_preserves_submitted_code_from_redis(self):
        fake_redis = FakeAsyncRedis()
        first_manager = MFAManager(redis_factory=_shared(fake_redis))
        second_manager = MFAManager(redis_factory=_shared(fake_redis))

        await first_manager.create_session("sess_resume", "bank", "otp")
        submitted = await first_manager.submit_code("sess_resume", "123456")
        assert submitted is True

        resumed = await second_manager.create_session(
            "sess_resume",
            "bank",
            "otp",
            metadata={"prompt": "Enter the one-time code"},
        )

        assert resumed.code == "123456"
        assert resumed.metadata["prompt"] == "Enter the one-time code"
        code = await resumed.wait_for_code(timeout=0.1)
        assert code == "123456"
        # Taken once: the stored copy is gone.
        assert "plaidify:mfa_code:sess_resume" not in fake_redis.strings


# ── Session Retrieval ─────────────────────────────────────────────────────────


class TestMFASessionRetrieval:
    @pytest.mark.asyncio
    async def test_get_session(self, mfa_manager):
        await mfa_manager.create_session("sess_5", "bank", "otp")
        session = await mfa_manager.get_session("sess_5")
        assert session is not None
        assert session.session_id == "sess_5"

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, mfa_manager):
        session = await mfa_manager.get_session("nonexistent")
        assert session is None

    @pytest.mark.asyncio
    async def test_remove_session(self, mfa_manager):
        await mfa_manager.create_session("sess_6", "bank", "otp")
        await mfa_manager.remove_session("sess_6")
        session = await mfa_manager.get_session("sess_6")
        assert session is None


# ── Expiry ────────────────────────────────────────────────────────────────────


class TestMFAExpiry:
    @pytest.mark.asyncio
    async def test_expired_session(self, mfa_manager):
        session = await mfa_manager.create_session("sess_7", "bank", "otp", ttl=0)
        await asyncio.sleep(0.1)
        assert session.expired is True

    @pytest.mark.asyncio
    async def test_get_expired_session_returns_none(self, mfa_manager):
        await mfa_manager.create_session("sess_8", "bank", "otp", ttl=0)
        await asyncio.sleep(0.1)
        session = await mfa_manager.get_session("sess_8")
        assert session is None

    @pytest.mark.asyncio
    async def test_submit_to_expired_session_fails(self, mfa_manager):
        await mfa_manager.create_session("sess_9", "bank", "otp", ttl=0)
        await asyncio.sleep(0.1)
        result = await mfa_manager.submit_code("sess_9", "123456")
        assert result is False


# ── Answered / consumed state (JOB-10) and async Redis (ENG-13) ──────────────


class TestSessionState:
    @pytest.mark.asyncio
    async def test_state_moves_from_awaiting_to_answered_to_consumed(self, mfa_manager):
        session = await mfa_manager.create_session("s", "bank", "otp")
        assert session.awaiting_code and not session.consumed and session.answered_at is None

        await mfa_manager.submit_code("s", "111111")
        assert not session.awaiting_code
        assert session.answered_at is not None and not session.consumed

        assert await session.wait_for_code(timeout=1) == "111111"
        assert session.consumed and session.consumed_at is not None
        assert session.attempts == 1
        assert session.code is None  # read once, not kept around

    @pytest.mark.asyncio
    async def test_reopen_asks_for_another_code(self, mfa_manager):
        session = await mfa_manager.create_session("s", "bank", "otp")
        await mfa_manager.submit_code("s", "000000")
        await session.wait_for_code(timeout=1)

        assert await mfa_manager.reopen_session("s", metadata={"mfa_error": "invalid_code", "attempts_remaining": 2})
        assert session.awaiting_code
        assert session.metadata["mfa_error"] == "invalid_code"
        assert await session.wait_for_code(timeout=0.05) is None  # the old code is not reused

        await mfa_manager.submit_code("s", "123456")
        assert await session.wait_for_code(timeout=1) == "123456"
        assert session.attempts == 2

    @pytest.mark.asyncio
    async def test_reopen_of_unknown_session(self, mfa_manager):
        assert await mfa_manager.reopen_session("missing") is False


class TestSharedStore:
    @pytest.mark.asyncio
    async def test_code_submitted_through_another_worker_reaches_the_engine(self):
        redis = FakeAsyncRedis()
        engine_side = MFAManager(redis_factory=_shared(redis))
        api_side = MFAManager(redis_factory=_shared(redis))

        session = await engine_side.create_session("job-1", "bank", "otp", metadata={"mfa_type": "otp"})
        waiter = asyncio.create_task(session.wait_for_code(timeout=5))

        await asyncio.sleep(0.05)
        seen = await api_side.get_session("job-1")
        assert seen is not None and seen.awaiting_code

        assert await api_side.submit_code("job-1", "654321") is True
        answered = await api_side.get_session("job-1")
        assert answered.answered_at is not None and not answered.awaiting_code

        assert await waiter == "654321"
        consumed = await api_side.get_session("job-1")
        assert consumed.consumed and consumed.attempts == 1

    @pytest.mark.asyncio
    async def test_polling_errors_mean_not_yet(self):
        redis = FakeAsyncRedis()
        engine_side = MFAManager(redis_factory=_shared(redis))
        api_side = MFAManager(redis_factory=_shared(redis))
        session = await engine_side.create_session("job-2", "bank", "otp")

        redis.fail_reads = True
        waiter = asyncio.create_task(session.wait_for_code(timeout=5))
        await asyncio.sleep(0.4)
        assert not waiter.done()  # errors while polling are "no code yet", not a crash

        redis.fail_reads = False
        await api_side.submit_code("job-2", "222222")
        assert await waiter == "222222"

    @pytest.mark.asyncio
    async def test_waiting_does_not_block_the_event_loop(self):
        redis = FakeAsyncRedis()
        manager = MFAManager(redis_factory=_shared(redis))
        session = await manager.create_session("job-3", "bank", "otp")

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())
        assert await session.wait_for_code(timeout=0.5) is None
        tick_task.cancel()
        assert ticks > 20
        # Polling, not a PING before every call.
        assert 1 <= redis.calls <= 5

    @pytest.mark.asyncio
    async def test_remove_clears_session_and_code(self):
        redis = FakeAsyncRedis()
        manager = MFAManager(redis_factory=_shared(redis))
        await manager.create_session("job-4", "bank", "otp")
        await manager.submit_code("job-4", "1")
        await manager.remove_session("job-4")
        assert redis.hashes == {} and redis.strings == {}
        assert await MFAManager(redis_factory=_shared(redis)).get_session("job-4") is None

    @pytest.mark.asyncio
    async def test_expired_sessions_refuse_codes(self):
        redis = FakeAsyncRedis()
        engine_side = MFAManager(redis_factory=_shared(redis))
        await engine_side.create_session("job-5", "bank", "otp", ttl=0)
        await asyncio.sleep(0.05)
        assert await MFAManager(redis_factory=_shared(redis)).submit_code("job-5", "1") is False


class TestAsyncRedisClient:
    @pytest.mark.asyncio
    async def test_client_has_socket_timeouts_and_is_per_loop(self, monkeypatch):
        from src.core import async_redis

        monkeypatch.setattr(async_redis.settings, "redis_url", "redis://127.0.0.1:1/0")
        monkeypatch.setattr(async_redis.settings, "engine_redis_socket_timeout", 1.5)
        client = async_redis.get_async_redis()
        try:
            assert client is async_redis.get_async_redis()
            kwargs = client.connection_pool.connection_kwargs
            assert kwargs["socket_timeout"] == 1.5
            assert kwargs["socket_connect_timeout"] == 1.5
        finally:
            await async_redis.close_async_redis()

    @pytest.mark.asyncio
    async def test_no_redis_url_means_in_memory(self, monkeypatch):
        from src.core import async_redis

        monkeypatch.setattr(async_redis.settings, "redis_url", None)
        assert async_redis.get_async_redis() is None
