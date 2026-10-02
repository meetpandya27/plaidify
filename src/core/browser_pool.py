"""
Browser Pool Manager — manages a pool of Playwright browser contexts.

Provides:
- Async context manager for safe browser lifecycle
- Configurable concurrency (max simultaneous contexts)
- Session isolation (each connection gets its own BrowserContext)
- Request policy on every request: private-network refusal, the read-only
  policy, and resource blocking (images, fonts, analytics) for speed
- Stealth mode (randomized viewport, user-agent)
- Recovery from a crashed or disconnected browser
- Reaping of leaked contexts
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Optional
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from src import metrics
from src.config import get_settings
from src.core.circuit_breaker import CircuitBreaker
from src.core.network_policy import AddressPolicy, navigation_block_reason
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
from src.logging_config import get_logger

logger = get_logger("browser_pool")

_bp_settings = get_settings()
_browser_breaker = CircuitBreaker(
    "browser",
    failure_threshold=_bp_settings.browser_circuit_failure_threshold,
    reset_timeout=_bp_settings.browser_circuit_reset_seconds,
)

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

# Resource types to block for performance. Stylesheets are deliberately not
# blocked: without them, elements the site hides with CSS count as visible and
# MFA / outcome detection sees prompts that are not really on screen.
BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
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

# How long to let in-flight downloads finish before a result is assembled.
_DOWNLOAD_DRAIN_SECONDS = 10.0

_SANDBOX_FAILURE_MARKERS = ("sandbox", "zygote", "namespace", "setuid", "suid")


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class PooledContext:
    """A browser context managed by the pool."""

    context: BrowserContext
    session_id: str
    read_only_policy: Optional[ReadOnlyExecutionPolicy] = None
    download_dir: Optional[str] = None
    downloads: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    address_policy: Optional[AddressPolicy] = None
    leases: int = 1
    # Set when a redirect hop went somewhere the request policy would have refused.
    network_violation: Optional[str] = None
    background_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    download_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)

    def touch(self) -> None:
        """Update last_used timestamp."""
        self.last_used = time.time()

    @property
    def idle_seconds(self) -> float:
        """Seconds since last use."""
        return time.time() - self.last_used

    def spawn(self, coro: Awaitable[Any], *, download: bool = False) -> asyncio.Task:
        """Run ``coro`` in the background, keeping a reference until it finishes."""
        task = asyncio.ensure_future(coro)
        bucket = self.download_tasks if download else self.background_tasks
        bucket.add(task)

        def _done(finished: asyncio.Task) -> None:
            bucket.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.debug(
                    "Browser background task failed",
                    extra={"extra_data": {"session_id": self.session_id, "error": str(finished.exception())}},
                )

        task.add_done_callback(_done)
        return task

    def cancel_background(self) -> None:
        for task in list(self.background_tasks | self.download_tasks):
            task.cancel()


def _looks_like_sandbox_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _SANDBOX_FAILURE_MARKERS)


def _strip_query(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    return url.split("?", 1)[0].split("#", 1)[0]


# Statuses a browser will follow. 301/302/303 are retried as GET when the
# original request had a body; 307/308 keep the method.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _headers_for_hop(request: Any, method: str) -> dict[str, str]:
    """Headers for a redirect hop we fetch ourselves.

    The original ``Cookie`` is dropped so the browser's cookie jar is used,
    including a ``Set-Cookie`` from the response that caused the hop. ``Host``
    is dropped so the hop's own host is sent. A hop that became a GET does not
    keep the previous body headers.
    """
    raw = getattr(request, "headers", {}) or {}
    drop = {"cookie", "host"}
    if method in {"GET", "HEAD"}:
        drop |= {"content-length", "content-type"}
    return {str(key): str(value) for key, value in dict(raw).items() if str(key).lower() not in drop}


def _method_after_redirect(status: int, method: str) -> str:
    """The method a browser uses on the next hop of a redirect."""
    method = (method or "GET").upper()
    if status in (301, 302, 303) and method not in {"GET", "HEAD"}:
        return "GET"
    return method


def _response_headers(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", None) or {}
    if callable(headers):
        headers = headers()
    try:
        items = list(headers.items())
    except Exception:
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _redirect_target(current_url: str, response: Any) -> Optional[str]:
    """Absolute URL a redirect points at.

    ``None`` when ``response`` is not a redirect. An empty string means the
    hop must be refused: the location is missing, or it contains characters a
    browser and this check could read differently.
    """
    status = getattr(response, "status", None)
    if status not in _REDIRECT_STATUSES:
        return None
    location = _response_headers(response).get("location", "").strip()
    if not location or "\\" in location or any(ord(char) < 32 or ord(char) == 127 for char in location):
        return ""
    return urljoin(current_url, location)


class _HopRequest:
    """A redirect hop, with the shape the read-only policy expects of a request."""

    def __init__(self, url: str, method: str, *, navigation: bool, main_frame: bool) -> None:
        self.url = url
        self.method = method
        self.headers: dict[str, str] = {}
        self.resource_type = "document" if navigation else "xhr"
        self._navigation = navigation
        self._main_frame = main_frame
        self.redirected_from = None

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> Any:
        parent = None if self._main_frame else object()
        return type("Frame", (), {"parent_frame": parent})()


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
        self._allow_read_downloads: bool = settings.browser_allow_read_downloads
        self._download_root: str = settings.browser_download_root
        self._chromium_sandbox: bool = settings.browser_chromium_sandbox
        # A lease can never legitimately outlive a whole run (automation budget
        # plus the MFA wait), so anything idle longer than that has leaked.
        self._max_idle_seconds: float = max(
            float(self._idle_timeout),
            float(settings.engine_timeout_seconds + settings.mfa_timeout_seconds + 60),
        )
        self._default_address_policy = AddressPolicy(
            block_private=settings.browser_block_private_networks,
            allow_loopback=settings.demo_mode or settings.engine_allow_internal_connectors,
        )

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._contexts: dict[str, PooledContext] = {}
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._max_size)
        self._launch_lock: asyncio.Lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the browser pool — launch Playwright and the browser instance."""
        if self._running:
            return

        logger.info(
            "Starting browser pool",
            extra={
                "extra_data": {
                    "max_size": self._max_size,
                    "headless": self._headless,
                    "stealth": self._stealth,
                    "chromium_sandbox": self._chromium_sandbox,
                }
            },
        )

        self._playwright = await async_playwright().start()
        try:
            # Fail fast if browser launches keep failing (e.g. resource exhaustion).
            self._browser = await _browser_breaker.call(self._launch)
        except BaseException:
            # Don't leave the Playwright driver process behind.
            playwright, self._playwright = self._playwright, None
            try:
                await playwright.stop()
            except Exception:
                pass
            raise

        self._running = True
        metrics.set_browser_pool_capacity(self._max_size)

        # Start background cleanup of leaked contexts
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Browser pool started")

    async def _launch(self) -> Browser:
        assert self._playwright is not None
        try:
            browser = await self._playwright.chromium.launch(
                headless=self._headless,
                chromium_sandbox=self._chromium_sandbox,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except Exception as exc:
            if self._chromium_sandbox and _looks_like_sandbox_failure(exc):
                raise RuntimeError(
                    "Chromium could not start its sandbox. Run the browser as a non-root user in a "
                    "container that allows user namespaces (or grant the seccomp profile Chromium "
                    "needs). As a last resort, BROWSER_CHROMIUM_SANDBOX=false runs it without the "
                    "sandbox, which lets a browser exploit on a third-party page reach this "
                    f"process's secrets. Launch error: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc!r}"
                ) from exc
            raise
        browser.on("disconnected", self._on_browser_disconnected)
        return browser

    def _on_browser_disconnected(self, browser: Browser) -> None:
        if browser is self._browser:
            logger.warning(
                "Browser disconnected; it will be relaunched on the next acquire",
                extra={"extra_data": {"active_contexts": len(self._contexts)}},
            )

    @property
    def is_healthy(self) -> bool:
        """Whether the pool is running on a connected browser."""
        browser = self._browser
        if not self._running or browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    async def ensure_browser(self) -> Browser:
        """Return a connected browser, relaunching it if it crashed or disconnected."""
        if not self._running:
            raise RuntimeError("Browser pool is not started. Call start() first.")
        if self.is_healthy:
            assert self._browser is not None
            return self._browser
        async with self._launch_lock:
            if self.is_healthy:
                assert self._browser is not None
                return self._browser
            stale, self._browser = self._browser, None
            if stale is not None:
                try:
                    await stale.close()
                except Exception:
                    pass
            logger.info("Relaunching browser")
            self._browser = await _browser_breaker.call(self._launch)
            return self._browser

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
        for session_id, pooled in list(self._contexts.items()):
            await self._close_pooled(session_id, pooled)

        # Close browser and playwright
        browser, self._browser = self._browser, None
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        playwright, self._playwright = self._playwright, None
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass

        metrics.set_browser_pool_capacity(0)
        logger.info("Browser pool stopped")

    async def __aenter__(self) -> BrowserPool:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ── Leases ───────────────────────────────────────────────────────────

    async def acquire(
        self,
        session_id: str,
        proxy: Optional[dict] = None,
        read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
        address_policy: Optional[AddressPolicy] = None,
    ) -> PooledContext:
        """
        Acquire a browser context for the given session.

        Blocks if the pool is at max capacity. Acquiring a session that already
        holds a context shares it; the context closes when every holder has
        released it.

        Args:
            session_id: Unique session identifier.
            proxy: Optional proxy config {"server": "http://...", "username": "...", "password": "..."}.
            read_only_policy: Policy applied to this context's requests.
            address_policy: Which network addresses the browser may reach; defaults to
                the pool's settings-based policy.

        Returns:
            PooledContext with an isolated BrowserContext.
        """
        if not self._running:
            raise RuntimeError("Browser pool is not started. Call start() first.")

        existing = self._contexts.get(session_id)
        if existing is not None:
            return self._share(existing, read_only_policy, address_policy)

        await self._semaphore.acquire()
        context: Optional[BrowserContext] = None
        try:
            browser = await self.ensure_browser()
            context = await browser.new_context(**self._build_context_options(proxy))
            context.set_default_navigation_timeout(self._nav_timeout)
            context.set_default_timeout(self._action_timeout)

            pooled = PooledContext(
                context=context,
                session_id=session_id,
                read_only_policy=read_only_policy,
                address_policy=address_policy or self._default_address_policy,
                download_dir=self._ensure_download_dir(session_id),
            )
            await self._setup_request_policy(context, pooled)
            await self._setup_page_guards(context, pooled)
        except BaseException:
            # Whatever failed, hand the slot back; a lost permit would starve the pool.
            self._semaphore.release()
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            raise

        # Another acquire for the same session may have finished while we awaited.
        existing = self._contexts.get(session_id)
        if existing is not None:
            self._semaphore.release()
            try:
                await context.close()
            except Exception:
                pass
            if pooled.download_dir:
                shutil.rmtree(pooled.download_dir, ignore_errors=True)
            return self._share(existing, read_only_policy, address_policy)

        self._contexts[session_id] = pooled
        logger.debug(
            "Context acquired",
            extra={"extra_data": {"session_id": session_id, "pool_size": len(self._contexts)}},
        )
        metrics.set_browser_pool_active(len(self._contexts))
        return pooled

    def _share(
        self,
        pooled: PooledContext,
        read_only_policy: Optional[ReadOnlyExecutionPolicy],
        address_policy: Optional[AddressPolicy],
    ) -> PooledContext:
        pooled.leases += 1
        pooled.touch()
        if read_only_policy is not None:
            pooled.read_only_policy = read_only_policy
        if address_policy is not None:
            pooled.address_policy = address_policy
        return pooled

    async def release(self, session_id: str) -> None:
        """
        Release a browser context. Idempotent: releasing an unknown or already
        closed session does nothing.

        Args:
            session_id: The session to release.
        """
        pooled = self._contexts.get(session_id)
        if pooled is None:
            return
        pooled.leases -= 1
        if pooled.leases > 0:
            return
        await self._close_pooled(session_id, pooled)

    async def _close_pooled(self, session_id: str, pooled: PooledContext) -> None:
        # Unregister and free the slot before awaiting anything, so a second
        # release (the engine's finally racing the reaper) can't double count.
        if self._contexts.get(session_id) is not pooled:
            return
        del self._contexts[session_id]
        self._semaphore.release()
        metrics.set_browser_pool_active(len(self._contexts))

        pooled.cancel_background()
        try:
            await pooled.context.close()
        except Exception as e:
            logger.warning(
                "Error closing context",
                extra={"extra_data": {"session_id": session_id, "error": str(e).splitlines()[0] if str(e) else ""}},
            )
        if pooled.download_dir:
            shutil.rmtree(pooled.download_dir, ignore_errors=True)
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
            # Never accept a bad certificate: the next step types the user's
            # credentials into whatever page answered.
            "ignore_https_errors": False,
            "java_script_enabled": True,
            "accept_downloads": True,
            # A service worker's fetches would bypass the request policy.
            "service_workers": "block",
        }

        if self._stealth:
            options["user_agent"] = random.choice(USER_AGENTS)
            options["viewport"] = random.choice(VIEWPORTS)
            options["locale"] = "en-US"
            options["timezone_id"] = "America/New_York"

        if proxy:
            options["proxy"] = proxy

        return options

    def _ensure_download_dir(self, session_id: str) -> str:
        os.makedirs(self._download_root, exist_ok=True)
        return tempfile.mkdtemp(prefix=f"{session_id[:12]}-", dir=self._download_root)

    # ── Request policy ───────────────────────────────────────────────────

    async def _network_block_reason(self, pooled: PooledContext, url: str) -> Optional[str]:
        policy = pooled.address_policy
        if policy is None or not policy.active:
            return None
        return await policy.url_block_reason(url)

    def _record(self, pooled: PooledContext, action: str, reason: str, target: Optional[str]) -> None:
        policy = pooled.read_only_policy
        if policy is not None:
            policy.record_blocked(action, reason, target=target)
        logger.warning(
            "Browser request refused",
            extra={
                "extra_data": {
                    "session_id": pooled.session_id,
                    "action": action,
                    "phase": policy.phase.value if policy is not None else None,
                    "url": target,
                    "reason": reason,
                }
            },
        )

    async def _setup_request_policy(
        self,
        context: BrowserContext,
        pooled: PooledContext,
    ) -> None:
        """Route every request through the network, read-only and resource policies."""

        async def handle(route) -> None:
            request = route.request
            pooled.touch()
            try:
                reason = await self._network_block_reason(pooled, request.url)
                if reason:
                    self._record(pooled, "network", reason, _strip_query(request.url))
                    await route.abort("blockedbyclient")
                    return

                policy = pooled.read_only_policy
                if policy is not None:
                    reason = policy.evaluate_request(request)
                    if reason:
                        self._record(pooled, "request", reason, _strip_query(request.url))
                        await route.abort("blockedbyclient")
                        return

                if self._block_resources:
                    if request.resource_type in BLOCKED_RESOURCE_TYPES:
                        await route.abort()
                        return

                    url = request.url.lower()
                    for pattern in BLOCKED_URL_PATTERNS:
                        if pattern in url:
                            await route.abort()
                            return

                # continue_() lets Chromium follow the whole redirect chain, and
                # those hops never re-enter this route — the refused request has
                # already been sent by the time the request event fires. Fetch
                # with no automatic redirects and refuse Location first.
                await self._fulfill_checked(route, pooled)
            except Exception as exc:
                # Fail closed; the route may also already be gone with its page.
                logger.debug(
                    "Request routing failed",
                    extra={
                        "extra_data": {
                            "session_id": pooled.session_id,
                            "error": str(exc).splitlines()[0] if str(exc) else "",
                        }
                    },
                )
                try:
                    await route.abort()
                except Exception:
                    pass

        await context.route("**/*", handle)

        async def handle_websocket(ws) -> None:
            reason = await self._network_block_reason(pooled, ws.url)
            if reason:
                self._record(pooled, "websocket", reason, _strip_query(ws.url))
                try:
                    await ws.close(code=1008, reason="blocked")
                except Exception:
                    pass
                return
            ws.connect_to_server()

        if pooled.address_policy is not None and pooled.address_policy.active:
            try:
                await context.route_web_socket("**/*", handle_websocket)
            except Exception as exc:  # pragma: no cover - depends on the Playwright build
                logger.warning("WebSocket routing unavailable", extra={"extra_data": {"error": str(exc)}})

        # Backstop. _fulfill_checked refuses a Location before the browser asks
        # for it; this still stops a hop that arrived as a request anyway (a
        # page-level route, or a chain this handler did not see).
        context.on("request", lambda request: self._on_request(request, pooled))

    async def _redirect_block_reason(
        self,
        pooled: PooledContext,
        request: Any,
        status: int,
        next_url: str,
        method: Optional[str] = None,
    ) -> Optional[str]:
        """Why the browser must not request ``next_url`` as the next hop, or None."""
        if not next_url:
            return "redirect location is missing or malformed"
        parts = urlsplit(next_url)
        scheme = (parts.scheme or "").lower()
        if scheme not in {"http", "https"} or not parts.hostname:
            return "redirect to a non-http(s) URL is not allowed"
        if parts.username is not None or parts.password is not None:
            return "redirect URL carries credentials"

        policy = pooled.read_only_policy
        navigation = ReadOnlyExecutionPolicy._is_navigation(request)
        main_frame = navigation and ReadOnlyExecutionPolicy._is_main_frame(request)
        # Host scoping needs no DNS, so an off-domain navigation is refused
        # before a name lookup.
        if policy is not None and main_frame and policy.host_rules:
            reason = navigation_block_reason(next_url, policy.host_rules)
            if reason:
                return reason

        reason = await self._network_block_reason(pooled, next_url)
        if reason:
            return reason
        if policy is None:
            return None
        hop = _HopRequest(
            next_url,
            _method_after_redirect(status, method or str(getattr(request, "method", "GET"))),
            navigation=navigation,
            main_frame=main_frame,
        )
        return policy.evaluate_request(hop)

    async def _fulfill_checked(self, route: Any, pooled: PooledContext) -> None:
        """Send this request without letting the browser follow an unchecked redirect.

        Playwright does not route a hop Chromium follows itself (``Fetch.continueRequest``
        on ``redirectedFrom``), so a refused ``Location`` would already be on the wire.
        The chain is followed here, with each hop checked before it is fetched. A
        main-frame navigation is then handed back as one redirect to the final URL,
        and only when that response itself was not a redirect.
        """
        request = route.request
        current_url = str(request.url)
        method = str(getattr(request, "method", "GET") or "GET").upper()
        response = await route.fetch(max_redirects=0)
        followed = 0
        while getattr(response, "status", None) in _REDIRECT_STATUSES:
            if followed >= 20:
                reason = "too many redirects"
                pooled.network_violation = f"redirect to {_strip_query(current_url)} refused: {reason}"
                self._record(pooled, "redirect", reason, _strip_query(current_url))
                await route.abort("blockedbyclient")
                return
            status = int(response.status)
            target = _redirect_target(current_url, response) or ""
            reason = await self._redirect_block_reason(pooled, request, status, target, method)
            if reason:
                shown = _strip_query(target) if target else None
                pooled.network_violation = f"redirect to {shown or 'an unreadable location'} refused: {reason}"
                self._record(pooled, "redirect", reason, shown)
                await route.abort("blockedbyclient")
                return
            method = _method_after_redirect(status, method)
            current_url = target
            followed += 1
            hop: dict[str, Any] = {
                "url": current_url,
                "method": method,
                "headers": _headers_for_hop(request, method),
                "max_redirects": 0,
            }
            # An empty body stops a POST's payload being replayed on a GET hop.
            # b"" is falsy, so the fetch sends no body rather than the original one.
            if method in {"GET", "HEAD"}:
                hop["post_data"] = b""
            response = await route.fetch(**hop)

        # A 302 Chromium follows is not routed. Hand it only the last URL, whose
        # response did not redirect, so there is no further Location to chase.
        navigation = ReadOnlyExecutionPolicy._is_navigation(request)
        main_frame = navigation and ReadOnlyExecutionPolicy._is_main_frame(request)
        if main_frame and current_url != str(request.url):
            await route.fulfill(status=302, headers={"location": current_url})
            return
        await route.fulfill(response=response)

    def _on_request(self, request: Any, pooled: PooledContext) -> None:
        if getattr(request, "redirected_from", None) is None:
            return
        pooled.touch()
        pooled.spawn(self._check_redirect(request, pooled))

    async def _check_redirect(self, request: Any, pooled: PooledContext) -> None:
        url = request.url
        reason = await self._network_block_reason(pooled, url)
        policy = pooled.read_only_policy
        if not reason and policy is not None and policy.host_rules:
            try:
                is_navigation = request.is_navigation_request()
                main_frame = is_navigation and request.frame.parent_frame is None
            except Exception:
                main_frame = False
            if main_frame:
                reason = navigation_block_reason(url, policy.host_rules)
        if not reason:
            return

        pooled.network_violation = f"redirect to {_strip_query(url)} refused: {reason}"
        self._record(pooled, "redirect", reason, _strip_query(url))
        # Stop the pages before anything can be read from what came back; the
        # engine turns the failure into a policy violation.
        for page in list(pooled.context.pages):
            pooled.spawn(page.close())

    async def _setup_page_guards(
        self,
        context: BrowserContext,
        pooled: PooledContext,
    ) -> None:
        def on_page(page) -> None:
            self._register_page_handlers(page, pooled)
            if (
                pooled.read_only_policy
                and pooled.read_only_policy.enabled
                and pooled.read_only_policy.phase == ExecutionPhase.READ
                and len(context.pages) > 1
            ):
                pooled.spawn(self._close_extra_page(page, pooled))

        context.on("page", on_page)

    def _register_page_handlers(self, page: Any, pooled: PooledContext) -> None:
        page.on(
            "download",
            lambda download: pooled.spawn(self._handle_download(download, pooled), download=True),
        )
        page.on(
            "dialog",
            lambda dialog: pooled.spawn(dialog.dismiss()),
        )

    async def _close_extra_page(self, page: Any, pooled: PooledContext) -> None:
        policy = pooled.read_only_policy
        if policy is not None:
            policy.record_blocked(
                "popup",
                "extra browser pages are blocked after authentication in strict read-only mode",
            )
        try:
            await page.close()
        except Exception as exc:
            logger.warning(
                "Failed to close extra page",
                extra={"extra_data": {"session_id": pooled.session_id, "error": str(exc)}},
            )

    # ── Downloads ────────────────────────────────────────────────────────

    async def _handle_download(self, download: Any, pooled: PooledContext) -> None:
        policy = pooled.read_only_policy
        download_url = getattr(download, "url", None)

        if policy is not None and policy.enabled:
            if policy.phase != ExecutionPhase.READ or not self._allow_read_downloads:
                reason = "downloads are only allowed during the read phase in strict read-only mode"
                policy.record_blocked("download", reason, target=_strip_query(download_url))
                cancel = getattr(download, "cancel", None)
                if callable(cancel):
                    try:
                        await cancel()
                    except Exception:
                        pass
                return

        suggested_filename = getattr(download, "suggested_filename", None) or "download.bin"
        destination = self._unique_download_path(
            pooled.download_dir or self._ensure_download_dir(pooled.session_id), suggested_filename
        )

        try:
            await download.save_as(destination)
            size_bytes = os.path.getsize(destination)
            pooled.downloads.append(
                {
                    "filename": os.path.basename(destination),
                    "size_bytes": size_bytes,
                    "url": download_url,
                    "path": destination,
                }
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist browser download",
                extra={"extra_data": {"session_id": pooled.session_id, "error": str(exc)}},
            )

    async def collect_downloads(self, pooled: PooledContext, *, max_bytes: int) -> list[dict[str, Any]]:
        """Downloads captured in this context, with their content, before release deletes the files.

        Waits briefly for downloads still being saved. Files up to ``max_bytes``
        are returned base64-encoded; larger ones are listed with ``omitted``.
        """
        pending = list(pooled.download_tasks)
        if pending:
            _done, still_running = await asyncio.wait(pending, timeout=_DOWNLOAD_DRAIN_SECONDS)
            for task in still_running:
                task.cancel()

        collected: list[dict[str, Any]] = []
        for item in pooled.downloads:
            entry: dict[str, Any] = {
                "filename": item["filename"],
                "size_bytes": item["size_bytes"],
                "url": _strip_query(item.get("url")),
            }
            if item["size_bytes"] > max_bytes:
                entry["omitted"] = "too_large"
            else:
                try:
                    with open(item["path"], "rb") as handle:
                        entry["content_base64"] = base64.b64encode(handle.read()).decode("ascii")
                except OSError:
                    entry["omitted"] = "unreadable"
            collected.append(entry)
        return collected

    def _unique_download_path(self, directory: str, filename: str) -> str:
        base_name = os.path.basename(filename) or "download.bin"
        stem, suffix = os.path.splitext(base_name)
        candidate = os.path.join(directory, base_name)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{stem}-{counter}{suffix}")
            counter += 1
        return candidate

    # ── Leak reaper ──────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Background task that closes leaked contexts."""
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
        """Close contexts with no activity for longer than any run could last.

        Activity is every request the context makes and every acquire; a run
        waiting for an MFA code is covered because the limit includes the MFA
        budget.
        """
        stale = [
            (session_id, pooled)
            for session_id, pooled in list(self._contexts.items())
            if pooled.idle_seconds > self._max_idle_seconds
        ]
        for session_id, pooled in stale:
            logger.info(
                "Closing leaked context",
                extra={"extra_data": {"session_id": session_id, "idle_seconds": int(pooled.idle_seconds)}},
            )
            await self._close_pooled(session_id, pooled)


# ── Singleton ─────────────────────────────────────────────────────────────────

_pool: Optional[BrowserPool] = None
_pool_lock: Optional[asyncio.Lock] = None
_pool_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock, _pool_lock_loop
    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_lock_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
    return _pool_lock


async def get_browser_pool() -> BrowserPool:
    """
    Get the global browser pool instance.

    Creates and starts the pool on first call (lazily, once, even when many
    callers race), and relaunches its browser if it crashed.
    """
    global _pool
    pool = _pool
    if pool is None or not pool._running:
        async with _get_pool_lock():
            if _pool is None or not _pool._running:
                candidate = BrowserPool()
                await candidate.start()
                _pool = candidate
            pool = _pool
    if not pool.is_healthy:
        await pool.ensure_browser()
    return pool


async def shutdown_browser_pool() -> None:
    """Shut down the global browser pool."""
    global _pool
    pool, _pool = _pool, None
    if pool:
        await pool.stop()
