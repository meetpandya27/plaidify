"""Honour a blueprint's ``rate_limit``: how often one account may be signed in to a site.

Limits are per account (site + username), since that is what a site sees and
what gets accounts locked: at most ``max_requests_per_hour`` connections in any
rolling hour, and at least ``min_interval_seconds`` between two of them.
Attempts count, whether or not they succeed.

With REDIS_URL set the window is shared by every worker (an atomic Lua
script); otherwise it is kept in this process. Usernames are hashed before
they are used as keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from collections import deque
from typing import Any, Callable, Optional

from src.core.async_redis import get_async_redis
from src.core.blueprint import RateLimitConfig
from src.exceptions import RateLimitedError
from src.logging_config import get_logger

logger = get_logger("site_rate_limit")

_WINDOW_SECONDS = 3600
_KEY_PREFIX = "plaidify:site_rate:"

# KEYS[1] = window key; ARGV = now, window, max_requests, min_interval, member
# Returns 0 when the attempt is recorded, else the seconds to wait.
_REDIS_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])
local min_interval = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local last = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if last[2] and now - tonumber(last[2]) < min_interval then
  return tostring(min_interval - (now - tonumber(last[2])))
end
if redis.call('ZCARD', KEYS[1]) >= max_requests then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return tostring(tonumber(first[2]) + window - now)
end
redis.call('ZADD', KEYS[1], now, ARGV[5])
redis.call('EXPIRE', KEYS[1], window)
return '0'
"""


def _account_key(site: str, username: str) -> str:
    digest = hashlib.sha256(f"{site}\0{username}".encode()).hexdigest()[:32]
    return f"{_KEY_PREFIX}{site}:{digest}"


class SiteRateLimiter:
    """Per-account attempt limiter for blueprint connections."""

    def __init__(
        self,
        redis_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis_factory = redis_factory or get_async_redis
        self._clock = clock
        self._windows: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, site: str, username: str, config: Optional[RateLimitConfig]) -> None:
        """Record an attempt, or raise RateLimitedError with how long to wait."""
        if config is None:
            return
        key = _account_key(site, username or "")
        redis_client = self._redis_factory()
        if redis_client is not None:
            wait = await self._acquire_redis(redis_client, key, config)
        else:
            wait = await self._acquire_local(key, config)
        if wait > 0:
            retry_after = max(1, math.ceil(wait))
            logger.warning(
                "Site rate limit reached for this account",
                extra={"extra_data": {"site": site, "retry_after": retry_after}},
            )
            raise RateLimitedError(retry_after=retry_after)

    async def _acquire_redis(self, redis_client: Any, key: str, config: RateLimitConfig) -> float:
        result = await redis_client.eval(
            _REDIS_SCRIPT,
            1,
            key,
            self._clock(),
            _WINDOW_SECONDS,
            config.max_requests_per_hour,
            config.min_interval_seconds,
            uuid.uuid4().hex,
        )
        return float(result or 0)

    async def _acquire_local(self, key: str, config: RateLimitConfig) -> float:
        async with self._lock:
            now = self._clock()
            window = self._windows.setdefault(key, deque())
            while window and window[0] <= now - _WINDOW_SECONDS:
                window.popleft()
            if window and now - window[-1] < config.min_interval_seconds:
                return config.min_interval_seconds - (now - window[-1])
            if len(window) >= config.max_requests_per_hour:
                return window[0] + _WINDOW_SECONDS - now
            window.append(now)
            if len(self._windows) > 10_000:
                # Drop accounts with nothing left in their window.
                for stale in [k for k, v in self._windows.items() if not v or v[-1] <= now - _WINDOW_SECONDS]:
                    self._windows.pop(stale, None)
            return 0.0


_limiter: Optional[SiteRateLimiter] = None


def get_site_rate_limiter() -> SiteRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = SiteRateLimiter()
    return _limiter
