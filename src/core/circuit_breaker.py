"""Resilience primitives: an async circuit breaker and exponential-backoff retry.

These wrap calls to flaky external dependencies (LLM providers, the browser
pool) so a degraded dependency fails fast instead of making every request pay
the full timeout cost, and so transient errors get a bounded, jittered retry.
"""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence, TypeVar

from src.logging_config import get_logger

logger = get_logger("resilience")

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, name: str, retry_after: float):
        super().__init__(f"Circuit '{name}' is open; failing fast (retry in ~{retry_after:.0f}s)")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    """A minimal async circuit breaker.

    After ``failure_threshold`` consecutive failures the breaker opens and
    rejects calls for ``reset_timeout`` seconds. It then allows a single trial
    call (half-open); a success closes it, a failure re-opens it.
    """

    def __init__(self, name: str, *, failure_threshold: int = 5, reset_timeout: float = 30.0):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.reset_timeout = max(0.0, reset_timeout)
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    def reset(self) -> None:
        """Force the breaker back to a clean closed state (mainly for tests)."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.reset_timeout:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit '%s' entering half-open trial", self.name)
                else:
                    raise CircuitBreakerOpenError(self.name, self.reset_timeout - elapsed)

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is not CircuitState.CLOSED:
                logger.info("Circuit '%s' closed (recovered)", self.name)
            self._failures = 0
            self._state = CircuitState.CLOSED

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("Circuit '%s' opened after %d consecutive failure(s)", self.name, self._failures)

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Run ``fn`` through the breaker, recording success/failure."""
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 2,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: bool = True,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    delay_for: Optional[Callable[[BaseException], Optional[float]]] = None,
) -> T:
    """Call async ``fn`` with exponential backoff on the given exceptions.

    ``delay_for`` may inspect the raised exception and return an explicit delay
    (e.g. honouring a provider ``retry-after`` header); it is clamped to
    ``max_delay``. Returns the first successful result or re-raises the final
    exception after ``retries`` extra attempts are exhausted.
    """
    attempt = 0
    retry_types = tuple(retry_on)
    while True:
        try:
            return await fn()
        except retry_types as exc:
            if attempt >= retries:
                raise
            delay = None
            if delay_for is not None:
                delay = delay_for(exc)
            if delay is None:
                delay = base_delay * (2**attempt)
            delay = min(max_delay, max(0.0, delay))
            if jitter:
                delay *= 0.5 + random.random() / 2
            logger.warning(
                "Retrying after error (attempt %d/%d, sleeping %.2fs): %s",
                attempt + 1,
                retries,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            attempt += 1
