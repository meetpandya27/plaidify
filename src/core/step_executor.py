"""
Step Executor — interprets blueprint steps and drives Playwright.

Takes a list of BlueprintStep objects and executes them sequentially
against a Playwright Page (or Frame). Handles variable interpolation,
conditional branching, iframes, and step-level timeouts, and asks the
read-only policy before every navigation, fill and click.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from typing import Any, Dict, Optional

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src.core.blueprint import BlueprintStep, OutcomeCheck, StepAction
from src.core.network_policy import navigation_block_reason
from src.core.page_checks import first_present, indicator_present, selector_visible
from src.core.read_only_policy import ReadOnlyExecutionPolicy
from src.exceptions import (
    AuthenticationError,
    ConnectionFailedError,
    ReadOnlyPolicyViolationError,
    SiteUnavailableError,
)
from src.logging_config import get_logger

logger = get_logger("step_executor")

# Playwright echoes an action's arguments in its call log, e.g. fill("hunter2").
_ECHOED_ARGUMENT = re.compile(r"\b(fill|type|press_sequentially|press|select_option)\((.*)\)", re.S)
_VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_CREDENTIAL_VARIABLES = frozenset({"username", "password"})


def describe_browser_error(exc: BaseException) -> str:
    """One loggable line about a browser failure, with no typed values in it.

    A failed fill() on a password or MFA field puts the value itself into the
    exception text, and that text used to reach logs, the access job row, API
    error bodies and hosted-link events. Keep the headline only, masked.
    """
    text = str(exc).strip()
    headline = text.splitlines()[0] if text else type(exc).__name__
    return _ECHOED_ARGUMENT.sub(lambda m: f"{m.group(1)}(<redacted>)", headline)


class StepExecutor:
    """
    Executes an ordered list of BlueprintStep actions against a Playwright Page.

    Supports variable interpolation ({{username}}, {{password}}, etc.)
    and all V2 step actions.
    """

    def __init__(
        self,
        page: Page,
        variables: Dict[str, str],
        *,
        allow_js_execution: bool = False,
        read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
        site: str = "unknown",
        failure_check: Optional[OutcomeCheck] = None,
        debug_screenshots: bool = False,
    ) -> None:
        """
        Args:
            page: A Playwright Page (or Frame) instance.
            variables: Dict of interpolation variables (e.g., {"username": "...", "password": "..."}).
            allow_js_execution: Whether execute_js steps may run; only trusted blueprints get True.
            read_only_policy: Policy consulted before navigations, fills and clicks.
            site: Site key, for error messages.
            failure_check: The blueprint's login-failure indicator. Once credentials
                have been typed, wait steps also watch for it and fail fast with
                AuthenticationError instead of timing out.
            debug_screenshots: Whether screenshot steps run (debug mode only).
        """
        self.page = page
        self.variables = variables
        self.allow_js_execution = allow_js_execution
        self.read_only_policy = read_only_policy
        self.site = site
        self.failure_check = failure_check
        self.debug_screenshots = debug_screenshots
        self.credentials_entered = False
        self._screenshot_dir: Optional[str] = None

    def _interpolate(self, value: Optional[str]) -> Optional[str]:
        """Replace {{variable}} placeholders with actual values."""
        if value is None:
            return None

        def replacer(match):
            key = match.group(1).strip()
            return self.variables.get(key, match.group(0))

        return _VARIABLE.sub(replacer, value)

    def _child(self, frame: Any) -> StepExecutor:
        """An executor for an iframe's document that shares this one's state."""
        child = StepExecutor(
            frame,
            self.variables,
            allow_js_execution=self.allow_js_execution,
            read_only_policy=self.read_only_policy,
            site=self.site,
            failure_check=self.failure_check,
            debug_screenshots=self.debug_screenshots,
        )
        child.credentials_entered = self.credentials_entered
        child._screenshot_dir = self._screenshot_dir
        return child

    async def execute_steps(
        self,
        steps: list[BlueprintStep],
        context: str = "auth",
    ) -> None:
        """
        Execute a list of steps sequentially.

        Args:
            steps: Ordered list of BlueprintStep to execute.
            context: Label for logging (e.g., "auth", "cleanup").

        Raises:
            ConnectionFailedError: If a step fails unexpectedly.
            AuthenticationError: If login is detected as failed.
            SiteUnavailableError: If the site is unreachable.
            ReadOnlyPolicyViolationError: If the policy refuses a step.
        """
        for i, step in enumerate(steps):
            step_label = f"{context}[{i}] {step.action.value}"
            logger.debug(
                f"Executing step: {step_label}",
                extra={"extra_data": {"step": i, "action": step.action.value}},
            )
            try:
                await self._execute_step(step)
            except PlaywrightTimeout as e:
                reason = describe_browser_error(e)
                logger.error(
                    f"Step timed out: {step_label}",
                    extra={"extra_data": {"step": i, "error": reason}},
                )
                raise ConnectionFailedError(
                    site=self.site,
                    detail=f"Step '{step.action.value}' timed out: {reason}",
                ) from e
            except ConnectionFailedError:
                raise
            except AuthenticationError:
                raise
            except ReadOnlyPolicyViolationError:
                raise
            except SiteUnavailableError:
                raise
            except Exception as e:
                reason = describe_browser_error(e)
                logger.error(
                    f"Step failed: {step_label}",
                    extra={"extra_data": {"step": i, "error": reason}},
                )
                raise ConnectionFailedError(
                    site=self.site,
                    detail=f"Step '{step.action.value}' failed: {reason}",
                ) from e

    async def _execute_step(self, step: BlueprintStep) -> None:
        """Dispatch a single step to the appropriate handler."""
        self._enforce_step_policy(step)

        handlers = {
            StepAction.GOTO: self._step_goto,
            StepAction.FILL: self._step_fill,
            StepAction.CLICK: self._step_click,
            StepAction.WAIT: self._step_wait,
            StepAction.SCREENSHOT: self._step_screenshot,
            StepAction.CONDITIONAL: self._step_conditional,
            StepAction.SCROLL: self._step_scroll,
            StepAction.SELECT: self._step_select,
            StepAction.IFRAME: self._step_iframe,
            StepAction.WAIT_FOR_NAVIGATION: self._step_wait_for_navigation,
            StepAction.EXECUTE_JS: self._step_execute_js,
        }

        handler = handlers.get(step.action)
        if handler is None:
            raise ConnectionFailedError(
                site=self.site,
                detail=f"Unknown step action: {step.action.value}",
            )

        await handler(step)

    def _block(self, action: str, reason: str, target: Optional[str]) -> None:
        if self.read_only_policy is not None:
            self.read_only_policy.record_blocked(action, reason, target=target)
        raise ReadOnlyPolicyViolationError(reason)

    def _enforce_step_policy(self, step: BlueprintStep) -> None:
        if not self.read_only_policy:
            return

        reason = self.read_only_policy.evaluate_step(step)
        if reason is None:
            return

        self._block(step.action.value, reason, step.selector or step.url)

    def _navigation_reason(self, url: str) -> Optional[str]:
        if self.read_only_policy is not None:
            return self.read_only_policy.evaluate_navigation(url)
        # Without a policy there are no domains to hold to, but the scheme rule still applies.
        return navigation_block_reason(url, ())

    async def _describe_click_target(self, selector: str) -> dict[str, Any]:
        if not self.read_only_policy:
            return {}

        try:
            metadata = await self.page.eval_on_selector(
                selector,
                """(element) => {
                    const form = element.closest('form');
                    return {
                        text: element.innerText || element.textContent || '',
                        ariaLabel: element.getAttribute('aria-label') || '',
                        title: element.getAttribute('title') || '',
                        value: element.getAttribute('value') || '',
                        name: element.getAttribute('name') || '',
                        id: element.id || '',
                        href: element.getAttribute('href') || '',
                        formAction: element.getAttribute('formaction') || form?.getAttribute('action') || '',
                        formMethod: form?.getAttribute('method') || '',
                    };
                }""",
            )
            if isinstance(metadata, dict):
                return metadata
        except Exception:
            return {}

        return {}

    # ── Step Handlers ─────────────────────────────────────────────────────────

    async def _step_goto(self, step: BlueprintStep) -> None:
        """Navigate to a URL on the blueprint's own domains."""
        url = self._interpolate(step.url)
        if not url:
            raise ConnectionFailedError(site=self.site, detail="goto step requires a 'url' field.")

        reason = self._navigation_reason(url)
        if reason:
            self._block("goto", reason, url.split("?", 1)[0])

        timeout = step.timeout or 30000
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if response and response.status >= 500:
                raise SiteUnavailableError(site=self.site, detail=f"HTTP {response.status}")
        except PlaywrightTimeout as e:
            raise SiteUnavailableError(site=self.site, detail=f"Navigation timeout: {describe_browser_error(e)}") from e

        logger.debug("Navigated", extra={"extra_data": {"site": self.site}})

    async def _step_fill(self, step: BlueprintStep) -> None:
        """Fill a text input."""
        selector = step.selector
        value = self._interpolate(step.value) or ""
        if not selector:
            raise ConnectionFailedError(site=self.site, detail="fill step requires a 'selector' field.")

        timeout = step.timeout or 10000
        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
        await self.page.fill(selector, value)
        if step.value and any(name in _CREDENTIAL_VARIABLES for name in _VARIABLE.findall(step.value)):
            self.credentials_entered = True
        logger.debug(f"Filled {selector}")

    async def _step_click(self, step: BlueprintStep) -> None:
        """Click an element, optionally waiting for navigation."""
        selector = step.selector
        if not selector:
            raise ConnectionFailedError(site=self.site, detail="click step requires a 'selector' field.")

        timeout = step.timeout or 10000
        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")

        if self.read_only_policy is not None:
            metadata = await self._describe_click_target(selector)
            reason = self.read_only_policy.evaluate_click(selector, metadata)
            if reason:
                self._block("click", reason, selector)

        if step.wait_for_navigation:
            async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=step.timeout or 30000):
                await self.page.click(selector)
        else:
            await self.page.click(selector)

        logger.debug(f"Clicked {selector}")

    async def _step_wait(self, step: BlueprintStep) -> None:
        """Wait for an element to appear, or pause for ``timeout`` ms when no selector is given."""
        selector = step.selector
        if not selector:
            await asyncio.sleep((step.timeout or 0) / 1000)
            return

        timeout = step.timeout or 10000
        if self.failure_check is not None and self.credentials_entered:
            # After the credentials go in, a rejected login shows the failure
            # indicator instead of the awaited element; stop waiting at once.
            outcome = await first_present(
                (
                    ("failure", lambda: indicator_present(self.page, self.failure_check)),
                    ("found", lambda: selector_visible(self.page, selector)),
                ),
                timeout_ms=timeout,
            )
            if outcome == "failure":
                raise AuthenticationError(site=self.site)
            if outcome is None:
                raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded waiting for {selector!r} to be visible")
            return

        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
        logger.debug(f"Found {selector}")

    async def _step_screenshot(self, step: BlueprintStep) -> None:
        """Take a screenshot — debug mode only, into a private per-run folder."""
        if not self.debug_screenshots:
            logger.debug("Screenshot step skipped (debug mode is off)")
            return

        name = re.sub(r"[^a-zA-Z0-9_-]", "_", step.screenshot_name or "debug")
        if self._screenshot_dir is None:
            # mkdtemp creates the folder 0700: signed-in pages stay private to this user.
            self._screenshot_dir = tempfile.mkdtemp(prefix="plaidify-screenshots-")
        path = os.path.join(self._screenshot_dir, f"{name}.png")
        await self.page.screenshot(path=path, full_page=False)
        logger.debug(f"Screenshot saved: {path}")

    async def _step_conditional(self, step: BlueprintStep) -> None:
        """Conditional branching based on selector presence."""
        selector = step.condition_selector
        if not selector:
            raise ConnectionFailedError(site=self.site, detail="conditional step requires 'condition_selector'.")

        timeout = step.timeout or 3000
        try:
            await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            condition_met = True
        except PlaywrightTimeout:
            condition_met = False

        if condition_met and step.then_steps:
            await self.execute_steps(step.then_steps, context="conditional_then")
        elif not condition_met and step.else_steps:
            await self.execute_steps(step.else_steps, context="conditional_else")

    async def _step_scroll(self, step: BlueprintStep) -> None:
        """Scroll the page."""
        if step.selector:
            # Scroll to element
            await self.page.locator(step.selector).scroll_into_view_if_needed()
        else:
            pixels = int(step.pixels or 500)
            delta = pixels if (step.direction or "down") == "down" else -pixels
            await self.page.evaluate("(delta) => window.scrollBy(0, delta)", delta)

        logger.debug("Scrolled page")

    async def _step_select(self, step: BlueprintStep) -> None:
        """Select a dropdown option."""
        selector = step.selector
        value = self._interpolate(step.value)
        if not selector or not value:
            raise ConnectionFailedError(site=self.site, detail="select step requires 'selector' and 'value'.")

        await self.page.select_option(selector, value)
        logger.debug(f"Selected an option in {selector}")

    async def _step_iframe(self, step: BlueprintStep) -> None:
        """Run ``step.steps`` inside the document of the iframe at ``iframe_selector``."""
        selector = step.iframe_selector or step.selector
        if not selector or not step.steps:
            raise ConnectionFailedError(site=self.site, detail="iframe step requires 'iframe_selector' and 'steps'.")

        handle = await self.page.wait_for_selector(selector, timeout=step.timeout or 10000, state="attached")
        frame = await handle.content_frame() if handle is not None else None
        if frame is None:
            raise ConnectionFailedError(site=self.site, detail=f"iframe step: {selector!r} is not an iframe.")

        child = self._child(frame)
        await child.execute_steps(step.steps, context="iframe")
        self.credentials_entered = self.credentials_entered or child.credentials_entered
        logger.debug(f"Ran {len(step.steps)} step(s) inside iframe {selector}")

    async def _step_wait_for_navigation(self, step: BlueprintStep) -> None:
        """Wait for a navigation event."""
        timeout = step.timeout or 30000
        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        logger.debug("Navigation complete")

    async def _step_execute_js(self, step: BlueprintStep) -> None:
        """Execute JavaScript in the page context.

        Only trusted blueprints (bundled with Plaidify, or vouched for by the
        operator) may run JavaScript; the read-only policy further limits it to
        the login and MFA phases.
        """
        if not self.allow_js_execution:
            raise ConnectionFailedError(
                site=self.site,
                detail="execute_js steps are not allowed for this blueprint. "
                "Only connectors bundled with Plaidify or listed in ENGINE_TRUSTED_CONNECTORS may run JavaScript.",
            )
        script = step.script
        if not script:
            raise ConnectionFailedError(site=self.site, detail="execute_js step requires a 'script' field.")

        result = await self.page.evaluate(script)
        logger.debug(f"JS executed, result type: {type(result).__name__}")
