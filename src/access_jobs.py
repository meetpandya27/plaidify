"""Access jobs: tracked site-access runs, their per-credential locks, and the
Redis-stream executor that runs them in redis-worker mode.

A job moves ``pending`` → ``running`` → one terminal status: ``completed``,
``failed``, ``blocked`` (another job held the credential's lock), ``cancelled``
or ``mfa_timeout``. "MFA required" is never stored: while a running job waits
for a code, :func:`serialize_access_job_runtime` reports it from the live MFA
session.

Only the process that moves a job from pending to running (a conditional
UPDATE) runs it. While it runs, a watchdog renews the job's claim every
``ACCESS_JOB_HEARTBEAT_SECONDS``: the scope lock (whose value is the job id),
the stream message's idle time (redis-worker mode, ``XCLAIM … JUSTID``) and
the ``heartbeat_at`` column. The reaper fails jobs whose heartbeat went stale
(their process died), that ran past ``deadline_at``, or that no executor
picked up. Cancelling a job (Link closed, ``POST /access_jobs/{id}/cancel``)
moves it to ``cancelled`` and frees its lock; the runner notices at its next
watchdog tick and stops the browser.

No database session stays open while the browser runs: every state change
uses a short session of its own, and the caller's session is committed before
the run starts. Results are stored encrypted under the owner's key
(``encrypt_json_for_user``); a job without an owner keeps only a summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from src import session_store
from src.audit import record_audit_event
from src.config import get_settings
from src.core.mfa_manager import MFARejectedError, MFATimeoutError, get_mfa_manager
from src.crypto import token_fingerprint
from src.database import (
    AccessJob,
    CredentialDecryptionError,
    SessionLocal,
    User,
    decrypt_credential,
    decrypt_json_for_user,
    encrypt_credential,
    encrypt_json_for_user,
    utcnow,
)
from src.error_taxonomy import LinkErrorCode, classify_exception
from src.exceptions import (
    AccessJobCancelledError,
    AuthenticationError,
    BlueprintNotFoundError,
    BlueprintValidationError,
    CaptchaRequiredError,
    ConcurrentAccessError,
    ConnectionFailedError,
    DataExtractionError,
    LockServiceUnavailableError,
    MFARequiredError,
    PlaidifyError,
    RateLimitedError,
    ReadOnlyPolicyViolationError,
    SiteUnavailableError,
)
from src.logging_config import get_logger

logger = get_logger("access_jobs")
settings = get_settings()

ACTIVE_JOB_STATUSES = ("pending", "running")
# "mfa_required" is terminal only in rows written by older releases, where it
# meant the MFA challenge had timed out.
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled", "mfa_timeout", "mfa_required"})

_LOCK_KEY_PREFIX = "plaidify:access_lock:"
_LOCK_WAIT_SECONDS = 0.25
_LOCK_POLL_INTERVAL = 0.05
_DISPATCH_PAYLOAD_PREFIX = "plaidify:access_job_payload:"
# Take the lock, or keep it when this job already holds it (a re-delivered message).
_ACQUIRE_LOCK_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
  redis.call('set', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
if current == ARGV[1] then
  redis.call('pexpire', KEYS[1], ARGV[2])
  return 1
end
return 0
"""
_RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

# Scope lock owners when there is no Redis (one process): scope -> job id.
_LOCAL_LOCK_OWNERS: Dict[str, str] = {}
# In-process background tasks by job id: the runner (inprocess mode) or the
# observer waiting for a dispatched job's outcome (redis-worker mode).
_BACKGROUND_TASKS: Dict[str, asyncio.Task] = {}
# Job ids whose background task runs the job here (not just observes it).
_LOCAL_RUNNERS: set = set()
# The executor call of each job running in this process, so a cancel can stop it.
_RUNNING_JOBS: Dict[str, asyncio.Task] = {}

_GENERIC_FAILURE_MESSAGE = "The connection failed unexpectedly. Please try again."
_SHUTDOWN_CANCEL_MESSAGE = "Access job cancelled before completion."


# ── Small helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return utcnow()


def _lock_key(scope: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{scope}"


def _job_id() -> str:
    return f"ajob-{uuid.uuid4()}"


def _session_id() -> str:
    return f"access-{uuid.uuid4()}"


def _dispatch_payload_key(job_id: str) -> str:
    return f"{_DISPATCH_PAYLOAD_PREFIX}{job_id}"


def _local_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _access_job_dispatch_enabled() -> bool:
    return settings.access_job_execution_mode == "redis-worker"


def _run_budget_seconds() -> float:
    """The longest a job may run: automation budget + MFA wait + slack."""
    return float(
        settings.engine_timeout_seconds + settings.mfa_timeout_seconds + settings.access_job_deadline_margin_seconds
    )


def _lock_ttl_ms() -> int:
    # Several heartbeats' worth: a live holder renews it long before it lapses.
    return int(max(settings.access_job_heartbeat_seconds * 6, 30.0) * 1000)


def _site_of_scope(scope: str) -> str:
    return scope.split(":site:")[-1]


def _serialize_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    if not metadata:
        return None
    return json.dumps(metadata, sort_keys=True, default=str)


def _deserialize_metadata(metadata_json: Optional[str]) -> Optional[Dict[str, Any]]:
    if not metadata_json:
        return None
    try:
        return json.loads(metadata_json)
    except (TypeError, ValueError):
        return {"raw": metadata_json}


def _merge_metadata(*parts: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    for part in parts:
        if part:
            merged.update(part)
    return merged or None


def _result_context(job_id: str) -> str:
    return f"access_job:{job_id}:result"


# ── Lock scope (per credential) ───────────────────────────────────────────────

_SCOPE_KEY_INFO = b"plaidify:access-lock-scope:v1"
_scope_key_cache: Optional[bytes] = None


def _scope_key() -> bytes:
    """HMAC key for lock scopes, derived from ENCRYPTION_KEY (a lock key never reveals the login)."""
    global _scope_key_cache
    if _scope_key_cache is None:
        from src.database import _get_encryption_key

        _scope_key_cache = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_SCOPE_KEY_INFO).derive(
            _get_encryption_key()
        )
    return _scope_key_cache


def _scope_hash(identity: str, site: str) -> str:
    message = f"{site.strip().lower()}\x00{identity}".encode("utf-8")
    return hmac.new(_scope_key(), message, hashlib.sha256).hexdigest()[:32]


def build_lock_scope(
    *,
    site: str,
    user_id: Optional[int] = None,
    principal_hint: Optional[str] = None,
    credential: Optional[str] = None,
) -> str:
    """The lock scope of a job: one credential on one site.

    ``credential`` (see :func:`credential_identity`) wins; ``principal_hint``
    and ``user_id`` are fallbacks for jobs that carry no credential.
    """
    site_key = site.strip().lower()
    if credential:
        return f"cred:{_scope_hash(credential, site_key)}:site:{site_key}"
    if principal_hint:
        return f"principal:{_scope_hash(principal_hint.strip().lower(), site_key)}:site:{site_key}"
    if user_id is not None:
        return f"user:{user_id}:site:{site_key}"
    return f"anonymous:site:{site_key}"


def credential_identity(
    executor_kwargs: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """What identifies the credential a job signs in with: the site login, else the token it came from.

    Two jobs for the same login on the same site would fight over one account's
    session at the site, so they share a lock; two end users of one developer
    do not.
    """
    kwargs = executor_kwargs or {}
    username = kwargs.get("username")
    if isinstance(username, str) and username.strip():
        return "login:" + username.strip().lower()
    for key in ("access_token", "link_token"):
        value = kwargs.get(key) or (metadata or {}).get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    return None


class _HeldScopeLock:
    """A scope lock held by one job (the lock's value is the job id)."""

    def __init__(self, *, backend: str, scope: str, owner: str) -> None:
        self.backend = backend
        self.scope = scope
        self.owner = owner
        self.token = owner
        self._released = False

    async def renew(self) -> bool:
        """Extend the lock; False when it is no longer this job's (cancelled or reaped)."""
        if self._released:
            return False
        if self.backend == "redis":
            client = session_store.async_redis()
            if client is None:
                return False
            return bool(await client.eval(_RENEW_LOCK_SCRIPT, 1, _lock_key(self.scope), self.owner, _lock_ttl_ms()))
        return _LOCAL_LOCK_OWNERS.get(self.scope) == self.owner

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await release_scope_lock(self.scope, self.owner)


def _acquire_local_lock(scope: str, owner: str) -> bool:
    current = _LOCAL_LOCK_OWNERS.get(scope)
    if current is None or current == owner:
        _LOCAL_LOCK_OWNERS[scope] = owner
        return True
    return False


async def acquire_scope_lock(scope: str, owner: Optional[str] = None) -> _HeldScopeLock:
    """Take the lock of a credential scope for ``owner`` (a job id), waiting briefly.

    Fails closed in production: when Redis cannot be reached the job is
    refused (503) rather than guarded by a lock only this process would see.
    Elsewhere it falls back to a process-local lock, with a warning.

    Raises:
        ConcurrentAccessError: another job holds the lock.
        LockServiceUnavailableError: Redis is unreachable in production.
    """
    owner = owner or uuid.uuid4().hex
    client = session_store.async_redis()
    backend = "redis" if client is not None else "local"
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        if backend == "redis":
            try:
                acquired = bool(await client.eval(_ACQUIRE_LOCK_SCRIPT, 1, _lock_key(scope), owner, _lock_ttl_ms()))
            except Exception as exc:
                if settings.env == "production":
                    logger.error(
                        "Redis is unreachable for access locks; refusing the job",
                        extra={"extra_data": {"error": type(exc).__name__}},
                    )
                    raise LockServiceUnavailableError() from exc
                logger.warning(
                    "Redis is unreachable for access locks; using a lock only this process sees",
                    extra={"extra_data": {"error": type(exc).__name__}},
                )
                backend = "local"
                acquired = _acquire_local_lock(scope, owner)
        else:
            acquired = _acquire_local_lock(scope, owner)
        if acquired:
            return _HeldScopeLock(backend=backend, scope=scope, owner=owner)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConcurrentAccessError(site=_site_of_scope(scope))
        await asyncio.sleep(min(_LOCK_POLL_INTERVAL, remaining))


async def release_scope_lock(scope: str, owner: str) -> bool:
    """Free a scope lock if ``owner`` still holds it. Safe to call from any process."""
    released = False
    if _LOCAL_LOCK_OWNERS.get(scope) == owner:
        del _LOCAL_LOCK_OWNERS[scope]
        released = True
    client = session_store.async_redis()
    if client is None:
        return released
    try:
        return bool(await client.eval(_RELEASE_LOCK_SCRIPT, 1, _lock_key(scope), owner)) or released
    except Exception as exc:
        logger.warning(
            "Failed to release access lock; it expires on its own",
            extra={"extra_data": {"scope_site": _site_of_scope(scope), "error": type(exc).__name__}},
        )
        return released


# ── Job rows (short sessions only) ────────────────────────────────────────────


@dataclass(frozen=True)
class _JobSpec:
    """What a runner needs to know about a job, detached from any DB session."""

    id: str
    user_id: Optional[int]
    site: str
    job_type: str
    lock_scope: str
    session_id: str
    metadata: Optional[Dict[str, Any]]

    @classmethod
    def of(cls, job: AccessJob) -> "_JobSpec":
        return cls(
            id=job.id,
            user_id=job.user_id,
            site=job.site,
            job_type=job.job_type,
            lock_scope=job.lock_scope,
            session_id=job.session_id or "",
            metadata=_deserialize_metadata(job.metadata_json),
        )


def _detached(db: Session, job: AccessJob) -> AccessJob:
    db.refresh(job)
    db.expunge(job)
    return job


def _load_job(job_id: str) -> Optional[AccessJob]:
    """The job row, loaded and detached (its attributes never touch the database again)."""
    with SessionLocal() as db:
        job = db.get(AccessJob, job_id)
        if job is None:
            return None
        db.expunge(job)
        return job


def _release_caller_transaction(db: Optional[Session]) -> None:
    """Commit the caller's session so no connection stays checked out while the browser runs."""
    if db is None:
        return
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _insert_job(
    *,
    site: str,
    job_type: str,
    user_id: Optional[int],
    lock_scope: str,
    session_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
) -> AccessJob:
    now = _now()
    with SessionLocal() as db:
        job = AccessJob(
            id=_job_id(),
            user_id=user_id,
            site=site,
            job_type=job_type,
            status="pending",
            lock_scope=lock_scope,
            session_id=session_id or _session_id(),
            metadata_json=_serialize_metadata(metadata),
            created_at=now,
            deadline_at=now + timedelta(seconds=settings.access_job_queue_timeout_seconds + _run_budget_seconds()),
        )
        db.add(job)
        db.commit()
        return _detached(db, job)


def _create_access_job(
    db: Optional[Session],
    *,
    site: str,
    job_type: str,
    user_id: Optional[int],
    principal_hint: Optional[str],
    session_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
    executor_kwargs: Optional[Dict[str, Any]] = None,
) -> AccessJob:
    """Insert a pending job (in a session of its own) and end the caller's transaction."""
    _release_caller_transaction(db)
    scope = build_lock_scope(
        site=site,
        user_id=user_id,
        principal_hint=principal_hint,
        credential=credential_identity(executor_kwargs, metadata),
    )
    return _insert_job(
        site=site,
        job_type=job_type,
        user_id=user_id,
        lock_scope=scope,
        session_id=session_id,
        metadata=metadata,
    )


def _claim_job(job_id: str, worker_id: str) -> bool:
    """Move a job from pending to running for this worker. Only one caller ever succeeds."""
    now = _now()
    with SessionLocal() as db:
        result = db.execute(
            update(AccessJob)
            .where(AccessJob.id == job_id, AccessJob.status == "pending")
            .values(
                status="running",
                started_at=now,
                heartbeat_at=now,
                worker_id=worker_id[:128],
                deadline_at=now + timedelta(seconds=_run_budget_seconds()),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1


def _heartbeat_job(job_id: str) -> bool:
    """Record that the job's runner is alive. False when the job is no longer running."""
    with SessionLocal() as db:
        result = db.execute(
            update(AccessJob)
            .where(AccessJob.id == job_id, AccessJob.status == "running")
            .values(heartbeat_at=_now())
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1


def _storable_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """The copy of a result kept on the job: downloaded files' contents are left out."""
    stored = json.loads(json.dumps(result, default=str))
    metadata = stored.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("downloads"), list):
        for download in metadata["downloads"]:
            if isinstance(download, dict) and "content_base64" in download:
                download.pop("content_base64", None)
                download["content_omitted"] = True
    return stored


def _finish_job(
    job_id: str,
    *,
    status: str,
    from_statuses: Tuple[str, ...] = ("running",),
    user_id: Optional[int] = None,
    error_message: Optional[str] = None,
    error_code: Optional[str] = None,
    error_type: Optional[str] = None,
    error_status: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> bool:
    """Write a job's final state, unless it already left ``from_statuses`` (cancelled, reaped).

    A result is stored encrypted under the owner's key; a job without an owner
    stores none (its metadata keeps a summary).
    """
    values: Dict[str, Any] = {
        "status": status,
        "completed_at": _now(),
        "error_message": error_message,
        "error_code": error_code,
        "error_type": error_type,
        "error_status": error_status,
    }
    if metadata is not None:
        values["metadata_json"] = _serialize_metadata(metadata)
    with SessionLocal() as db:
        if result is not None and user_id is not None:
            owner = db.get(User, user_id)
            if owner is not None:
                values["result_json"] = encrypt_json_for_user(
                    db, owner, _storable_result(result), context=_result_context(job_id)
                )
        outcome = db.execute(
            update(AccessJob)
            .where(AccessJob.id == job_id, AccessJob.status.in_(from_statuses))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return outcome.rowcount == 1


def _mark_job_failed(job_id: str, message: str) -> None:
    _finish_job(
        job_id,
        status="failed",
        from_statuses=ACTIVE_JOB_STATUSES,
        error_message=message,
        error_code=LinkErrorCode.INTERNAL_ERROR.value,
        error_type="PlaidifyError",
        error_status=500,
    )


def _mark_job_cancelled(job_id: str, message: str) -> bool:
    return _finish_job(
        job_id,
        status="cancelled",
        from_statuses=ACTIVE_JOB_STATUSES,
        error_message=message,
        error_type=AccessJobCancelledError.__name__,
        error_status=409,
    )


def _job_is_terminal(job_id: str) -> bool:
    job = _load_job(job_id)
    return job is None or job.status in TERMINAL_JOB_STATUSES


# ── Results and outcomes ──────────────────────────────────────────────────────


def _result_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if "status" in result:
        metadata["result_status"] = result["status"]
    data = result.get("data")
    if isinstance(data, dict):
        metadata["result_field_count"] = len(data)
        metadata["result_fields"] = sorted(data.keys())

    response_metadata = result.get("metadata")
    if isinstance(response_metadata, dict):
        policy = response_metadata.get("read_only_policy")
        if isinstance(policy, dict):
            metadata["read_only_policy"] = policy
            metadata["read_only_policy_blocked_count"] = policy.get(
                "blocked_action_count", len(policy.get("blocked_actions", []))
            )
    return metadata


def _record_policy_audit_if_needed(
    spec: _JobSpec,
    metadata: Optional[Dict[str, Any]],
    *,
    status: str,
) -> None:
    if not metadata:
        return

    policy = metadata.get("read_only_policy")
    if not isinstance(policy, dict):
        return

    blocked_count = policy.get("blocked_action_count", len(policy.get("blocked_actions", [])))
    if not blocked_count:
        return

    with SessionLocal() as db:
        record_audit_event(
            db,
            "access_job",
            "read_only_policy_blocked",
            user_id=spec.user_id,
            agent_id=metadata.get("agent_id"),
            resource=spec.id,
            metadata={
                "site": spec.site,
                "job_type": spec.job_type,
                "status": status,
                "blocked_action_count": blocked_count,
                "blocked_actions": policy.get("blocked_actions", []),
            },
        )


@dataclass(frozen=True)
class _Failure:
    status: str
    message: str
    code: Optional[str]
    error_type: str
    http_status: int


def _failure_of(exc: BaseException) -> _Failure:
    """How an executor exception ends a job."""
    name = type(exc).__name__
    if isinstance(exc, MFATimeoutError):
        return _Failure("mfa_timeout", exc.message, LinkErrorCode.MFA_TIMEOUT.value, name, exc.status_code)
    if isinstance(exc, MFARequiredError):
        # Only a non-interactive run (scheduled refresh) stops at an MFA challenge.
        return _Failure("failed", exc.message, "mfa_required", name, exc.status_code)
    if isinstance(exc, ConcurrentAccessError):
        return _Failure("blocked", exc.message, classify_exception(exc).value, name, exc.status_code)
    if isinstance(exc, PlaidifyError):
        return _Failure("failed", exc.message, classify_exception(exc).value, name, exc.status_code)
    return _Failure("failed", _GENERIC_FAILURE_MESSAGE, classify_exception(exc).value, name, 500)


# Exceptions a stored job failure is raised as again, by class name.
_ERROR_TYPES: Dict[str, type] = {
    cls.__name__: cls
    for cls in (
        AccessJobCancelledError,
        AuthenticationError,
        BlueprintNotFoundError,
        BlueprintValidationError,
        CaptchaRequiredError,
        ConcurrentAccessError,
        ConnectionFailedError,
        DataExtractionError,
        LockServiceUnavailableError,
        MFARejectedError,
        MFARequiredError,
        MFATimeoutError,
        RateLimitedError,
        ReadOnlyPolicyViolationError,
        SiteUnavailableError,
    )
}

_DEFAULT_ERROR_STATUS = {"blocked": 409, "cancelled": 409, "mfa_timeout": 408, "mfa_required": 408}


def error_for_job(job: AccessJob) -> PlaidifyError:
    """The exception a finished, unsuccessful job stands for, with the HTTP status it had in-process."""
    status = job.status
    message = job.error_message or f"Access job {status}: {job.id}"
    http_status = job.error_status or _DEFAULT_ERROR_STATUS.get(status, 500)
    metadata = _deserialize_metadata(job.metadata_json) or {}

    error_type = job.error_type
    if status == "mfa_required" and not error_type:
        error_type = MFATimeoutError.__name__  # an older release's word for a timed-out challenge
    if status == "cancelled" and not error_type:
        error_type = AccessJobCancelledError.__name__
    cls = _ERROR_TYPES.get(error_type or "", PlaidifyError)

    exc = cls.__new__(cls)
    PlaidifyError.__init__(exc, message=message, status_code=http_status)
    exc.site = job.site
    exc.job_id = job.id
    if cls in (MFATimeoutError, MFARequiredError, MFARejectedError):
        exc.mfa_type = metadata.get("mfa_type", "unknown")
        exc.session_id = job.session_id or ""
    if cls is MFARejectedError:
        exc.attempts = 1
    if cls is ReadOnlyPolicyViolationError:
        exc.detail = message
        exc.metadata = metadata.get("read_only_policy") and {"read_only_policy": metadata["read_only_policy"]}
    if cls is RateLimitedError:
        exc.retry_after = 60
    if cls is CaptchaRequiredError:
        exc.captcha_type = "unknown"
    if job.error_code:
        try:
            exc.error_code = LinkErrorCode(job.error_code)
        except ValueError:
            pass
    return exc


def load_job_owner(user_id: Optional[int]) -> Optional[User]:
    """A detached owner row to decrypt job results with. It queries the database: call it off the event loop."""
    if user_id is None:
        return None
    with SessionLocal() as db:
        owner = db.get(User, user_id)
        if owner is None:
            return None
        db.expunge(owner)
        return owner


def _read_stored_result(job: AccessJob, owner: Optional[User] = None) -> Optional[Dict[str, Any]]:
    """A job's stored result, decrypted for its owner. None for jobs without an owner."""
    if not job.result_json or job.user_id is None:
        return None
    if owner is None or owner.id != job.user_id:
        with SessionLocal() as db:
            owner = db.get(User, job.user_id)
            if owner is None:
                return None
            db.expunge(owner)
    try:
        value = decrypt_json_for_user(owner, job.result_json, context=_result_context(job.id))
    except (CredentialDecryptionError, ValueError) as exc:
        logger.warning(
            "Stored access job result could not be decrypted",
            extra={"extra_data": {"job_id": job.id, "error": type(exc).__name__}},
        )
        return None
    return value if isinstance(value, dict) else None


def _summary_result(job: AccessJob) -> Dict[str, Any]:
    """What a caller gets for a completed job whose result was not stored (no owner)."""
    metadata = _deserialize_metadata(job.metadata_json) or {}
    summary = {key: metadata[key] for key in ("result_field_count", "result_fields") if key in metadata}
    summary["result_stored"] = False
    return {"status": metadata.get("result_status", "completed"), "metadata": summary}


def _load_job_outcome(job_id: str) -> Tuple[Optional[AccessJob], Optional[Dict[str, Any]]]:
    job = _load_job(job_id)
    if job is None or job.status != "completed":
        return job, None
    return job, _read_stored_result(job) or _summary_result(job)


# ── Running a job ─────────────────────────────────────────────────────────────


async def _finish(spec: _JobSpec, **fields: Any) -> bool:
    return await asyncio.to_thread(_finish_job, spec.id, user_id=spec.user_id, **fields)


async def _finish_failed(spec: _JobSpec, exc: BaseException, metadata: Optional[Dict[str, Any]]) -> None:
    failure = _failure_of(exc)
    error_metadata = _merge_metadata(metadata, getattr(exc, "metadata", None))
    if isinstance(exc, (MFATimeoutError, MFARequiredError)):
        error_metadata = _merge_metadata(error_metadata, {"mfa_type": exc.mfa_type})
    written = await _finish(
        spec,
        status=failure.status,
        error_message=failure.message,
        error_code=failure.code,
        error_type=failure.error_type,
        error_status=failure.http_status,
        metadata=error_metadata,
    )
    if written:
        await asyncio.to_thread(_record_policy_audit_if_needed, spec, error_metadata, status=failure.status)
    if not isinstance(exc, PlaidifyError):
        logger.error(
            "Access job failed unexpectedly",
            extra={"extra_data": {"job_id": spec.id, "site": spec.site, "error": type(exc).__name__}},
        )


async def _stored_outcome_error(job_id: str) -> PlaidifyError:
    """The error for a job that was stopped from outside (cancelled or reaped)."""
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None:
        return AccessJobCancelledError(job_id=job_id)
    if job.status in TERMINAL_JOB_STATUSES and job.status != "completed":
        return error_for_job(job)
    return AccessJobCancelledError(job_id=job_id)


async def _watch_job(
    spec: _JobSpec,
    held_lock: _HeldScopeLock,
    exec_task: asyncio.Task,
    renew_claim: Optional[Callable[[], Awaitable[Any]]],
) -> None:
    """Keep a running job's claim fresh; stop it when the job was cancelled or reaped elsewhere."""
    interval = settings.access_job_heartbeat_seconds
    tick = min(2.0, interval)
    next_heartbeat = time.monotonic() + interval
    while not exec_task.done():
        await asyncio.sleep(tick)
        if exec_task.done():
            return
        try:
            still_held = await held_lock.renew()
        except Exception as exc:  # a Redis blip: try again next tick
            logger.warning(
                "Could not renew an access job's lock",
                extra={"extra_data": {"job_id": spec.id, "error": type(exc).__name__}},
            )
            still_held = True
        if not still_held:
            logger.info(
                "Access job lost its lock (cancelled or reaped); stopping it", extra={"extra_data": {"job_id": spec.id}}
            )
            exec_task.cancel()
            return
        if time.monotonic() < next_heartbeat:
            continue
        next_heartbeat = time.monotonic() + interval
        try:
            alive = await asyncio.to_thread(_heartbeat_job, spec.id)
        except Exception as exc:
            logger.warning(
                "Could not record an access job heartbeat",
                extra={"extra_data": {"job_id": spec.id, "error": type(exc).__name__}},
            )
            continue
        if not alive:
            logger.info(
                "Access job is no longer running (cancelled or reaped); stopping it",
                extra={"extra_data": {"job_id": spec.id}},
            )
            exec_task.cancel()
            return
        if renew_claim is not None:
            try:
                await renew_claim()
            except Exception as exc:
                logger.warning(
                    "Could not renew an access job's stream claim",
                    extra={"extra_data": {"job_id": spec.id, "error": type(exc).__name__}},
                )


async def _run_claimed_job(
    spec: _JobSpec,
    *,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
    metadata: Optional[Dict[str, Any]],
    renew_claim: Optional[Callable[[], Awaitable[Any]]] = None,
) -> Dict[str, Any]:
    """Run a job this process moved to running, write its final state, and return its result.

    Raises the executor's error (after storing it), ``ConcurrentAccessError``
    when the credential is busy, the stored error when the job was cancelled or
    reaped from outside, or ``CancelledError`` when this task is cancelled
    (e.g. shutdown), after marking the job cancelled.
    """
    try:
        held_lock = await acquire_scope_lock(spec.lock_scope, owner=spec.id)
    except ConcurrentAccessError as exc:
        exc.job_id = spec.id
        await _finish_failed(spec, exc, metadata)
        raise
    except Exception as exc:
        await _finish_failed(spec, exc, metadata)
        raise

    execution_kwargs = dict(executor_kwargs)
    execution_kwargs.setdefault("session_id", spec.session_id)
    exec_task = asyncio.ensure_future(executor(**execution_kwargs))
    _RUNNING_JOBS[spec.id] = exec_task
    watchdog = asyncio.create_task(_watch_job(spec, held_lock, exec_task, renew_claim))
    deadline = asyncio.timeout(_run_budget_seconds())
    try:
        try:
            async with deadline:
                result = await exec_task
        except TimeoutError as exc:
            if not deadline.expired():  # a timeout inside the executor: an ordinary failure
                await _finish_failed(spec, exc, metadata)
                raise
            error = PlaidifyError(
                message="The connection took too long and was stopped. Please try again.",
                status_code=504,
            )
            error.job_id = spec.id
            await _finish(
                spec,
                status="failed",
                error_message=error.message,
                error_code=LinkErrorCode.INTERNAL_ERROR.value,
                error_type=PlaidifyError.__name__,
                error_status=error.status_code,
            )
            raise error from None
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # This task is being cancelled (shutdown, executor drain).
                await asyncio.shield(
                    _finish(
                        spec,
                        status="cancelled",
                        error_message=_SHUTDOWN_CANCEL_MESSAGE,
                        error_type=AccessJobCancelledError.__name__,
                        error_status=409,
                    )
                )
                raise
            # The watchdog stopped it: the job was cancelled or reaped elsewhere.
            raise await _stored_outcome_error(spec.id) from None
        except PlaidifyError as exc:
            exc.job_id = spec.id
            await _finish_failed(spec, exc, metadata)
            raise
        except Exception as exc:
            await _finish_failed(spec, exc, metadata)
            raise

        completed_metadata = _merge_metadata(metadata, _result_metadata(result))
        written = await _finish(spec, status="completed", metadata=completed_metadata, result=result)
        if not written:
            # Cancelled or reaped while the result came in: that decision stands.
            raise await _stored_outcome_error(spec.id)
        await asyncio.to_thread(_record_policy_audit_if_needed, spec, completed_metadata, status="completed")
        return result
    finally:
        watchdog.cancel()
        if not exec_task.done():
            exec_task.cancel()
        _RUNNING_JOBS.pop(spec.id, None)
        await asyncio.shield(_cleanup_after_run(spec, held_lock))


async def _cleanup_after_run(spec: _JobSpec, held_lock: _HeldScopeLock) -> None:
    await held_lock.release()
    try:
        await get_mfa_manager().remove_session(spec.session_id)
    except Exception:  # pragma: no cover - the session expires on its own
        pass


async def _execute_background_job(
    *,
    job_id: str,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
) -> Tuple[AccessJob, Dict[str, Any]]:
    """Claim a pending job for this process, run it, and return (job, result)."""
    try:
        job = await asyncio.to_thread(_load_job, job_id)
        if job is None:
            raise RuntimeError(f"Access job not found: {job_id}")
        claimed = await asyncio.to_thread(_claim_job, job_id, _local_worker_id())
    except asyncio.CancelledError:
        # Cancelled before it started (e.g. shutdown): it never will.
        await asyncio.shield(asyncio.to_thread(_mark_job_cancelled, job_id, _SHUTDOWN_CANCEL_MESSAGE))
        raise
    if not claimed:
        raise await _stored_outcome_error(job_id)
    spec = _JobSpec.of(job)
    result = await _run_claimed_job(spec, executor=executor, executor_kwargs=executor_kwargs, metadata=spec.metadata)
    finished = await asyncio.to_thread(_load_job, job_id)
    return (finished or job), result


def _register_background_task(job_id: str, task: asyncio.Task, *, runs_job: bool = False) -> None:
    _BACKGROUND_TASKS[job_id] = task
    if runs_job:
        _LOCAL_RUNNERS.add(job_id)

    def _cleanup(done_task: asyncio.Task) -> None:
        if _BACKGROUND_TASKS.get(job_id) is done_task:
            _BACKGROUND_TASKS.pop(job_id, None)
            _LOCAL_RUNNERS.discard(job_id)
        try:
            done_task.exception()
        except asyncio.CancelledError:
            logger.info(
                "Background access job cancelled",
                extra={"extra_data": {"job_id": job_id}},
            )
        except Exception as exc:
            logger.debug(
                "Background access job finished with exception",
                extra={"extra_data": {"job_id": job_id, "error": type(exc).__name__}},
            )

    task.add_done_callback(_cleanup)


async def shutdown_access_jobs(*, timeout: float = 10.0) -> None:
    """Cancel and await all in-process background access jobs."""
    loop = asyncio.get_running_loop()
    tasks = [(job_id, task) for job_id, task in list(_BACKGROUND_TASKS.items()) if task.get_loop() is loop]
    if not tasks:
        return

    logger.info(
        "Shutting down in-process access jobs",
        extra={"extra_data": {"count": len(tasks)}},
    )

    for _job_id, task in tasks:
        task.cancel()

    runners = set(_LOCAL_RUNNERS)
    for job_id, task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for access job shutdown",
                extra={"extra_data": {"job_id": job_id, "timeout": timeout}},
            )
        except Exception as exc:
            logger.warning(
                "Background access job failed during shutdown",
                extra={"extra_data": {"job_id": job_id, "error": type(exc).__name__}},
            )
        if job_id in runners:
            # A job this process ran (or had yet to start) must not stay
            # pending/running; a dispatched job carries on in the executor.
            await asyncio.to_thread(_mark_job_cancelled, job_id, _SHUTDOWN_CANCEL_MESSAGE)


# ── Cancel and reap ───────────────────────────────────────────────────────────


def _cancel_task(task: asyncio.Task) -> None:
    """Cancel a task from any thread (its loop may not be the running one)."""
    if task.done():
        return
    loop = task.get_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is running:
        task.cancel()
    elif not loop.is_closed():
        loop.call_soon_threadsafe(task.cancel)


async def _stop_job_everywhere(job: AccessJob) -> None:
    """After a job left pending/running from outside: stop its runner, free its lock, end its MFA challenge."""
    exec_task = _RUNNING_JOBS.get(job.id)
    if exec_task is not None:
        _cancel_task(exec_task)
    await release_scope_lock(job.lock_scope, job.id)
    if job.session_id:
        try:
            await get_mfa_manager().remove_session(job.session_id)
        except Exception:  # pragma: no cover - it expires on its own
            pass


async def cancel_access_job(job_id: str, *, reason: str = "The access job was cancelled.") -> bool:
    """Cancel a pending or running job wherever it runs. False if it had already finished.

    The job is marked cancelled at once and its lock freed, so the user can
    retry immediately; the process running it notices within a watchdog tick
    and closes the browser.
    """
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None or job.status not in ACTIVE_JOB_STATUSES:
        return False
    if not await asyncio.to_thread(_mark_job_cancelled, job_id, reason):
        return False
    await _stop_job_everywhere(job)
    logger.info("Access job cancelled", extra={"extra_data": {"job_id": job_id, "site": job.site}})
    return True


_ORPHANED_MESSAGE = "The connection was interrupted because the worker running it stopped. Please try again."
_DEADLINE_MESSAGE = "The connection took too long and was stopped. Please try again."
_UNCLAIMED_MESSAGE = "No worker picked up the connection in time. Please try again."


def _stale_predicates(now: datetime):
    stale_cutoff = now - timedelta(seconds=settings.access_job_stale_after_seconds)
    queue_cutoff = now - timedelta(seconds=settings.access_job_queue_timeout_seconds)
    last_sign_of_life = func.coalesce(AccessJob.heartbeat_at, AccessJob.started_at, AccessJob.created_at)
    orphaned = and_(AccessJob.status == "running", last_sign_of_life < stale_cutoff)
    unclaimed = and_(AccessJob.status == "pending", AccessJob.created_at < queue_cutoff)
    overdue = and_(AccessJob.status.in_(ACTIVE_JOB_STATUSES), AccessJob.deadline_at < now)
    return orphaned, unclaimed, overdue


def _reap_job_row(job_id: str, now: datetime) -> Optional[AccessJob]:
    """Fail one stuck job if it is still stuck (a heartbeat may have just arrived). Returns it if reaped."""
    orphaned, unclaimed, overdue = _stale_predicates(now)
    with SessionLocal() as db:
        job = db.get(AccessJob, job_id)
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        stuck = db.execute(
            select(AccessJob.id).where(AccessJob.id == job_id, or_(orphaned, unclaimed, overdue))
        ).first()
        if stuck is None:
            return None
        deadline_at = job.deadline_at
        if deadline_at is not None and deadline_at < now:
            message = _DEADLINE_MESSAGE
        elif job.status == "pending":
            message = _UNCLAIMED_MESSAGE
        else:
            message = _ORPHANED_MESSAGE
        outcome = db.execute(
            update(AccessJob)
            .where(AccessJob.id == job_id, AccessJob.status == job.status, or_(orphaned, unclaimed, overdue))
            .values(
                status="failed",
                completed_at=now,
                error_message=message,
                error_code=LinkErrorCode.INTERNAL_ERROR.value,
                error_type=PlaidifyError.__name__,
                error_status=503,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if outcome.rowcount != 1:
            return None
        db.refresh(job)
        db.expunge(job)
        return job


def _find_stuck_job_ids(now: datetime, limit: int = 200) -> List[str]:
    orphaned, unclaimed, overdue = _stale_predicates(now)
    with SessionLocal() as db:
        rows = db.execute(select(AccessJob.id).where(or_(orphaned, unclaimed, overdue)).limit(limit)).all()
        return [row[0] for row in rows]


async def reap_access_job(job_id: str, *, now: Optional[datetime] = None) -> Optional[AccessJob]:
    """Fail one job if it is orphaned, overdue or never picked up; free its lock. Returns it if reaped."""
    job = await asyncio.to_thread(_reap_job_row, job_id, now or _now())
    if job is None:
        return None
    await _stop_job_everywhere(job)
    logger.warning(
        "Reaped a stuck access job",
        extra={"extra_data": {"job_id": job.id, "site": job.site, "reason": job.error_message}},
    )
    return job


async def reap_stuck_access_jobs(*, now: Optional[datetime] = None) -> List[AccessJob]:
    """Fail every job past its deadline, orphaned by a dead process, or never picked up."""
    moment = now or _now()
    job_ids = await asyncio.to_thread(_find_stuck_job_ids, moment)
    reaped: List[AccessJob] = []
    for job_id in job_ids:
        job = await reap_access_job(job_id, now=moment)
        if job is not None:
            reaped.append(job)
    return reaped


# ── Redis-stream dispatch (redis-worker mode) ─────────────────────────────────


def _dispatch_redis():
    client = session_store.async_redis()
    if client is None:
        raise RuntimeError("Redis-backed access job execution requires Redis to be configured and reachable.")
    return client


def _worker_redis():
    """A client for the executor: its socket timeout outlasts a blocking XREADGROUP."""
    if not settings.redis_url:
        raise RuntimeError("The access job worker requires REDIS_URL.")
    import redis.asyncio as redis_asyncio

    block_seconds = settings.access_job_worker_block_ms / 1000
    return redis_asyncio.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=block_seconds + 10,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )


async def _ensure_dispatch_consumer_group(redis_client: Any) -> None:
    try:
        await redis_client.xgroup_create(
            settings.access_job_stream_key,
            settings.access_job_consumer_group,
            id="0-0",
            mkstream=True,
        )
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _encrypt_dispatch_kwargs(executor_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    payload_kwargs = dict(executor_kwargs)
    for field_name in ("username", "password"):
        value = payload_kwargs.pop(field_name, None)
        if value is not None:
            payload_kwargs[f"{field_name}_encrypted"] = encrypt_credential(value)
    return payload_kwargs


def _decrypt_dispatch_kwargs(executor_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    resolved_kwargs = dict(executor_kwargs)
    for field_name in ("username", "password"):
        encrypted_value = resolved_kwargs.pop(f"{field_name}_encrypted", None)
        if encrypted_value is not None:
            resolved_kwargs[field_name] = decrypt_credential(encrypted_value)
    return resolved_kwargs


async def _store_dispatch_payload(
    redis_client: Any,
    *,
    job_id: str,
    executor_name: str,
    executor_kwargs: Dict[str, Any],
) -> None:
    payload = {
        "job_id": job_id,
        "executor": executor_name,
        "executor_kwargs": _encrypt_dispatch_kwargs(executor_kwargs),
        "queued_at": time.time(),
    }
    await redis_client.set(
        _dispatch_payload_key(job_id),
        json.dumps(payload, sort_keys=True),
        ex=settings.access_job_payload_ttl,
    )


async def _load_dispatch_payload(redis_client: Any, job_id: str) -> Optional[Dict[str, Any]]:
    raw = await redis_client.get(_dispatch_payload_key(job_id))
    if not raw:
        return None
    return json.loads(raw)


def _resolve_dispatched_executor(
    executor_name: str,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    if executor_overrides and executor_name in executor_overrides:
        return executor_overrides[executor_name]

    if executor_name == "connect_to_site":
        from src.core.engine import connect_to_site

        return connect_to_site

    raise RuntimeError(f"Unsupported dispatched executor: {executor_name}")


async def _queue_dispatched_access_job(
    job: AccessJob,
    *,
    executor_name: str,
    executor_kwargs: Dict[str, Any],
) -> None:
    redis_client = _dispatch_redis()
    await _ensure_dispatch_consumer_group(redis_client)
    await _store_dispatch_payload(
        redis_client,
        job_id=job.id,
        executor_name=executor_name,
        executor_kwargs=executor_kwargs,
    )
    await redis_client.xadd(settings.access_job_stream_key, {"job_id": job.id})


async def _claim_dispatched_message(
    redis_client: Any,
    consumer_name: str,
    *,
    block_ms: Optional[int] = None,
) -> Optional[Tuple[str, Dict[str, str]]]:
    """The next message for this consumer: a dead consumer's stale one first, else a new one."""
    stream, group = settings.access_job_stream_key, settings.access_job_consumer_group
    for attempt in range(2):
        try:
            auto_claim_response = await redis_client.xautoclaim(
                stream,
                group,
                consumer_name,
                settings.access_job_reclaim_idle_ms,
                start_id="0-0",
                count=1,
            )
            claimed_messages = auto_claim_response[1] if len(auto_claim_response) > 1 else []
            for message_id, fields in claimed_messages:
                if fields is not None:
                    return message_id, fields
            entries = await redis_client.xreadgroup(
                group,
                consumer_name,
                {stream: ">"},
                count=1,
                block=settings.access_job_worker_block_ms if block_ms is None else block_ms,
            )
        except Exception as exc:
            if attempt == 0 and "NOGROUP" in str(exc):
                await _ensure_dispatch_consumer_group(redis_client)
                continue
            raise
        if not entries:
            return None
        _stream_name, messages = entries[0]
        if not messages:
            return None
        message_id, fields = messages[0]
        return message_id, fields or {}
    return None


async def _complete_dispatched_message(redis_client: Any, message_id: str, job_id: Optional[str]) -> None:
    """Ack and drop a message whose job has its final state stored (and its payload)."""
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.xack(settings.access_job_stream_key, settings.access_job_consumer_group, message_id)
        pipe.xdel(settings.access_job_stream_key, message_id)
        if job_id:
            pipe.delete(_dispatch_payload_key(job_id))
        await pipe.execute()


async def _requeue_dispatched_message(redis_client: Any, message_id: str, fields: Dict[str, str]) -> None:
    """Hand a message this worker will not run back to the stream at once (graceful shutdown)."""
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.xadd(settings.access_job_stream_key, fields)
        pipe.xack(settings.access_job_stream_key, settings.access_job_consumer_group, message_id)
        pipe.xdel(settings.access_job_stream_key, message_id)
        await pipe.execute()


async def _settle_unclaimable_message(redis_client: Any, message_id: str, job_id: str) -> None:
    """A message whose job this worker could not claim: drop it if the job is over, reap it if orphaned."""
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None or job.status in TERMINAL_JOB_STATUSES:
        await _complete_dispatched_message(redis_client, message_id, job_id)
        return
    if job.status != "running":
        return
    reaped = await reap_access_job(job_id)
    if reaped is not None:
        await _complete_dispatched_message(redis_client, message_id, job_id)
        await _notify_reaped([reaped])
    # Otherwise its runner is alive (its heartbeat is recent): leave the message to it.


async def _notify_reaped(jobs: List[AccessJob]) -> None:
    """End the hosted-link sessions of reaped jobs (ERROR event + LINK_ERROR webhook)."""
    if not jobs:
        return
    try:
        from src.routers.link_sessions import end_link_sessions_for_jobs

        await end_link_sessions_for_jobs(jobs)
    except Exception as exc:
        logger.warning(
            "Could not update link sessions of reaped jobs", extra={"extra_data": {"error": type(exc).__name__}}
        )


async def _process_dispatched_message(
    redis_client: Any,
    consumer_name: str,
    message_id: str,
    fields: Dict[str, str],
    *,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
) -> None:
    job_id = fields.get("job_id")
    if not job_id:
        await _complete_dispatched_message(redis_client, message_id, None)
        return

    # Only the consumer that moves the job from pending to running runs it; a
    # re-delivered message for a job that already started never runs it twice.
    if not await asyncio.to_thread(_claim_job, job_id, consumer_name):
        await _settle_unclaimable_message(redis_client, message_id, job_id)
        return

    job = await asyncio.to_thread(_load_job, job_id)
    if job is None:
        await _complete_dispatched_message(redis_client, message_id, job_id)
        return
    spec = _JobSpec.of(job)

    payload = await _load_dispatch_payload(redis_client, job_id)
    try:
        if payload is None:
            raise PlaidifyError("Access job dispatch payload expired before execution.", status_code=500)
        executor = _resolve_dispatched_executor(payload["executor"], executor_overrides=executor_overrides)
        executor_kwargs = _decrypt_dispatch_kwargs(payload["executor_kwargs"])
    except Exception as exc:
        await _finish_failed(spec, exc, spec.metadata)
        await _complete_dispatched_message(redis_client, message_id, job_id)
        return

    async def renew_claim() -> None:
        # Resets the message's idle time (JUSTID: no delivery count bump), so
        # no other consumer reclaims a job that is still running.
        await redis_client.xclaim(
            settings.access_job_stream_key,
            settings.access_job_consumer_group,
            consumer_name,
            0,
            [message_id],
            justid=True,
        )

    try:
        await _run_claimed_job(
            spec,
            executor=executor,
            executor_kwargs=executor_kwargs,
            metadata=spec.metadata,
            renew_claim=renew_claim,
        )
    except asyncio.CancelledError:
        if await asyncio.shield(asyncio.to_thread(_job_is_terminal, job_id)):
            await asyncio.shield(_complete_dispatched_message(redis_client, message_id, job_id))
        raise
    except MFARequiredError as exc:
        logger.info(
            "Dispatched access job stopped at an MFA challenge",
            extra={
                "extra_data": {
                    "job_id": job_id,
                    "site": exc.site,
                    "mfa_type": exc.mfa_type,
                    "session": token_fingerprint(exc.session_id),
                }
            },
        )
    except PlaidifyError as exc:
        logger.info(
            "Dispatched access job did not complete",
            extra={"extra_data": {"job_id": job_id, "error_code": classify_exception(exc).value}},
        )
    except Exception as exc:
        logger.error(
            "Dispatched access job execution failed",
            extra={"extra_data": {"job_id": job_id, "error": type(exc).__name__}},
        )

    # Ack only once the job's final state is stored; otherwise the message
    # stays pending and the job is reaped when its heartbeat goes stale.
    if await asyncio.to_thread(_job_is_terminal, job_id):
        await _complete_dispatched_message(redis_client, message_id, job_id)
    else:
        logger.warning(
            "Dispatched access job ended without a stored final state; leaving its message for recovery",
            extra={"extra_data": {"job_id": job_id}},
        )


async def process_dispatched_access_job(
    *,
    consumer_name: str,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
    redis_client: Any = None,
    block_ms: Optional[int] = None,
) -> bool:
    """Claim and execute one Redis-dispatched access job message.

    Returns True when a job message was processed, False when the queue was idle.
    """
    own_client = redis_client is None
    client = redis_client if redis_client is not None else _worker_redis()
    try:
        await _ensure_dispatch_consumer_group(client)
        claimed = await _claim_dispatched_message(client, consumer_name, block_ms=block_ms)
        if not claimed:
            return False
        message_id, fields = claimed
        await _process_dispatched_message(
            client,
            consumer_name,
            message_id,
            fields,
            executor_overrides=executor_overrides,
        )
        return True
    finally:
        if own_client:
            await client.aclose()


async def _sleep_unless(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_access_job_worker(
    *,
    stop_event: Optional[asyncio.Event] = None,
    consumer_name: Optional[str] = None,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
    drain_timeout: Optional[float] = None,
) -> None:
    """Run the Redis-backed access job worker until ``stop_event`` is set (or cancelled).

    On stop it takes no new jobs, gives running ones ``drain_timeout`` seconds
    (ACCESS_JOB_DRAIN_SECONDS) to finish, then cancels them; a cancelled job is
    marked cancelled, its lock freed and its message acked.
    """
    stop_event = stop_event or asyncio.Event()
    concurrency = max(1, settings.access_job_worker_concurrency)
    consumer_prefix = consumer_name or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    drain = settings.access_job_drain_seconds if drain_timeout is None else drain_timeout
    client = _worker_redis()
    busy: Dict[int, bool] = {}

    async def _consumer_loop(index: int) -> None:
        current_consumer = f"{consumer_prefix}-{index}"
        backoff = 0.5
        while not stop_event.is_set():
            busy[index] = False
            try:
                claimed = await _claim_dispatched_message(client, current_consumer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Access job worker could not read the queue",
                    extra={"extra_data": {"consumer": current_consumer, "error": type(exc).__name__}},
                )
                await _sleep_unless(stop_event, backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 0.5
            if claimed is None:
                continue
            message_id, fields = claimed
            if stop_event.is_set():
                await _requeue_dispatched_message(client, message_id, fields)
                return
            busy[index] = True
            try:
                await _process_dispatched_message(
                    client,
                    current_consumer,
                    message_id,
                    fields,
                    executor_overrides=executor_overrides,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Access job worker loop failed",
                    extra={"extra_data": {"consumer": current_consumer, "error": type(exc).__name__}},
                )

    try:
        await _ensure_dispatch_consumer_group(client)
        tasks = {index: asyncio.create_task(_consumer_loop(index)) for index in range(concurrency)}
        stop_waiter = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait([stop_waiter, *tasks.values()], return_when=asyncio.FIRST_COMPLETED)
            stop_event.set()
            # Idle consumers are only waiting on the queue: stop them now. A
            # message one of them was just handed stays pending and is taken
            # over by another worker once its reclaim window passes.
            for index, task in tasks.items():
                if not busy.get(index):
                    task.cancel()
            _done, pending = await asyncio.wait(tasks.values(), timeout=drain)
            if pending:
                logger.warning(
                    "Cancelling access jobs still running after the drain window",
                    extra={"extra_data": {"count": len(pending), "drain_seconds": drain}},
                )
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            stop_waiter.cancel()
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(stop_waiter, *tasks.values(), return_exceptions=True)
    finally:
        await client.aclose()


# ── Public entry points ───────────────────────────────────────────────────────


async def start_access_job(
    db: Session,
    *,
    site: str,
    job_type: str,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
    executor_name: Optional[str] = None,
    user_id: Optional[int] = None,
    principal_hint: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[AccessJob, asyncio.Task]:
    """Create a job record and run it detached: in a task here, or on the executor (redis-worker mode).

    The lock is per credential (the site login in ``executor_kwargs``), so two
    end users of one developer never block each other. Commits ``db``.
    """
    job = _create_access_job(
        db,
        site=site,
        job_type=job_type,
        user_id=user_id,
        principal_hint=principal_hint,
        session_id=executor_kwargs.get("session_id"),
        metadata=metadata,
        executor_kwargs=executor_kwargs,
    )
    execution_kwargs = dict(executor_kwargs)
    execution_kwargs.setdefault("session_id", job.session_id)

    if _access_job_dispatch_enabled() and executor_name:
        try:
            await _queue_dispatched_access_job(
                job,
                executor_name=executor_name,
                executor_kwargs=execution_kwargs,
            )
        except Exception as exc:
            await asyncio.to_thread(_mark_job_failed, job.id, "The access job could not be queued.")
            raise PlaidifyError("The access job could not be queued. Please try again.", status_code=503) from exc
        observer_task = asyncio.create_task(_wait_for_terminal_job_result(job.id))
        _register_background_task(job.id, observer_task)
        return job, observer_task

    task = asyncio.create_task(
        _execute_background_job(
            job_id=job.id,
            executor=executor,
            executor_kwargs=execution_kwargs,
        )
    )
    _register_background_task(job.id, task, runs_job=True)
    return job, task


async def run_access_job(
    db: Session,
    *,
    site: str,
    job_type: str,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
    user_id: Optional[int] = None,
    principal_hint: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[AccessJob, Dict[str, Any]]:
    """Create, lock, execute, and persist a tracked access job in this process. Commits ``db``."""

    job = _create_access_job(
        db,
        site=site,
        job_type=job_type,
        user_id=user_id,
        principal_hint=principal_hint,
        session_id=executor_kwargs.get("session_id"),
        metadata=metadata,
        executor_kwargs=executor_kwargs,
    )
    return await _execute_background_job(job_id=job.id, executor=executor, executor_kwargs=executor_kwargs)


async def _wait_for_terminal_job_result(
    job_id: str,
    *,
    poll_interval: float = 0.05,
    max_poll_interval: float = 1.0,
    timeout: Optional[float] = None,
) -> Tuple[AccessJob, Dict[str, Any]]:
    """Wait for a dispatched job's outcome: its result, or the error it failed with."""
    wait = timeout if timeout is not None else settings.access_job_queue_timeout_seconds + _run_budget_seconds() + 60
    deadline = time.monotonic() + wait
    interval = poll_interval
    while True:
        job, result = await asyncio.to_thread(_load_job_outcome, job_id)
        if job is None:
            raise RuntimeError(f"Access job not found: {job_id}")
        if job.status == "completed":
            return job, result or {"status": "completed"}
        if job.status in TERMINAL_JOB_STATUSES:
            raise error_for_job(job)
        if time.monotonic() >= deadline:
            raise PlaidifyError(message=f"Timed out waiting for access job: {job_id}", status_code=504)
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, max_poll_interval)


async def wait_for_mfa_session(
    session_id: str,
    *,
    timeout: float,
    poll_interval: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Wait briefly for a running job's MFA challenge to appear; only a challenge still awaiting a code counts."""
    mfa_manager = get_mfa_manager()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        session = await mfa_manager.get_session(session_id)
        if session is not None and session.awaiting_code:
            return {
                "session_id": session.session_id,
                "site": session.site,
                "mfa_type": session.mfa_type,
                "metadata": session.metadata,
                "attempts": session.attempts,
            }
        await asyncio.sleep(poll_interval)

    return None


def serialize_access_job(
    job: AccessJob,
    *,
    include_result: bool = True,
    owner: Optional[User] = None,
) -> Dict[str, Any]:
    """Convert an AccessJob ORM row into an API-safe response payload.

    ``result`` is decrypted for the job's owner (jobs without an owner have
    none). This may reach the key service: from async code prefer
    :func:`serialize_access_job_runtime`, which runs it off the event loop.
    """

    metadata = _deserialize_metadata(job.metadata_json)
    result = _read_stored_result(job, owner) if include_result else None
    mfa_type = None
    if isinstance(metadata, dict):
        mfa_type = metadata.get("mfa_type")

    return {
        "job_id": job.id,
        "site": job.site,
        "job_type": job.job_type,
        "status": job.status,
        "session_id": job.session_id,
        "mfa_type": mfa_type,
        "error_message": job.error_message,
        "error_code": job.error_code,
        "metadata": metadata,
        "result": result,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


async def serialize_access_job_runtime(
    job: AccessJob,
    *,
    include_result: bool = True,
    owner: Optional[User] = None,
) -> Dict[str, Any]:
    """Serialize a job and overlay the live MFA state of a running job.

    A running job reports ``mfa_required`` only while its MFA challenge awaits
    a code. Once a code is in, it reports ``running`` with
    ``mfa_state: "verifying"`` until the site answers; a rejected code reopens
    the challenge, and ``mfa_required`` comes back with ``mfa_error`` and
    ``attempts_remaining`` in the metadata.
    """
    if include_result and job.result_json:
        payload = await asyncio.to_thread(serialize_access_job, job, include_result=True, owner=owner)
    else:
        payload = serialize_access_job(job, include_result=False)

    if job.status != "running" or not job.session_id:
        return payload

    mfa_session = await get_mfa_manager().get_session(job.session_id)
    if mfa_session is None:
        return payload

    if not mfa_session.awaiting_code:
        payload["mfa_state"] = "verifying"
        return payload

    merged_metadata: Dict[str, Any] = {}
    if isinstance(payload.get("metadata"), dict):
        merged_metadata.update(payload["metadata"])
    merged_metadata.update(mfa_session.metadata)

    payload["status"] = "mfa_required"
    payload["mfa_state"] = "awaiting_code"
    payload["mfa_type"] = mfa_session.mfa_type
    payload["mfa_attempts"] = mfa_session.attempts
    payload["metadata"] = merged_metadata or None
    return payload
