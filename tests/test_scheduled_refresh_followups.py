"""Tests for scheduled-refresh follow-ups (#23):

- Schedule presets (hourly / daily / weekly)
- PATCH /refresh/schedule/{access_token}
- refresh_schedule passthrough on /create_link
- REFRESH_FAILED webhook event
- Per-user max-schedule abuse cap
"""

from unittest.mock import AsyncMock

import pytest

from src.scheduled_refresh import (
    SCHEDULE_FORMAT_DAILY,
    SCHEDULE_FORMAT_HOURLY,
    SCHEDULE_FORMAT_INTERVAL,
    SCHEDULE_FORMAT_WEEKLY,
    RefreshScheduler,
    resolve_schedule,
)


class TestResolveSchedule:
    def test_default_format_is_interval(self):
        fmt, secs = resolve_schedule(interval_seconds=600)
        assert fmt == SCHEDULE_FORMAT_INTERVAL
        assert secs == 600

    @pytest.mark.parametrize(
        "preset,expected",
        [
            (SCHEDULE_FORMAT_HOURLY, 3600),
            (SCHEDULE_FORMAT_DAILY, 86400),
            (SCHEDULE_FORMAT_WEEKLY, 604800),
        ],
    )
    def test_presets_resolve_to_canonical_intervals(self, preset, expected):
        fmt, secs = resolve_schedule(schedule_format=preset)
        assert fmt == preset
        assert secs == expected

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Unknown schedule format"):
            resolve_schedule(schedule_format="cron")

    def test_interval_format_requires_interval_seconds(self):
        with pytest.raises(ValueError, match="interval_seconds is required"):
            resolve_schedule(schedule_format="interval", interval_seconds=None)

    def test_resolve_does_not_enforce_minimum(self):
        # The 5-minute floor is enforced at the API layer, not the resolver.
        fmt, secs = resolve_schedule(schedule_format="interval", interval_seconds=10)
        assert secs == 10


class TestSchedulerPresetsAndUpdate:
    @pytest.fixture
    def scheduler(self):
        return RefreshScheduler(
            fetch_callback=AsyncMock(return_value={"a": 1}),
            webhook_callback=AsyncMock(),
            interval_seconds=60,
        )

    def test_schedule_with_preset_uses_canonical_interval(self, scheduler):
        job = scheduler.schedule(
            "acc-1",
            user_id=7,
            schedule_format=SCHEDULE_FORMAT_HOURLY,
        )
        assert job.schedule_format == SCHEDULE_FORMAT_HOURLY
        assert job.interval_seconds == 3600

    def test_update_returns_none_for_unknown_token(self, scheduler):
        assert scheduler.update("nope") is None

    def test_update_changes_interval_only(self, scheduler):
        scheduler.schedule("acc-1", user_id=7, interval_seconds=600)
        job = scheduler.update("acc-1", interval_seconds=900)
        assert job is not None
        assert job.interval_seconds == 900
        assert job.schedule_format == SCHEDULE_FORMAT_INTERVAL

    def test_update_switches_to_preset(self, scheduler):
        scheduler.schedule("acc-1", user_id=7, interval_seconds=600)
        job = scheduler.update("acc-1", schedule_format=SCHEDULE_FORMAT_DAILY)
        assert job.schedule_format == SCHEDULE_FORMAT_DAILY
        assert job.interval_seconds == 86400

    def test_update_can_disable_and_reenable(self, scheduler):
        scheduler.schedule("acc-1", user_id=7, interval_seconds=600)
        scheduler.update("acc-1", enabled=False)
        assert scheduler._jobs["acc-1"].enabled is False
        job = scheduler.update("acc-1", enabled=True)
        assert job.enabled is True
        # Re-enabling should reset the failure counter so backoff resets.
        assert job.consecutive_failures == 0

    def test_update_rejects_invalid_format(self, scheduler):
        scheduler.schedule("acc-1", user_id=7, interval_seconds=600)
        with pytest.raises(ValueError):
            scheduler.update("acc-1", schedule_format="quarterly")

    def test_jobs_for_user_isolates_owners(self, scheduler):
        scheduler.schedule("acc-a", user_id=1, interval_seconds=600)
        scheduler.schedule("acc-b", user_id=1, interval_seconds=600)
        scheduler.schedule("acc-c", user_id=2, interval_seconds=600)
        owned = scheduler.jobs_for_user(1)
        assert {j.access_token for j in owned} == {"acc-a", "acc-b"}


class TestRefreshFailedWebhook:
    def test_max_failures_disables_and_emits_failure_payload(self):
        webhook = AsyncMock()

        async def _failing_fetch(_token, _uid):
            raise RuntimeError("boom")

        scheduler = RefreshScheduler(
            fetch_callback=_failing_fetch,
            webhook_callback=webhook,
            interval_seconds=60,
        )
        scheduler.schedule("acc-1", user_id=1, interval_seconds=60)
        job = scheduler._jobs["acc-1"]
        # Force one-shy-of-max so a single failure triggers the disable path.
        job.consecutive_failures = scheduler._MAX_CONSECUTIVE_FAILURES - 1

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            sem = asyncio.Semaphore(1)
            loop.run_until_complete(scheduler._execute_job(job, sem))
        finally:
            loop.close()

        assert job.enabled is False
        assert job.consecutive_failures >= scheduler._MAX_CONSECUTIVE_FAILURES
        assert webhook.await_count == 1
        kwargs_or_args = webhook.await_args
        # third positional arg is the data dict
        data = kwargs_or_args.args[2]
        assert data.get("__refresh_failed__") is True
        assert data.get("error") == "boom"


class TestApiAbuseControls:
    def test_per_user_cap_constant_is_exposed(self):
        from src.routers import refresh as refresh_router

        assert isinstance(refresh_router.MAX_SCHEDULES_PER_USER, int)
        assert refresh_router.MAX_SCHEDULES_PER_USER >= 1

    def test_jobs_for_user_drives_quota_check(self):
        scheduler = RefreshScheduler(
            fetch_callback=AsyncMock(return_value={}),
            interval_seconds=60,
        )
        for i in range(3):
            scheduler.schedule(f"acc-{i}", user_id=42, interval_seconds=600)
        assert len(scheduler.jobs_for_user(42)) == 3
        assert len(scheduler.jobs_for_user(99)) == 0
