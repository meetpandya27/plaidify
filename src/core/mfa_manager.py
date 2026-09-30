"""
MFA Session Manager — manages pending MFA challenges.

When a site requires multi-factor authentication (OTP, email code, push, etc.),
the engine pauses and stores the session state here. The client submits the MFA
code via the API, and the engine resumes the flow.

With REDIS_URL set, sessions live in Redis so the worker that receives the
code and the worker driving the browser can differ; otherwise they live in
memory. Redis is used through the async client with socket timeouts, so a
session waiting for its code never blocks the event loop.

A session moves through three states, exposed so status endpoints can stop
reporting an answered challenge as still "mfa_required":

* awaiting a code — ``awaiting_code`` is True;
* answered — the user submitted a code (``answered_at`` is set);
* consumed — the engine has read the code and is checking it with the site
  (``consumed`` / ``consumed_at``). If the site rejects it, the engine reopens
  the session for another attempt (``attempts`` counts codes read).

Sessions auto-expire after their TTL (default: 5 minutes).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.core.async_redis import get_async_redis
from src.crypto import token_fingerprint
from src.error_taxonomy import LinkErrorCode
from src.exceptions import AuthenticationError, PlaidifyError
from src.logging_config import get_logger

logger = get_logger("mfa_manager")


class MFATimeoutError(PlaidifyError):
    """The user did not answer the MFA challenge within its time budget (MFA_TIMEOUT_SECONDS).

    Distinct from MFARequiredError: the challenge is over and its session is
    gone, so the connection has to start again. Maps to ``mfa_timeout``.
    """

    error_code = LinkErrorCode.MFA_TIMEOUT

    def __init__(self, site: str, mfa_type: str = "unknown", session_id: str = ""):
        super().__init__(
            message=f"MFA timeout: no verification was received for site: {site} (type: {mfa_type}).",
            status_code=408,
        )
        self.site = site
        self.mfa_type = mfa_type
        self.session_id = session_id


class MFARejectedError(AuthenticationError):
    """The site rejected the MFA code(s) or push approval. Maps to ``invalid_credentials``."""

    def __init__(self, site: str, mfa_type: str = "unknown", attempts: int = 1):
        super().__init__(site=site)
        self.message = f"Authentication failed for site: {site} — the site rejected the verification code."
        self.args = (self.message,)
        self.mfa_type = mfa_type
        self.attempts = attempts


# Default MFA session TTL: 5 minutes
DEFAULT_MFA_TTL = 300
_MFA_SESSION_PREFIX = "plaidify:mfa_session:"
_MFA_CODE_PREFIX = "plaidify:mfa_code:"
_MFA_POLL_INTERVAL = 0.25


@dataclass
class MFASession:
    """A pending MFA challenge waiting for user input."""

    session_id: str
    site: str
    mfa_type: str
    created_at: float = field(default_factory=time.time)
    ttl: int = DEFAULT_MFA_TTL

    # The asyncio Event that the engine waits on
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    # User-submitted MFA code, until the engine reads it
    code: Optional[str] = None

    # Metadata (e.g., question text for security questions)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # When the user submitted the current code, and when the engine read it.
    answered_at: Optional[float] = None
    consumed_at: Optional[float] = None
    # How many codes the engine has read for this challenge.
    attempts: int = 0

    # Optional backend fetcher for multi-worker polling: takes (and removes)
    # a code submitted through another worker.
    _code_fetcher: Optional[Callable[[str], Awaitable[Optional[str]]]] = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Records consumption in the shared store.
    _on_consumed: Optional[Callable[[MFASession], Awaitable[None]]] = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def expired(self) -> bool:
        """Check if this session has expired."""
        return time.time() - self.created_at > self.ttl

    @property
    def consumed(self) -> bool:
        """Whether the engine has read the current code (and is verifying it)."""
        return self.consumed_at is not None

    @property
    def awaiting_code(self) -> bool:
        """Whether the challenge still needs the user: not expired, no code submitted yet."""
        return not self.expired and self.answered_at is None and self.consumed_at is None

    def submit_code(self, code: str) -> None:
        """Submit the MFA code and wake up the waiting engine."""
        self.code = code
        self.answered_at = time.time()
        self.consumed_at = None
        self._event.set()

    def _reopen(self, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.code = None
        self.answered_at = None
        self.consumed_at = None
        self._event.clear()
        if metadata:
            self.metadata = {**self.metadata, **metadata}

    async def _take_code(self) -> Optional[str]:
        """The submitted code, marking the session consumed; None when there is none yet."""
        code = self.code
        if not code and self._code_fetcher is not None:
            code = await self._code_fetcher(self.session_id)
        if not code:
            return None

        now = time.time()
        self.code = None
        self._event.clear()
        self.consumed_at = now
        if self.answered_at is None:
            self.answered_at = now
        self.attempts += 1
        if self._on_consumed is not None:
            await self._on_consumed(self)
        return code

    async def wait_for_code(self, timeout: Optional[float] = None) -> Optional[str]:
        """
        Wait for the user to submit their MFA code, and mark it consumed.

        Args:
            timeout: Max seconds to wait. Defaults to TTL.

        Returns:
            The submitted code, or None if timed out.
        """
        wait_timeout = self.ttl if timeout is None else timeout
        deadline = time.monotonic() + wait_timeout

        while True:
            code = await self._take_code()
            if code:
                return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "MFA session timed out",
                    extra={"extra_data": {"session": token_fingerprint(self.session_id), "site": self.site}},
                )
                return None

            interval = remaining if self._code_fetcher is None else min(_MFA_POLL_INTERVAL, remaining)
            try:
                await asyncio.wait_for(self._event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


class MFAManager:
    """
    Manages active MFA sessions.

    Usage:
        manager = MFAManager()

        # Engine side: create session, wait for code
        session = await manager.create_session("sess_123", "example_bank", "otp")
        code = await session.wait_for_code()

        # API side: submit user's MFA code
        await manager.submit_code("sess_123", "123456")
    """

    def __init__(self, redis_factory: Optional[Callable[[], Any]] = None) -> None:
        self._sessions: Dict[str, MFASession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._redis_factory = redis_factory or get_async_redis

    @staticmethod
    def _redis_key(session_id: str) -> str:
        return f"{_MFA_SESSION_PREFIX}{session_id}"

    @staticmethod
    def _code_key(session_id: str) -> str:
        return f"{_MFA_CODE_PREFIX}{session_id}"

    def _redis(self):
        return self._redis_factory()

    @staticmethod
    def _remaining_ttl(created_at: float, ttl: int) -> int:
        remaining = ttl - (time.time() - created_at)
        return max(1, int(remaining))

    # ── Shared store ──────────────────────────────────────────────────────

    async def _persist_session(self, session: MFASession) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return

        mapping = {
            "session_id": session.session_id,
            "site": session.site,
            "mfa_type": session.mfa_type,
            "metadata": json.dumps(session.metadata),
            "ttl": session.ttl,
            "created_at": session.created_at,
            "attempts": session.attempts,
        }
        key = self._redis_key(session.session_id)
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=mapping)
            stale = [
                name
                for name, value in (("answered_at", session.answered_at), ("consumed_at", session.consumed_at))
                if value is None
            ]
            if stale:
                pipe.hdel(key, *stale)
            present = {
                name: value
                for name, value in (("answered_at", session.answered_at), ("consumed_at", session.consumed_at))
                if value is not None
            }
            if present:
                pipe.hset(key, mapping=present)
            pipe.expire(key, self._remaining_ttl(session.created_at, session.ttl))
            await pipe.execute()

    async def _load(self, session_id: str) -> Optional[Dict[str, str]]:
        redis_client = self._redis()
        if redis_client is None:
            return None
        payload = await redis_client.hgetall(self._redis_key(session_id))
        return payload or None

    def _restore(self, payload: Dict[str, str]) -> MFASession:
        try:
            metadata = json.loads(payload.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        session = MFASession(
            session_id=payload["session_id"],
            site=payload.get("site", ""),
            mfa_type=payload.get("mfa_type", "unknown"),
            created_at=float(payload.get("created_at", time.time())),
            ttl=int(float(payload.get("ttl", DEFAULT_MFA_TTL))),
            metadata=metadata if isinstance(metadata, dict) else {},
            answered_at=float(payload["answered_at"]) if payload.get("answered_at") else None,
            consumed_at=float(payload["consumed_at"]) if payload.get("consumed_at") else None,
            attempts=int(float(payload.get("attempts", 0) or 0)),
        )
        self._attach(session)
        return session

    def _attach(self, session: MFASession) -> None:
        if self._redis() is not None:
            session._code_fetcher = self._fetch_submitted_code
            session._on_consumed = self._record_consumed

    async def _fetch_submitted_code(self, session_id: str) -> Optional[str]:
        """Take a code submitted through any worker. Errors mean "not yet"."""
        try:
            redis_client = self._redis()
            if redis_client is None:
                return None
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.get(self._code_key(session_id))
                pipe.delete(self._code_key(session_id))
                code, _deleted = await pipe.execute()
            return code or None
        except Exception as exc:
            logger.warning(
                "MFA code poll failed; will retry",
                extra={"extra_data": {"session": token_fingerprint(session_id), "error": type(exc).__name__}},
            )
            return None

    async def _record_consumed(self, session: MFASession) -> None:
        try:
            redis_client = self._redis()
            if redis_client is None:
                return
            key = self._redis_key(session.session_id)
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.delete(self._code_key(session.session_id))
                pipe.hset(
                    key,
                    mapping={
                        "consumed_at": session.consumed_at or time.time(),
                        "answered_at": session.answered_at or time.time(),
                        "attempts": session.attempts,
                    },
                )
                await pipe.execute()
        except Exception as exc:
            logger.warning(
                "Could not record MFA code consumption",
                extra={"extra_data": {"session": token_fingerprint(session.session_id), "error": type(exc).__name__}},
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start_cleanup(self) -> None:
        """Start the background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop_cleanup(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()

    async def create_session(
        self,
        session_id: str,
        site: str,
        mfa_type: str,
        metadata: Optional[Dict[str, Any]] = None,
        ttl: int = DEFAULT_MFA_TTL,
    ) -> MFASession:
        """
        Create a new MFA session, or resume one that already exists.

        Args:
            session_id: Unique session identifier (usually from the browser pool).
            site: Site that requires MFA.
            mfa_type: Type of MFA (otp, email_code, security_question, push).
            metadata: Extra info to send to the client (e.g., question text).
            ttl: Time-to-live in seconds.

        Returns:
            The created MFASession.
        """
        existing = await self.get_session(session_id)
        if existing is not None:
            existing.site = site
            existing.mfa_type = mfa_type
            existing.metadata = {**existing.metadata, **(metadata or {})}
            self._attach(existing)
            if existing.code is None and existing._code_fetcher is not None:
                # A code submitted before a worker restart is still waiting in the store.
                redis_client = self._redis()
                existing.code = await redis_client.get(self._code_key(session_id)) or None

            async with self._lock:
                self._sessions[session_id] = existing

            await self._persist_session(existing)

            logger.info(
                "MFA session resumed",
                extra={
                    "extra_data": {
                        "session": token_fingerprint(session_id),
                        "site": site,
                        "mfa_type": mfa_type,
                        "has_code": bool(existing.code),
                    }
                },
            )

            return existing

        session = MFASession(
            session_id=session_id,
            site=site,
            mfa_type=mfa_type,
            metadata=metadata or {},
            ttl=ttl,
        )
        self._attach(session)

        async with self._lock:
            self._sessions[session_id] = session

        await self._persist_session(session)

        logger.info(
            "MFA session created",
            extra={
                "extra_data": {
                    "session": token_fingerprint(session_id),
                    "site": site,
                    "mfa_type": mfa_type,
                }
            },
        )

        return session

    async def submit_code(self, session_id: str, code: str) -> bool:
        """
        Submit an MFA code for a pending session.

        Args:
            session_id: The MFA session to complete.
            code: The MFA code from the user.

        Returns:
            True if the session was found and code submitted, False otherwise.
        """
        async with self._lock:
            session = self._sessions.get(session_id)

        redis_client = self._redis()
        payload = await self._load(session_id) if redis_client is not None else None

        if session is None and payload is None:
            logger.warning(
                "MFA session not found",
                extra={"extra_data": {"session": token_fingerprint(session_id)}},
            )
            return False

        created_at = session.created_at if session is not None else float(payload.get("created_at", 0))
        ttl = session.ttl if session is not None else int(float(payload.get("ttl", DEFAULT_MFA_TTL)))
        if time.time() - created_at > ttl:
            logger.warning(
                "MFA session expired",
                extra={"extra_data": {"session": token_fingerprint(session_id)}},
            )
            await self.remove_session(session_id)
            return False

        if redis_client is not None:
            now = time.time()
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.set(self._code_key(session_id), code, ex=self._remaining_ttl(created_at, ttl))
                pipe.hset(self._redis_key(session_id), mapping={"answered_at": now})
                pipe.hdel(self._redis_key(session_id), "consumed_at")
                await pipe.execute()

        if session is not None:
            session.submit_code(code)
        logger.info(
            "MFA code submitted",
            extra={"extra_data": {"session": token_fingerprint(session_id)}},
        )
        return True

    async def reopen_session(self, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Ask the user for another code: clear the answered/consumed state and merge ``metadata``.

        Used when the site rejected the last code. Returns False if the session is gone.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            session = await self.get_session(session_id)
        if session is None:
            return False

        session._reopen(metadata)
        async with self._lock:
            self._sessions[session_id] = session

        redis_client = self._redis()
        if redis_client is not None:
            await redis_client.delete(self._code_key(session_id))
        await self._persist_session(session)
        return True

    async def get_session(self, session_id: str) -> Optional[MFASession]:
        """Get an MFA session by ID."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session and session.expired:
                del self._sessions[session_id]
                session = None

        if session is not None:
            return session

        try:
            payload = await self._load(session_id)
        except Exception as exc:
            logger.warning(
                "MFA session lookup failed",
                extra={"extra_data": {"session": token_fingerprint(session_id), "error": type(exc).__name__}},
            )
            return None
        if not payload:
            return None

        restored = self._restore(payload)
        if restored.expired:
            await self.remove_session(session_id)
            return None
        return restored

    async def remove_session(self, session_id: str) -> None:
        """Remove an MFA session."""
        async with self._lock:
            self._sessions.pop(session_id, None)

        try:
            redis_client = self._redis()
            if redis_client is not None:
                await redis_client.delete(self._redis_key(session_id), self._code_key(session_id))
        except Exception as exc:
            logger.warning(
                "MFA session removal failed; it will expire on its own",
                extra={"extra_data": {"session": token_fingerprint(session_id), "error": type(exc).__name__}},
            )

    @property
    def active_count(self) -> int:
        """Number of active (non-expired) MFA sessions."""
        return sum(1 for s in self._sessions.values() if not s.expired)

    async def _cleanup_loop(self) -> None:
        """Background task that removes expired sessions."""
        while True:
            try:
                await asyncio.sleep(30)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "MFA cleanup error",
                    extra={"extra_data": {"error": str(e)}},
                )

    async def _cleanup_expired(self) -> None:
        """Remove all expired sessions."""
        expired: list[str] = []
        async with self._lock:
            for session_id, session in self._sessions.items():
                if session.expired:
                    expired.append(session_id)
            for session_id in expired:
                del self._sessions[session_id]

        if expired:
            logger.debug(
                f"Cleaned up {len(expired)} expired MFA sessions",
                extra={"extra_data": {"count": len(expired)}},
            )


# ── Singleton ─────────────────────────────────────────────────────────────────

_manager: Optional[MFAManager] = None


def get_mfa_manager() -> MFAManager:
    """Get the global MFA manager instance."""
    global _manager
    if _manager is None:
        _manager = MFAManager()
        _manager.start_cleanup()
    return _manager
