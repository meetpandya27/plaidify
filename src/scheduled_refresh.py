"""
Scheduled data refresh for Plaidify.

Schedules live in the ``scheduled_refresh_jobs`` table and nowhere else. Each
tick the scheduler reads the rows that are due, so a deleted schedule stays
deleted and a restart loses nothing. It runs in one process at a time (a
lease, see ``src.background_services``) and claims each due row with a
conditional UPDATE of ``next_run_at`` before running it, so one refresh never
runs twice. A tick only starts refreshes; it never waits for one to finish.

Refreshes are unattended: the fetch callback calls the engine with
``interactive_mfa=False``. A site that asks for MFA or rejects the stored
credentials disables the schedule (``disabled_reason = "needs_reauth"``) and
sends one REFRESH_FAILED webhook, instead of texting the user a code every
interval. Other failures back off exponentially and disable the schedule after
``_MAX_CONSECUTIVE_FAILURES``.

Usage:
    scheduler = RefreshScheduler(fetch_callback=..., webhook_callback=...)
    scheduler.schedule(access_token="acc-xxx", user_id=1)   # writes the row
    scheduler.start()                                     # lease-protected loop
    await scheduler.stop()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from src.config import get_settings
from src.crypto import token_fingerprint
from src.database import AccessToken, ScheduledRefreshJob, SessionLocal, User, as_utc, utcnow
from src.exceptions import AuthenticationError, ConcurrentAccessError, MFARequiredError, PlaidifyError

logger = logging.getLogger("plaidify.scheduler")
settings = get_settings()


# ── Schedule formats ─────────────────────────────────────────────────────────

SCHEDULE_FORMAT_INTERVAL = "interval"
SCHEDULE_FORMAT_HOURLY = "hourly"
SCHEDULE_FORMAT_DAILY = "daily"
SCHEDULE_FORMAT_WEEKLY = "weekly"

SCHEDULE_FORMATS = (
    SCHEDULE_FORMAT_INTERVAL,
    SCHEDULE_FORMAT_HOURLY,
    SCHEDULE_FORMAT_DAILY,
    SCHEDULE_FORMAT_WEEKLY,
)

_PRESET_INTERVAL_SECONDS = {
    SCHEDULE_FORMAT_HOURLY: 3600,
    SCHEDULE_FORMAT_DAILY: 86400,
    SCHEDULE_FORMAT_WEEKLY: 604800,
}

MIN_INTERVAL_SECONDS = 300  # 5 minutes

# Why a schedule was disabled.
REASON_NEEDS_REAUTH = "needs_reauth"
REASON_MAX_FAILURES = "max_failures"

# A refresh blocked by another job on the same credential is retried this soon.
_BUSY_RETRY_SECONDS = 60


def resolve_schedule(
    schedule_format: Optional[str] = None,
    interval_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """Normalize a (format, interval) pair.

    Returns ``(schedule_format, interval_seconds)`` where the interval reflects
    the chosen preset. Raises ``ValueError`` for unknown formats or a missing
    interval. The 5-minute floor is enforced by the API before anything is saved.
    """
    fmt = (schedule_format or SCHEDULE_FORMAT_INTERVAL).lower()
    if fmt not in SCHEDULE_FORMATS:
        raise ValueError(f"Unknown schedule format '{schedule_format}'. Supported: {', '.join(SCHEDULE_FORMATS)}.")
    if fmt in _PRESET_INTERVAL_SECONDS:
        return fmt, _PRESET_INTERVAL_SECONDS[fmt]
    if interval_seconds is None:
        raise ValueError("interval_seconds is required when schedule_format is 'interval'.")
    try:
        interval = int(interval_seconds)
    except (TypeError, ValueError):
        raise ValueError("interval_seconds must be a whole number of seconds.") from None
    return SCHEDULE_FORMAT_INTERVAL, interval


def mask_access_token(access_token: str) -> str:
    """How an access token is shown in listings and webhooks: its first 12 characters, never the token."""
    return f"{access_token[:12]}..."


class RefreshTargetMissing(Exception):
    """The access token (or its link or owner) of a schedule no longer exists."""


class RefreshOwnerInactive(Exception):
    """The schedule's owner is deactivated: skip it until they are active again."""


@dataclass
class RefreshJob:
    """A snapshot of one scheduled refresh row."""

    access_token: str
    user_id: int
    interval_seconds: int
    schedule_format: str = SCHEDULE_FORMAT_INTERVAL
    last_refreshed: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    enabled: bool = True
    next_run_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None

    @classmethod
    def from_row(cls, row: ScheduledRefreshJob) -> "RefreshJob":
        return cls(
            access_token=row.access_token,
            user_id=row.user_id,
            interval_seconds=row.interval_seconds,
            schedule_format=row.schedule_format or SCHEDULE_FORMAT_INTERVAL,
            last_refreshed=as_utc(row.last_refreshed),
            last_error=row.last_error,
            consecutive_failures=row.consecutive_failures or 0,
            enabled=bool(row.enabled),
            next_run_at=as_utc(row.next_run_at),
            disabled_reason=row.disabled_reason,
        )

    def summary(self, *, include_owner: bool = False) -> Dict[str, Any]:
        """The API view: the token masked, never in full."""
        item: Dict[str, Any] = {
            "access_token": mask_access_token(self.access_token),
            "interval_seconds": self.interval_seconds,
            "schedule_format": self.schedule_format,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "last_refreshed": self.last_refreshed.isoformat() if self.last_refreshed else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
        if include_owner:
            item["user_id"] = self.user_id
        return item


class RefreshScheduler:
    """Background scheduler that periodically re-fetches data for linked accounts.

    Args:
        fetch_callback: Async ``(access_token, user_id) -> dict`` performing the
            fetch; it must run the engine non-interactively. It raises
            ``RefreshTargetMissing`` when the token is gone.
        webhook_callback: Optional async ``(access_token, user_id, data) -> None``
            for DATA_REFRESHED / REFRESH_FAILED (``data["__refresh_failed__"]``).
        interval_seconds: Default refresh interval for new schedules.
        max_backoff_seconds: Upper bound for exponential backoff on failures.
        tick_seconds: How often the loop looks for due rows.
        max_concurrency: Refreshes that may run at once.
    """

    _MAX_CONSECUTIVE_FAILURES = 10  # Disable job after this many failures

    def __init__(
        self,
        fetch_callback: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
        webhook_callback: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
        interval_seconds: int = 3600,
        max_backoff_seconds: int = 86400,
        *,
        tick_seconds: Optional[float] = None,
        max_concurrency: Optional[int] = None,
    ):
        self._fetch = fetch_callback
        self._webhook = webhook_callback
        self._default_interval = interval_seconds
        self._max_backoff = max_backoff_seconds
        self._tick_seconds = tick_seconds or settings.refresh_tick_seconds
        self._max_concurrency = max_concurrency or settings.refresh_max_concurrency
        self._in_flight: Dict[str, asyncio.Task] = {}
        self._loop = None

    # ── Schedules (rows) ──────────────────────────────────────────────────────

    def schedule(
        self,
        access_token: str,
        user_id: int,
        interval_seconds: Optional[int] = None,
        schedule_format: Optional[str] = None,
    ) -> RefreshJob:
        """Create or update the schedule of an access token (enabled, failures reset).

        Raises ``ValueError`` for invalid formats / intervals, before any write.
        """
        fmt, resolved_interval = resolve_schedule(
            schedule_format=schedule_format,
            interval_seconds=interval_seconds or self._default_interval,
        )
        for _attempt in range(2):
            now = utcnow()
            with SessionLocal() as db:
                row = db.get(ScheduledRefreshJob, access_token)
                if row is None:
                    row = ScheduledRefreshJob(
                        access_token=access_token,
                        user_id=user_id,
                        interval_seconds=resolved_interval,
                        schedule_format=fmt,
                        enabled=True,
                        consecutive_failures=0,
                        next_run_at=now,
                        created_at=now,
                    )
                    db.add(row)
                else:
                    row.interval_seconds = resolved_interval
                    row.schedule_format = fmt
                    row.enabled = True
                    row.consecutive_failures = 0
                    row.disabled_reason = None
                    row.next_run_at = self._next_run(row.last_refreshed, resolved_interval, now)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()  # created concurrently: update it instead
                    continue
                db.refresh(row)
                job = RefreshJob.from_row(row)
            logger.info(
                "Scheduled refresh for %s (format=%s, interval=%ds)",
                token_fingerprint(access_token),
                fmt,
                resolved_interval,
            )
            return job
        raise RuntimeError("Could not save the refresh schedule")

    def update(
        self,
        access_token: str,
        interval_seconds: Optional[int] = None,
        schedule_format: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[RefreshJob]:
        """Update an existing schedule in place; None when there is none.

        Raises ``ValueError`` for invalid formats / intervals, before any write.
        """
        now = utcnow()
        with SessionLocal() as db:
            row = db.get(ScheduledRefreshJob, access_token)
            if row is None:
                return None
            if schedule_format is not None or interval_seconds is not None:
                fmt, resolved_interval = resolve_schedule(
                    schedule_format=schedule_format or row.schedule_format,
                    interval_seconds=interval_seconds or row.interval_seconds,
                )
                row.schedule_format = fmt
                row.interval_seconds = resolved_interval
                row.next_run_at = self._next_run(row.last_refreshed, resolved_interval, now)
            if enabled is not None:
                row.enabled = bool(enabled)
                if enabled:
                    row.consecutive_failures = 0
                    row.disabled_reason = None
                    row.next_run_at = self._next_run(row.last_refreshed, row.interval_seconds, now)
            db.commit()
            db.refresh(row)
            return RefreshJob.from_row(row)

    def get(self, access_token: str) -> Optional[RefreshJob]:
        with SessionLocal() as db:
            row = db.get(ScheduledRefreshJob, access_token)
            return RefreshJob.from_row(row) if row is not None else None

    def jobs_for_user(self, user_id: int) -> list[RefreshJob]:
        """Return all schedules owned by ``user_id``."""
        with SessionLocal() as db:
            rows = db.execute(select(ScheduledRefreshJob).where(ScheduledRefreshJob.user_id == user_id)).scalars()
            return [RefreshJob.from_row(row) for row in rows]

    def all_jobs(self) -> list[RefreshJob]:
        """Every schedule (the operator's view)."""
        with SessionLocal() as db:
            rows = db.execute(select(ScheduledRefreshJob)).scalars()
            return [RefreshJob.from_row(row) for row in rows]

    def list_jobs(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Summaries (tokens masked) of one user's schedules, or of all when ``user_id`` is None."""
        jobs = self.all_jobs() if user_id is None else self.jobs_for_user(user_id)
        return [job.summary(include_owner=user_id is None) for job in jobs]

    def load_from_db(self) -> int:
        """Drop schedules whose access token is gone and return how many are enabled.

        Nothing is loaded into memory: every tick reads the table. (The foreign
        key removes such rows on PostgreSQL; SQLite does not enforce it.)
        """
        with SessionLocal() as db:
            orphans = select(ScheduledRefreshJob.access_token).where(
                ~select(AccessToken.token).where(AccessToken.token == ScheduledRefreshJob.access_token).exists()
            )
            removed = (
                db.query(ScheduledRefreshJob)
                .filter(ScheduledRefreshJob.access_token.in_(orphans))
                .delete(synchronize_session=False)
            )
            db.commit()
            count = db.query(ScheduledRefreshJob).filter(ScheduledRefreshJob.enabled.is_(True)).count()
        if removed:
            logger.info("Removed %d refresh schedules whose access token is gone", removed)
        logger.info("%d refresh schedules enabled", count)
        return count

    @property
    def _jobs(self) -> Dict[str, RefreshJob]:
        """Every schedule by access token, read from the table (a snapshot, for diagnostics and tests)."""
        return {job.access_token: job for job in self.all_jobs()}

    def unschedule(self, access_token: str) -> bool:
        """Remove a schedule. True if it existed."""
        with SessionLocal() as db:
            deleted = db.query(ScheduledRefreshJob).filter_by(access_token=access_token).delete()
            db.commit()
        if deleted:
            logger.info("Unscheduled refresh for %s", token_fingerprint(access_token))
        return bool(deleted)

    # ── Loop ──────────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the lease-protected loop, if this process may run it. Safe to call from every worker."""
        from src.background_services import LeasedLoop, services_allowed_here

        if not services_allowed_here():
            return False
        if self._loop is None:
            self._loop = LeasedLoop("refresh-scheduler", self._tick_seconds, self.run_due)
        started = self._loop.start()
        if started:
            logger.info("Refresh scheduler started (tick=%ss)", self._tick_seconds)
        return started

    async def stop(self) -> None:
        """Stop the loop and cancel refreshes in flight (their rows become due again)."""
        if self._loop is not None:
            await self._loop.stop()
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.running

    async def run_due(self, now: Optional[datetime] = None) -> int:
        """One tick: claim due schedules (up to the free capacity) and start their refreshes."""
        capacity = self._max_concurrency - len(self._in_flight)
        if capacity <= 0:
            return 0
        due = await asyncio.to_thread(self._claim_due, now or utcnow(), capacity)
        for job in due:
            task = asyncio.create_task(
                self._execute_job(job), name=f"plaidify-refresh-{token_fingerprint(job.access_token)}"
            )
            self._in_flight[job.access_token] = task
            task.add_done_callback(lambda _t, token=job.access_token: self._in_flight.pop(token, None))
        return len(due)

    def _claim_window(self) -> timedelta:
        """How long a claimed row stays invisible to other ticks while its refresh runs."""
        return timedelta(
            seconds=settings.engine_timeout_seconds
            + settings.access_job_deadline_margin_seconds
            + settings.access_job_queue_timeout_seconds
        )

    def _claim_due(self, now: datetime, limit: int) -> List[RefreshJob]:
        due_filter = (
            ScheduledRefreshJob.enabled.is_(True),
            or_(ScheduledRefreshJob.next_run_at.is_(None), ScheduledRefreshJob.next_run_at <= now),
        )
        claimed: List[RefreshJob] = []
        with SessionLocal() as db:
            # Only schedules whose token still exists and whose owner is active.
            rows = (
                db.execute(
                    select(ScheduledRefreshJob)
                    .join(AccessToken, AccessToken.token == ScheduledRefreshJob.access_token)
                    .join(User, User.id == ScheduledRefreshJob.user_id)
                    .where(*due_filter, User.is_active.isnot(False))
                    .order_by(ScheduledRefreshJob.next_run_at)
                    .limit(limit * 2)
                )
                .scalars()
                .all()
            )
            snapshots = [RefreshJob.from_row(row) for row in rows if row.access_token not in self._in_flight]
            for job in snapshots:
                result = db.execute(
                    update(ScheduledRefreshJob)
                    .where(ScheduledRefreshJob.access_token == job.access_token, *due_filter)
                    .values(next_run_at=now + self._claim_window())
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    claimed.append(job)
                if len(claimed) >= limit:
                    break
            db.commit()
        return claimed

    # ── Due logic ─────────────────────────────────────────────────────────────

    @staticmethod
    def _next_run(last_refreshed: Optional[datetime], interval: int, now: datetime) -> datetime:
        last = as_utc(last_refreshed)
        if last is None:
            return now
        return max(now, last + timedelta(seconds=interval))

    def _is_due(self, job: RefreshJob, now: datetime) -> bool:
        """Check if a job is due for refresh, considering backoff."""
        if not job.last_refreshed:
            return True  # Never refreshed — run immediately
        effective_interval = self._effective_interval(job)
        elapsed = (now - as_utc(job.last_refreshed)).total_seconds()
        return elapsed >= effective_interval

    def _effective_interval(self, job: RefreshJob) -> float:
        """Compute effective interval with exponential backoff on failure."""
        if job.consecutive_failures == 0:
            return job.interval_seconds
        backoff = job.interval_seconds * (2**job.consecutive_failures)
        return min(backoff, self._max_backoff)

    # ── Running one refresh ───────────────────────────────────────────────────

    def _save(self, job: RefreshJob) -> None:
        """Write a refresh's outcome to its row, if the row still exists (never re-inserts it)."""
        with SessionLocal() as db:
            db.execute(
                update(ScheduledRefreshJob)
                .where(ScheduledRefreshJob.access_token == job.access_token)
                .values(
                    enabled=job.enabled,
                    last_refreshed=job.last_refreshed,
                    last_error=job.last_error,
                    consecutive_failures=job.consecutive_failures,
                    next_run_at=job.next_run_at,
                    disabled_reason=job.disabled_reason,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()

    async def _notify(self, job: RefreshJob, data: Dict[str, Any]) -> None:
        if self._webhook is None:
            return
        try:
            await self._webhook(job.access_token, job.user_id, data)
        except Exception:
            logger.exception("Refresh webhook callback failed for %s", token_fingerprint(job.access_token))

    async def _execute_job(self, job: RefreshJob, semaphore: Optional[asyncio.Semaphore] = None) -> None:
        """Run one refresh and record its outcome on the row."""
        token_label = token_fingerprint(job.access_token)
        async with semaphore or nullcontext():
            now = datetime.now(timezone.utc)
            try:
                logger.info("Refreshing data for %s...", token_label)
                data = await self._fetch(job.access_token, job.user_id)
            except asyncio.CancelledError:
                # Shutdown: let the next tick (in whichever process leads then) pick it up.
                job.next_run_at = datetime.now(timezone.utc)
                await asyncio.shield(asyncio.to_thread(self._save, job))
                raise
            except RefreshTargetMissing:
                logger.info("Access token of a refresh schedule is gone; removing the schedule (%s)", token_label)
                await asyncio.to_thread(self.unschedule, job.access_token)
                return
            except RefreshOwnerInactive:
                # Not a failure: it resumes if the owner is reactivated.
                job.next_run_at = now + timedelta(seconds=job.interval_seconds)
                await asyncio.to_thread(self._save, job)
                return
            except ConcurrentAccessError:
                # Another connection for this credential is running: not a failure.
                job.next_run_at = now + timedelta(seconds=_BUSY_RETRY_SECONDS)
                await asyncio.to_thread(self._save, job)
                return
            except (AuthenticationError, MFARequiredError) as exc:
                # Credentials rejected (MFARejectedError included) or MFA needed:
                # retrying cannot help and would text the user a code each time.
                job.enabled = False
                job.disabled_reason = REASON_NEEDS_REAUTH
                job.last_error = exc.message
                job.last_refreshed = now
                job.next_run_at = None
                await asyncio.to_thread(self._save, job)
                logger.warning("Refresh for %s needs the user to sign in again; schedule disabled", token_label)
                await self._notify(
                    job,
                    {
                        "__refresh_failed__": True,
                        "reason": REASON_NEEDS_REAUTH,
                        "error": exc.message,
                        "consecutive_failures": job.consecutive_failures,
                    },
                )
                return
            except Exception as exc:
                job.consecutive_failures += 1
                job.last_error = exc.message if isinstance(exc, PlaidifyError) else str(exc)
                job.last_refreshed = now
                logger.warning(
                    "Refresh failed for %s (attempt %d): %s",
                    token_label,
                    job.consecutive_failures,
                    type(exc).__name__,
                )
                if job.consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                    job.enabled = False
                    job.disabled_reason = REASON_MAX_FAILURES
                    job.next_run_at = None
                    await asyncio.to_thread(self._save, job)
                    logger.error(
                        "Disabled refresh for %s after %d consecutive failures",
                        token_label,
                        job.consecutive_failures,
                    )
                    await self._notify(
                        job,
                        {
                            "__refresh_failed__": True,
                            "reason": REASON_MAX_FAILURES,
                            "error": job.last_error,
                            "consecutive_failures": job.consecutive_failures,
                        },
                    )
                else:
                    job.next_run_at = now + timedelta(seconds=self._effective_interval(job))
                    await asyncio.to_thread(self._save, job)
                return

            job.last_refreshed = now
            job.last_error = None
            job.consecutive_failures = 0
            job.next_run_at = now + timedelta(seconds=job.interval_seconds)
            await asyncio.to_thread(self._save, job)
            logger.info("Successfully refreshed %s", token_label)
            if data:
                await self._notify(job, data)
