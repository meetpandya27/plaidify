"""Access job orchestration and per-site concurrency control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import time
import uuid
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from src import session_store
from src.audit import record_audit_event
from src.config import get_settings
from src.core.mfa_manager import get_mfa_manager
from src.database import AccessJob, SessionLocal, decrypt_credential, encrypt_credential
from src.exceptions import ConcurrentAccessError, MFARequiredError, PlaidifyError
from src.logging_config import get_logger

logger = get_logger("access_jobs")
settings = get_settings()

_LOCK_KEY_PREFIX = "plaidify:access_lock:"
_LOCK_TTL_SECONDS = 360
_LOCK_WAIT_SECONDS = 0.25
_LOCK_POLL_INTERVAL = 0.05
_DISPATCH_PAYLOAD_PREFIX = "plaidify:access_job_payload:"
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_LOCAL_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCAL_LOCKS_GUARD = asyncio.Lock()
_BACKGROUND_TASKS: Dict[str, asyncio.Task] = {}


class _HeldScopeLock:
    def __init__(
        self,
        *,
        backend: str,
        scope: str,
        redis_client: Any = None,
        token: Optional[str] = None,
        local_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.redis_client = redis_client
        self.token = token
        self.local_lock = local_lock
        self._released = False

    async def release(self) -> None:
        if self._released:
            return

        self._released = True
        if self.backend == "redis" and self.redis_client is not None and self.token:
            try:
                self.redis_client.eval(
                    _RELEASE_LOCK_SCRIPT,
                    1,
                    _lock_key(self.scope),
                    self.token,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to release Redis access lock",
                    extra={"extra_data": {"scope": self.scope, "error": str(exc)}},
                )
            return

        if self.local_lock is not None and self.local_lock.locked():
            self.local_lock.release()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_key(scope: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{scope}"


def _job_id() -> str:
    return f"ajob-{uuid.uuid4()}"


def _session_id() -> str:
    return f"access-{uuid.uuid4()}"


def _dispatch_payload_key(job_id: str) -> str:
    return f"{_DISPATCH_PAYLOAD_PREFIX}{job_id}"


def _access_job_dispatch_enabled() -> bool:
    return settings.access_job_execution_mode == "redis-worker"


def _dispatch_redis():
    redis_client = session_store._redis()
    if redis_client is None:
        raise RuntimeError("Redis-backed access job execution requires Redis to be configured and reachable.")
    return redis_client


def _ensure_dispatch_consumer_group(redis_client: Any) -> None:
    try:
        redis_client.xgroup_create(
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


def _store_dispatch_payload(
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
    redis_client.set(
        _dispatch_payload_key(job_id),
        json.dumps(payload, sort_keys=True),
        ex=settings.access_job_payload_ttl,
    )


def _load_dispatch_payload(redis_client: Any, job_id: str) -> Optional[Dict[str, Any]]:
    raw = redis_client.get(_dispatch_payload_key(job_id))
    if not raw:
        return None
    return json.loads(raw)


def _delete_dispatch_payload(redis_client: Any, job_id: str) -> None:
    redis_client.delete(_dispatch_payload_key(job_id))


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


def _queue_dispatched_access_job(
    job: AccessJob,
    *,
    executor_name: str,
    executor_kwargs: Dict[str, Any],
) -> None:
    redis_client = _dispatch_redis()
    _ensure_dispatch_consumer_group(redis_client)
    _store_dispatch_payload(
        redis_client,
        job_id=job.id,
        executor_name=executor_name,
        executor_kwargs=executor_kwargs,
    )
    redis_client.xadd(settings.access_job_stream_key, {"job_id": job.id})


def _claim_dispatched_message(redis_client: Any, consumer_name: str):
    _ensure_dispatch_consumer_group(redis_client)

    auto_claim_response = redis_client.xautoclaim(
        settings.access_job_stream_key,
        settings.access_job_consumer_group,
        consumer_name,
        settings.access_job_reclaim_idle_ms,
        start_id="0-0",
        count=1,
    )
    if len(auto_claim_response) == 3:
        _next_start_id, claimed_messages, _deleted = auto_claim_response
    else:
        _next_start_id, claimed_messages = auto_claim_response
    if claimed_messages:
        return claimed_messages[0]

    entries = redis_client.xreadgroup(
        settings.access_job_consumer_group,
        consumer_name,
        {settings.access_job_stream_key: ">"},
        count=1,
        block=settings.access_job_worker_block_ms,
    )
    if not entries:
        return None

    _stream_name, messages = entries[0]
    if not messages:
        return None
    return messages[0]


def _ack_dispatched_message(redis_client: Any, message_id: str) -> None:
    redis_client.xack(
        settings.access_job_stream_key,
        settings.access_job_consumer_group,
        message_id,
    )
    redis_client.xdel(settings.access_job_stream_key, message_id)


async def _wait_for_terminal_job_result(
    job_id: str,
    *,
    poll_interval: float = 0.05,
) -> Tuple[AccessJob, Dict[str, Any]]:
    while True:
        db = SessionLocal()
        try:
            job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
            if job is None:
                raise RuntimeError(f"Access job not found: {job_id}")

            if job.status == "completed":
                result = _deserialize_metadata(job.result_json) or {"status": "completed"}
                return job, result

            if job.status == "mfa_required":
                metadata = _deserialize_metadata(job.metadata_json) or {}
                raise MFARequiredError(
                    site=job.site,
                    mfa_type=metadata.get("mfa_type", "unknown"),
                    session_id=job.session_id or "",
                )

            if job.status == "blocked":
                raise PlaidifyError(
                    message=job.error_message or f"Access job blocked: {job.id}",
                    status_code=409,
                )

            if job.status in {"failed", "cancelled"}:
                raise PlaidifyError(
                    message=job.error_message or f"Access job failed: {job.id}",
                    status_code=500,
                )
        finally:
            db.close()

        await asyncio.sleep(poll_interval)


def _mark_job_failed(job_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
        if job is None:
            return
        if job.status not in {"pending", "running"}:
            return
        _apply_job_state(
            job,
            status="failed",
            completed_at=_now(),
            error_message=message,
        )
        _persist_job(db, job, strict=False)
    finally:
        db.close()


def _mark_job_cancelled(job_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
        if job is None:
            return
        if job.status not in {"pending", "running"}:
            return
        _apply_job_state(
            job,
            status="cancelled",
            completed_at=_now(),
            error_message=message,
        )
        _persist_job(db, job, strict=False)
    finally:
        db.close()


def _scope_hash(principal_hint: str, site: str) -> str:
    digest = hashlib.sha256(f"{site.strip().lower()}:{principal_hint.strip().lower()}".encode()).hexdigest()
    return digest[:16]


def build_lock_scope(
    *,
    site: str,
    user_id: Optional[int] = None,
    principal_hint: Optional[str] = None,
) -> str:
    site_key = site.strip().lower()
    if user_id is not None:
        return f"user:{user_id}:site:{site_key}"
    if principal_hint:
        return f"principal:{_scope_hash(principal_hint, site_key)}:site:{site_key}"
    return f"anonymous:site:{site_key}"


async def _get_local_lock(scope: str) -> asyncio.Lock:
    async with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(scope, asyncio.Lock())


async def _acquire_local_lock(scope: str) -> _HeldScopeLock:
    lock = await _get_local_lock(scope)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ConcurrentAccessError(site=scope.split(":site:")[-1]) from exc
    return _HeldScopeLock(backend="local", scope=scope, local_lock=lock)


async def _acquire_redis_lock(redis_client: Any, scope: str) -> _HeldScopeLock:
    token = uuid.uuid4().hex
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    while True:
        try:
            acquired = redis_client.set(
                _lock_key(scope),
                token,
                ex=_LOCK_TTL_SECONDS,
                nx=True,
            )
        except Exception as exc:
            logger.warning(
                "Redis access lock unavailable, falling back to local lock",
                extra={"extra_data": {"scope": scope, "error": str(exc)}},
            )
            return await _acquire_local_lock(scope)

        if acquired:
            return _HeldScopeLock(
                backend="redis",
                scope=scope,
                redis_client=redis_client,
                token=token,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConcurrentAccessError(site=scope.split(":site:")[-1])
        await asyncio.sleep(min(_LOCK_POLL_INTERVAL, remaining))


async def acquire_scope_lock(scope: str) -> _HeldScopeLock:
    redis_client = session_store._redis()
    if redis_client is not None:
        return await _acquire_redis_lock(redis_client, scope)
    return await _acquire_local_lock(scope)


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
    db: Session,
    job: AccessJob,
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

    record_audit_event(
        db,
        "access_job",
        "read_only_policy_blocked",
        user_id=job.user_id,
        agent_id=metadata.get("agent_id"),
        resource=job.id,
        metadata={
            "site": job.site,
            "job_type": job.job_type,
            "status": status,
            "blocked_action_count": blocked_count,
            "blocked_actions": policy.get("blocked_actions", []),
        },
    )


def _persist_job(db: Session, job: AccessJob, *, strict: bool) -> None:
    try:
        db.add(job)
        db.commit()
        db.refresh(job)
    except Exception as exc:
        db.rollback()
        if strict:
            raise
        logger.error(
            "Failed to persist access job state",
            extra={"extra_data": {"job_id": job.id, "error": str(exc)}},
        )


def _apply_job_state(
    job: AccessJob,
    *,
    status: str,
    session_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    job.status = status
    if session_id is not None:
        job.session_id = session_id
    if started_at is not None:
        job.started_at = started_at
    if completed_at is not None:
        job.completed_at = completed_at
    if error_message is not None:
        job.error_message = error_message
    if metadata is not None:
        job.metadata_json = _serialize_metadata(metadata)
    if result is not None:
        job.result_json = _serialize_metadata(result)


def _create_access_job(
    db: Session,
    *,
    site: str,
    job_type: str,
    user_id: Optional[int],
    principal_hint: Optional[str],
    session_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
) -> AccessJob:
    scope = build_lock_scope(site=site, user_id=user_id, principal_hint=principal_hint)
    job = AccessJob(
        id=_job_id(),
        user_id=user_id,
        site=site,
        job_type=job_type,
        status="pending",
        lock_scope=scope,
        session_id=session_id or _session_id(),
        metadata_json=_serialize_metadata(metadata),
        created_at=_now(),
    )
    _persist_job(db, job, strict=True)
    return job


async def _execute_existing_job(
    db: Session,
    job: AccessJob,
    *,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
    metadata: Optional[Dict[str, Any]],
) -> Tuple[AccessJob, Dict[str, Any]]:
    held_lock: Optional[_HeldScopeLock] = None
    try:
        held_lock = await acquire_scope_lock(job.lock_scope)
    except ConcurrentAccessError as exc:
        _apply_job_state(
            job,
            status="blocked",
            completed_at=_now(),
            error_message=exc.message,
        )
        _persist_job(db, job, strict=False)
        raise

    _apply_job_state(job, status="running", started_at=_now())
    _persist_job(db, job, strict=False)

    execution_kwargs = dict(executor_kwargs)
    execution_kwargs.setdefault("session_id", job.session_id)

    try:
        result = await executor(**execution_kwargs)
    except asyncio.CancelledError:
        await get_mfa_manager().remove_session(job.session_id)
        _apply_job_state(
            job,
            status="cancelled",
            completed_at=_now(),
            error_message="Access job cancelled before completion.",
        )
        _persist_job(db, job, strict=False)
        raise
    except MFARequiredError as exc:
        exc.job_id = job.id
        _apply_job_state(
            job,
            status="mfa_required",
            session_id=exc.session_id or job.session_id,
            completed_at=_now(),
            error_message=exc.message,
            metadata=_merge_metadata(metadata, {"mfa_type": exc.mfa_type}),
        )
        _persist_job(db, job, strict=False)
        raise
    except PlaidifyError as exc:
        await get_mfa_manager().remove_session(job.session_id)
        error_metadata = _merge_metadata(metadata, getattr(exc, "metadata", None))
        _apply_job_state(
            job,
            status="failed",
            completed_at=_now(),
            error_message=exc.message,
            metadata=error_metadata,
        )
        _persist_job(db, job, strict=False)
        _record_policy_audit_if_needed(db, job, error_metadata, status="failed")
        raise
    except Exception as exc:
        await get_mfa_manager().remove_session(job.session_id)
        _apply_job_state(
            job,
            status="failed",
            completed_at=_now(),
            error_message=str(exc),
        )
        _persist_job(db, job, strict=False)
        raise
    finally:
        if held_lock is not None:
            await held_lock.release()

    completed_metadata = _merge_metadata(metadata, _result_metadata(result))
    _apply_job_state(
        job,
        status="completed",
        completed_at=_now(),
        metadata=completed_metadata,
        result=result,
    )
    _persist_job(db, job, strict=False)
    _record_policy_audit_if_needed(db, job, completed_metadata, status="completed")
    await get_mfa_manager().remove_session(job.session_id)
    return job, result


async def _execute_background_job(
    *,
    job_id: str,
    executor: Callable[..., Awaitable[Dict[str, Any]]],
    executor_kwargs: Dict[str, Any],
) -> Tuple[AccessJob, Dict[str, Any]]:
    db = SessionLocal()
    try:
        job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
        if not job:
            raise RuntimeError(f"Access job not found: {job_id}")
        metadata = _deserialize_metadata(job.metadata_json)
        return await _execute_existing_job(
            db,
            job,
            executor=executor,
            executor_kwargs=executor_kwargs,
            metadata=metadata,
        )
    finally:
        db.close()


def _register_background_task(job_id: str, task: asyncio.Task) -> None:
    _BACKGROUND_TASKS[job_id] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        _BACKGROUND_TASKS.pop(job_id, None)
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
                extra={"extra_data": {"job_id": job_id, "error": str(exc)}},
            )

    task.add_done_callback(_cleanup)


async def shutdown_access_jobs(*, timeout: float = 10.0) -> None:
    """Cancel and await all in-process background access jobs."""
    tasks = list(_BACKGROUND_TASKS.items())
    if not tasks:
        return

    logger.info(
        "Shutting down in-process access jobs",
        extra={"extra_data": {"count": len(tasks)}},
    )

    for _job_id, task in tasks:
        task.cancel()

    for job_id, task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            _mark_job_cancelled(job_id, "Access job cancelled before completion.")
            logger.warning(
                "Timed out waiting for access job shutdown",
                extra={"extra_data": {"job_id": job_id, "timeout": timeout}},
            )
        except Exception as exc:
            logger.warning(
                "Background access job failed during shutdown",
                extra={"extra_data": {"job_id": job_id, "error": str(exc)}},
            )


async def process_dispatched_access_job(
    *,
    consumer_name: str,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
) -> bool:
    """Claim and execute one Redis-dispatched access job message.

    Returns True when a job message was processed, False when the queue was idle.
    """
    redis_client = _dispatch_redis()
    claimed = _claim_dispatched_message(redis_client, consumer_name)
    if not claimed:
        return False

    message_id, fields = claimed
    job_id = fields.get("job_id")
    if not job_id:
        _ack_dispatched_message(redis_client, message_id)
        return True

    payload = _load_dispatch_payload(redis_client, job_id)
    if payload is None:
        _mark_job_failed(job_id, "Access job dispatch payload expired before execution.")
        _ack_dispatched_message(redis_client, message_id)
        return True

    try:
        executor = _resolve_dispatched_executor(
            payload["executor"],
            executor_overrides=executor_overrides,
        )
        executor_kwargs = _decrypt_dispatch_kwargs(payload["executor_kwargs"])
        await _execute_background_job(
            job_id=job_id,
            executor=executor,
            executor_kwargs=executor_kwargs,
        )
    except MFARequiredError as exc:
        logger.info(
            "Dispatched access job is awaiting MFA",
            extra={
                "extra_data": {
                    "job_id": job_id,
                    "site": exc.site,
                    "mfa_type": exc.mfa_type,
                    "session_id": exc.session_id,
                }
            },
        )
    except Exception as exc:
        _mark_job_failed(job_id, str(exc))
        logger.error(
            "Dispatched access job execution failed",
            extra={"extra_data": {"job_id": job_id, "error": str(exc)}},
        )
    finally:
        _delete_dispatch_payload(redis_client, job_id)
        _ack_dispatched_message(redis_client, message_id)

    return True


async def run_access_job_worker(
    *,
    stop_event: Optional[asyncio.Event] = None,
    consumer_name: Optional[str] = None,
    executor_overrides: Optional[Dict[str, Callable[..., Awaitable[Dict[str, Any]]]]] = None,
) -> None:
    """Run the Redis-backed access job worker until cancelled."""
    stop_event = stop_event or asyncio.Event()
    concurrency = max(1, settings.access_job_worker_concurrency)
    consumer_prefix = consumer_name or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    async def _consumer_loop(index: int) -> None:
        current_consumer = f"{consumer_prefix}-{index}"
        while not stop_event.is_set():
            try:
                processed = await process_dispatched_access_job(
                    consumer_name=current_consumer,
                    executor_overrides=executor_overrides,
                )
                if not processed:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Access job worker loop failed",
                    extra={"extra_data": {"consumer": current_consumer, "error": str(exc)}},
                )
                await asyncio.sleep(0.5)

    tasks = [asyncio.create_task(_consumer_loop(index)) for index in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


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
    """Create a job record and execute it in a detached background task."""

    job_session_id = executor_kwargs.get("session_id") or _session_id()
    job = _create_access_job(
        db,
        site=site,
        job_type=job_type,
        user_id=user_id,
        principal_hint=principal_hint,
        session_id=job_session_id,
        metadata=metadata,
    )
    execution_kwargs = dict(executor_kwargs)
    execution_kwargs.setdefault("session_id", job.session_id)

    if _access_job_dispatch_enabled() and executor_name:
        _queue_dispatched_access_job(
            job,
            executor_name=executor_name,
            executor_kwargs=execution_kwargs,
        )
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
    _register_background_task(job.id, task)
    return job, task


async def wait_for_mfa_session(
    session_id: str,
    *,
    timeout: float,
    poll_interval: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Wait briefly for an MFA session to appear for a running access job."""
    mfa_manager = get_mfa_manager()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        session = await mfa_manager.get_session(session_id)
        if session is not None:
            return {
                "session_id": session.session_id,
                "site": session.site,
                "mfa_type": session.mfa_type,
                "metadata": session.metadata,
            }
        await asyncio.sleep(poll_interval)

    return None


def serialize_access_job(job: AccessJob) -> Dict[str, Any]:
    """Convert an AccessJob ORM row into an API-safe response payload."""

    metadata = _deserialize_metadata(job.metadata_json)
    result = _deserialize_metadata(job.result_json)
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
        "metadata": metadata,
        "result": result,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


async def serialize_access_job_runtime(job: AccessJob) -> Dict[str, Any]:
    """Serialize a job and overlay live MFA state for running background flows."""
    payload = serialize_access_job(job)

    if job.status != "running" or not job.session_id:
        return payload

    mfa_session = await get_mfa_manager().get_session(job.session_id)
    if mfa_session is None:
        return payload

    merged_metadata: Dict[str, Any] = {}
    if isinstance(payload.get("metadata"), dict):
        merged_metadata.update(payload["metadata"])
    merged_metadata.update(mfa_session.metadata)

    payload["status"] = "mfa_required"
    payload["mfa_type"] = mfa_session.mfa_type
    payload["metadata"] = merged_metadata or None
    return payload


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
    """Create, lock, execute, and persist a tracked access job."""

    job = _create_access_job(
        db,
        site=site,
        job_type=job_type,
        user_id=user_id,
        principal_hint=principal_hint,
        session_id=executor_kwargs.get("session_id"),
        metadata=metadata,
    )
    return await _execute_existing_job(
        db,
        job,
        executor=executor,
        executor_kwargs=executor_kwargs,
        metadata=metadata,
    )
