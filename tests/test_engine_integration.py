"""
Integration tests for the Playwright browser engine.

These tests start an internal browser fixture portal, then run the engine
against it to verify the full flow: login → extract data → logout.

Requires: playwright browsers installed (run: playwright install chromium)
"""

import multiprocessing
import os
import time

import httpx
import pytest

# Mark all tests in this module as requiring playwright
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_BROWSER_TESTS", "1") == "1",
    reason="Browser tests disabled. Set SKIP_BROWSER_TESTS=0 and install Playwright to run.",
)


# ── Test Site Fixture ─────────────────────────────────────────────────────────


def _run_test_site():
    """Run the test site in a subprocess."""
    import uvicorn

    from tests.fixtures.internal_portal import app

    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="error")


@pytest.fixture(scope="module")
def test_site():
    """Start the test site server for the duration of the test module."""
    proc = multiprocessing.Process(target=_run_test_site, daemon=True)
    proc.start()

    # Wait for server to be ready
    for _ in range(30):
        try:
            resp = httpx.get("http://127.0.0.1:18080/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("Test site did not start in time")

    yield "http://127.0.0.1:18080"
    proc.terminate()
    proc.join(timeout=5)


# ── Blueprint Loading ─────────────────────────────────────────────────────────


@pytest.fixture
def internal_bank_blueprint():
    """Load the internal_bank V2 blueprint."""
    from pathlib import Path

    from src.core.blueprint import load_blueprint

    path = Path("connectors/internal_bank.json")
    bp = load_blueprint(path)
    # Override URL to use our test port
    for step in bp.auth.steps:
        if step.url and "8080" in step.url:
            step.url = step.url.replace("8080", "18080")
    if bp.health_check:
        bp.health_check.url = bp.health_check.url.replace("8080", "18080")
    return bp


# ── Step Executor Tests ───────────────────────────────────────────────────────


class TestStepExecutor:
    @pytest.mark.asyncio
    async def test_login_flow(self, test_site, internal_bank_blueprint):
        """Test that the step executor can log into the test site."""
        from playwright.async_api import async_playwright

        from src.core.step_executor import StepExecutor

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            variables = {"username": "test_user", "password": "test_pass"}
            executor = StepExecutor(page, variables)

            await executor.execute_steps(internal_bank_blueprint.auth.steps, context="auth")

            # Should be on the dashboard
            title = await page.title()
            assert "Dashboard" in title

            await context.close()
            await browser.close()

    @pytest.mark.asyncio
    async def test_login_invalid_creds(self, test_site, internal_bank_blueprint):
        """Invalid credentials show the blueprint's failure indicator, which the engine reports as such."""
        from playwright.async_api import async_playwright

        from src.core.engine import _await_login_outcome
        from src.core.page_checks import indicator_present
        from src.core.step_executor import StepExecutor
        from src.exceptions import AuthenticationError

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            variables = {"username": "bad_user", "password": "bad_pass"}
            executor = StepExecutor(page, variables)
            await executor.execute_steps(internal_bank_blueprint.auth.steps, context="auth")

            assert await indicator_present(page, internal_bank_blueprint.auth.failure)
            with pytest.raises(AuthenticationError):
                await _await_login_outcome(page, internal_bank_blueprint, "internal_bank")

            await context.close()
            await browser.close()


# ── Data Extractor Tests ──────────────────────────────────────────────────────


class TestDataExtraction:
    @pytest.mark.asyncio
    async def test_extract_account_data(self, test_site, internal_bank_blueprint):
        """Test that we can extract structured data from the dashboard."""
        from playwright.async_api import async_playwright

        from src.core.data_extractor import DataExtractor
        from src.core.step_executor import StepExecutor

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            # Login first
            variables = {"username": "test_user", "password": "test_pass"}
            executor = StepExecutor(page, variables)
            await executor.execute_steps(internal_bank_blueprint.auth.steps, context="auth")

            # Extract data
            extractor = DataExtractor(page)
            data = await extractor.extract(internal_bank_blueprint.extract, site="internal_bank")

            # Verify scalar fields
            assert data["current_bill"] == 142.57
            assert "Active" in str(data["account_status"])
            assert data["customer_name"] == "Alex Johnson"
            assert data["customer_email"] == "alex.johnson@email.com"

            # Verify usage history list
            assert isinstance(data["usage_history"], list)
            assert len(data["usage_history"]) == 6
            assert data["usage_history"][0]["month"] == "March 2026"

            # Verify payments list
            assert isinstance(data["payments"], list)
            assert len(data["payments"]) == 4
            assert "February Bill" in data["payments"][0]["description"]

            await context.close()
            await browser.close()

    @pytest.mark.asyncio
    async def test_extract_specific_fields(self, test_site, internal_bank_blueprint):
        """Test extracting only a subset of fields."""
        from playwright.async_api import async_playwright

        from src.core.data_extractor import DataExtractor
        from src.core.step_executor import StepExecutor

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            variables = {"username": "test_user", "password": "test_pass"}
            executor = StepExecutor(page, variables)
            await executor.execute_steps(internal_bank_blueprint.auth.steps, context="auth")

            # Extract only current_bill
            limited_fields = {k: v for k, v in internal_bank_blueprint.extract.items() if k == "current_bill"}
            extractor = DataExtractor(page)
            data = await extractor.extract(limited_fields, site="internal_bank")

            assert "current_bill" in data
            assert "usage_history" not in data

            await context.close()
            await browser.close()


# ── Browser Pool Tests ────────────────────────────────────────────────────────


class TestBrowserPool:
    @pytest.mark.asyncio
    async def test_pool_lifecycle(self):
        """Test that a browser pool can start, acquire, release, and stop."""
        from src.core.browser_pool import BrowserPool

        pool = BrowserPool()
        await pool.start()

        assert pool.active_count == 0
        assert pool.available_slots > 0

        ctx = await pool.acquire("test_session")
        assert pool.active_count == 1

        page = await ctx.context.new_page()
        await page.goto("about:blank")
        await page.close()

        await pool.release("test_session")
        assert pool.active_count == 0

        await pool.stop()

    @pytest.mark.asyncio
    async def test_pool_context_manager(self):
        """Test the async context manager interface."""
        from src.core.browser_pool import BrowserPool

        async with BrowserPool() as pool:
            assert pool.active_count == 0
            await pool.acquire("cm_session")
            assert pool.active_count == 1
            await pool.release("cm_session")

    @pytest.mark.asyncio
    async def test_pool_reuse_session(self):
        """Test that acquiring the same session twice returns the same context."""
        from src.core.browser_pool import BrowserPool

        async with BrowserPool() as pool:
            ctx1 = await pool.acquire("reuse_session")
            ctx2 = await pool.acquire("reuse_session")
            assert ctx1.context is ctx2.context
            await pool.release("reuse_session")


# ── Full Engine Integration ───────────────────────────────────────────────────


@pytest.fixture
def engine_env(test_site, monkeypatch, tmp_path):
    """The engine pointed at a copy of internal_bank on the test port, with internal connectors allowed."""
    import json
    from pathlib import Path

    from src.core import engine

    text = Path("connectors/internal_bank.json").read_text().replace("localhost:8080", "127.0.0.1:18080")
    (tmp_path / "internal_bank.json").write_text(text)
    json.loads(text)  # still valid JSON

    monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
    monkeypatch.setattr(engine.settings, "engine_allow_internal_connectors", True)
    engine.get_site_rate_limiter()._windows.clear()
    yield engine
    engine.get_site_rate_limiter()._windows.clear()


async def _until(predicate, timeout=15.0):
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


class TestEngineIntegration:
    @pytest.mark.asyncio
    async def test_full_connect_flow(self, engine_env):
        """Test the full connect_to_site function against the test site."""
        from src.core.browser_pool import shutdown_browser_pool

        try:
            result = await engine_env.connect_to_site(
                site="internal_bank",
                username="test_user",
                password="test_pass",
            )

            assert result["status"] == "connected"
            assert result["data"]["current_bill"] == 142.57
            assert isinstance(result["data"]["usage_history"], list)
            assert len(result["data"]["usage_history"]) == 6
            metadata = result["metadata"]
            # Storage learns which values to protect; the engine never logs them.
            assert metadata["sensitive_fields"] == ["account_number"]
            assert metadata["read_only_policy"]["blocked_action_count"] == 0
        finally:
            await shutdown_browser_pool()

    @pytest.mark.asyncio
    async def test_wrong_password_is_invalid_credentials_not_a_timeout(self, engine_env):
        import time

        from src.core.browser_pool import shutdown_browser_pool
        from src.exceptions import AuthenticationError

        started = time.monotonic()
        try:
            with pytest.raises(AuthenticationError):
                await engine_env.connect_to_site(site="internal_bank", username="test_user", password="wrong")
        finally:
            await shutdown_browser_pool()
        assert time.monotonic() - started < 20

    @pytest.mark.asyncio
    async def test_mfa_wrong_code_then_right_code(self, engine_env):
        import asyncio

        from src.core.browser_pool import shutdown_browser_pool
        from src.core.mfa_manager import get_mfa_manager
        from tests.fixtures.internal_portal import MFA_CODE

        manager = get_mfa_manager()
        session_id = "integration-mfa-1"

        async def awaiting(with_error=False):
            session = await manager.get_session(session_id)
            return session is not None and session.awaiting_code and (not with_error or "mfa_error" in session.metadata)

        task = asyncio.create_task(
            engine_env.connect_to_site(
                site="internal_bank", username="fixture_mfa", password="test_pass", session_id=session_id
            )
        )
        try:
            await _until(awaiting)
            assert await manager.submit_code(session_id, "000000")
            await _until(lambda: awaiting(with_error=True))
            assert await manager.submit_code(session_id, MFA_CODE)
            result = await asyncio.wait_for(task, timeout=60)
            assert result["status"] == "connected"
            assert result["data"]["current_bill"] == 142.57
            assert await manager.get_session(session_id) is None
        finally:
            if not task.done():
                task.cancel()
            await shutdown_browser_pool()

    @pytest.mark.asyncio
    async def test_unanswered_mfa_is_an_mfa_timeout(self, engine_env, monkeypatch):
        from src.core.browser_pool import shutdown_browser_pool
        from src.core.mfa_manager import MFATimeoutError

        monkeypatch.setattr(engine_env.settings, "mfa_timeout_seconds", 2)
        try:
            with pytest.raises(MFATimeoutError):
                await engine_env.connect_to_site(site="internal_bank", username="fixture_mfa", password="test_pass")
        finally:
            await shutdown_browser_pool()


class TestBrowserGuards:
    @pytest.mark.asyncio
    async def test_private_addresses_are_refused_per_request(self, test_site):
        from src.core.browser_pool import BrowserPool
        from src.core.network_policy import AddressPolicy
        from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy

        async with BrowserPool() as pool:
            policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.AUTH)
            lease = await pool.acquire(
                "private",
                read_only_policy=policy,
                address_policy=AddressPolicy(block_private=True, allow_loopback=False),
            )
            page = await lease.context.new_page()
            with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
                await page.goto("http://127.0.0.1:18080/login")
            assert policy.blocked_actions[-1].action == "network"
            await pool.release("private")

    @pytest.mark.asyncio
    async def test_redirect_hops_are_caught(self, test_site):
        import asyncio

        from src.core.browser_pool import BrowserPool
        from src.core.network_policy import AddressPolicy
        from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy

        class RefuseLocalhostName(AddressPolicy):
            async def host_block_reason(self, host):
                return "private address" if host == "localhost" else None

        async with BrowserPool() as pool:
            policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
            lease = await pool.acquire("redirect", read_only_policy=policy, address_policy=RefuseLocalhostName())
            page = await lease.context.new_page()
            await page.route(
                "http://127.0.0.1:18080/hop",
                lambda route: route.fulfill(status=302, headers={"Location": "http://localhost:18080/login"}),
            )
            try:
                await page.goto("http://127.0.0.1:18080/hop", timeout=5000)
            except Exception:
                pass
            await _until(lambda: asyncio.sleep(0, result=lease.network_violation is not None), timeout=5)
            assert "localhost:18080/login" in lease.network_violation
            await pool.release("redirect")

    @pytest.mark.asyncio
    async def test_goto_file_is_refused_in_cleanup(self, test_site):
        from src.core.blueprint import BlueprintStep, StepAction
        from src.core.browser_pool import BrowserPool
        from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
        from src.core.step_executor import StepExecutor
        from src.exceptions import ReadOnlyPolicyViolationError

        async with BrowserPool() as pool:
            policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.CLEANUP)
            lease = await pool.acquire("file", read_only_policy=policy)
            page = await lease.context.new_page()
            executor = StepExecutor(page, {}, read_only_policy=policy)
            with pytest.raises(ReadOnlyPolicyViolationError):
                await executor.execute_steps(
                    [BlueprintStep.model_construct(action=StepAction.GOTO, url="file:///etc/hosts")], context="cleanup"
                )
            assert page.url == "about:blank"
            await pool.release("file")

    @pytest.mark.asyncio
    async def test_steps_run_inside_an_iframe(self):
        from src.core.blueprint import BlueprintStep
        from src.core.browser_pool import BrowserPool
        from src.core.step_executor import StepExecutor

        async with BrowserPool() as pool:
            lease = await pool.acquire("iframe")
            page = await lease.context.new_page()
            await page.set_content(
                "<iframe id='login' srcdoc=\"<input id='u'><button id='go' "
                "onclick='document.body.dataset.done=document.getElementById(&quot;u&quot;).value'>Go</button>\"></iframe>"
            )
            step = BlueprintStep.model_validate(
                {
                    "action": "iframe",
                    "iframe_selector": "#login",
                    "steps": [
                        {"action": "fill", "selector": "#u", "value": "{{username}}"},
                        {"action": "click", "selector": "#go"},
                    ],
                }
            )
            await StepExecutor(page, {"username": "alice"}).execute_steps([step])
            frame = page.frame_locator("#login")
            assert await frame.locator("body").get_attribute("data-done") == "alice"
            await pool.release("iframe")

    @pytest.mark.asyncio
    async def test_pool_recovers_after_the_browser_dies(self):
        import asyncio

        from src.core import browser_pool as bpm

        try:
            pool = await bpm.get_browser_pool()
            await pool.release((await pool.acquire("warm")).session_id)
            await pool._browser.close()  # what an OOM-killed Chromium looks like to the pool
            assert not pool.is_healthy

            same_pool = await bpm.get_browser_pool()
            assert same_pool is pool and pool.is_healthy
            for i in range(pool._max_size + 2):
                lease = await asyncio.wait_for(pool.acquire(f"after-{i}"), timeout=10)
                page = await lease.context.new_page()
                await page.set_content("<p>ok</p>")
                await pool.release(f"after-{i}")
            assert pool._semaphore._value == pool._max_size
        finally:
            await bpm.shutdown_browser_pool()
