"""Engine behaviour that does not need a real browser: budgets, gating, connectors, login and MFA outcomes."""

import asyncio
import json
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src.core import engine
from src.core.blueprint import TrustTier, load_blueprint_from_dict
from src.core.mfa_manager import MFARejectedError, MFATimeoutError, get_mfa_manager
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
from src.core.step_executor import StepExecutor
from src.exceptions import (
    AuthenticationError,
    BlueprintNotFoundError,
    ConnectionFailedError,
    MFARequiredError,
    RateLimitedError,
)

CONNECTORS = Path("connectors").resolve()


# ── Time budget ───────────────────────────────────────────────────────────────


class TestRunClock:
    def test_waiting_on_the_user_does_not_use_the_budget(self):
        clock = engine._RunClock(10)
        with clock.waiting_on_user():
            time.sleep(0.05)
            assert clock.paused
        assert clock.remaining() > 9.9

    @pytest.mark.asyncio
    async def test_automation_budget_cancels_the_run(self):
        cancelled = asyncio.Event()

        async def hangs():
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        with pytest.raises(ConnectionFailedError, match="timed out"):
            await engine._run_within_budget(hangs(), clock=engine._RunClock(0.2), site="bank")
        assert cancelled.is_set()  # the run's own cleanup ran

    @pytest.mark.asyncio
    async def test_a_long_mfa_wait_does_not_time_the_run_out(self):
        clock = engine._RunClock(0.3)

        async def waits_for_user():
            with clock.waiting_on_user():
                await asyncio.sleep(0.8)
            return {"status": "connected"}

        assert await engine._run_within_budget(waits_for_user(), clock=clock, site="bank") == {"status": "connected"}


# ── What may run (JOB-04) ─────────────────────────────────────────────────────


@pytest.fixture
def stub_execution(monkeypatch):
    calls = []

    async def fake_execute(**kwargs):
        calls.append(kwargs)
        return {"status": "connected", "data": {}}

    monkeypatch.setattr(engine, "_execute_blueprint", fake_execute)
    monkeypatch.setattr(engine.settings, "connectors_dir", str(CONNECTORS))
    return calls


class TestExecutionGating:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["internal_bank", "demo_utility", "demo_bank", "demo_saas"])
    async def test_internal_and_sandbox_connectors_do_not_run_outside_demo_mode(
        self, stub_execution, monkeypatch, site
    ):
        monkeypatch.setattr(engine.settings, "demo_mode", False)
        monkeypatch.setattr(engine.settings, "engine_allow_internal_connectors", False)
        with pytest.raises(BlueprintNotFoundError):
            await engine.connect_to_site(site, "demo_user", "demo_pass")
        assert stub_execution == []

    @pytest.mark.asyncio
    async def test_they_run_in_demo_mode(self, stub_execution, monkeypatch):
        monkeypatch.setattr(engine.settings, "demo_mode", True)
        monkeypatch.setattr(engine.settings, "engine_allow_internal_connectors", False)
        engine_limiter = engine.get_site_rate_limiter()
        engine_limiter._windows.clear()
        result = await engine.connect_to_site("internal_bank", "gating-user", "pw")
        assert result["status"] == "connected"
        assert stub_execution[0]["trust"] is TrustTier.BUNDLED

    @pytest.mark.asyncio
    async def test_the_template_connector_is_not_loaded(self, stub_execution, monkeypatch):
        monkeypatch.setattr(engine.settings, "demo_mode", True)
        assert not list(CONNECTORS.glob("*_connector.py"))
        with pytest.raises(BlueprintNotFoundError):
            await engine.connect_to_site("template", "anyone", "wrong")
        assert engine.load_python_connectors(str(CONNECTORS)) == {}

    @pytest.mark.asyncio
    async def test_blueprints_outside_the_repo_run_untrusted(self, stub_execution, monkeypatch, tmp_path):
        shutil.copy(CONNECTORS / "hydro_one.json", tmp_path / "hydro_one.json")
        monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
        engine.get_site_rate_limiter()._windows.clear()
        await engine.connect_to_site("hydro_one", "trust-user", "pw")
        assert stub_execution[0]["trust"] is TrustTier.UNTRUSTED


class TestAddressPolicyForTrust:
    def test_untrusted_always_blocks_private_networks(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "browser_block_private_networks", False)
        assert engine._address_policy_for(TrustTier.UNTRUSTED).block_private is True
        assert engine._address_policy_for(TrustTier.BUNDLED).block_private is False

    def test_loopback_only_in_demo_mode_or_tests(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "demo_mode", False)
        monkeypatch.setattr(engine.settings, "engine_allow_internal_connectors", False)
        assert engine._address_policy_for(TrustTier.UNTRUSTED).allow_loopback is False
        monkeypatch.setattr(engine.settings, "demo_mode", True)
        assert engine._address_policy_for(TrustTier.UNTRUSTED).allow_loopback is True


# ── Rate limits (ENG-14) ──────────────────────────────────────────────────────


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_blueprint_rate_limit_is_honoured_per_account(self, stub_execution, monkeypatch, tmp_path):
        blueprint = json.loads((CONNECTORS / "hydro_one.json").read_text())
        blueprint["rate_limit"] = {"max_requests_per_hour": 10, "min_interval_seconds": 60}
        (tmp_path / "limited.json").write_text(json.dumps(blueprint))
        monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
        engine.get_site_rate_limiter()._windows.clear()

        await engine.connect_to_site("limited", "alice", "pw")
        with pytest.raises(RateLimitedError) as caught:
            await engine.connect_to_site("limited", "alice", "pw")
        assert 1 <= caught.value.retry_after <= 60
        # Another account on the same site is unaffected.
        await engine.connect_to_site("limited", "bob", "pw")
        assert len(stub_execution) == 2


# ── Python connectors (ENG-20) ────────────────────────────────────────────────


class TestPythonConnectors:
    def _write(self, directory, name, body):
        (directory / f"{name}_connector.py").write_text(body)

    @pytest.mark.asyncio
    async def test_a_slow_connector_is_timed_out_off_the_event_loop(self, monkeypatch, tmp_path):
        self._write(
            tmp_path,
            "slowsite",
            "import time\nfrom src.core.connector_base import BaseConnector\n"
            "class Slow(BaseConnector):\n"
            "    def connect(self, username, password):\n"
            "        time.sleep(1.0)\n"
            "        return {'status': 'connected', 'data': {}}\n",
        )
        monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
        monkeypatch.setattr(engine.settings, "engine_timeout_seconds", 0.2)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())
        with pytest.raises(ConnectionFailedError, match="timed out"):
            await engine.connect_to_site("slowsite", "u", "p")
        tick_task.cancel()
        assert ticks >= 10  # the loop kept running while the connector slept

    @pytest.mark.asyncio
    async def test_imports_are_cached_until_the_file_changes(self, monkeypatch, tmp_path):
        self._write(
            tmp_path,
            "counted",
            "from src.core.connector_base import BaseConnector\n"
            "import builtins\n"
            "builtins.__dict__['_plaidify_imports'] = builtins.__dict__.get('_plaidify_imports', 0) + 1\n"
            "class Counted(BaseConnector):\n"
            "    async def connect(self, username, password):\n"
            "        return {'status': 'connected', 'data': {'user': username}}\n",
        )
        monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
        import builtins

        builtins.__dict__["_plaidify_imports"] = 0
        for _ in range(3):
            assert (await engine.connect_to_site("counted", "u", "p"))["data"] == {"user": "u"}
        assert builtins.__dict__.pop("_plaidify_imports") == 1

    @pytest.mark.asyncio
    async def test_internal_python_connectors_follow_the_same_gating(self, monkeypatch, tmp_path):
        self._write(
            tmp_path,
            "hidden",
            "from src.core.connector_base import BaseConnector\n"
            "class Hidden(BaseConnector):\n"
            "    tags = ['internal']\n"
            "    def connect(self, username, password):\n"
            "        return {'status': 'connected'}\n",
        )
        monkeypatch.setattr(engine.settings, "connectors_dir", str(tmp_path))
        monkeypatch.setattr(engine.settings, "demo_mode", False)
        monkeypatch.setattr(engine.settings, "engine_allow_internal_connectors", False)
        with pytest.raises(BlueprintNotFoundError):
            await engine.connect_to_site("hidden", "u", "p")


# ── A scripted page for login and MFA ────────────────────────────────────────


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return 1 if self.page.visible(self.selector) else 0

    def nth(self, index):
        return self

    async def is_visible(self):
        return self.page.visible(self.selector)

    async def inner_text(self, **kwargs):
        return "error" if self.page.visible(self.selector) else ""


class ScriptedPage:
    """An MFA page that accepts only ``right_code`` and shows #mfa-error after a wrong one."""

    def __init__(self, *, right_code="123456", state="mfa"):
        self.right_code = right_code
        self.state = state
        self.error = False
        self.filled = {}
        self.url = "https://bank.example/mfa"

    def visible(self, selector):
        return {
            "#otp": self.state == "mfa",
            "#otp-submit": self.state == "mfa",
            "#dashboard": self.state == "dashboard",
            "#mfa-error": self.error,
            "#login-error": self.state == "login-error",
        }.get(selector, False)

    def locator(self, selector):
        return _Locator(self, selector)

    async def wait_for_selector(self, selector, timeout=None, state=None):
        if self.visible(selector):
            return object()
        raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded")

    async def fill(self, selector, value):
        self.filled[selector] = value

    async def eval_on_selector(self, selector, script):
        return {"text": "Verify"}

    async def click(self, selector):
        if self.filled.get("#otp") == self.right_code:
            self.state, self.error = "dashboard", False
        else:
            self.error = True

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def query_selector(self, selector):
        return None

    async def inner_text(self, selector, **kwargs):
        return ""


def _mfa_blueprint(mfa=None):
    return load_blueprint_from_dict(
        {
            "schema_version": "2.0",
            "name": "Bank",
            "domain": "bank.example",
            "auth": {
                "submit_targets": ["/login"],
                "steps": [{"action": "goto", "url": "https://bank.example/login"}],
                "success": {"selector": "#dashboard", "timeout": 300},
                "failure": {"selector": "#login-error"},
            },
            "mfa": mfa
            or {
                "detection": {"selector": "#otp", "timeout": 100},
                "type": "otp_input",
                "input_selector": "#otp",
                "submit_selector": "#otp-submit",
                "submit_targets": ["/mfa"],
                "success": {"selector": "#dashboard", "timeout": 300},
                "failure": {"selector": "#mfa-error"},
            },
            "extract": {"balance": {"selector": "#balance"}},
        }
    )


def _executor(page):
    policy = ReadOnlyExecutionPolicy.for_blueprint(_mfa_blueprint())
    policy.set_phase(ExecutionPhase.MFA)
    return StepExecutor(page, {"username": "u", "password": "p"}, read_only_policy=policy, site="bank"), policy


async def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _awaiting(session_id, *, with_error=False):
    session = await get_mfa_manager().get_session(session_id)
    return session is not None and session.awaiting_code and (not with_error or "mfa_error" in session.metadata)


class TestLoginOutcome:
    @pytest.mark.asyncio
    async def test_the_login_error_is_invalid_credentials(self):
        with pytest.raises(AuthenticationError):
            await engine._await_login_outcome(ScriptedPage(state="login-error"), _mfa_blueprint(), "bank")

    @pytest.mark.asyncio
    async def test_signed_in_and_mfa_pages_are_recognised(self):
        assert await engine._await_login_outcome(ScriptedPage(state="dashboard"), _mfa_blueprint(), "bank") == "success"
        assert await engine._await_login_outcome(ScriptedPage(state="mfa"), _mfa_blueprint(), "bank") == "mfa"

    @pytest.mark.asyncio
    async def test_no_outcome_is_a_connection_failure_not_bad_credentials(self):
        with pytest.raises(ConnectionFailedError, match="Sign-in did not complete"):
            await engine._await_login_outcome(ScriptedPage(state="blank"), _mfa_blueprint(), "bank")


class TestMFAFlow:
    @pytest.mark.asyncio
    async def test_wrong_code_asks_again_then_succeeds(self):
        page = ScriptedPage()
        executor, policy = _executor(page)
        task = asyncio.create_task(
            engine._handle_mfa(
                page,
                _mfa_blueprint(),
                "bank",
                "mfa-flow-1",
                executor=executor,
                policy=policy,
                clock=engine._RunClock(60),
            )
        )
        manager = get_mfa_manager()
        await _until(lambda: _awaiting("mfa-flow-1"))
        await manager.submit_code("mfa-flow-1", "000000")
        await _until(lambda: _awaiting("mfa-flow-1", with_error=True))
        session = await manager.get_session("mfa-flow-1")
        assert session.metadata["attempts_remaining"] == 2

        await manager.submit_code("mfa-flow-1", "123456")
        await asyncio.wait_for(task, timeout=5)
        assert page.state == "dashboard"
        assert page.filled["#otp"] == "123456"
        assert await manager.get_session("mfa-flow-1") is None  # the challenge is over

    @pytest.mark.asyncio
    async def test_too_many_wrong_codes_is_invalid_credentials(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "mfa_max_attempts", 2)
        page = ScriptedPage()
        executor, policy = _executor(page)
        task = asyncio.create_task(
            engine._handle_mfa(
                page,
                _mfa_blueprint(),
                "bank",
                "mfa-flow-2",
                executor=executor,
                policy=policy,
                clock=engine._RunClock(60),
            )
        )
        manager = get_mfa_manager()
        await _until(lambda: _awaiting("mfa-flow-2"))
        await manager.submit_code("mfa-flow-2", "000000")
        await _until(lambda: _awaiting("mfa-flow-2", with_error=True))
        await manager.submit_code("mfa-flow-2", "111111")
        with pytest.raises(MFARejectedError):
            await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_no_answer_is_an_mfa_timeout(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "mfa_timeout_seconds", 1)
        page = ScriptedPage()
        executor, policy = _executor(page)
        clock = engine._RunClock(60)
        with pytest.raises(MFATimeoutError):
            await engine._handle_mfa(
                page, _mfa_blueprint(), "bank", "mfa-flow-3", executor=executor, policy=policy, clock=clock
            )
        assert clock.remaining() > 59  # the wait did not use the automation budget
        assert await get_mfa_manager().get_session("mfa-flow-3") is None

    @pytest.mark.asyncio
    async def test_unattended_runs_fail_fast(self):
        page = ScriptedPage()
        executor, policy = _executor(page)
        with pytest.raises(MFARequiredError):
            await engine._handle_mfa(
                page,
                _mfa_blueprint(),
                "bank",
                "mfa-flow-4",
                executor=executor,
                policy=policy,
                clock=engine._RunClock(60),
                interactive=False,
            )
        assert await get_mfa_manager().get_session("mfa-flow-4") is None


class FlakyPushPage(ScriptedPage):
    """The push prompt's page errors while "navigating", then keeps showing the prompt."""

    def __init__(self, *, approve_after=None):
        super().__init__(state="mfa")
        self.polls = 0
        self.approve_after = approve_after

    def locator(self, selector):
        page = self

        class Flaky(_Locator):
            async def is_visible(self):
                page.polls += 1
                if page.polls <= 3:
                    raise RuntimeError("Execution context was destroyed")
                if page.approve_after is not None and page.polls > page.approve_after:
                    return False
                return True

        return Flaky(self, selector)


_PUSH = {
    "detection": {"selector": "#push", "timeout": 100},
    "type": "push",
    "poll_interval": 100,
    "poll_timeout": 60000,
}


class TestPushMFA:
    @pytest.mark.asyncio
    async def test_polling_errors_are_not_approval(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "mfa_timeout_seconds", 1)
        blueprint = _mfa_blueprint(mfa=_PUSH)
        blueprint.auth.success = None
        page = FlakyPushPage()
        page.visible = lambda selector: selector == "#push"
        with pytest.raises(MFATimeoutError):
            await engine._handle_push_mfa(page, blueprint, "bank", "push-1", metadata={}, clock=engine._RunClock(60))

    @pytest.mark.asyncio
    async def test_prompt_going_away_is_approval(self, monkeypatch):
        monkeypatch.setattr(engine.settings, "mfa_timeout_seconds", 5)
        blueprint = _mfa_blueprint(mfa=_PUSH)
        blueprint.auth.success = None
        page = FlakyPushPage(approve_after=5)
        await engine._handle_push_mfa(page, blueprint, "bank", "push-2", metadata={}, clock=engine._RunClock(60))
        assert await get_mfa_manager().get_session("push-2") is None


class TestSignIn:
    @pytest.mark.asyncio
    async def test_a_step_timeout_on_the_error_page_is_reported_as_bad_credentials(self):
        from src.core.blueprint import BlueprintStep, StepAction

        page = ScriptedPage(state="login-error")
        blueprint = _mfa_blueprint()
        policy = ReadOnlyExecutionPolicy.for_blueprint(blueprint)
        executor = StepExecutor(page, {"username": "u", "password": "p"}, read_only_policy=policy, site="bank")
        executor.credentials_entered = True
        blueprint.auth.steps = [BlueprintStep(action=StepAction.WAIT, selector="#dashboard", timeout=100)]
        with patch.object(StepExecutor, "_step_wait", AsyncMock(side_effect=PlaywrightTimeout("Timeout 100ms"))):
            with pytest.raises(AuthenticationError):
                await engine._sign_in(page, executor, blueprint, "bank", policy)
