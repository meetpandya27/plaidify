"""
Step Executor — interprets blueprint steps and drives Playwright.

Takes a list of BlueprintStep objects and executes them sequentially
against a Playwright Page. Handles variable interpolation, conditional
branching, and step-level timeouts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.core.blueprint import BlueprintStep, StepAction
from src.exceptions import (
    AuthenticationError,
    ConnectionFailedError,
    SiteUnavailableError,
)
from src.logging_config import get_logger

logger = get_logger("step_executor")


class StepExecutor:
    """
    Executes an ordered list of BlueprintStep actions against a Playwright Page.

    Supports variable interpolation ({{username}}, {{password}}, etc.)
    and all V2 step actions.
    """

    def __init__(self, page: Page, variables: Dict[str, str]) -> None:
        """
        Args:
            page: A Playwright Page instance.
            variables: Dict of interpolation variables (e.g., {"username": "...", "password": "..."}).
        """
        self.page = page
        self.variables = variables

    def _interpolate(self, value: Optional[str]) -> Optional[str]:
        """Replace {{variable}} placeholders with actual values."""
        if value is None:
            return None

        def replacer(match):
            key = match.group(1).strip()
            return self.variables.get(key, match.group(0))

        return re.sub(r"\{\{(\w+)\}\}", replacer, value)

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
                logger.error(
                    f"Step timed out: {step_label}",
                    extra={"extra_data": {"step": i, "error": str(e)}},
                )
                raise ConnectionFailedError(
                    site="unknown",
                    detail=f"Step '{step.action.value}' timed out: {e}",
                ) from e
            except ConnectionFailedError:
                raise
            except AuthenticationError:
                raise
            except SiteUnavailableError:
                raise
            except Exception as e:
                logger.error(
                    f"Step failed: {step_label}",
                    extra={"extra_data": {"step": i, "error": str(e)}},
                )
                raise ConnectionFailedError(
                    site="unknown",
                    detail=f"Step '{step.action.value}' failed: {e}",
                ) from e

    async def _execute_step(self, step: BlueprintStep) -> None:
        """Dispatch a single step to the appropriate handler."""
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
                site="unknown",
                detail=f"Unknown step action: {step.action.value}",
            )

        await handler(step)

    # ── Step Handlers ─────────────────────────────────────────────────────────

    async def _step_goto(self, step: BlueprintStep) -> None:
        """Navigate to a URL."""
        url = self._interpolate(step.url)
        if not url:
            raise ConnectionFailedError(
                site="unknown", detail="goto step requires a 'url' field."
            )

        timeout = step.timeout or 30000
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if response and response.status >= 500:
                raise SiteUnavailableError(
                    site=url, detail=f"HTTP {response.status}"
                )
        except PlaywrightTimeout as e:
            raise SiteUnavailableError(site=url, detail=f"Navigation timeout: {e}") from e

        logger.debug(f"Navigated to {url}")

    async def _step_fill(self, step: BlueprintStep) -> None:
        """Fill a text input."""
        selector = step.selector
        value = self._interpolate(step.value) or ""
        if not selector:
            raise ConnectionFailedError(
                site="unknown", detail="fill step requires a 'selector' field."
            )

        timeout = step.timeout or 10000
        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
        await self.page.fill(selector, value)
        logger.debug(f"Filled {selector}")

    async def _step_click(self, step: BlueprintStep) -> None:
        """Click an element, optionally waiting for navigation."""
        selector = step.selector
        if not selector:
            raise ConnectionFailedError(
                site="unknown", detail="click step requires a 'selector' field."
            )

        timeout = step.timeout or 10000
        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")

        if step.wait_for_navigation:
            async with self.page.expect_navigation(
                wait_until="domcontentloaded", timeout=step.timeout or 30000
            ):
                await self.page.click(selector)
        else:
            await self.page.click(selector)

        logger.debug(f"Clicked {selector}")

    async def _step_wait(self, step: BlueprintStep) -> None:
        """Wait for an element to appear."""
        selector = step.selector
        if not selector:
            raise ConnectionFailedError(
                site="unknown", detail="wait step requires a 'selector' field."
            )

        timeout = step.timeout or 10000
        await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
        logger.debug(f"Found {selector}")

    async def _step_screenshot(self, step: BlueprintStep) -> None:
        """Take a screenshot (debug only)."""
        name = step.screenshot_name or "debug"
        path = f"/tmp/plaidify_screenshot_{name}.png"
        await self.page.screenshot(path=path, full_page=False)
        logger.debug(f"Screenshot saved: {path}")

    async def _step_conditional(self, step: BlueprintStep) -> None:
        """Conditional branching based on selector presence."""
        selector = step.condition_selector
        if not selector:
            raise ConnectionFailedError(
                site="unknown", detail="conditional step requires 'condition_selector'."
            )

        timeout = step.timeout or 3000
        try:
            await self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            # Condition met — execute then_steps
            if step.then_steps:
                await self.execute_steps(step.then_steps, context="conditional_then")
        except PlaywrightTimeout:
            # Condition not met — execute else_steps
            if step.else_steps:
                await self.execute_steps(step.else_steps, context="conditional_else")

    async def _step_scroll(self, step: BlueprintStep) -> None:
        """Scroll the page."""
        if step.selector:
            # Scroll to element
            await self.page.locator(step.selector).scroll_into_view_if_needed()
        elif step.pixels:
            direction = step.direction or "down"
            delta = step.pixels if direction == "down" else -step.pixels
            await self.page.evaluate(f"window.scrollBy(0, {delta})")
        else:
            await self.page.evaluate("window.scrollBy(0, 500)")

        logger.debug("Scrolled page")

    async def _step_select(self, step: BlueprintStep) -> None:
        """Select a dropdown option."""
        selector = step.selector
        value = self._interpolate(step.value)
        if not selector or not value:
            raise ConnectionFailedError(
                site="unknown", detail="select step requires 'selector' and 'value'."
            )

        await self.page.select_option(selector, value)
        logger.debug(f"Selected {value} in {selector}")

    async def _step_iframe(self, step: BlueprintStep) -> None:
        """Switch into an iframe context."""
        selector = step.iframe_selector or step.selector
        if not selector:
            raise ConnectionFailedError(
                site="unknown", detail="iframe step requires 'iframe_selector' or 'selector'."
            )

        frame = self.page.frame_locator(selector)
        # Store frame reference for subsequent steps
        # Note: Playwright's frame_locator doesn't directly replace page,
        # so this is handled by using frame_locator in extraction
        logger.debug(f"Switched to iframe: {selector}")

    async def _step_wait_for_navigation(self, step: BlueprintStep) -> None:
        """Wait for a navigation event."""
        timeout = step.timeout or 30000
        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        logger.debug("Navigation complete")

    async def _step_execute_js(self, step: BlueprintStep) -> None:
        """Execute JavaScript in the page context."""
        script = step.script
        if not script:
            raise ConnectionFailedError(
                site="unknown", detail="execute_js step requires a 'script' field."
            )

        result = await self.page.evaluate(script)
        logger.debug(f"JS executed, result type: {type(result).__name__}")
