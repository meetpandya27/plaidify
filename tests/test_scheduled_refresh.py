"""
Tests for the scheduled data refresh worker.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.scheduled_refresh import RefreshJob, RefreshScheduler


@pytest.fixture
def fetch_callback():
    """Mock fetch callback."""
    return AsyncMock(return_value={"current_bill": "$42.00", "usage_kwh": "350"})


@pytest.fixture
def webhook_callback():
    """Mock webhook callback."""
    return AsyncMock()


@pytest.fixture
def scheduler(fetch_callback, webhook_callback):
    """Create a scheduler with mock callbacks."""
    return RefreshScheduler(
        fetch_callback=fetch_callback,
        webhook_callback=webhook_callback,
        interval_seconds=60,
        max_backoff_seconds=600,
    )


class TestRefreshScheduler:
    def test_schedule_creates_job(self, scheduler):
        job = scheduler.schedule("acc-123", user_id=1)
        assert isinstance(job, RefreshJob)
        assert job.access_token == "acc-123"
        assert job.user_id == 1
        assert job.interval_seconds == 60
        assert job.enabled is True

    def test_schedule_custom_interval(self, scheduler):
        job = scheduler.schedule("acc-456", user_id=2, interval_seconds=300)
        assert job.interval_seconds == 300

    def test_schedule_updates_existing(self, scheduler):
        scheduler.schedule("acc-123", user_id=1, interval_seconds=60)
        job = scheduler.schedule("acc-123", user_id=1, interval_seconds=120)
        assert job.interval_seconds == 120
        assert len(scheduler._jobs) == 1

    def test_unschedule_removes_job(self, scheduler):
        scheduler.schedule("acc-123", user_id=1)
        assert scheduler.unschedule("acc-123") is True
        assert len(scheduler._jobs) == 0

    def test_unschedule_nonexistent(self, scheduler):
        assert scheduler.unschedule("acc-999") is False

    def test_list_jobs(self, scheduler):
        scheduler.schedule("acc-1", user_id=1)
        scheduler.schedule("acc-2", user_id=2, interval_seconds=300)
        jobs = scheduler.list_jobs()
        assert len(jobs) == 2
        assert "acc-1" in jobs
        assert jobs["acc-2"]["interval_seconds"] == 300

    def test_list_jobs_includes_status(self, scheduler):
        scheduler.schedule("acc-1", user_id=1)
        jobs = scheduler.list_jobs()
        assert jobs["acc-1"]["enabled"] is True
        assert jobs["acc-1"]["last_refreshed"] is None
        assert jobs["acc-1"]["consecutive_failures"] == 0

    def test_start_stop(self, scheduler):
        assert scheduler.running is False
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            scheduler.start()
            assert scheduler.running is True
            loop.run_until_complete(scheduler.stop())
            assert scheduler.running is False
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class TestRefreshJobDueLogic:
    def test_never_refreshed_is_due(self, scheduler):
        job = RefreshJob(access_token="acc-1", user_id=1, interval_seconds=60)
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(job, now) is True

    def test_recently_refreshed_not_due(self, scheduler):
        job = RefreshJob(
            access_token="acc-1",
            user_id=1,
            interval_seconds=60,
            last_refreshed=datetime.now(timezone.utc),
        )
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(job, now) is False

    def test_past_interval_is_due(self, scheduler):
        job = RefreshJob(
            access_token="acc-1",
            user_id=1,
            interval_seconds=60,
            last_refreshed=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(job, now) is True

    def test_backoff_on_failure(self, scheduler):
        job = RefreshJob(
            access_token="acc-1",
            user_id=1,
            interval_seconds=60,
            consecutive_failures=3,
            last_refreshed=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        # With 3 failures: effective = 60 * 2^3 = 480 seconds
        # 120 seconds elapsed < 480, so not due
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(job, now) is False

    def test_backoff_capped_at_max(self, scheduler):
        effective = scheduler._effective_interval(
            RefreshJob(access_token="x", user_id=1, interval_seconds=60, consecutive_failures=20)
        )
        assert effective == 600  # max_backoff_seconds


class TestRefreshExecution:
    @pytest.mark.asyncio
    async def test_execute_success(self, scheduler, fetch_callback, webhook_callback):
        job = scheduler.schedule("acc-1", user_id=1)
        semaphore = asyncio.Semaphore(5)

        await scheduler._execute_job(job, semaphore)

        fetch_callback.assert_awaited_once_with("acc-1", 1)
        webhook_callback.assert_awaited_once()
        assert job.consecutive_failures == 0
        assert job.last_error is None
        assert job.last_refreshed is not None

    @pytest.mark.asyncio
    async def test_execute_failure_increments_count(self, scheduler, fetch_callback):
        fetch_callback.side_effect = Exception("connection timeout")
        job = scheduler.schedule("acc-1", user_id=1)
        semaphore = asyncio.Semaphore(5)

        await scheduler._execute_job(job, semaphore)

        assert job.consecutive_failures == 1
        assert job.last_error == "connection timeout"
        assert job.enabled is True  # Not yet disabled

    @pytest.mark.asyncio
    async def test_disabled_after_max_failures(self, scheduler, fetch_callback):
        fetch_callback.side_effect = Exception("fail")
        job = scheduler.schedule("acc-1", user_id=1)
        job.consecutive_failures = 9  # One away from max
        semaphore = asyncio.Semaphore(5)

        await scheduler._execute_job(job, semaphore)

        assert job.consecutive_failures == 10
        assert job.enabled is False
