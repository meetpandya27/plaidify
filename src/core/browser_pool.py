"""
Browser Pool Manager — manages a pool of Playwright browser contexts.

Provides:
- Async context manager for safe browser lifecycle
- Configurable concurrency (max simultaneous contexts)
- Session isolation (each connection gets its own BrowserContext)
- Resource blocking (images, fonts, analytics) for speed
- Stealth mode (randomized viewport, user-agent)
- Automatic cleanup of idle contexts
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("browser_pool")

# ── Stealth Profiles ─────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

# Resource types to block for performance
BLOCKED_RESOURCE_TYPES = {"image", "font", "media", "stylesheet"}
BLOCKED_URL_PATTERNS = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "doubleclick.net",
    "hotjar.com",
    "mixpanel.com",
    "segment.io",
    "amplitude.com",
]


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class PooledContext:
    """A browser context managed by the pool."""

    context: BrowserContext
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        """Update last_used timestamp."""
        self.last_used = time.time()

    @property
    def idle_seconds(self) -> float:
        """Seconds since last use."""
        return time.time() - self.last_used


# ── Browser Pool ──────────────────────────────────────────────────────────────


class BrowserPool:
    """
    Manages a pool of Playwright browser contexts.

    Usage:
        pool = BrowserPool()
        await pool.start()

        ctx = await pool.acquire("session_123")
        page = await ctx.context.new_page()
        # ... use page ...
        await pool.release("session_123")

        await pool.stop()

    Or as an async context manager:
        async with BrowserPool() as pool:
            ctx = await pool.acquire("session_123")
            ...
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._max_size: int = settings.browser_pool_size
        self._headless: bool = settings.browser_headless
        self._idle_timeout: int = settings.browser_idle_timeout
        self._block_resources: bool = settings.browser_block_resources
        self._stealth: bool = settings.browser_stealth
        self._nav_timeout: int = settings.browser_navigation_timeout
        self._action_timeout: int = settings.browser_action_timeout

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._contexts: dict[str, PooledContext] = {}
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._max_size)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running: bool = False

    async def start(self) -> None:
        """Start the browser pool — launch Playwright and the browser instance."""
        if self._running:
            return

        logger.info(
            "Starting browser pool",
            extra={"extra_data": {
                "max_size": self._max_size,
                "headless": self._headless,
                "stealth": self._stealth,
            }},
        )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._running = True

        # Start background cleanup of idle contexts
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Browser pool started")

    async def stop(self) -> None:
        """Stop the browser pool — close all contexts, browser, and Playwright."""
        if not self._running:
            return

        self._running = False

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all contexts
        async with self._lock:
            for session_id, pooled in list(self._contexts.items()):
                try:
                    await pooled.context.close()
                except Exception:
                    pass
            self._contexts.clear()

        # Close browser and playwright
        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("Browser pool stopped")

    async def __aenter__(self) -> BrowserPool:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def acquire(
        self,
        session_id: str,
        proxy: Optional[dict] = None,
    ) -> PooledContext:
        """
        Acquire a browser context for the given session.

        Blocks if the pool is at max capacity.

        Args:
            session_id: Unique session identifier.
            proxy: Optional proxy config {"server": "http://...", "username": "...", "password": "..."}.

        Returns:
            PooledContext with an isolated BrowserContext.
        """
        await self._semaphore.acquire()

        async with self._lock:
            # Return existing context if session already has one
            if session_id in self._contexts:
                self._contexts[session_id].touch()
                self._semaphore.release()  # Didn't actually use a new slot
                return self._contexts[session_id]

            if not self._browser:
                self._semaphore.release()
                raise RuntimeError("Browser pool is not started. Call start() first.")

            # Build context options
            context_options = self._build_context_options(proxy)

            context = await self._browser.new_context(**context_options)
            context.set_default_navigation_timeout(self._nav_timeout)
            context.set_default_timeout(self._action_timeout)

            # Set up resource blocking
            if self._block_resources:
                await self._setup_resource_blocking(context)

            pooled = PooledContext(context=context, session_id=session_id)
            self._contexts[session_id] = pooled

            logger.debug(
                "Context acquired",
                extra={"extra_data": {"session_id": session_id, "pool_size": len(self._contexts)}},
            )

            return pooled

    async def release(self, session_id: str) -> None:
        """
        Release and close a browser context.

        Args:
            session_id: The session to release.
        """
        async with self._lock:
            pooled = self._contexts.pop(session_id, None)
            if pooled:
                try:
                    await pooled.context.close()
                except Exception as e:
                    logger.warning(
                        "Error closing context",
                        extra={"extra_data": {"session_id": session_id, "error": str(e)}},
                    )

        self._semaphore.release()
        logger.debug(
            "Context released",
            extra={"extra_data": {"session_id": session_id, "pool_size": len(self._contexts)}},
        )

    @property
    def active_count(self) -> int:
        """Number of active browser contexts."""
        return len(self._contexts)

    @property
    def available_slots(self) -> int:
        """Number of available slots in the pool."""
        return self._max_size - len(self._contexts)

    def _build_context_options(self, proxy: Optional[dict] = None) -> dict:
        """Build Playwright BrowserContext options with optional stealth."""
        options: dict = {
            "ignore_https_errors": True,
            "java_script_enabled": True,
        }

        if self._stealth:
            options["user_agent"] = random.choice(USER_AGENTS)
            options["viewport"] = random.choice(VIEWPORTS)
            options["locale"] = "en-US"
            options["timezone_id"] = "America/New_York"

        if proxy:
            options["proxy"] = proxy

        return options

    async def _setup_resource_blocking(self, context: BrowserContext) -> None:
        """Set up route-based resource blocking on a context."""

        async def block_resources(route):
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return

            url = route.request.url.lower()
            for pattern in BLOCKED_URL_PATTERNS:
                if pattern in url:
                    await route.abort()
                    return

            await route.continue_()

        await context.route("**/*", block_resources)

    async def _cleanup_loop(self) -> None:
        """Background task that closes idle contexts."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                await self._cleanup_idle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Cleanup loop error",
                    extra={"extra_data": {"error": str(e)}},
                )

    async def _cleanup_idle(self) -> None:
        """Close contexts that have been idle longer than the timeout."""
        to_remove: list[str] = []

        async with self._lock:
            for session_id, pooled in self._contexts.items():
                if pooled.idle_seconds > self._idle_timeout:
                    to_remove.append(session_id)

        for session_id in to_remove:
            logger.info(
                "Closing idle context",
                extra={"extra_data": {"session_id": session_id}},
            )
            await self.release(session_id)


# ── Singleton ─────────────────────────────────────────────────────────────────

_pool: Optional[BrowserPool] = None


async def get_browser_pool() -> BrowserPool:
    """
    Get the global browser pool instance.

    Creates and starts the pool on first call (lazy initialization).
    """
    global _pool
    if _pool is None or not _pool._running:
        _pool = BrowserPool()
        await _pool.start()
    return _pool


async def shutdown_browser_pool() -> None:
    """Shut down the global browser pool."""
    global _pool
    if _pool:
        await _pool.stop()
        _pool = None
