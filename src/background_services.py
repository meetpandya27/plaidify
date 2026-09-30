"""Background services: scheduled refresh, the webhook outbox and the stuck-job reaper.

Each service runs in exactly one process at a time, under a lease (Redis
``SET NX PX``, renewed while held; without Redis a process-local lease for the
single development process). Where they may run:

* ``ACCESS_JOB_EXECUTION_MODE=inprocess`` — the web workers. Every worker
  starts them at boot; the lease holder does the work and another worker takes
  over within a lease TTL if it dies.
* ``ACCESS_JOB_EXECUTION_MODE=redis-worker`` — the access-job executor
  (``python -m src.access_job_worker``) only, which has the browser.

Row-level claims (a conditional UPDATE per refresh and per webhook delivery)
keep a unit of work from running twice even during a lease handover.

The web app starts them through :func:`web_lifespan`, which the access-jobs
router contributes to the application's lifespan.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src import session_store
from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("background_services")
settings = get_settings()

_LEASE_PREFIX = "plaidify:lease:"
_RENEW_LEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

# Process-local leases (no Redis): lease name -> holder token.
_LOCAL_LEASES: Dict[str, str] = {}

_process_role = "web"


def set_process_role(role: str) -> None:
    """Declare what this process is: "web" (an API worker) or "executor" (the access-job worker)."""
    global _process_role
    if role not in ("web", "executor"):
        raise ValueError("role must be 'web' or 'executor'")
    _process_role = role


def process_role() -> str:
    return _process_role


def services_allowed_here() -> bool:
    """Whether this process may run the background services (see the module docstring)."""
    if not settings.background_services_enabled:
        return False
    if settings.access_job_execution_mode == "redis-worker":
        return _process_role == "executor"
    return True


class Lease:
    """A named lease one process holds at a time."""

    def __init__(self, name: str, ttl_seconds: float) -> None:
        self.name = name
        self.ttl_ms = max(1000, int(ttl_seconds * 1000))
        self.token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.held = False

    @property
    def key(self) -> str:
        return f"{_LEASE_PREFIX}{self.name}"

    async def acquire_or_renew(self) -> bool:
        """Take the lease or extend it. True while this process holds it."""
        client = session_store.async_redis()
        if client is None:
            holder = _LOCAL_LEASES.setdefault(self.name, self.token)
            self.held = holder == self.token
            return self.held
        if self.held:
            if await client.eval(_RENEW_LEASE_SCRIPT, 1, self.key, self.token, self.ttl_ms):
                return True
            self.held = False
            logger.warning("Lost a background-service lease", extra={"extra_data": {"lease": self.name}})
        self.held = bool(await client.set(self.key, self.token, nx=True, px=self.ttl_ms))
        if self.held:
            logger.info("Took a background-service lease", extra={"extra_data": {"lease": self.name}})
        return self.held

    async def release(self) -> None:
        if not self.held:
            return
        self.held = False
        client = session_store.async_redis()
        if client is None:
            if _LOCAL_LEASES.get(self.name) == self.token:
                del _LOCAL_LEASES[self.name]
            return
        try:
            await client.eval(_RELEASE_LEASE_SCRIPT, 1, self.key, self.token)
        except Exception as exc:  # it expires on its own
            logger.warning(
                "Could not release a background-service lease",
                extra={"extra_data": {"lease": self.name, "error": type(exc).__name__}},
            )


class LeasedLoop:
    """Calls ``tick()`` every ``interval`` seconds while this process holds the lease ``name``."""

    def __init__(
        self,
        name: str,
        interval: float,
        tick: Callable[[], Awaitable[Any]],
        *,
        lease_ttl: Optional[float] = None,
    ) -> None:
        self.name = name
        self.interval = interval
        self._tick = tick
        self.lease = Lease(name, lease_ttl if lease_ttl is not None else max(3 * interval + 5, 30.0))
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"plaidify-{self.name}")
        return True

    async def run_once(self) -> bool:
        """One tick if this process holds (or takes) the lease. True when the tick ran."""
        if not await self.lease.acquire_or_renew():
            return False
        await self._tick()
        return True

    async def _run(self) -> None:
        assert self._stop is not None
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Background service tick failed", extra={"extra_data": {"service": self.name}})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await asyncio.shield(self.lease.release())

    async def stop(self, timeout: float = 10.0) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # pragma: no cover - logged by the loop
            pass


# ── The services ──────────────────────────────────────────────────────────────

_loops: List[LeasedLoop] = []
_WEBHOOK_PURGE_EVERY_TICKS = 720  # about hourly at the default 5 s poll interval
_outbox_ticks = 0


async def _outbox_tick() -> None:
    global _outbox_ticks
    from src.routers.webhooks import deliver_due_webhooks, purge_webhook_deliveries

    await deliver_due_webhooks()
    _outbox_ticks += 1
    if _outbox_ticks % _WEBHOOK_PURGE_EVERY_TICKS == 1:
        await asyncio.to_thread(purge_webhook_deliveries)


async def _reaper_tick() -> None:
    from src.access_jobs import _notify_reaped, reap_stuck_access_jobs

    reaped = await reap_stuck_access_jobs()
    await _notify_reaped(reaped)


async def start_background_services(*, role: Optional[str] = None) -> bool:
    """Start the scheduler, webhook outbox and reaper here, if this process may run them."""
    if role is not None:
        set_process_role(role)
    if not services_allowed_here():
        logger.info(
            "Background services run elsewhere",
            extra={"extra_data": {"role": _process_role, "mode": settings.access_job_execution_mode}},
        )
        return False
    if _loops:
        return True

    from src.routers.refresh import get_refresh_scheduler

    get_refresh_scheduler().start()
    _loops.extend(
        [
            LeasedLoop(
                "webhook-outbox",
                settings.webhook_poll_interval_seconds,
                _outbox_tick,
                lease_ttl=max(120.0, settings.webhook_timeout_seconds * 6),
            ),
            LeasedLoop("access-job-reaper", settings.access_job_reaper_interval_seconds, _reaper_tick),
        ]
    )
    for loop in _loops:
        loop.start()
    logger.info("Background services started", extra={"extra_data": {"role": _process_role}})
    return True


async def stop_background_services() -> None:
    """Stop the services this process runs and let in-flight webhook attempts finish briefly."""
    from src.routers.refresh import get_refresh_scheduler
    from src.routers.webhooks import shutdown_webhook_deliveries

    loops = list(_loops)
    _loops.clear()
    await asyncio.gather(get_refresh_scheduler().stop(), *(loop.stop() for loop in loops), return_exceptions=True)
    await shutdown_webhook_deliveries(timeout=5.0)


@asynccontextmanager
async def web_lifespan(app: Any):
    """Web-process lifespan part: fail fast on unsafe shared-state setups, run the background services."""
    session_store.check_shared_state_requirements()
    await start_background_services(role="web")
    try:
        yield
    finally:
        await stop_background_services()
