"""
Redis-backed session store for link sessions, link scopes, and webhook deliveries.

Falls back to in-memory dicts when Redis is not configured (single-worker dev mode).
"""

import json
import time
from typing import Any, Dict, List, Optional

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("session_store")
settings = get_settings()

# TTLs
LINK_SESSION_TTL = 1800  # 30 minutes
LINK_SCOPE_TTL = 1800  # 30 minutes
WEBHOOK_DELIVERY_TTL = 86400  # 24 hours
WEBHOOK_DELIVERY_MAX = 200  # max deliveries stored per webhook


def _get_redis():
    """Return a Redis client or None if not configured."""
    if not settings.redis_url:
        return None
    try:
        import redis
        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}")
        return None


_redis_client = None


def _redis():
    """Lazy singleton Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = _get_redis()
    return _redis_client


# ══════════════════════════════════════════════════════════════════════════════
# Link Session Store
# ══════════════════════════════════════════════════════════════════════════════

# In-memory fallback
_mem_link_sessions: Dict[str, Dict[str, Any]] = {}


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


def set_link_scopes(link_token: str, scopes_json: str) -> None:
    """Store scopes for a link token."""
    r = _redis()
    if r:
        r.set(f"plaidify:link_scope:{link_token}", scopes_json, ex=LINK_SCOPE_TTL)
    else:
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


# ══════════════════════════════════════════════════════════════════════════════
# Webhook Delivery Log
# ══════════════════════════════════════════════════════════════════════════════

_mem_webhook_deliveries: Dict[str, List] = {}


def add_webhook_delivery(webhook_id: str, delivery: Dict[str, Any]) -> None:
    """Record a webhook delivery attempt."""
    r = _redis()
    if r:
        key = f"plaidify:webhook_delivery:{webhook_id}"
        r.rpush(key, json.dumps(delivery))
        r.ltrim(key, -WEBHOOK_DELIVERY_MAX, -1)  # Keep last N
        r.expire(key, WEBHOOK_DELIVERY_TTL)
    else:
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
# Test Helpers
# ══════════════════════════════════════════════════════════════════════════════

def clear_all():
    """Clear all in-memory state. Used in tests."""
    _mem_link_sessions.clear()
    _mem_link_scopes.clear()
    _mem_webhook_deliveries.clear()
