"""Small, failure-tolerant probes of what a page is showing.

Used to recognise login and MFA outcomes ("signed in", "wrong password",
"code rejected") declared in a blueprint. A probe that errors — the page is
mid-navigation, the frame was replaced — answers "not present", never raises.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from src.core.blueprint import OutcomeCheck

Probe = Callable[[], Awaitable[bool]]

_MAX_ELEMENTS = 10
_TEXT_TIMEOUT_MS = 1000


async def selector_visible(page: Any, selector: str) -> bool:
    """Whether any element matching ``selector`` is visible right now."""
    try:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(min(count, _MAX_ELEMENTS)):
            if await locator.nth(index).is_visible():
                return True
    except Exception:
        return False
    return False


async def indicator_present(page: Any, check: Optional[OutcomeCheck]) -> bool:
    """Whether the page currently shows ``check``'s selector and/or text."""
    if check is None:
        return False
    pattern = check.pattern
    try:
        if check.selector:
            locator = page.locator(check.selector)
            count = await locator.count()
            for index in range(min(count, _MAX_ELEMENTS)):
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                if pattern is None:
                    return True
                if pattern.search(await element.inner_text(timeout=_TEXT_TIMEOUT_MS)):
                    return True
            return False
        if pattern is None:
            return False
        return bool(pattern.search(await page.inner_text("body", timeout=_TEXT_TIMEOUT_MS)))
    except Exception:
        return False


async def first_present(
    probes: Sequence[tuple[str, Probe]],
    *,
    timeout_ms: float,
    poll_ms: float = 250,
) -> Optional[str]:
    """Poll ``probes`` in order until one answers True; return its name, or None on timeout.

    Every probe is checked at least once, even with a zero timeout.
    """
    deadline = time.monotonic() + max(0.0, timeout_ms) / 1000
    while True:
        for name, probe in probes:
            if await probe():
                return name
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(poll_ms / 1000, remaining))
