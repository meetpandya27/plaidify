import asyncio
import os

import pytest

from src.core.browser_pool import BrowserPool, PooledContext
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy


class FakeDownload:
    def __init__(self, *, url: str = "https://example.test/statement.pdf", filename: str = "statement.pdf"):
        self.url = url
        self.suggested_filename = filename
        self.cancelled = False
        self.saved_path = None

    async def cancel(self) -> None:
        self.cancelled = True

    async def save_as(self, path: str) -> None:
        self.saved_path = path
        with open(path, "wb") as handle:
            handle.write(b"pdf-bytes")


class FakePage:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_read_phase_download_is_captured(tmp_path):
    pool = BrowserPool()
    pool._download_root = str(tmp_path)
    pool._allow_read_downloads = True

    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pooled = PooledContext(
        context=None,
        session_id="session-download",
        read_only_policy=policy,
        download_dir=str(tmp_path),
    )
    download = FakeDownload()

    await pool._handle_download(download, pooled)

    assert download.cancelled is False
    assert len(pooled.downloads) == 1
    assert pooled.downloads[0]["filename"] == "statement.pdf"
    assert pooled.downloads[0]["size_bytes"] == len(b"pdf-bytes")
    assert os.path.exists(download.saved_path)


@pytest.mark.asyncio
async def test_non_read_phase_download_is_blocked(tmp_path):
    pool = BrowserPool()
    pool._download_root = str(tmp_path)

    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.AUTH)
    pooled = PooledContext(
        context=None,
        session_id="session-auth",
        read_only_policy=policy,
        download_dir=str(tmp_path),
    )
    download = FakeDownload(filename="auth.pdf")

    await pool._handle_download(download, pooled)

    assert download.cancelled is True
    assert pooled.downloads == []
    assert policy.blocked_actions[-1].action == "download"


@pytest.mark.asyncio
async def test_extra_page_is_closed_in_read_phase():
    pool = BrowserPool()
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pooled = PooledContext(context=None, session_id="session-popup", read_only_policy=policy)
    page = FakePage()

    await pool._close_extra_page(page, pooled)

    assert page.closed is True
    assert policy.blocked_actions[-1].action == "popup"


def test_contexts_reject_bad_tls_certificates():
    # Credentials are typed into whatever answers; a spoofed certificate must stop the run.
    assert BrowserPool()._build_context_options()["ignore_https_errors"] is False


_PLAYWRIGHT_FILL_ERROR = (
    "Page.fill: Timeout 5000ms exceeded.\n"
    "Call log:\n"
    '  - waiting for locator("#password")\n'
    '  -   locator resolved to <input readonly id="password" type="password"/>\n'
    '  - fill("S3cretPassw0rd!")\n'
)


def test_browser_error_description_drops_typed_values():
    from src.core.step_executor import describe_browser_error

    assert describe_browser_error(Exception(_PLAYWRIGHT_FILL_ERROR)) == "Page.fill: Timeout 5000ms exceeded."
    assert "123456" not in describe_browser_error(Exception('Page.fill: failed fill("123456")'))
    assert describe_browser_error(Exception("")) == "Exception"


@pytest.mark.asyncio
async def test_failed_fill_never_carries_the_password():
    from src.core.blueprint import BlueprintStep, StepAction
    from src.core.step_executor import StepExecutor
    from src.exceptions import ConnectionFailedError

    class RefusingPage:
        async def wait_for_selector(self, selector, **kwargs):
            return None

        async def fill(self, selector, value, **kwargs):
            raise Exception(_PLAYWRIGHT_FILL_ERROR.replace("S3cretPassw0rd!", value))

    executor = StepExecutor(RefusingPage(), {"password": "S3cretPassw0rd!"}, site="bank")
    step = BlueprintStep(action=StepAction.FILL, selector="#password", value="{{password}}")

    with pytest.raises(ConnectionFailedError) as caught:
        await executor.execute_steps([step], context="auth")

    assert "S3cretPassw0rd!" not in str(caught.value)
    assert "Timeout 5000ms exceeded" in str(caught.value)
    assert caught.value.site == "bank"


# ── Pool housekeeping (ENG-05, ENG-11) ────────────────────────────────────────


class FakeContext:
    def __init__(self):
        self.closed = False
        self.pages = []
        self.routes = []
        self.handlers = {}

    def set_default_navigation_timeout(self, timeout):
        pass

    def set_default_timeout(self, timeout):
        pass

    async def route(self, pattern, handler):
        self.routes.append(handler)

    async def route_web_socket(self, pattern, handler):
        pass

    def on(self, event, handler):
        self.handlers[event] = handler

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.connected = True
        self.contexts = []
        self.options = []

    def is_connected(self):
        return self.connected

    async def new_context(self, **options):
        self.options.append(options)
        if self.fail:
            raise RuntimeError("bad proxy config")
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.connected = False

    def on(self, event, handler):
        pass


def _pool(tmp_path, browser):
    pool = BrowserPool()
    pool._download_root = str(tmp_path)
    pool._browser = browser
    pool._running = True
    return pool


@pytest.mark.asyncio
async def test_failed_context_creation_gives_the_slot_back(tmp_path):
    pool = _pool(tmp_path, FakeBrowser(fail=True))
    for i in range(pool._max_size + 2):
        with pytest.raises(RuntimeError):
            await pool.acquire(f"s{i}")
    assert pool._semaphore._value == pool._max_size

    pool._browser = FakeBrowser()
    lease = await asyncio.wait_for(pool.acquire("healthy"), timeout=1)
    assert lease.context is pool._browser.contexts[0]


@pytest.mark.asyncio
async def test_release_is_idempotent(tmp_path):
    pool = _pool(tmp_path, FakeBrowser())
    lease = await pool.acquire("run-1")
    await pool.release("run-1")
    await pool.release("run-1")
    await pool.release("never-acquired")
    assert lease.context.closed
    assert pool._semaphore._value == pool._max_size


@pytest.mark.asyncio
async def test_shared_session_closes_after_the_last_holder(tmp_path):
    pool = _pool(tmp_path, FakeBrowser())
    first = await pool.acquire("s")
    second = await pool.acquire("s")
    assert first is second
    await pool.release("s")
    assert not first.context.closed
    await pool.release("s")
    assert first.context.closed
    assert pool._semaphore._value == pool._max_size


@pytest.mark.asyncio
async def test_reaper_leaves_active_runs_alone_and_closes_leaks(tmp_path):
    pool = _pool(tmp_path, FakeBrowser())
    active = await pool.acquire("active")
    leaked = await pool.acquire("leaked")
    leaked.last_used -= pool._max_idle_seconds + 1

    await pool._cleanup_idle()

    assert not active.context.closed
    assert leaked.context.closed
    # The engine's own release of the reaped lease must not double count.
    await pool.release("leaked")
    await pool.release("active")
    assert pool._semaphore._value == pool._max_size


def test_reaper_outlasts_a_whole_run_including_mfa():
    from src.config import get_settings

    settings = get_settings()
    assert BrowserPool()._max_idle_seconds > settings.engine_timeout_seconds + settings.mfa_timeout_seconds


@pytest.mark.asyncio
async def test_disconnected_browser_is_relaunched(tmp_path, monkeypatch):
    pool = _pool(tmp_path, FakeBrowser())
    pool._browser.connected = False
    assert not pool.is_healthy

    replacement = FakeBrowser()

    async def launch():
        return replacement

    monkeypatch.setattr(pool, "_launch", launch)
    lease = await pool.acquire("after-crash")
    assert pool._browser is replacement and pool.is_healthy
    assert lease.context is replacement.contexts[0]


@pytest.mark.asyncio
async def test_concurrent_first_callers_start_one_pool(monkeypatch):
    from src.core import browser_pool as bpm

    starts = []

    async def fake_start(self):
        starts.append(self)
        await asyncio.sleep(0.05)
        self._running = True
        self._browser = FakeBrowser()

    monkeypatch.setattr(bpm.BrowserPool, "start", fake_start)
    monkeypatch.setattr(bpm, "_pool", None)
    pools = await asyncio.gather(*(bpm.get_browser_pool() for _ in range(5)))
    assert len(starts) == 1
    assert len({id(p) for p in pools}) == 1
    monkeypatch.setattr(bpm, "_pool", None)


@pytest.mark.asyncio
async def test_failed_start_stops_the_driver(monkeypatch):
    from src.core import browser_pool as bpm

    class Chromium:
        async def launch(self, **kwargs):
            raise RuntimeError("launch failed")

    class Driver:
        chromium = Chromium()
        stopped = False

        async def stop(self):
            Driver.stopped = True

    class Starter:
        async def start(self):
            return Driver()

    monkeypatch.setattr(bpm, "async_playwright", lambda: Starter())
    bpm._browser_breaker.reset()
    pool = BrowserPool()
    with pytest.raises(RuntimeError, match="launch failed"):
        await pool.start()
    bpm._browser_breaker.reset()
    assert Driver.stopped
    assert pool._playwright is None and not pool._running


@pytest.mark.asyncio
async def test_chromium_sandbox_is_requested_and_failures_explain_themselves(monkeypatch):
    seen = {}

    class Chromium:
        async def launch(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("Failed to launch: No usable sandbox! Update your kernel")

    pool = BrowserPool()
    pool._playwright = type("Driver", (), {"chromium": Chromium()})()
    with pytest.raises(RuntimeError, match="BROWSER_CHROMIUM_SANDBOX"):
        await pool._launch()
    assert seen["chromium_sandbox"] is True


def test_stylesheets_are_not_blocked_and_service_workers_are():
    from src.core.browser_pool import BLOCKED_RESOURCE_TYPES

    assert "stylesheet" not in BLOCKED_RESOURCE_TYPES
    assert BrowserPool()._build_context_options()["service_workers"] == "block"


# ── Request policy on every request (SEC-03, ENG-03) ─────────────────────────


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.outcome = None

    async def abort(self, error_code=None):
        self.outcome = ("abort", error_code)

    async def continue_(self):
        self.outcome = ("continue", None)


class FakeRouteRequest:
    def __init__(self, url, method="GET", *, navigation=False, resource_type="document", headers=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self._navigation = navigation
        self.redirected_from = None
        self.frame = type("Frame", (), {"parent_frame": None})()

    def is_navigation_request(self):
        return self._navigation


async def _route_through(pool, lease, request):
    route = FakeRoute(request)
    await lease.context.routes[0](route)
    return route.outcome


@pytest.mark.asyncio
async def test_every_request_is_checked_against_the_address_policy(tmp_path):
    from src.core.network_policy import AddressPolicy

    pool = _pool(tmp_path, FakeBrowser())
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    lease = await pool.acquire("s", read_only_policy=policy, address_policy=AddressPolicy(block_private=True))

    assert await _route_through(
        pool, lease, FakeRouteRequest("http://169.254.169.254/latest/meta-data/", resource_type="xhr")
    ) == ("abort", "blockedbyclient")
    assert await _route_through(pool, lease, FakeRouteRequest("http://10.0.0.8/admin", resource_type="image")) == (
        "abort",
        "blockedbyclient",
    )
    assert await _route_through(
        pool, lease, FakeRouteRequest("https://93.184.216.34/app.js", resource_type="script")
    ) == ("continue", None)
    assert [b.action for b in policy.blocked_actions] == ["network", "network"]


@pytest.mark.asyncio
async def test_redirect_hops_to_private_addresses_fail_the_run(tmp_path):
    from src.core.network_policy import AddressPolicy

    pool = _pool(tmp_path, FakeBrowser())
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    lease = await pool.acquire("s", read_only_policy=policy, address_policy=AddressPolicy(block_private=True))
    closed = []

    class Page:
        async def close(self):
            closed.append(True)

    lease.context.pages = [Page()]
    hop = FakeRouteRequest("http://169.254.169.254/latest/meta-data/iam/", navigation=True)
    hop.redirected_from = FakeRouteRequest("https://93.184.216.34/login", navigation=True)

    await pool._check_redirect(hop, lease)
    await asyncio.sleep(0)

    assert lease.network_violation and "169.254.169.254" in lease.network_violation
    assert closed == [True]
    assert policy.blocked_actions[-1].action == "redirect"


# ── Downloads come back with the result (ENG-21) ─────────────────────────────


@pytest.mark.asyncio
async def test_downloads_are_returned_before_the_files_are_deleted(tmp_path):
    import base64

    pool = BrowserPool()
    pool._download_root = str(tmp_path)
    pool._allow_read_downloads = True
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pooled = PooledContext(context=None, session_id="dl", read_only_policy=policy, download_dir=str(tmp_path))

    pooled.spawn(pool._handle_download(FakeDownload(url="https://bank.test/s.pdf?token=abc"), pooled), download=True)
    small = await pool.collect_downloads(pooled, max_bytes=1024)
    assert small == [
        {
            "filename": "statement.pdf",
            "size_bytes": len(b"pdf-bytes"),
            "url": "https://bank.test/s.pdf",
            "content_base64": base64.b64encode(b"pdf-bytes").decode(),
        }
    ]
    too_big = await pool.collect_downloads(pooled, max_bytes=2)
    assert too_big[0]["omitted"] == "too_large" and "content_base64" not in too_big[0]
