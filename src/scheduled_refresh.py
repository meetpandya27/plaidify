"""
Scheduled data refresh worker for Plaidify.

Provides background refresh of linked account data on configurable intervals.
Uses asyncio for lightweight scheduling without external dependencies.

Usage:
    # Start the refresh worker alongside the FastAPI app:
    from src.scheduled_refresh import RefreshScheduler
    scheduler = RefreshScheduler(interval_seconds=3600)
    scheduler.start()

    # Register a token for periodic refresh:
    scheduler.schedule(access_token="acc-xxx", user_id=1)

    # Stop gracefully:
    await scheduler.stop()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional, Set

logger = logging.getLogger("plaidify.scheduler")


@dataclass
class RefreshJob:
    """A single scheduled refresh job."""

    access_token: str
    user_id: int
    interval_seconds: int
    last_refreshed: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    enabled: bool = True


class RefreshScheduler:
    """Background scheduler that periodically re-fetches data for linked accounts.

    The scheduler maintains an in-memory registry of access tokens to refresh.
    Each tick, it iterates jobs whose interval has elapsed and invokes the
    provided ``fetch_callback`` (which should call the same logic as GET /fetch_data).

    Exponential backoff is applied on failure: base interval * 2^(failures), capped at 24 h.

    Args:
        fetch_callback: Async callable ``(access_token, user_id) -> dict`` that
            performs the actual data fetch. Typically wraps the server's internal
            ``_do_fetch_data`` helper.
        webhook_callback: Optional async callable ``(access_token, user_id, data) -> None``
            fired after a successful refresh so webhooks can be dispatched.
        interval_seconds: Default refresh interval for new jobs.
        max_backoff_seconds: Upper bound for exponential backoff on failures.
    """

    _MAX_CONSECUTIVE_FAILURES = 10  # Disable job after this many failures

    def __init__(
        self,
        fetch_callback: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
        webhook_callback: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
        interval_seconds: int = 3600,
        max_backoff_seconds: int = 86400,
    ):
        self._fetch = fetch_callback
        self._webhook = webhook_callback
        self._default_interval = interval_seconds
        self._max_backoff = max_backoff_seconds
        self._jobs: Dict[str, RefreshJob] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def schedule(
        self,
        access_token: str,
        user_id: int,
        interval_seconds: Optional[int] = None,
    ) -> RefreshJob:
        """Register or update a refresh job for an access token.

        Args:
            access_token: The access token to periodically refresh.
            user_id: Owner of the token (used for auth in fetch).
            interval_seconds: Override the default interval for this job.

        Returns:
            The created or updated RefreshJob.
        """
        if access_token in self._jobs:
            job = self._jobs[access_token]
            job.interval_seconds = interval_seconds or self._default_interval
            job.enabled = True
            job.consecutive_failures = 0
            logger.info("Updated refresh job for %s (interval=%ds)", access_token[:12], job.interval_seconds)
        else:
            job = RefreshJob(
                access_token=access_token,
                user_id=user_id,
                interval_seconds=interval_seconds or self._default_interval,
            )
            self._jobs[access_token] = job
            logger.info("Scheduled refresh for %s (interval=%ds)", access_token[:12], job.interval_seconds)
        self._persist_job(job)
        return job

    def unschedule(self, access_token: str) -> bool:
        """Remove a refresh job.

        Returns:
            True if the job existed and was removed.
        """
        job = self._jobs.pop(access_token, None)
        if job:
            self._delete_persisted_job(access_token)
            logger.info("Unscheduled refresh for %s", access_token[:12])
            return True
        return False

    def list_jobs(self) -> Dict[str, Dict[str, Any]]:
        """Return a summary of all scheduled jobs."""
        return {
            token: {
                "user_id": job.user_id,
                "interval_seconds": job.interval_seconds,
                "enabled": job.enabled,
                "last_refreshed": job.last_refreshed.isoformat() if job.last_refreshed else None,
                "last_error": job.last_error,
                "consecutive_failures": job.consecutive_failures,
            }
            for token, job in self._jobs.items()
        }

    def start(self) -> None:
        """Start the background refresh loop."""
        if self._task and not self._task.done():
            logger.warning("Refresh scheduler already running")
            return
        self._stop_event.clear()
        self._task = asyncio.ensure_future(self._run_loop())
        logger.info("Refresh scheduler started (default interval=%ds)", self._default_interval)

    async def stop(self) -> None:
        """Stop the background refresh loop gracefully."""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Refresh scheduler stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main scheduler loop. Wakes every 30 seconds to check for due jobs."""
        tick_interval = 30  # seconds between checks
        while not self._stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                due_jobs = [
                    job for job in self._jobs.values()
                    if job.enabled and self._is_due(job, now)
                ]
                if due_jobs:
                    logger.debug("Found %d due refresh jobs", len(due_jobs))
                    # Run up to 5 refreshes concurrently
                    semaphore = asyncio.Semaphore(5)
                    tasks = [self._execute_job(job, semaphore) for job in due_jobs]
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                logger.exception("Error in refresh scheduler loop")
            # Wait for tick or stop
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick_interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                continue

    def _is_due(self, job: RefreshJob, now: datetime) -> bool:
        """Check if a job is due for refresh, considering backoff."""
        if not job.last_refreshed:
            return True  # Never refreshed — run immediately
        effective_interval = self._effective_interval(job)
        elapsed = (now - job.last_refreshed).total_seconds()
        return elapsed >= effective_interval

    def _effective_interval(self, job: RefreshJob) -> float:
        """Compute effective interval with exponential backoff on failure."""
        if job.consecutive_failures == 0:
            return job.interval_seconds
        backoff = job.interval_seconds * (2 ** job.consecutive_failures)
        return min(backoff, self._max_backoff)

    async def _execute_job(self, job: RefreshJob, semaphore: asyncio.Semaphore) -> None:
        """Execute a single refresh job."""
        async with semaphore:
            token_short = job.access_token[:12]
            try:
                logger.info("Refreshing data for %s...", token_short)
                data = await self._fetch(job.access_token, job.user_id)
                job.last_refreshed = datetime.now(timezone.utc)
                job.last_error = None
                job.consecutive_failures = 0
                logger.info("Successfully refreshed %s", token_short)

                # Fire webhook if callback is provided
                if self._webhook and data:
                    try:
                        await self._webhook(job.access_token, job.user_id, data)
                    except Exception:
                        logger.exception("Webhook callback failed for %s", token_short)

            except Exception as exc:
                job.consecutive_failures += 1
                job.last_error = str(exc)
                job.last_refreshed = datetime.now(timezone.utc)
                logger.warning(
                    "Refresh failed for %s (attempt %d): %s",
                    token_short,
                    job.consecutive_failures,
                    exc,
                )
                if job.consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                    job.enabled = False
                    logger.error(
                        "Disabled refresh for %s after %d consecutive failures",
                        token_short,
                        job.consecutive_failures,
                    )
            finally:
                self._persist_job(job)

    # ── DB Persistence ────────────────────────────────────────────────────────

    def load_from_db(self) -> int:
        """Load persisted jobs from the database. Returns number of jobs loaded."""
        try:
            from src.database import ScheduledRefreshJob, get_db
            db = next(get_db())
            try:
                rows = db.query(ScheduledRefreshJob).filter_by(enabled=True).all()
                for row in rows:
                    self._jobs[row.access_token] = RefreshJob(
                        access_token=row.access_token,
                        user_id=row.user_id,
                        interval_seconds=row.interval_seconds,
                        last_refreshed=row.last_refreshed,
                        last_error=row.last_error,
                        consecutive_failures=row.consecutive_failures,
                        enabled=row.enabled,
                    )
                logger.info("Loaded %d refresh jobs from database", len(rows))
                return len(rows)
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to load refresh jobs from database")
            return 0

    def _persist_job(self, job: RefreshJob) -> None:
        """Save a job's state to the database."""
        try:
            from src.database import ScheduledRefreshJob, get_db
            db = next(get_db())
            try:
                row = db.query(ScheduledRefreshJob).filter_by(
                    access_token=job.access_token
                ).first()
                if row:
                    row.interval_seconds = job.interval_seconds
                    row.enabled = job.enabled
                    row.last_refreshed = job.last_refreshed
                    row.last_error = job.last_error
                    row.consecutive_failures = job.consecutive_failures
                else:
                    row = ScheduledRefreshJob(
                        access_token=job.access_token,
                        user_id=job.user_id,
                        interval_seconds=job.interval_seconds,
                        enabled=job.enabled,
                        last_refreshed=job.last_refreshed,
                        last_error=job.last_error,
                        consecutive_failures=job.consecutive_failures,
                    )
                    db.add(row)
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to persist refresh job %s", job.access_token[:12])

    def _delete_persisted_job(self, access_token: str) -> None:
        """Remove a job from the database."""
        try:
            from src.database import ScheduledRefreshJob, get_db
            db = next(get_db())
            try:
                db.query(ScheduledRefreshJob).filter_by(
                    access_token=access_token
                ).delete()
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("Failed to delete refresh job %s", access_token[:12])
