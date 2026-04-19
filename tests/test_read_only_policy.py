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
