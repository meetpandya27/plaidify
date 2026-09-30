"""Async Redis for the engine (MFA sessions, per-site rate limits).

The engine polls Redis while a connection waits for an MFA code, so these
calls must never block the event loop: this uses ``redis.asyncio`` with socket
timeouts, and no PING before each call (the connection pool reconnects on its
own). A client is bound to the event loop that created it, so one is kept per
loop.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Optional

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("async_redis")
settings = get_settings()

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def get_async_redis() -> Optional[Any]:
    """The engine's async Redis client for the running loop, or None without REDIS_URL.

    Raises:
        RuntimeError: in production when REDIS_URL is not configured, since MFA
            codes and rate limits must be shared between workers.
    """
    if not settings.redis_url:
        if settings.env == "production":
            raise RuntimeError("REDIS_URL is required in production for MFA sessions and site rate limits.")
        return None

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        import redis.asyncio as redis_asyncio

        timeout = settings.engine_redis_socket_timeout
        client = redis_asyncio.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            health_check_interval=30,
        )
        _clients[loop] = client
    return client


async def close_async_redis() -> None:
    """Close the running loop's client (used at shutdown and in tests)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _clients.pop(loop, None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - best effort
            pass
