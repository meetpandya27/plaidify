"""
MFA Session Manager — manages pending MFA challenges.

When a site requires multi-factor authentication (OTP, email code, push, etc.),
the engine pauses and stores the session state here. The client submits the MFA
code via the API, and the engine resumes the flow.

Sessions auto-expire after a configurable TTL (default: 5 minutes).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

from src.logging_config import get_logger

logger = get_logger("mfa_manager")

# Default MFA session TTL: 5 minutes
DEFAULT_MFA_TTL = 300


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
        try:
            await asyncio.wait_for(self._event.wait(), timeout=wait_timeout)
            return self.code
        except asyncio.TimeoutError:
            logger.warning(
                "MFA session timed out",
                extra={"extra_data": {"session_id": self.session_id, "site": self.site}},
            )
            return None


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
        session = MFASession(
            session_id=session_id,
            site=site,
            mfa_type=mfa_type,
            metadata=metadata or {},
            ttl=ttl,
        )

        async with self._lock:
            self._sessions[session_id] = session

        logger.info(
            "MFA session created",
            extra={"extra_data": {
                "session_id": session_id,
                "site": site,
                "mfa_type": mfa_type,
            }},
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

        if not session:
            logger.warning(
                "MFA session not found",
                extra={"extra_data": {"session_id": session_id}},
            )
            return False

        if session.expired:
            logger.warning(
                "MFA session expired",
                extra={"extra_data": {"session_id": session_id}},
            )
            await self.remove_session(session_id)
            return False

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
                return None
            return session

    async def remove_session(self, session_id: str) -> None:
        """Remove an MFA session."""
        async with self._lock:
            self._sessions.pop(session_id, None)

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
