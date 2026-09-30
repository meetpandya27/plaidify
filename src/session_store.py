"""
Shared state for the hosted Link flow: link sessions, link scopes, deferred
refresh schedules, hosted-link launch bootstraps, and the pub/sub that carries
link-session events to server-sent event streams.

With ``REDIS_URL`` set, everything lives in Redis and every worker sees the
same state. Without it, state is this process's memory: fine for a single
development process, refused (``RuntimeError``) when more than one worker
runs outside development, because each worker would then hold its own copy of
every session.

Link-session writes are atomic (``WATCH``/``MULTI`` in Redis, a lock in
memory): a writer reads, changes and writes a session without losing another
writer's change, never writes back a session that expired meanwhile, and can
make an event "emit once" (the first writer wins, later ones are told no).

Blocking and async clients: the synchronous functions use a client with
socket timeouts; the ``a*`` variants and :class:`LinkEventSubscription` use
``redis.asyncio`` so an event stream never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger("session_store")
settings = get_settings()

# TTLs
LINK_SESSION_TTL = 600  # 10 minutes (reduced from 30 for production security)
LINK_SCOPE_TTL = 600  # 10 minutes
LINK_LAUNCH_BOOTSTRAP_TTL = settings.link_launch_token_expire_seconds

# Session statuses after which the flow is over.
TERMINAL_STATUSES = frozenset({"completed", "error", "exited", "expired"})

# Events kept on a session for replay to late subscribers (newest win).
MAX_STORED_EVENTS = 200

# Max in-memory entries (evict oldest when exceeded)
_MAX_MEM_LINK_SESSIONS = 10_000
_MAX_MEM_LINK_SCOPES = 10_000
_MAX_MEM_LINK_LAUNCH_BOOTSTRAPS = 10_000
# Undelivered events one in-memory subscriber may hold before new ones are dropped.
_LOCAL_QUEUE_MAX = 256
# Optimistic-transaction retries for one link-session write.
_MAX_WRITE_RETRIES = 25

_SESSION_PREFIX = "plaidify:link_session:"
_SCOPE_PREFIX = "plaidify:link_scope:"
_REFRESH_PREFIX = "plaidify:link_refresh:"
_LAUNCH_PREFIX = "plaidify:link_launch_bootstrap:"
_LAUNCH_SITES_PREFIX = "plaidify:link_launch_sites:"
_JOB_LINK_PREFIX = "plaidify:job_link:"
_LINK_EVENT_CHANNEL_PREFIX = "plaidify:link_events:"


# ══════════════════════════════════════════════════════════════════════════════
# Backend selection
# ══════════════════════════════════════════════════════════════════════════════


def web_worker_count() -> int:
    """How many web worker processes this deployment runs (best effort).

    gunicorn.conf.py takes the count from GUNICORN_WORKERS / WEB_CONCURRENCY and
    otherwise starts at least two; gunicorn marks its workers with
    SERVER_SOFTWARE. Anything else (uvicorn, tests, the executor) counts as one.
    """
    for name in ("GUNICORN_WORKERS", "WEB_CONCURRENCY"):
        raw = os.environ.get(name)
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                continue
    if os.environ.get("SERVER_SOFTWARE", "").lower().startswith("gunicorn"):
        return 2
    return 1


def check_shared_state_requirements() -> None:
    """Refuse per-process memory where it would split state between workers.

    Raises:
        RuntimeError: REDIS_URL is unset in production, or unset while more
            than one worker runs outside development.
    """
    if settings.redis_url:
        return
    if settings.env == "production":
        raise RuntimeError("REDIS_URL is required in production for link session storage.")
    workers = web_worker_count()
    if settings.env != "development" and workers > 1:
        raise RuntimeError(
            f"REDIS_URL is required when more than one worker runs ({workers} configured, ENV={settings.env}): "
            "without it every worker keeps its own link sessions, MFA sessions and locks, so a hosted-link "
            "request answered by another worker loses its session. Set REDIS_URL, or run a single worker "
            "(GUNICORN_WORKERS=1)."
        )


_redis_client = None
_redis_client_lock = threading.Lock()
_memory_checked = False


def _redis():
    """The shared blocking Redis client, or None when state lives in this process.

    With REDIS_URL set there is no silent fallback to memory: Redis errors
    surface to the caller.
    """
    global _redis_client, _memory_checked
    if settings.redis_url:
        client = _redis_client
        if client is None:
            with _redis_client_lock:
                if _redis_client is None:
                    import redis

                    timeout = settings.redis_socket_timeout_seconds
                    _redis_client = redis.Redis.from_url(
                        settings.redis_url,
                        decode_responses=True,
                        socket_timeout=timeout,
                        socket_connect_timeout=timeout,
                        health_check_interval=30,
                    )
                client = _redis_client
        return client
    if not _memory_checked:
        check_shared_state_requirements()
        _memory_checked = True
    return None


_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def async_redis() -> Optional[Any]:
    """The ``redis.asyncio`` client for the running loop, or None without REDIS_URL.

    A client is bound to the loop that created it, so one is kept per loop.
    """
    if not settings.redis_url:
        _redis()  # applies the memory-mode checks
        return None
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    if client is None:
        import redis.asyncio as redis_asyncio

        timeout = settings.redis_socket_timeout_seconds
        client = redis_asyncio.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            health_check_interval=30,
        )
        _async_clients[loop] = client
    return client


async def close_async_redis() -> None:
    """Close the running loop's async client (shutdown and tests)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _async_clients.pop(loop, None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - best effort
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Link Session Store
# ══════════════════════════════════════════════════════════════════════════════

# In-memory fallback
_mem_link_sessions: Dict[str, Dict[str, Any]] = {}
_mem_lock = threading.RLock()

# A session mutator changes the session dict in place and returns True to
# write it, False to leave the stored session as it was. It may run more than
# once (optimistic retries), so it must not have side effects.
Mutator = Callable[[Dict[str, Any]], bool]


def _session_key(link_token: str) -> str:
    return f"{_SESSION_PREFIX}{link_token}"


def _is_expired(session: Dict[str, Any]) -> bool:
    return time.time() - float(session.get("created_at", 0) or 0) > LINK_SESSION_TTL


def _view(session: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of a stored session for callers, with ``status`` "expired" once its TTL passed."""
    view = json.loads(json.dumps(session))
    view.setdefault("events", [])
    if _is_expired(view):
        view["status"] = "expired"
    return view


def _trim_events(session: Dict[str, Any]) -> None:
    events = session.get("events")
    if isinstance(events, list) and len(events) > MAX_STORED_EVENTS:
        session["events"] = events[-MAX_STORED_EVENTS:]


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


def _new_record(data: Dict[str, Any]) -> Dict[str, Any]:
    record = {k: v for k, v in data.items() if k != "subscribers"}
    record["created_at"] = record.get("created_at") or time.time()
    record.setdefault("events", [])
    # Keys of events that may be emitted only once (see transition_link_session).
    record.setdefault("emitted", [])
    return record


def create_link_session(link_token: str, data: Dict[str, Any]) -> None:
    """Create a new link session."""
    record = _new_record(data)
    r = _redis()
    if r:
        r.set(_session_key(link_token), json.dumps(record), ex=LINK_SESSION_TTL)
        return
    with _mem_lock:
        _evict_expired_sessions()
        _mem_link_sessions[link_token] = json.loads(json.dumps(record))


def get_link_session(link_token: str) -> Optional[Dict[str, Any]]:
    """Get a copy of a link session; None if unknown. Its status reads "expired" once its TTL passed."""
    r = _redis()
    if r:
        raw = r.get(_session_key(link_token))
        return _view(json.loads(raw)) if raw else None
    with _mem_lock:
        session = _mem_link_sessions.get(link_token)
        return _view(session) if session is not None else None


async def aget_link_session(link_token: str) -> Optional[Dict[str, Any]]:
    """:func:`get_link_session` without blocking the event loop."""
    client = async_redis()
    if client is not None:
        raw = await client.get(_session_key(link_token))
        return _view(json.loads(raw)) if raw else None
    return get_link_session(link_token)


def _mutate_in_memory(link_token: str, mutate: Mutator) -> Tuple[bool, Optional[Dict[str, Any]]]:
    with _mem_lock:
        stored = _mem_link_sessions.get(link_token)
        if stored is None:
            return False, None
        if _is_expired(stored):
            return False, _view(stored)
        working = json.loads(json.dumps(stored))
        if not mutate(working):
            return False, _view(working)
        _trim_events(working)
        _mem_link_sessions[link_token] = working
        return True, _view(working)


def mutate_link_session(link_token: str, mutate: Mutator) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Read, change and write a link session atomically.

    Returns ``(written, session)``: whether ``mutate`` asked for the write and it
    happened, and the session as stored afterwards (None if it does not exist).
    An expired session is never written, so a late update cannot resurrect it.
    """
    r = _redis()
    if r is None:
        return _mutate_in_memory(link_token, mutate)

    import redis as redis_mod

    key = _session_key(link_token)
    with r.pipeline(transaction=True) as pipe:
        for _attempt in range(_MAX_WRITE_RETRIES):
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    pipe.reset()
                    return False, None
                session = json.loads(raw)
                if _is_expired(session) or not mutate(session):
                    pipe.reset()
                    return False, _view(session)
                _trim_events(session)
                pipe.multi()
                # XX: never recreate a key that expired meanwhile; KEEPTTL: an
                # update does not extend the session's lifetime.
                pipe.set(key, json.dumps(session), xx=True, keepttl=True)
                written = bool(pipe.execute()[0])
                return written, (_view(session) if written else None)
            except redis_mod.WatchError:
                continue
    logger.warning("Link session write kept conflicting; giving up", extra={"extra_data": {"key": "link_session"}})
    return False, get_link_session(link_token)


async def amutate_link_session(link_token: str, mutate: Mutator) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """:func:`mutate_link_session` without blocking the event loop."""
    client = async_redis()
    if client is None:
        return _mutate_in_memory(link_token, mutate)

    from redis.exceptions import WatchError

    key = _session_key(link_token)
    async with client.pipeline(transaction=True) as pipe:
        for _attempt in range(_MAX_WRITE_RETRIES):
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                if not raw:
                    await pipe.reset()
                    return False, None
                session = json.loads(raw)
                if _is_expired(session) or not mutate(session):
                    await pipe.reset()
                    return False, _view(session)
                _trim_events(session)
                pipe.multi()
                pipe.set(key, json.dumps(session), xx=True, keepttl=True)
                written = bool((await pipe.execute())[0])
                return written, (_view(session) if written else None)
            except WatchError:
                continue
    logger.warning("Link session write kept conflicting; giving up", extra={"extra_data": {"key": "link_session"}})
    return False, await aget_link_session(link_token)


# job id -> link token, so a job that ends from outside (reaped, cancelled)
# can find its hosted session. Written whenever a session takes a job.
_mem_job_links: Dict[str, str] = {}


def _index_job(link_token: str, updates: Optional[Dict[str, Any]]) -> None:
    job_id = (updates or {}).get("current_job_id")
    if not job_id:
        return
    r = _redis()
    if r is None:
        with _mem_lock:
            if len(_mem_job_links) > _MAX_MEM_LINK_SESSIONS:
                for key in list(_mem_job_links)[: len(_mem_job_links) - _MAX_MEM_LINK_SESSIONS]:
                    del _mem_job_links[key]
            _mem_job_links[job_id] = link_token
        return
    r.set(f"{_JOB_LINK_PREFIX}{job_id}", link_token, ex=LINK_SESSION_TTL)


async def _aindex_job(link_token: str, updates: Optional[Dict[str, Any]]) -> None:
    job_id = (updates or {}).get("current_job_id")
    if not job_id:
        return
    client = async_redis()
    if client is None:
        _index_job(link_token, updates)
        return
    await client.set(f"{_JOB_LINK_PREFIX}{job_id}", link_token, ex=LINK_SESSION_TTL)


def link_token_for_job(job_id: str) -> Optional[str]:
    """The hosted-link session a job was started for, while that session can still exist."""
    r = _redis()
    if r is None:
        with _mem_lock:
            return _mem_job_links.get(job_id)
    return r.get(f"{_JOB_LINK_PREFIX}{job_id}")


async def alink_token_for_job(job_id: str) -> Optional[str]:
    """:func:`link_token_for_job` without blocking the event loop."""
    client = async_redis()
    if client is None:
        return link_token_for_job(job_id)
    return await client.get(f"{_JOB_LINK_PREFIX}{job_id}")


def _apply_updates(updates: Dict[str, Any]) -> Mutator:
    clean = {k: v for k, v in updates.items() if k != "subscribers"}

    def mutate(session: Dict[str, Any]) -> bool:
        session.update(clean)
        return True

    return mutate


def update_link_session(link_token: str, updates: Dict[str, Any]) -> bool:
    """Update fields on an existing link session. Returns False if not found or expired."""
    written, _session = mutate_link_session(link_token, _apply_updates(updates))
    if written:
        _index_job(link_token, updates)
    return written


async def aupdate_link_session(link_token: str, updates: Dict[str, Any]) -> bool:
    """:func:`update_link_session` without blocking the event loop."""
    written, _session = await amutate_link_session(link_token, _apply_updates(updates))
    if written:
        await _aindex_job(link_token, updates)
    return written


def delete_link_session(link_token: str) -> None:
    """Delete a link session."""
    r = _redis()
    if r:
        r.delete(_session_key(link_token))
    else:
        with _mem_lock:
            _mem_link_sessions.pop(link_token, None)


def _append_event(event: Dict[str, Any]) -> Mutator:
    def mutate(session: Dict[str, Any]) -> bool:
        session.setdefault("events", []).append(event)
        return True

    return mutate


def append_link_session_event(link_token: str, event: Dict[str, Any]) -> bool:
    """Append an event to the session's events list. Returns False if not found or expired."""
    written, _session = mutate_link_session(link_token, _append_event(event))
    return written


def transition_mutator(
    *,
    event: Optional[Dict[str, Any]] = None,
    updates: Optional[Dict[str, Any]] = None,
    once_key: Optional[str] = None,
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Mutator:
    """A mutator that records ``event`` and applies ``updates`` together.

    ``guard(session)`` returning False refuses the transition. ``once_key``
    makes it emit-once: a key already recorded on the session refuses it too.
    """

    clean_updates = {k: v for k, v in (updates or {}).items() if k != "subscribers"}

    def mutate(session: Dict[str, Any]) -> bool:
        if guard is not None and not guard(session):
            return False
        if once_key:
            emitted = session.setdefault("emitted", [])
            if once_key in emitted:
                return False
            emitted.append(once_key)
        session.update(clean_updates)
        if event is not None:
            session.setdefault("events", []).append(event)
        return True

    return mutate


async def atransition_link_session(
    link_token: str,
    *,
    event: Optional[Dict[str, Any]] = None,
    updates: Optional[Dict[str, Any]] = None,
    once_key: Optional[str] = None,
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Record an event and its state change atomically; see :func:`transition_mutator`.

    Returns ``(applied, session)``. Only an applied transition's event should be
    published and fanned out to webhooks.
    """
    applied, session = await amutate_link_session(
        link_token,
        transition_mutator(event=event, updates=updates, once_key=once_key, guard=guard),
    )
    if applied:
        await _aindex_job(link_token, updates)
    return applied, session


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
        r.set(f"{_SCOPE_PREFIX}{link_token}", scopes_json, ex=LINK_SCOPE_TTL)
    else:
        with _mem_lock:
            _evict_scopes_if_full()
            _mem_link_scopes[link_token] = scopes_json


def pop_link_scopes(link_token: str) -> Optional[str]:
    """Get and remove scopes for a link token (consume-once, atomically)."""
    r = _redis()
    if r:
        return r.getdel(f"{_SCOPE_PREFIX}{link_token}")
    with _mem_lock:
        return _mem_link_scopes.pop(link_token, None)


# ── Refresh schedule (deferred) ─────────────────────────────────────────────
# Stored at /create_link, consumed at /submit_credentials so a refresh job can
# be registered as soon as the access_token is minted.

_mem_link_refresh_schedules: Dict[str, str] = {}


def set_link_refresh_schedule(link_token: str, schedule_json: str) -> None:
    """Store a deferred refresh-schedule directive keyed on link_token."""
    r = _redis()
    if r:
        r.set(f"{_REFRESH_PREFIX}{link_token}", schedule_json, ex=LINK_SCOPE_TTL)
    else:
        with _mem_lock:
            if len(_mem_link_refresh_schedules) > _MAX_MEM_LINK_SCOPES:
                excess = len(_mem_link_refresh_schedules) - _MAX_MEM_LINK_SCOPES
                for key in list(_mem_link_refresh_schedules)[:excess]:
                    del _mem_link_refresh_schedules[key]
            _mem_link_refresh_schedules[link_token] = schedule_json


def pop_link_refresh_schedule(link_token: str) -> Optional[str]:
    """Get and remove the deferred refresh-schedule for a link token (atomically)."""
    r = _redis()
    if r:
        return r.getdel(f"{_REFRESH_PREFIX}{link_token}")
    with _mem_lock:
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


def store_link_launch_bootstrap(
    launch_id: str,
    expires_in: int = LINK_LAUNCH_BOOTSTRAP_TTL,
    *,
    allowed_sites: Optional[List[str]] = None,
) -> None:
    """Store a one-time hosted link launch bootstrap identifier.

    ``allowed_sites`` carries the issuing agent's site restriction to the
    session the bootstrap is redeemed into (see :func:`pop_link_launch_sites`).
    """
    r = _redis()
    if r:
        r.set(f"{_LAUNCH_PREFIX}{launch_id}", "issued", ex=max(expires_in, 1))
        if allowed_sites is not None:
            r.set(f"{_LAUNCH_SITES_PREFIX}{launch_id}", json.dumps(allowed_sites), ex=max(expires_in, 1))
        return

    with _mem_lock:
        _evict_expired_link_launch_bootstraps()
        _mem_link_launch_bootstraps[launch_id] = {
            "status": "issued",
            "created_at": time.time(),
            "expires_in": max(expires_in, 1),
            "allowed_sites": allowed_sites,
        }


def pop_link_launch_sites(launch_id: str) -> Optional[List[str]]:
    """The site restriction stored with a launch bootstrap (None: unrestricted), read once."""
    r = _redis()
    if r:
        raw = r.getdel(f"{_LAUNCH_SITES_PREFIX}{launch_id}")
        return json.loads(raw) if raw else None
    with _mem_lock:
        entry = _mem_link_launch_bootstraps.get(launch_id) or {}
        return entry.pop("allowed_sites", None)


def consume_link_launch_bootstrap(launch_id: str) -> bool:
    """Consume a one-time hosted link launch bootstrap identifier."""
    r = _redis()
    if r:
        key = f"{_LAUNCH_PREFIX}{launch_id}"
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

    with _mem_lock:
        _evict_expired_link_launch_bootstraps()
        entry = _mem_link_launch_bootstraps.get(launch_id)
        if not entry or entry.get("status") == "consumed":
            return False

        entry["status"] = "consumed"
        return True


# ══════════════════════════════════════════════════════════════════════════════
# Pub/Sub for Link Session Events (cross-worker notifications)
# ══════════════════════════════════════════════════════════════════════════════

# In-memory subscribers: link_token -> [(loop, queue)]. Publishing hands each
# event to the subscriber's own loop, so it is safe from any thread.
_local_subscribers: Dict[str, List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}


def _channel(link_token: str) -> str:
    return f"{_LINK_EVENT_CHANNEL_PREFIX}{link_token}"


def _offer(queue: asyncio.Queue, event: Dict[str, Any]) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # A stalled reader; it re-reads the session state on its next keep-alive.
        pass


def _publish_local(link_token: str, event: Dict[str, Any]) -> None:
    with _mem_lock:
        subscribers = list(_local_subscribers.get(link_token, ()))
    for loop, queue in subscribers:
        try:
            loop.call_soon_threadsafe(_offer, queue, event)
        except RuntimeError:
            pass  # that loop is closed


def publish_link_event(link_token: str, event: Dict[str, Any]) -> bool:
    """Publish a link session event to every open event stream. Returns False if it could not be sent."""
    r = _redis()
    if r is None:
        _publish_local(link_token, event)
        return True
    try:
        r.publish(_channel(link_token), json.dumps(event))
        return True
    except Exception as exc:
        logger.warning("Failed to publish link event via Redis", extra={"extra_data": {"error": type(exc).__name__}})
        return False


async def apublish_link_event(link_token: str, event: Dict[str, Any]) -> bool:
    """:func:`publish_link_event` without blocking the event loop."""
    client = async_redis()
    if client is None:
        _publish_local(link_token, event)
        return True
    try:
        await client.publish(_channel(link_token), json.dumps(event))
        return True
    except Exception as exc:
        logger.warning("Failed to publish link event via Redis", extra={"extra_data": {"error": type(exc).__name__}})
        return False


class LinkEventSubscription:
    """The live events of one link session, while an event stream is open.

    Redis mode subscribes with ``redis.asyncio`` pub/sub on a connection of its
    own; :meth:`get` waits with a timeout and never blocks the event loop.
    Always :meth:`close` it (or use ``async with``), which unsubscribes and
    returns the connection.
    """

    def __init__(self, link_token: str) -> None:
        self.link_token = link_token
        self._pubsub: Any = None
        self._queue: Optional[asyncio.Queue] = None
        self._entry: Optional[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = None
        self._closed = False

    async def open(self) -> "LinkEventSubscription":
        client = async_redis()
        if client is None:
            self._queue = asyncio.Queue(maxsize=_LOCAL_QUEUE_MAX)
            self._entry = (asyncio.get_running_loop(), self._queue)
            with _mem_lock:
                _local_subscribers.setdefault(self.link_token, []).append(self._entry)
            return self
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(_channel(self.link_token))
        except BaseException:
            await pubsub.aclose()
            raise
        self._pubsub = pubsub
        return self

    async def get(self, timeout: float) -> Optional[Dict[str, Any]]:
        """The next event, or None when none arrived within ``timeout`` seconds."""
        if self._closed:
            return None
        if self._pubsub is None:
            assert self._queue is not None
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if not message or message.get("type") != "message":
            return None
        try:
            event = json.loads(message["data"])
        except (TypeError, ValueError):
            return None
        return event if isinstance(event, dict) else None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pubsub is not None:
            pubsub, self._pubsub = self._pubsub, None
            try:
                await pubsub.unsubscribe()
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
            return
        if self._entry is not None:
            with _mem_lock:
                entries = _local_subscribers.get(self.link_token, [])
                if self._entry in entries:
                    entries.remove(self._entry)
                if not entries:
                    _local_subscribers.pop(self.link_token, None)
            self._entry = None

    async def __aenter__(self) -> "LinkEventSubscription":
        return await self.open()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def local_subscriber_count(link_token: str) -> int:
    """Open in-memory subscriptions for a session (tests and diagnostics)."""
    with _mem_lock:
        return len(_local_subscribers.get(link_token, ()))


# ══════════════════════════════════════════════════════════════════════════════
# Test Helpers
# ══════════════════════════════════════════════════════════════════════════════


def clear_all():
    """Clear all in-memory state. Used in tests."""
    with _mem_lock:
        _mem_link_sessions.clear()
        _mem_link_scopes.clear()
        _mem_link_refresh_schedules.clear()
        _mem_link_launch_bootstraps.clear()
        _mem_job_links.clear()
        _local_subscribers.clear()
