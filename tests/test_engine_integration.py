"""
Integration tests for the Playwright browser engine.

These tests start the example test site, then run the engine against it
to verify the full flow: login → extract data → logout.

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

    from example_site.server import app

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
def test_bank_blueprint():
    """Load the test_bank V2 blueprint."""
    from pathlib import Path

    from src.core.blueprint import load_blueprint

    path = Path("connectors/test_bank.json")
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
    async def test_login_flow(self, test_site, test_bank_blueprint):
        """Test that the step executor can log into the test site."""
        from playwright.async_api import async_playwright

        from src.core.step_executor import StepExecutor

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            variables = {"username": "test_user", "password": "test_pass"}
            executor = StepExecutor(page, variables)

            await executor.execute_steps(test_bank_blueprint.auth.steps, context="auth")

            # Should be on the dashboard
            title = await page.title()
            assert "Dashboard" in title

            await context.close()
            await browser.close()

    @pytest.mark.asyncio
    async def test_login_invalid_creds(self, test_site, test_bank_blueprint):
        """Test that invalid credentials don't reach the dashboard."""
        from playwright.async_api import async_playwright

        from src.core.step_executor import StepExecutor
        from src.exceptions import ConnectionFailedError

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            variables = {"username": "bad_user", "password": "bad_pass"}
            executor = StepExecutor(page, variables)

            # The "wait for #dashboard" step should timeout since login fails
            with pytest.raises(ConnectionFailedError):
                await executor.execute_steps(test_bank_blueprint.auth.steps, context="auth")

            await context.close()
            await browser.close()


# ── Data Extractor Tests ──────────────────────────────────────────────────────


class TestDataExtraction:
    @pytest.mark.asyncio
    async def test_extract_account_data(self, test_site, test_bank_blueprint):
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
            await executor.execute_steps(test_bank_blueprint.auth.steps, context="auth")

            # Extract data
            extractor = DataExtractor(page)
            data = await extractor.extract(test_bank_blueprint.extract, site="test_bank")

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
    async def test_extract_specific_fields(self, test_site, test_bank_blueprint):
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
            await executor.execute_steps(test_bank_blueprint.auth.steps, context="auth")

            # Extract only current_bill
            limited_fields = {k: v for k, v in test_bank_blueprint.extract.items() if k == "current_bill"}
            extractor = DataExtractor(page)
            data = await extractor.extract(limited_fields, site="test_bank")

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


class TestEngineIntegration:
    @pytest.mark.asyncio
    async def test_full_connect_flow(self, test_site):
        """Test the full connect_to_site function against the test site."""
        # We need to temporarily set the connectors dir and use port 18080
        import json
        import tempfile
        from pathlib import Path

        from src.core.browser_pool import shutdown_browser_pool
        from src.core.engine import connect_to_site

        # Create a modified blueprint pointing to test port
        bp_path = Path("connectors/test_bank.json")
        with open(bp_path) as f:
            bp_data = json.load(f)

        # Update URLs to test port
        for step in bp_data["auth"]["steps"]:
            if "url" in step and "8080" in step["url"]:
                step["url"] = step["url"].replace("8080", "18080")

        with tempfile.TemporaryDirectory() as tmpdir:
            modified_bp = Path(tmpdir) / "test_bank.json"
            modified_bp.write_text(json.dumps(bp_data))

            # Temporarily override connectors dir
            original_dir = os.environ.get("CONNECTORS_DIR")
            os.environ["CONNECTORS_DIR"] = tmpdir

            try:
                # Use a fresh settings load to pick up the new dir
                # The engine uses settings.connectors_dir
                result = await connect_to_site(
                    site="test_bank",
                    username="test_user",
                    password="test_pass",
                )

                assert result["status"] == "connected"
                assert "data" in result
                assert result["data"]["current_bill"] == 142.57
                assert isinstance(result["data"]["usage_history"], list)
            finally:
                if original_dir:
                    os.environ["CONNECTORS_DIR"] = original_dir
                else:
                    os.environ.pop("CONNECTORS_DIR", None)
                await shutdown_browser_pool()
