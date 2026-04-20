"""
Tests for MFA session manager — creation, submission, expiry.
"""

import asyncio
from unittest.mock import patch

import pytest

from src.core.mfa_manager import MFAManager


class FakeRedisKV:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1

    def ping(self):
        return True


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
        fake_redis = FakeRedisKV()
        first_manager = MFAManager()
        second_manager = MFAManager()

        with patch("src.core.mfa_manager.session_store._redis", return_value=fake_redis):
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
