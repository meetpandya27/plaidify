import pytest

from src.core.blueprint import BlueprintStep, StepAction
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
from src.core.step_executor import StepExecutor
from src.exceptions import ReadOnlyPolicyViolationError


class FakeRequest:
    def __init__(self, method: str, *, headers: dict[str, str] | None = None, navigation: bool = False):
        self.method = method
        self.headers = headers or {}
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakePage:
    def __init__(self, click_metadata: dict[str, dict[str, str]] | None = None):
        self.click_metadata = click_metadata or {}
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.evaluated: list[str] = []

    async def wait_for_selector(self, selector: str, timeout: int, state: str) -> None:
        return None

    async def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def select_option(self, selector: str, value: str) -> None:
        return None

    async def eval_on_selector(self, selector: str, script: str) -> dict[str, str]:
        return self.click_metadata.get(selector, {})

    async def evaluate(self, script: str):
        self.evaluated.append(script)
        return None


def test_request_policy_blocks_navigation_post_after_auth() -> None:
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)

    reason = policy.evaluate_request(FakeRequest("POST", navigation=True))

    assert reason is not None
    assert "navigation POST" in reason


def test_request_policy_allows_json_fetch_post_after_auth() -> None:
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)

    reason = policy.evaluate_request(
        FakeRequest("POST", headers={"content-type": "application/json"}, navigation=False)
    )

    assert reason is None


def test_request_policy_blocks_delete_after_auth() -> None:
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)

    reason = policy.evaluate_request(FakeRequest("DELETE"))

    assert reason is not None
    assert "DELETE" in reason


@pytest.mark.asyncio
async def test_step_executor_blocks_fill_after_auth() -> None:
    page = FakePage()
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    executor = StepExecutor(page, {"username": "user"}, read_only_policy=policy, site="test_site")

    with pytest.raises(ReadOnlyPolicyViolationError):
        await executor.execute_steps(
            [BlueprintStep(action=StepAction.FILL, selector="#username", value="next")],
            context="read",
        )

    assert page.filled == []
    assert policy.blocked_actions[-1].action == "fill"


@pytest.mark.asyncio
async def test_step_executor_blocks_risky_click_after_auth() -> None:
    page = FakePage(click_metadata={"#transfer": {"text": "Transfer funds"}})
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    executor = StepExecutor(page, {}, read_only_policy=policy, site="test_site")

    with pytest.raises(ReadOnlyPolicyViolationError):
        await executor.execute_steps(
            [BlueprintStep(action=StepAction.CLICK, selector="#transfer")],
            context="read",
        )

    assert page.clicked == []
    assert policy.blocked_actions[-1].action == "click"


@pytest.mark.asyncio
async def test_step_executor_allows_harmless_click_after_auth() -> None:
    page = FakePage(click_metadata={"#history": {"text": "Transaction History"}})
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    executor = StepExecutor(page, {}, read_only_policy=policy, site="test_site")

    await executor.execute_steps(
        [BlueprintStep(action=StepAction.CLICK, selector="#history")],
        context="read",
    )

    assert page.clicked == ["#history"]


@pytest.mark.asyncio
async def test_execute_js_allowed_during_auth_but_blocked_after_auth() -> None:
    page = FakePage()
    step = BlueprintStep(action=StepAction.EXECUTE_JS, script="window.test = true")

    auth_policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.AUTH)
    auth_executor = StepExecutor(
        page,
        {},
        allow_js_execution=True,
        read_only_policy=auth_policy,
        site="test_site",
    )
    await auth_executor.execute_steps([step], context="auth")
    assert page.evaluated == ["window.test = true"]

    read_policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    read_executor = StepExecutor(
        page,
        {},
        allow_js_execution=True,
        read_only_policy=read_policy,
        site="test_site",
    )
    with pytest.raises(ReadOnlyPolicyViolationError):
        await read_executor.execute_steps([step], context="read")


# ── Click vocabulary (ENG-06) ─────────────────────────────────────────────────


RISKY_CLICKS = [
    ("#btn", {"text": "Pay now"}),
    ("#btn", {"text": "TRANSFER"}),
    ("#btn", {"text": "Pay bill"}),
    ("#btn", {"text": "Pay $120.00"}),
    ("#btn", {"text": "Pay"}),
    ("#btn", {"text": "Place order"}),
    ("#btn", {"text": "Buy"}),
    ("#btn", {"text": "Sell shares"}),
    ("#btn", {"text": "Wire funds"}),
    ("#btn", {"text": "Send with Zelle"}),
    ("#btn", {"text": "Cancel subscription"}),
    ("#btn", {"text": "Enroll in AutoPay"}),
    ("#btn", {"text": "Deactivate account"}),
    ("#btn", {"text": "Transferring..."}),
    ("#btn", {"text": "Make a withdrawal"}),
    ("#btn", {"text": "Submit payment"}),
    ("#btn", {"text": "Close my account"}),
    ("#transferFunds", {}),
    ("#btnPayNow", {}),
    ("#btn", {"text": "Trаnsfer"}),  # Cyrillic a
    ("#btn", {"text": "Trans​fer"}),  # zero-width space
    ("#btn", {"text": "Überweisen"}),
    ("#btn", {"text": "Payer maintenant"}),
    ("#btn", {"text": "Pagar ahora"}),
    ("#btn", {"text": "Delete"}),
    ("#btn", {"text": "Upload"}),
    ("a", {"text": "Details", "href": "/transfer/new"}),
]
SUBMIT_CLICKS = [
    ("#btn", {"text": "Submit"}),
    ("#btn", {"text": "Confirm"}),
    ("#btn", {"text": "Yes, continue"}),
    ("#btn", {"text": "Send"}),
    ("#btn", {"text": "Save"}),
]
HARMLESS_CLICKS = [
    ("#history", {"text": "Transaction History"}),
    ("#payments", {"text": "Payment history"}),
    ("#tab", {"text": "Payments"}),
    ("#tab", {"text": "Deposits"}),
    ("#tab", {"text": "Transfer history"}),
    ("#next", {"text": "Next"}),
    ("#more", {"text": "Load more"}),
    ("#statements", {"text": "View statements"}),
]


@pytest.mark.parametrize("selector, metadata", RISKY_CLICKS)
@pytest.mark.parametrize("phase", list(ExecutionPhase))
def test_money_moving_clicks_are_refused_in_every_phase(phase, selector, metadata) -> None:
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=phase)
    assert policy.evaluate_click(selector, metadata) is not None


@pytest.mark.parametrize("selector, metadata", SUBMIT_CLICKS)
def test_submit_clicks_are_fine_for_login_but_refused_once_signed_in(selector, metadata) -> None:
    for phase in (ExecutionPhase.AUTH, ExecutionPhase.MFA):
        assert ReadOnlyExecutionPolicy(enabled=True, phase=phase).evaluate_click(selector, metadata) is None
    for phase in (ExecutionPhase.READ, ExecutionPhase.CLEANUP):
        assert ReadOnlyExecutionPolicy(enabled=True, phase=phase).evaluate_click(selector, metadata) is not None


@pytest.mark.parametrize("selector, metadata", HARMLESS_CLICKS)
def test_read_only_navigation_is_not_a_false_positive(selector, metadata) -> None:
    for phase in ExecutionPhase:
        assert ReadOnlyExecutionPolicy(enabled=True, phase=phase).evaluate_click(selector, metadata) is None


@pytest.mark.parametrize(
    "selector, metadata",
    [
        ("#login-btn", {"text": "Continue"}),
        ("#btnSubmit", {"text": "Log in"}),
        ("#accept", {"text": "Accept all cookies"}),
        ("#otp-submit", {"text": "Verify"}),
        ("#send-code", {"text": "Send me a code"}),
    ],
)
def test_login_and_mfa_buttons_pass(selector, metadata) -> None:
    for phase in (ExecutionPhase.AUTH, ExecutionPhase.MFA):
        assert ReadOnlyExecutionPolicy(enabled=True, phase=phase).evaluate_click(selector, metadata) is None


@pytest.mark.parametrize(
    "selector, metadata",
    [
        ("#logout-btn", {"text": "Sign Out"}),
        ("#signout", {"text": "Sign out"}),
        ("#confirm", {"text": "Yes, sign me out"}),
        ("a[href*='pkmslogout']", {"text": "Logout"}),
    ],
)
def test_logging_out_is_allowed_in_cleanup(selector, metadata) -> None:
    assert (
        ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.CLEANUP).evaluate_click(selector, metadata) is None
    )


def test_cleanup_still_refuses_destructive_clicks() -> None:
    policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.CLEANUP)
    assert policy.evaluate_click("#delete-account", {"text": "Delete account"})
    assert policy.evaluate_click("#x", {"text": "Sign out and close account"})


def test_disabled_policy_does_not_judge_clicks() -> None:
    assert (
        ReadOnlyExecutionPolicy(enabled=False, phase=ExecutionPhase.READ).evaluate_click("#b", {"text": "Pay"}) is None
    )


# ── Requests per phase (ENG-03) ───────────────────────────────────────────────


class RoutedRequest(FakeRequest):
    def __init__(self, method, url, *, headers=None, navigation=False, main_frame=True, redirected_from=None):
        super().__init__(method, headers=headers, navigation=navigation)
        self.url = url
        self.redirected_from = redirected_from
        self.frame = type("Frame", (), {"parent_frame": None if main_frame else object()})()


def _policy_for(phase: ExecutionPhase) -> ReadOnlyExecutionPolicy:
    from src.core.blueprint import load_blueprint_from_dict

    blueprint = load_blueprint_from_dict(
        {
            "schema_version": "2.0",
            "name": "Bank",
            "domain": "www.bank.example",
            "auth": {
                "submit_targets": ["/login"],
                "steps": [{"action": "goto", "url": "https://www.bank.example/login"}],
            },
            "mfa": {
                "detection": {"selector": "#otp"},
                "type": "otp_input",
                "input_selector": "#otp",
                "submit_targets": ["/mfa"],
            },
            "logout_targets": ["/logout"],
            "extract": {"balance": {"selector": "#balance"}},
        }
    )
    policy = ReadOnlyExecutionPolicy.for_blueprint(blueprint)
    policy.set_phase(phase)
    return policy


FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_login_form_may_only_submit_to_declared_targets() -> None:
    policy = _policy_for(ExecutionPhase.AUTH)
    assert (
        policy.evaluate_request(RoutedRequest("POST", "https://www.bank.example/login", headers=FORM, navigation=True))
        is None
    )
    reason = policy.evaluate_request(
        RoutedRequest("POST", "https://www.bank.example/transfer", headers=FORM, navigation=True)
    )
    assert reason and "submit_targets" in reason
    assert policy.evaluate_request(RoutedRequest("DELETE", "https://www.bank.example/account"))
    # A JSON fetch (single-page app) is not a form submission.
    assert (
        policy.evaluate_request(
            RoutedRequest("POST", "https://www.bank.example/api/session", headers={"content-type": "application/json"})
        )
        is None
    )


def test_mfa_form_may_submit_to_mfa_and_login_targets() -> None:
    policy = _policy_for(ExecutionPhase.MFA)
    assert (
        policy.evaluate_request(RoutedRequest("POST", "https://www.bank.example/mfa", headers=FORM, navigation=True))
        is None
    )
    assert policy.evaluate_request(RoutedRequest("POST", "https://www.bank.example/login", headers=FORM)) is None
    assert policy.evaluate_request(RoutedRequest("POST", "https://www.bank.example/pay", headers=FORM, navigation=True))


def test_cleanup_may_only_reach_the_logout_target() -> None:
    policy = _policy_for(ExecutionPhase.CLEANUP)
    logout = RoutedRequest("GET", "https://www.bank.example/logout", navigation=True)
    assert policy.evaluate_request(logout) is None
    # The logout's redirect back to the login page is part of the same navigation.
    assert (
        policy.evaluate_request(
            RoutedRequest("GET", "https://www.bank.example/login", navigation=True, redirected_from=logout)
        )
        is None
    )
    assert policy.evaluate_request(
        RoutedRequest("GET", "https://www.bank.example/transfer?to=x&confirm=1", navigation=True)
    )
    assert policy.evaluate_request(
        RoutedRequest("POST", "https://www.bank.example/api/close", headers={"content-type": "application/json"})
    )
    # Sub-resources of the logged-out page are plain reads.
    assert policy.evaluate_request(RoutedRequest("GET", "https://cdn.example/app.css")) is None


@pytest.mark.parametrize("phase", list(ExecutionPhase))
def test_main_frame_navigation_stays_on_the_blueprints_domains(phase) -> None:
    policy = _policy_for(phase)
    assert policy.evaluate_request(RoutedRequest("GET", "https://evil.example/", navigation=True))
    # Third-party sub-frames and sub-resources are not navigation of the page itself.
    assert (
        policy.evaluate_request(RoutedRequest("GET", "https://ads.example/frame", navigation=True, main_frame=False))
        is None
    )


def test_navigation_scoping_holds_even_when_read_only_mode_is_off() -> None:
    policy = _policy_for(ExecutionPhase.READ)
    policy.enabled = False
    assert policy.evaluate_request(RoutedRequest("GET", "https://evil.example/", navigation=True))
    assert policy.evaluate_navigation("file:///etc/hosts")


# ── Step executor enforcement ────────────────────────────────────────────────


class NavigatingPage(FakePage):
    def __init__(self):
        super().__init__()
        self.visited: list[str] = []
        self.screenshots: list[str] = []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        return None

    async def screenshot(self, path=None, **kwargs):
        self.screenshots.append(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", list(ExecutionPhase))
@pytest.mark.parametrize(
    "url",
    ["file:///etc/hosts", "http://169.254.169.254/latest/meta-data/", "https://evil.example/", "chrome://version"],
)
async def test_goto_is_held_to_the_blueprint_in_every_phase(phase, url) -> None:
    page = NavigatingPage()
    policy = _policy_for(phase)
    executor = StepExecutor(page, {}, read_only_policy=policy, site="bank")
    step = BlueprintStep.model_construct(action=StepAction.GOTO, url=url)

    with pytest.raises(ReadOnlyPolicyViolationError):
        await executor.execute_steps([step], context=phase.value)

    assert page.visited == []
    assert policy.blocked_actions[-1].action == "goto"


@pytest.mark.asyncio
async def test_goto_without_a_policy_still_refuses_local_files() -> None:
    page = NavigatingPage()
    executor = StepExecutor(page, {}, site="bank")
    with pytest.raises(ReadOnlyPolicyViolationError):
        await executor.execute_steps([BlueprintStep.model_construct(action=StepAction.GOTO, url="file:///etc/hosts")])
    assert page.visited == []


@pytest.mark.asyncio
async def test_cleanup_goto_only_to_logout_targets() -> None:
    page = NavigatingPage()
    executor = StepExecutor(page, {}, read_only_policy=_policy_for(ExecutionPhase.CLEANUP), site="bank")
    await executor.execute_steps([BlueprintStep(action=StepAction.GOTO, url="https://www.bank.example/logout")])
    with pytest.raises(ReadOnlyPolicyViolationError):
        await executor.execute_steps([BlueprintStep(action=StepAction.GOTO, url="https://www.bank.example/settings")])
    assert page.visited == ["https://www.bank.example/logout"]


@pytest.mark.asyncio
async def test_untrusted_blueprints_cannot_run_javascript() -> None:
    from src.exceptions import ConnectionFailedError

    page = FakePage()
    executor = StepExecutor(page, {}, read_only_policy=_policy_for(ExecutionPhase.AUTH), site="bank")
    with pytest.raises(ConnectionFailedError, match="not allowed"):
        await executor.execute_steps([BlueprintStep(action=StepAction.EXECUTE_JS, script="1")])
    assert page.evaluated == []


@pytest.mark.asyncio
async def test_wait_with_only_a_timeout_pauses() -> None:
    import time

    executor = StepExecutor(FakePage(), {}, site="bank")
    started = time.monotonic()
    await executor.execute_steps([BlueprintStep(action=StepAction.WAIT, timeout=120)])
    assert time.monotonic() - started >= 0.1


class LoginErrorPage(FakePage):
    """A page that shows a login error and never the dashboard."""

    class _Locator:
        def __init__(self, visible):
            self._visible = visible
            self.first = self

        async def count(self):
            return 1 if self._visible else 0

        def nth(self, index):
            return self

        async def is_visible(self):
            return self._visible

        async def inner_text(self, **kwargs):
            return "Invalid credentials." if self._visible else ""

    def locator(self, selector):
        return self._Locator(selector == "#login-error")


@pytest.mark.asyncio
async def test_wait_step_fails_fast_when_the_login_error_appears() -> None:
    import time

    from src.core.blueprint import OutcomeCheck
    from src.exceptions import AuthenticationError

    executor = StepExecutor(
        LoginErrorPage(),
        {"username": "u", "password": "wrong"},
        site="bank",
        failure_check=OutcomeCheck(selector="#login-error"),
    )
    started = time.monotonic()
    with pytest.raises(AuthenticationError):
        await executor.execute_steps(
            [
                BlueprintStep(action=StepAction.FILL, selector="#password", value="{{password}}"),
                BlueprintStep(action=StepAction.WAIT, selector="#dashboard", timeout=30000),
            ],
            context="auth",
        )
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_screenshots_only_in_debug_mode_and_in_a_private_folder() -> None:
    import os
    import stat

    page = NavigatingPage()
    step = BlueprintStep(action=StepAction.SCREENSHOT, screenshot_name="../../etc/x")

    await StepExecutor(page, {}, site="bank").execute_steps([step])
    assert page.screenshots == []

    await StepExecutor(page, {}, site="bank", debug_screenshots=True).execute_steps([step])
    [path] = page.screenshots
    folder = os.path.dirname(path)
    assert not path.startswith("/tmp/plaidify_screenshot_")
    assert os.path.basename(path) == "______etc_x.png"
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    os.rmdir(folder)
