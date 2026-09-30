"""
Tests for the scheduled data refresh worker: schedules are database rows, one
refresh runs once, unattended MFA never retries, and the tick never waits.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src import background_services
from src.core.mfa_manager import MFARejectedError
from src.database import ScheduledRefreshJob
from src.exceptions import AuthenticationError, ConcurrentAccessError, MFARequiredError
from src.scheduled_refresh import (
    REASON_MAX_FAILURES,
    REASON_NEEDS_REAUTH,
    RefreshJob,
    RefreshScheduler,
    RefreshTargetMissing,
)
from tests.conftest import TestSessionLocal
from tests.jobs_support import make_access_token, make_user


@pytest.fixture
def tokens():
    """Two real access tokens of one user (schedules reference them)."""
    with TestSessionLocal() as db:
        user = make_user(db)
        first = make_access_token(db, user)
        second = make_access_token(db, user, username="other-login")
        return user.id, first.token, second.token


@pytest.fixture
def fetch_callback():
    return AsyncMock(return_value={"current_bill": "$42.00", "usage_kwh": "350"})


@pytest.fixture
def webhook_callback():
    return AsyncMock()


@pytest.fixture
def scheduler(fetch_callback, webhook_callback):
    return RefreshScheduler(
        fetch_callback=fetch_callback,
        webhook_callback=webhook_callback,
        interval_seconds=60,
        max_backoff_seconds=600,
    )


def _row(access_token):
    with TestSessionLocal() as db:
        row = db.get(ScheduledRefreshJob, access_token)
        if row is not None:
            db.expunge(row)
        return row


async def _drain(scheduler):
    tasks = list(scheduler._in_flight.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestSchedules:
    def test_schedule_creates_a_row_due_at_once(self, scheduler, tokens):
        user_id, token, _ = tokens
        job = scheduler.schedule(token, user_id=user_id)
        assert isinstance(job, RefreshJob)
        assert (job.access_token, job.user_id, job.interval_seconds, job.enabled) == (token, user_id, 60, True)
        row = _row(token)
        assert row.next_run_at is not None and row.next_run_at <= datetime.now(timezone.utc)

    def test_schedule_custom_interval_and_update_existing(self, scheduler, tokens):
        user_id, token, _ = tokens
        assert scheduler.schedule(token, user_id=user_id, interval_seconds=300).interval_seconds == 300
        job = scheduler.schedule(token, user_id=user_id, interval_seconds=120)
        assert job.interval_seconds == 120
        assert len(scheduler.jobs_for_user(user_id)) == 1

    def test_unschedule_removes_the_row(self, scheduler, tokens):
        user_id, token, _ = tokens
        scheduler.schedule(token, user_id=user_id)
        assert scheduler.unschedule(token) is True
        assert _row(token) is None
        assert scheduler.unschedule(token) is False

    def test_list_jobs_masks_tokens(self, scheduler, tokens):
        user_id, first, second = tokens
        scheduler.schedule(first, user_id=user_id)
        scheduler.schedule(second, user_id=user_id, interval_seconds=300)
        jobs = scheduler.list_jobs(user_id=user_id)
        assert len(jobs) == 2
        assert {job["access_token"] for job in jobs} == {first[:12] + "...", second[:12] + "..."}
        assert all(first not in str(job) and second not in str(job) for job in jobs)
        assert {job["interval_seconds"] for job in jobs} == {60, 300}
        assert all("user_id" not in job for job in jobs)
        assert all(job["user_id"] == user_id for job in scheduler.list_jobs())

    def test_a_new_scheduler_sees_existing_rows_with_aware_timestamps(self, scheduler, tokens, fetch_callback):
        """JOB-07: nothing lives in memory, so a restart (a new instance) loses nothing."""
        user_id, token, _ = tokens
        scheduler.schedule(token, user_id=user_id)
        with TestSessionLocal() as db:
            db.get(ScheduledRefreshJob, token).last_refreshed = datetime.now(timezone.utc) - timedelta(hours=2)
            db.commit()
        restarted = RefreshScheduler(fetch_callback=fetch_callback)
        [job] = restarted.jobs_for_user(user_id)
        assert job.last_refreshed.tzinfo is not None
        assert restarted._is_due(job, datetime.now(timezone.utc))


class TestDueLogic:
    def test_never_refreshed_is_due(self, scheduler):
        job = RefreshJob(access_token="acc-1", user_id=1, interval_seconds=60)
        assert scheduler._is_due(job, datetime.now(timezone.utc)) is True

    def test_recently_refreshed_not_due(self, scheduler):
        job = RefreshJob(access_token="a", user_id=1, interval_seconds=60, last_refreshed=datetime.now(timezone.utc))
        assert scheduler._is_due(job, datetime.now(timezone.utc)) is False

    def test_past_interval_is_due(self, scheduler):
        job = RefreshJob(
            access_token="a",
            user_id=1,
            interval_seconds=60,
            last_refreshed=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        assert scheduler._is_due(job, datetime.now(timezone.utc)) is True

    def test_backoff_on_failure(self, scheduler):
        job = RefreshJob(
            access_token="a",
            user_id=1,
            interval_seconds=60,
            consecutive_failures=3,
            last_refreshed=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
        assert scheduler._is_due(job, datetime.now(timezone.utc)) is False  # 60 * 2^3 = 480 s

    def test_backoff_capped_at_max(self, scheduler):
        job = RefreshJob(access_token="x", user_id=1, interval_seconds=60, consecutive_failures=20)
        assert scheduler._effective_interval(job) == 600


class TestRunning:
    @pytest.mark.asyncio
    async def test_a_due_refresh_runs_and_moves_the_row_forward(
        self, scheduler, tokens, fetch_callback, webhook_callback
    ):
        user_id, token, _ = tokens
        scheduler.schedule(token, user_id=user_id)
        assert await scheduler.run_due() == 1
        await _drain(scheduler)
        fetch_callback.assert_awaited_once_with(token, user_id)
        webhook_callback.assert_awaited_once()
        row = _row(token)
        assert row.consecutive_failures == 0 and row.last_error is None and row.last_refreshed is not None
        assert row.next_run_at > datetime.now(timezone.utc) + timedelta(seconds=50)
        assert await scheduler.run_due() == 0

    @pytest.mark.asyncio
    async def test_two_schedulers_run_one_refresh_once(self, tokens, fetch_callback):
        """Even without a lease (two processes looking at once), a due row is claimed by one."""
        user_id, token, _ = tokens
        first = RefreshScheduler(fetch_callback=fetch_callback)
        second = RefreshScheduler(fetch_callback=fetch_callback)
        first.schedule(token, user_id=user_id)
        counts = await asyncio.gather(first.run_due(), second.run_due())
        await _drain(first)
        await _drain(second)
        assert sorted(counts) == [0, 1]
        assert fetch_callback.await_count == 1

    @pytest.mark.asyncio
    async def test_the_tick_never_waits_for_a_refresh(self, tokens):
        user_id, token, other = tokens
        release = asyncio.Event()

        async def hanging_fetch(access_token, user_id):
            await release.wait()
            return {"a": 1}

        scheduler = RefreshScheduler(fetch_callback=hanging_fetch)
        scheduler.schedule(token, user_id=user_id)
        started = asyncio.get_running_loop().time()
        assert await scheduler.run_due() == 1
        assert asyncio.get_running_loop().time() - started < 1
        # In flight: not claimed again by the next tick.
        assert await scheduler.run_due() == 0
        scheduler.schedule(other, user_id=user_id)
        assert await scheduler.run_due() == 1
        release.set()
        await _drain(scheduler)

    @pytest.mark.asyncio
    async def test_a_deleted_schedule_is_never_written_back(self, tokens):
        user_id, token, _ = tokens
        release = asyncio.Event()

        async def slow_fetch(access_token, user_id):
            await release.wait()
            return {"a": 1}

        scheduler = RefreshScheduler(fetch_callback=slow_fetch)
        scheduler.schedule(token, user_id=user_id)
        await scheduler.run_due()
        scheduler.unschedule(token)  # e.g. the token was deleted meanwhile
        release.set()
        await _drain(scheduler)
        assert _row(token) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            MFARequiredError(site="internal_bank", mfa_type="otp", session_id="s"),
            AuthenticationError(site="internal_bank"),
            MFARejectedError(site="internal_bank", mfa_type="otp"),
        ],
    )
    async def test_mfa_or_rejected_credentials_disable_at_once(self, tokens, webhook_callback, error):
        """JOB-14: no retries (each would text the user a code); one needs_reauth webhook."""
        error.message = error.message.encode("ascii", "replace").decode()  # SQL_ASCII test databases
        user_id, token, _ = tokens
        fetch = AsyncMock(side_effect=error)
        scheduler = RefreshScheduler(fetch_callback=fetch, webhook_callback=webhook_callback)
        scheduler.schedule(token, user_id=user_id)
        await scheduler.run_due()
        await _drain(scheduler)
        row = _row(token)
        assert row.enabled is False and row.disabled_reason == REASON_NEEDS_REAUTH
        assert row.consecutive_failures == 0
        data = webhook_callback.await_args.args[2]
        assert data["__refresh_failed__"] is True and data["reason"] == REASON_NEEDS_REAUTH
        assert await scheduler.run_due() == 0
        assert fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_a_busy_credential_is_retried_soon_without_counting_a_failure(self, tokens):
        user_id, token, _ = tokens
        scheduler = RefreshScheduler(fetch_callback=AsyncMock(side_effect=ConcurrentAccessError(site="internal_bank")))
        scheduler.schedule(token, user_id=user_id)
        await scheduler.run_due()
        await _drain(scheduler)
        row = _row(token)
        assert row.enabled is True and row.consecutive_failures == 0
        assert row.next_run_at < datetime.now(timezone.utc) + timedelta(minutes=5)

    @pytest.mark.asyncio
    async def test_a_missing_token_removes_the_schedule(self, tokens):
        user_id, token, _ = tokens
        scheduler = RefreshScheduler(fetch_callback=AsyncMock(side_effect=RefreshTargetMissing("gone")))
        scheduler.schedule(token, user_id=user_id)
        await scheduler.run_due()
        await _drain(scheduler)
        assert _row(token) is None

    @pytest.mark.asyncio
    async def test_failures_back_off_then_disable(self, tokens, webhook_callback):
        user_id, token, _ = tokens
        scheduler = RefreshScheduler(
            fetch_callback=AsyncMock(side_effect=Exception("connection timeout")), webhook_callback=webhook_callback
        )
        job = scheduler.schedule(token, user_id=user_id)

        await scheduler._execute_job(job)
        assert job.consecutive_failures == 1 and job.last_error == "connection timeout" and job.enabled is True
        assert _row(token).next_run_at > datetime.now(timezone.utc) + timedelta(seconds=100)

        job.consecutive_failures = scheduler._MAX_CONSECUTIVE_FAILURES - 1
        await scheduler._execute_job(job)
        row = _row(token)
        assert row.enabled is False and row.disabled_reason == REASON_MAX_FAILURES
        data = webhook_callback.await_args.args[2]
        assert data["__refresh_failed__"] is True and data["reason"] == REASON_MAX_FAILURES
        assert data["error"] == "connection timeout"


class TestLoop:
    @pytest.mark.asyncio
    async def test_start_stop(self, scheduler):
        assert scheduler.running is False
        assert scheduler.start() is True
        assert scheduler.running is True
        assert scheduler.start() is False  # already running
        await scheduler.stop()
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_web_workers_do_not_run_it_when_an_executor_does(self, scheduler, monkeypatch):
        monkeypatch.setattr(background_services.settings, "access_job_execution_mode", "redis-worker")
        monkeypatch.setattr(background_services, "_process_role", "web")
        assert scheduler.start() is False
        monkeypatch.setattr(background_services, "_process_role", "executor")
        assert scheduler.start() is True
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_only_the_lease_holder_ticks(self, monkeypatch):
        ticks = []

        async def tick_a():
            ticks.append("a")

        async def tick_b():
            ticks.append("b")

        first = background_services.LeasedLoop("test-lease", 60, tick_a)
        second = background_services.LeasedLoop("test-lease", 60, tick_b)
        assert await first.run_once() is True
        assert await second.run_once() is False
        await first.lease.release()
        assert await second.run_once() is True
        await second.lease.release()
        assert ticks == ["a", "b"]


@pytest.mark.asyncio
async def test_do_refresh_runs_the_engine_unattended(tokens, monkeypatch):
    """JOB-14: scheduled refreshes call the engine with interactive_mfa=False."""
    from src.routers import refresh

    user_id, token, _ = tokens
    engine = AsyncMock(return_value={"status": "connected", "data": {"balance": "$1"}})
    monkeypatch.setattr(refresh, "connect_to_site", engine)
    result = await refresh._do_refresh(token, user_id)
    assert result["data"] == {"balance": "$1"}
    kwargs = engine.await_args.kwargs
    assert kwargs["interactive_mfa"] is False
    assert kwargs["username"] == "site-user" and kwargs["password"] == "site-pass"
    with pytest.raises(RefreshTargetMissing):
        await refresh._do_refresh("no-such-token", user_id)


class TestOrphansAndInactiveOwners:
    """R3: schedules of deleted tokens or deactivated owners never run."""

    @pytest.mark.asyncio
    async def test_a_deactivated_owners_schedules_are_skipped_until_reactivated(
        self, scheduler, tokens, fetch_callback
    ):
        from src.database import User

        user_id, token, _ = tokens
        scheduler.schedule(token, user_id=user_id)
        with TestSessionLocal() as db:
            db.get(User, user_id).is_active = False
            db.commit()
        assert await scheduler.run_due() == 0
        with TestSessionLocal() as db:
            db.get(User, user_id).is_active = True
            db.commit()
        assert await scheduler.run_due() == 1
        await _drain(scheduler)
        assert fetch_callback.await_count == 1

    @pytest.mark.asyncio
    async def test_do_refresh_skips_an_inactive_owner(self, tokens):
        from src.database import User
        from src.routers import refresh
        from src.scheduled_refresh import RefreshOwnerInactive

        user_id, token, _ = tokens
        with TestSessionLocal() as db:
            db.get(User, user_id).is_active = False
            db.commit()
        with pytest.raises(RefreshOwnerInactive):
            await refresh._do_refresh(token, user_id)

        scheduler = RefreshScheduler(fetch_callback=refresh._do_refresh)
        job = scheduler.schedule(token, user_id=user_id)
        await scheduler._execute_job(job)
        row = _row(token)
        assert row.enabled is True and row.consecutive_failures == 0
        assert row.next_run_at > datetime.now(timezone.utc)

    def test_load_from_db_drops_schedules_whose_token_is_gone(self, scheduler, tokens):
        user_id, token, other = tokens
        scheduler.schedule(token, user_id=user_id)
        scheduler.schedule(other, user_id=user_id)
        with TestSessionLocal() as db:
            # SQLite does not enforce the foreign key; PostgreSQL would cascade.
            if db.bind.dialect.name == "sqlite":
                from src.database import AccessToken

                db.query(AccessToken).filter_by(token=token).delete()
                db.commit()
            else:
                pytest.skip("the foreign key already removes the schedule")
        assert scheduler.load_from_db() == 1
        assert _row(token) is None and _row(other) is not None
