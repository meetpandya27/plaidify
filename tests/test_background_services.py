"""Background services: where they run, the lease that keeps them to one process,
the web lifespan that starts them at boot, and the executor's entry point."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src import access_job_worker, background_services, session_store
from src.background_services import Lease, services_allowed_here
from src.main import app
from tests.jobs_support import redis_mode_fixture, requires_redis  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_role(monkeypatch):
    monkeypatch.setattr(background_services, "_process_role", "web")
    background_services._LOCAL_LEASES.clear()
    yield
    background_services._LOCAL_LEASES.clear()


class TestWhereTheyRun:
    def test_inprocess_mode_runs_them_in_web_workers(self, monkeypatch):
        monkeypatch.setattr(background_services.settings, "access_job_execution_mode", "inprocess")
        assert services_allowed_here() is True

    def test_redis_worker_mode_runs_them_in_the_executor_only(self, monkeypatch):
        monkeypatch.setattr(background_services.settings, "access_job_execution_mode", "redis-worker")
        assert services_allowed_here() is False
        background_services.set_process_role("executor")
        assert services_allowed_here() is True

    def test_they_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(background_services.settings, "background_services_enabled", False)
        assert services_allowed_here() is False


class TestLease:
    @pytest.mark.asyncio
    async def test_one_holder_at_a_time_in_memory(self):
        first, second = Lease("svc", 30), Lease("svc", 30)
        assert await first.acquire_or_renew() is True
        assert await second.acquire_or_renew() is False
        await first.release()
        assert await second.acquire_or_renew() is True
        await second.release()

    @requires_redis
    @pytest.mark.asyncio
    async def test_one_holder_at_a_time_on_redis_and_takeover_after_expiry(self, redis_mode):
        first, second = Lease("svc", 1), Lease("svc", 1)
        assert await first.acquire_or_renew() is True
        assert await second.acquire_or_renew() is False
        assert await first.acquire_or_renew() is True  # renewal
        # The holder dies (no renewal): the lease lapses and another process takes over.
        await asyncio.sleep(1.2)
        assert await second.acquire_or_renew() is True
        assert await first.acquire_or_renew() is False
        await second.release()
        assert await session_store.async_redis().get(second.key) is None


def test_the_web_app_starts_and_stops_them_at_boot(monkeypatch):
    """JOB-07: started at boot through the app lifespan (the access-jobs router contributes it)."""
    monkeypatch.setattr(background_services.settings, "access_job_execution_mode", "inprocess")
    started = []
    monkeypatch.setattr("src.routers.refresh.RefreshScheduler.start", lambda self: started.append("scheduler") or True)
    with TestClient(app):
        assert started == ["scheduler"]
        assert [loop.name for loop in background_services._loops] == ["webhook-outbox", "access-job-reaper"]
        assert all(loop.running for loop in background_services._loops)
    assert background_services._loops == []


def test_the_web_app_refuses_to_boot_on_split_state(monkeypatch):
    """JOB-15: several workers outside development without Redis fail fast at startup."""
    monkeypatch.setattr(session_store.settings, "redis_url", None)
    monkeypatch.setattr(session_store.settings, "env", "staging")
    monkeypatch.setenv("GUNICORN_WORKERS", "2")
    with pytest.raises(RuntimeError, match="REDIS_URL is required"):
        with TestClient(app):
            pass


@pytest.mark.asyncio
async def test_the_executor_wires_tracing_metrics_services_and_drain(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        "src.tracing.configure_tracer_provider",
        lambda settings, service_name=None: calls.setdefault("tracer", service_name),
    )
    monkeypatch.setattr("src.metrics.start_worker_metrics_server", lambda port: calls.setdefault("metrics", port))
    monkeypatch.setattr("src.background_services.start_background_services", AsyncMock(return_value=True))
    monkeypatch.setattr("src.background_services.stop_background_services", AsyncMock())
    monkeypatch.setattr("src.core.browser_pool.shutdown_browser_pool", AsyncMock())

    async def fake_worker(*, stop_event):
        calls["stop_event"] = stop_event
        await stop_event.wait()

    monkeypatch.setattr(access_job_worker, "run_access_job_worker", fake_worker)
    stop = asyncio.Event()
    runner = asyncio.create_task(access_job_worker._main(stop))
    await asyncio.sleep(0.05)
    stop.set()  # what SIGTERM does
    await asyncio.wait_for(runner, timeout=5)

    assert calls["tracer"] == "plaidify-access-executor"
    assert calls["metrics"] == access_job_worker.settings.access_worker_metrics_port
    assert calls["stop_event"] is stop
    assert background_services.process_role() == "executor"


@pytest.mark.asyncio
async def test_the_browser_pool_reports_its_capacity(monkeypatch):
    """Ops request: the saturation alert divides by the pool's capacity."""
    from src.core import browser_pool

    recorded = []
    monkeypatch.setattr(browser_pool.metrics, "set_browser_pool_capacity", recorded.append)
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr(browser_pool, "async_playwright", lambda: starter)
    browser = MagicMock()
    browser.close = AsyncMock()
    monkeypatch.setattr(browser_pool._browser_breaker, "call", AsyncMock(return_value=browser))

    pool = browser_pool.BrowserPool()
    await pool.start()
    await pool.stop()
    assert recorded == [pool._max_size, 0]
