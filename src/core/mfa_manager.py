"""
MFA Session Manager — manages pending MFA challenges.

When a site requires multi-factor authentication (OTP, email code, push, etc.),
the engine pauses and stores the session state here. The client submits the MFA
code via the API, and the engine resumes the flow.

Sessions auto-expire after a configurable TTL (default: 5 minutes).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src import session_store
from src.logging_config import get_logger

logger = get_logger("mfa_manager")

# Default MFA session TTL: 5 minutes
DEFAULT_MFA_TTL = 300
_MFA_SESSION_PREFIX = "plaidify:mfa_session:"
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

    # User-submitted MFA code (set when code is submitted)
    code: Optional[str] = None

    # Metadata (e.g., question text for security questions)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Optional backend fetcher for multi-worker polling.
    _code_fetcher: Optional[Callable[[str], Awaitable[Optional[str]]]] = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def expired(self) -> bool:
        """Check if this session has expired."""
        return time.time() - self.created_at > self.ttl

    def submit_code(self, code: str) -> None:
        """Submit the MFA code and wake up the waiting engine."""
        self.code = code
        self._event.set()

    async def wait_for_code(self, timeout: Optional[float] = None) -> Optional[str]:
        """
        Wait for the user to submit their MFA code.

        Args:
            timeout: Max seconds to wait. Defaults to TTL.

        Returns:
            The submitted code, or None if timed out.
        """
        wait_timeout = timeout or self.ttl

        if self._code_fetcher is None:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=wait_timeout)
                return self.code
            except asyncio.TimeoutError:
                logger.warning(
                    "MFA session timed out",
                    extra={"extra_data": {"session_id": self.session_id, "site": self.site}},
                )
                return None

        deadline = time.monotonic() + wait_timeout
        while True:
            if self.code:
                return self.code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "MFA session timed out",
                    extra={"extra_data": {"session_id": self.session_id, "site": self.site}},
                )
                return None

            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(_MFA_POLL_INTERVAL, remaining))
                if self.code:
                    return self.code
            except asyncio.TimeoutError:
                pass

            code = await self._code_fetcher(self.session_id)
            if code:
                self.code = code
                return code


class MFAManager:
    """
    Manages active MFA sessions.

    Usage:
        manager = MFAManager()

        # Engine side: create session, wait for code
        session = manager.create_session("sess_123", "example_bank", "otp")
        code = await session.wait_for_code()

        # API side: submit user's MFA code
        manager.submit_code("sess_123", "123456")
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, MFASession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @staticmethod
    def _redis_key(session_id: str) -> str:
        return f"{_MFA_SESSION_PREFIX}{session_id}"

    @staticmethod
    def _session_payload(
        session_id: str,
        site: str,
        mfa_type: str,
        metadata: Dict[str, Any],
        ttl: int,
        created_at: float,
        code: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "site": site,
            "mfa_type": mfa_type,
            "metadata": metadata,
            "ttl": ttl,
            "created_at": created_at,
            "code": code,
        }

    @staticmethod
    def _payload_expired(payload: Dict[str, Any]) -> bool:
        created_at = float(payload.get("created_at", 0))
        ttl = int(payload.get("ttl", DEFAULT_MFA_TTL))
        return time.time() - created_at > ttl

    @staticmethod
    def _remaining_ttl(payload: Dict[str, Any]) -> int:
        created_at = float(payload.get("created_at", 0))
        ttl = int(payload.get("ttl", DEFAULT_MFA_TTL))
        remaining = ttl - (time.time() - created_at)
        return max(1, int(remaining))

    def _redis(self):
        return session_store._redis()

    async def _fetch_submitted_code(self, session_id: str) -> Optional[str]:
        redis_client = self._redis()
        if redis_client is None:
            async with self._lock:
                session = self._sessions.get(session_id)
                return session.code if session else None

        raw = redis_client.get(self._redis_key(session_id))
        if not raw:
            return None

        payload = json.loads(raw)
        if self._payload_expired(payload):
            redis_client.delete(self._redis_key(session_id))
            return None

        return payload.get("code")

    def _persist_session(self, session: MFASession) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return

        payload = self._session_payload(
            session_id=session.session_id,
            site=session.site,
            mfa_type=session.mfa_type,
            metadata=session.metadata,
            ttl=session.ttl,
            created_at=session.created_at,
            code=session.code,
        )
        redis_client.set(
            self._redis_key(session.session_id),
            json.dumps(payload),
            ex=session.ttl,
        )

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
        Create a new MFA session.

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
            if self._redis() is not None:
                existing._code_fetcher = self._fetch_submitted_code

            async with self._lock:
                self._sessions[session_id] = existing

            self._persist_session(existing)

            logger.info(
                "MFA session resumed",
                extra={
                    "extra_data": {
                        "session_id": session_id,
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

        if self._redis() is not None:
            session._code_fetcher = self._fetch_submitted_code

        async with self._lock:
            self._sessions[session_id] = session

        self._persist_session(session)

        logger.info(
            "MFA session created",
            extra={
                "extra_data": {
                    "session_id": session_id,
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
        payload = None
        if redis_client is not None:
            raw = redis_client.get(self._redis_key(session_id))
            if raw:
                payload = json.loads(raw)

        if session is None and payload is None:
            logger.warning(
                "MFA session not found",
                extra={"extra_data": {"session_id": session_id}},
            )
            return False

        if payload is not None and self._payload_expired(payload):
            logger.warning(
                "MFA session expired",
                extra={"extra_data": {"session_id": session_id}},
            )
            await self.remove_session(session_id)
            return False

        if session is not None and session.expired:
            logger.warning(
                "MFA session expired",
                extra={"extra_data": {"session_id": session_id}},
            )
            await self.remove_session(session_id)
            return False

        if payload is not None and redis_client is not None:
            payload["code"] = code
            redis_client.set(
                self._redis_key(session_id),
                json.dumps(payload),
                ex=self._remaining_ttl(payload),
            )

        if session is not None:
            session.submit_code(code)
        logger.info(
            "MFA code submitted",
            extra={"extra_data": {"session_id": session_id}},
        )
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

        redis_client = self._redis()
        if redis_client is None:
            return None

        raw = redis_client.get(self._redis_key(session_id))
        if not raw:
            return None

        payload = json.loads(raw)
        if self._payload_expired(payload):
            redis_client.delete(self._redis_key(session_id))
            return None

        restored = MFASession(
            session_id=payload["session_id"],
            site=payload["site"],
            mfa_type=payload["mfa_type"],
            created_at=float(payload.get("created_at", time.time())),
            ttl=int(payload.get("ttl", DEFAULT_MFA_TTL)),
            code=payload.get("code"),
            metadata=payload.get("metadata") or {},
        )
        restored._code_fetcher = self._fetch_submitted_code
        return restored

    async def remove_session(self, session_id: str) -> None:
        """Remove an MFA session."""
        async with self._lock:
            self._sessions.pop(session_id, None)

        redis_client = self._redis()
        if redis_client is not None:
            redis_client.delete(self._redis_key(session_id))

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
