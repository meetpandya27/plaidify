"""
Redis-backed session store for link sessions, link scopes, and webhook deliveries.

Falls back to in-memory dicts when Redis is not configured (single-worker dev mode).
Supports Redis pub/sub for cross-worker link session event notifications.
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("session_store")
settings = get_settings()

# TTLs
LINK_SESSION_TTL = 600  # 10 minutes (reduced from 30 for production security)
LINK_SCOPE_TTL = 600  # 10 minutes
LINK_LAUNCH_BOOTSTRAP_TTL = settings.link_launch_token_expire_seconds
WEBHOOK_DELIVERY_TTL = 86400  # 24 hours
WEBHOOK_DELIVERY_MAX = 200  # max deliveries stored per webhook

# Max in-memory entries (evict oldest when exceeded)
_MAX_MEM_LINK_SESSIONS = 10_000
_MAX_MEM_LINK_SCOPES = 10_000
_MAX_MEM_LINK_LAUNCH_BOOTSTRAPS = 10_000
_MAX_MEM_WEBHOOK_DELIVERIES = 5_000


def _get_redis():
    """Return a Redis client or None if not configured."""
    if not settings.redis_url:
        if settings.env == "production":
            raise RuntimeError("REDIS_URL is required in production for link session storage.")
        return None
    try:
        import redis

        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as e:
        if settings.env == "production":
            raise RuntimeError("Redis is unavailable for link session storage in production.") from e

        logger.warning(f"Failed to connect to Redis: {e}")
        return None


_redis_client = None


def _redis():
    """Lazy singleton Redis client. Reconnects if the cached client is dead."""
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
        except Exception as exc:
            if settings.env == "production":
                raise RuntimeError("Redis connection lost for link session storage.") from exc

            logger.warning("Redis connection lost, reconnecting...")
            _redis_client = None
    if _redis_client is None:
        _redis_client = _get_redis()
    return _redis_client


# ══════════════════════════════════════════════════════════════════════════════
# Link Session Store
# ══════════════════════════════════════════════════════════════════════════════

# In-memory fallback
_mem_link_sessions: Dict[str, Dict[str, Any]] = {}


def _evict_expired_sessions() -> None:
    """Remove expired sessions and enforce max size on the in-memory store."""
    now = time.time()
    expired = [k for k, v in _mem_link_sessions.items() if now - v.get("created_at", 0) > LINK_SESSION_TTL]
    for k in expired:
        del _mem_link_sessions[k]
    # If still over limit, evict oldest
    if len(_mem_link_sessions) > _MAX_MEM_LINK_SESSIONS:
        sorted_keys = sorted(_mem_link_sessions, key=lambda k: _mem_link_sessions[k].get("created_at", 0))
        for k in sorted_keys[: len(_mem_link_sessions) - _MAX_MEM_LINK_SESSIONS]:
            del _mem_link_sessions[k]


def create_link_session(link_token: str, data: Dict[str, Any]) -> None:
    """Create a new link session."""
    data["created_at"] = data.get("created_at", time.time())
    r = _redis()
    if r:
        key = f"plaidify:link_session:{link_token}"
        # Don't serialize 'subscribers' — those are local asyncio.Queue objects
        serializable = {k: v for k, v in data.items() if k != "subscribers"}
        r.set(key, json.dumps(serializable), ex=LINK_SESSION_TTL)
    else:
        data.setdefault("subscribers", [])
        _evict_expired_sessions()
        _mem_link_sessions[link_token] = data


def get_link_session(link_token: str) -> Optional[Dict[str, Any]]:
    """Get a link session, returning None if not found or expired."""
    r = _redis()
    if r:
        key = f"plaidify:link_session:{link_token}"
        raw = r.get(key)
        if not raw:
            return None
        session = json.loads(raw)
        # Check TTL-based expiry (belt and suspenders — Redis TTL is primary)
        if time.time() - session.get("created_at", 0) > LINK_SESSION_TTL:
            session["status"] = "expired"
        return session
    else:
        session = _mem_link_sessions.get(link_token)
        if not session:
            return None
        if time.time() - session.get("created_at", 0) > LINK_SESSION_TTL:
            session["status"] = "expired"
        return session


def update_link_session(link_token: str, updates: Dict[str, Any]) -> bool:
    """Update fields on an existing link session. Returns False if not found."""
    r = _redis()
    if r:
        key = f"plaidify:link_session:{link_token}"
        raw = r.get(key)
        if not raw:
            return False
        session = json.loads(raw)
        session.update({k: v for k, v in updates.items() if k != "subscribers"})
        ttl = r.ttl(key)
        r.set(key, json.dumps(session), ex=max(ttl, 60))
        return True
    else:
        session = _mem_link_sessions.get(link_token)
        if not session:
            return False
        session.update(updates)
        return True


def delete_link_session(link_token: str) -> None:
    """Delete a link session."""
    r = _redis()
    if r:
        r.delete(f"plaidify:link_session:{link_token}")
    else:
        _mem_link_sessions.pop(link_token, None)


def append_link_session_event(link_token: str, event: Dict[str, Any]) -> bool:
    """Append an event to the session's events list. Returns False if not found."""
    r = _redis()
    if r:
        key = f"plaidify:link_session:{link_token}"
        raw = r.get(key)
        if not raw:
            return False
        session = json.loads(raw)
        session.setdefault("events", []).append(event)
        ttl = r.ttl(key)
        r.set(key, json.dumps(session), ex=max(ttl, 60))
        return True
    else:
        session = _mem_link_sessions.get(link_token)
        if not session:
            return False
        session.setdefault("events", []).append(event)
        return True


# ══════════════════════════════════════════════════════════════════════════════
# Link Scopes Store
# ══════════════════════════════════════════════════════════════════════════════

_mem_link_scopes: Dict[str, str] = {}


def _evict_scopes_if_full() -> None:
    """Enforce max size on the in-memory scopes store (FIFO eviction)."""
    if len(_mem_link_scopes) > _MAX_MEM_LINK_SCOPES:
        # Remove oldest entries (dict preserves insertion order in 3.7+)
        excess = len(_mem_link_scopes) - _MAX_MEM_LINK_SCOPES
        for key in list(_mem_link_scopes)[:excess]:
            del _mem_link_scopes[key]


def set_link_scopes(link_token: str, scopes_json: str) -> None:
    """Store scopes for a link token."""
    r = _redis()
    if r:
        r.set(f"plaidify:link_scope:{link_token}", scopes_json, ex=LINK_SCOPE_TTL)
    else:
        _evict_scopes_if_full()
        _mem_link_scopes[link_token] = scopes_json


def pop_link_scopes(link_token: str) -> Optional[str]:
    """Get and remove scopes for a link token (consume-once)."""
    r = _redis()
    if r:
        key = f"plaidify:link_scope:{link_token}"
        val = r.get(key)
        if val:
            r.delete(key)
        return val
    else:
        return _mem_link_scopes.pop(link_token, None)


# ── Refresh schedule (deferred) ─────────────────────────────────────────────
# Stored at /create_link, consumed at /submit_credentials so a refresh job can
# be registered as soon as the access_token is minted.

_mem_link_refresh_schedules: Dict[str, str] = {}


def set_link_refresh_schedule(link_token: str, schedule_json: str) -> None:
    """Store a deferred refresh-schedule directive keyed on link_token."""
    r = _redis()
    if r:
        r.set(
            f"plaidify:link_refresh:{link_token}",
            schedule_json,
            ex=LINK_SCOPE_TTL,
        )
    else:
        if len(_mem_link_refresh_schedules) > _MAX_MEM_LINK_SCOPES:
            excess = len(_mem_link_refresh_schedules) - _MAX_MEM_LINK_SCOPES
            for key in list(_mem_link_refresh_schedules)[:excess]:
                del _mem_link_refresh_schedules[key]
        _mem_link_refresh_schedules[link_token] = schedule_json


def pop_link_refresh_schedule(link_token: str) -> Optional[str]:
    """Get and remove the deferred refresh-schedule for a link token."""
    r = _redis()
    if r:
        key = f"plaidify:link_refresh:{link_token}"
        val = r.get(key)
        if val:
            r.delete(key)
        return val
    else:
        return _mem_link_refresh_schedules.pop(link_token, None)


# ══════════════════════════════════════════════════════════════════════════════
# Link Launch Bootstrap Store
# ══════════════════════════════════════════════════════════════════════════════

_mem_link_launch_bootstraps: Dict[str, Dict[str, Any]] = {}


def _evict_expired_link_launch_bootstraps() -> None:
    now = time.time()
    expired = [
        key
        for key, value in _mem_link_launch_bootstraps.items()
        if now - value.get("created_at", 0) > value.get("expires_in", LINK_LAUNCH_BOOTSTRAP_TTL)
    ]
    for key in expired:
        del _mem_link_launch_bootstraps[key]

    if len(_mem_link_launch_bootstraps) > _MAX_MEM_LINK_LAUNCH_BOOTSTRAPS:
        sorted_keys = sorted(
            _mem_link_launch_bootstraps,
            key=lambda key: _mem_link_launch_bootstraps[key].get("created_at", 0),
        )
        for key in sorted_keys[: len(_mem_link_launch_bootstraps) - _MAX_MEM_LINK_LAUNCH_BOOTSTRAPS]:
            del _mem_link_launch_bootstraps[key]


def store_link_launch_bootstrap(launch_id: str, expires_in: int = LINK_LAUNCH_BOOTSTRAP_TTL) -> None:
    """Store a one-time hosted link launch bootstrap identifier."""
    r = _redis()
    if r:
        r.set(f"plaidify:link_launch_bootstrap:{launch_id}", "issued", ex=max(expires_in, 1))
        return

    _evict_expired_link_launch_bootstraps()
    _mem_link_launch_bootstraps[launch_id] = {
        "status": "issued",
        "created_at": time.time(),
        "expires_in": max(expires_in, 1),
    }


def consume_link_launch_bootstrap(launch_id: str) -> bool:
    """Consume a one-time hosted link launch bootstrap identifier."""
    r = _redis()
    if r:
        key = f"plaidify:link_launch_bootstrap:{launch_id}"
        script = """
local value = redis.call('GET', KEYS[1])
if (not value) or value == 'consumed' then
  return 0
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = 60 end
redis.call('SET', KEYS[1], 'consumed', 'EX', ttl)
return 1
"""
        try:
            return bool(r.eval(script, 1, key))
        except Exception as exc:
            logger.warning(f"Failed to consume link launch bootstrap atomically: {exc}")
            value = r.get(key)
            if not value or value == "consumed":
                return False
            ttl = r.ttl(key)
            r.set(key, "consumed", ex=max(ttl, 60))
            return True

    _evict_expired_link_launch_bootstraps()
    entry = _mem_link_launch_bootstraps.get(launch_id)
    if not entry or entry.get("status") == "consumed":
        return False

    entry["status"] = "consumed"
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Webhook Delivery Log
# ══════════════════════════════════════════════════════════════════════════════

_mem_webhook_deliveries: Dict[str, List] = {}


def _evict_deliveries_if_full() -> None:
    """Enforce max size on the in-memory webhook delivery store."""
    if len(_mem_webhook_deliveries) > _MAX_MEM_WEBHOOK_DELIVERIES:
        excess = len(_mem_webhook_deliveries) - _MAX_MEM_WEBHOOK_DELIVERIES
        for key in list(_mem_webhook_deliveries)[:excess]:
            del _mem_webhook_deliveries[key]


def add_webhook_delivery(webhook_id: str, delivery: Dict[str, Any]) -> None:
    """Record a webhook delivery attempt."""
    r = _redis()
    if r:
        key = f"plaidify:webhook_delivery:{webhook_id}"
        r.rpush(key, json.dumps(delivery))
        r.ltrim(key, -WEBHOOK_DELIVERY_MAX, -1)  # Keep last N
        r.expire(key, WEBHOOK_DELIVERY_TTL)
    else:
        _evict_deliveries_if_full()
        deliveries = _mem_webhook_deliveries.setdefault(webhook_id, [])
        deliveries.append(delivery)
        # Trim in-memory too
        if len(deliveries) > WEBHOOK_DELIVERY_MAX:
            del deliveries[: len(deliveries) - WEBHOOK_DELIVERY_MAX]


def get_webhook_deliveries(webhook_id: str) -> List[Dict[str, Any]]:
    """Get delivery history for a webhook."""
    r = _redis()
    if r:
        key = f"plaidify:webhook_delivery:{webhook_id}"
        raw_list = r.lrange(key, 0, -1)
        return [json.loads(item) for item in raw_list]
    else:
        return list(_mem_webhook_deliveries.get(webhook_id, []))


def delete_webhook_deliveries(webhook_id: str) -> None:
    """Delete delivery log for a webhook."""
    r = _redis()
    if r:
        r.delete(f"plaidify:webhook_delivery:{webhook_id}")
    else:
        _mem_webhook_deliveries.pop(webhook_id, None)


# ══════════════════════════════════════════════════════════════════════════════
# Redis Pub/Sub for Link Session Events (cross-worker notifications)
# ══════════════════════════════════════════════════════════════════════════════

_LINK_EVENT_CHANNEL_PREFIX = "plaidify:link_events:"


def publish_link_event(link_token: str, event: Dict[str, Any]) -> bool:
    """Publish a link session event via Redis pub/sub for cross-worker delivery.

    Returns True if published via Redis, False if only local delivery is possible.
    """
    r = _redis()
    if r:
        try:
            channel = f"{_LINK_EVENT_CHANNEL_PREFIX}{link_token}"
            r.publish(channel, json.dumps(event))
            return True
        except Exception as e:
            logger.warning(f"Failed to publish link event via Redis: {e}")
    return False


async def subscribe_link_events(link_token: str) -> Optional[asyncio.Queue]:
    """Subscribe to link session events via Redis pub/sub.

    Returns an asyncio.Queue that receives events, or None if Redis is unavailable.
    In non-Redis mode, callers should use in-memory subscriber lists.
    """
    r = _redis()
    if not r:
        return None

    queue: asyncio.Queue = asyncio.Queue()

    async def _listener():
        try:
            import redis as redis_mod

            sub_client = redis_mod.Redis.from_url(settings.redis_url, decode_responses=True)
            pubsub = sub_client.pubsub()
            channel = f"{_LINK_EVENT_CHANNEL_PREFIX}{link_token}"
            pubsub.subscribe(channel)
            try:
                for message in pubsub.listen():
                    if message["type"] == "message":
                        event = json.loads(message["data"])
                        await queue.put(event)
                        if event.get("event") in ("CONNECTED", "ERROR"):
                            break
            finally:
                pubsub.unsubscribe(channel)
                pubsub.close()
                sub_client.close()
        except Exception as e:
            logger.warning(f"Redis pubsub listener error: {e}")

    asyncio.create_task(_listener())
    return queue


# ══════════════════════════════════════════════════════════════════════════════
# Test Helpers
# ══════════════════════════════════════════════════════════════════════════════


def clear_all():
    """Clear all in-memory state. Used in tests."""
    _mem_link_sessions.clear()
    _mem_link_scopes.clear()
    _mem_link_launch_bootstraps.clear()
    _mem_webhook_deliveries.clear()
