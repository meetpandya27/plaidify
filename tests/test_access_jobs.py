"""Tests for access job tracking and scope locking."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.access_jobs import (
    process_dispatched_access_job,
    run_access_job,
    shutdown_access_jobs,
    start_access_job,
    wait_for_mfa_session,
)
from src.core.mfa_manager import get_mfa_manager
from src.database import AccessJob, AuditLog, User
from src.exceptions import ConcurrentAccessError, MFARequiredError, ReadOnlyPolicyViolationError
from tests.conftest import TestSessionLocal


class FakeRedisStream:
    def __init__(self):
        self.values = {}
        self.streams = {}
        self.groups = set()
        self.stream_id = 0

    def ping(self):
        return True

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        return 1

    def xgroup_create(self, name, groupname, id="0-0", mkstream=False):
        self.groups.add((name, groupname))
        self.streams.setdefault(name, [])
        return True

    def xadd(self, name, fields):
        self.stream_id += 1
        message_id = f"{self.stream_id}-0"
        self.streams.setdefault(name, []).append((message_id, dict(fields)))
        return message_id

    def xautoclaim(self, name, groupname, consumername, min_idle_time, start_id="0-0", count=1):
        return "0-0", [], []

    def xreadgroup(self, groupname, consumername, streams, count=1, block=None):
        stream_name = next(iter(streams))
        messages = self.streams.get(stream_name, [])
        if not messages:
            return []
        message = messages[0]
        return [(stream_name, [message])]

    def xack(self, name, groupname, message_id):
        return 1

    def xdel(self, name, message_id):
        messages = self.streams.get(name, [])
        self.streams[name] = [item for item in messages if item[0] != message_id]
        return 1


class TestAccessJobTracking:
    def test_connect_creates_completed_access_job(self, client):
        response = client.post(
            "/connect",
            json={
                "site": "internal_bank",
                "username": "test_user",
                "password": "secret123",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "connected"
        assert payload["job_id"]

        db = TestSessionLocal()
        try:
            jobs = db.query(AccessJob).all()
            assert len(jobs) == 1

            job = jobs[0]
            assert payload["job_id"] == job.id
            assert job.site == "internal_bank"
            assert job.job_type == "connect"
            assert job.status == "completed"
            assert job.user_id is None
            assert job.lock_scope.startswith("principal:")
            assert job.session_id.startswith("access-")
            assert job.started_at is not None
            assert job.completed_at is not None
        finally:
            db.close()

    def test_fetch_data_creates_user_scoped_access_job(self, client, auth_headers):
        link_response = client.post(
            "/create_link",
            params={"site": "internal_bank"},
            headers=auth_headers,
        )
        assert link_response.status_code == 200
        link_token = link_response.json()["link_token"]

        credential_response = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        assert credential_response.status_code == 200
        access_token = credential_response.json()["access_token"]

        fetch_response = client.get(
            "/fetch_data",
            params={"access_token": access_token},
            headers=auth_headers,
        )
        assert fetch_response.status_code == 200
        payload = fetch_response.json()
        assert payload["status"] == "connected"
        assert payload["job_id"]

        db = TestSessionLocal()
        try:
            user = db.query(User).filter_by(username="testuser").first()
            assert user is not None

            jobs = db.query(AccessJob).all()
            assert len(jobs) == 1

            job = jobs[0]
            assert payload["job_id"] == job.id
            assert job.site == "internal_bank"
            assert job.job_type == "fetch_data"
            assert job.status == "completed"
            assert job.user_id == user.id
            assert job.lock_scope == f"user:{user.id}:site:internal_bank"
            assert job.session_id.startswith("access-")
        finally:
            db.close()

    def test_get_access_job_for_authenticated_user(self, client, auth_headers):
        link_response = client.post(
            "/create_link",
            params={"site": "internal_bank"},
            headers=auth_headers,
        )
        link_token = link_response.json()["link_token"]

        credential_response = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        access_token = credential_response.json()["access_token"]

        fetch_response = client.get(
            "/fetch_data",
            params={"access_token": access_token},
            headers=auth_headers,
        )
        job_id = fetch_response.json()["job_id"]

        status_response = client.get(
            f"/access_jobs/{job_id}",
            headers=auth_headers,
        )
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["job_id"] == job_id
        assert payload["site"] == "internal_bank"
        assert payload["job_type"] == "fetch_data"
        assert payload["status"] == "completed"
        assert payload["metadata"]["result_status"] == "connected"
        assert payload["result"]["status"] == "connected"
        assert payload["result"]["data"]

    def test_list_access_jobs_for_authenticated_user(self, client, auth_headers):
        link_response = client.post(
            "/create_link",
            params={"site": "internal_bank"},
            headers=auth_headers,
        )
        link_token = link_response.json()["link_token"]

        credential_response = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        access_token = credential_response.json()["access_token"]

        client.get(
            "/fetch_data",
            params={"access_token": access_token},
            headers=auth_headers,
        )

        list_response = client.get("/access_jobs", headers=auth_headers)
        assert list_response.status_code == 200
        payload = list_response.json()
        assert payload["count"] == 1
        assert payload["jobs"][0]["job_type"] == "fetch_data"

    def test_access_job_requires_matching_user(self, client, auth_headers, second_user_headers):
        link_response = client.post(
            "/create_link",
            params={"site": "internal_bank"},
            headers=auth_headers,
        )
        link_token = link_response.json()["link_token"]

        credential_response = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        access_token = credential_response.json()["access_token"]

        fetch_response = client.get(
            "/fetch_data",
            params={"access_token": access_token},
            headers=auth_headers,
        )
        job_id = fetch_response.json()["job_id"]

        other_user_response = client.get(
            f"/access_jobs/{job_id}",
            headers=second_user_headers,
        )
        assert other_user_response.status_code == 404

        anonymous_response = client.get(f"/access_jobs/{job_id}")
        assert anonymous_response.status_code == 401

    def test_anonymous_access_job_is_retrievable_by_job_id(self, client):
        db = TestSessionLocal()
        try:
            job = AccessJob(
                id="ajob-anonymous-test",
                user_id=None,
                site="internal_bank",
                job_type="connect",
                status="mfa_required",
                lock_scope="principal:test:site:internal_bank",
                session_id="mfa-session-123",
                created_at=datetime.now(timezone.utc),
            )
            db.add(job)
            db.commit()

            found = client.get(
                "/access_jobs/ajob-anonymous-test",
            )
            assert found.status_code == 200
            assert found.json()["job_id"] == "ajob-anonymous-test"
        finally:
            db.close()

    def test_connect_mfa_response_includes_job_id(self, client):
        with patch(
            "src.routers.connection.connect_to_site",
            AsyncMock(
                side_effect=MFARequiredError(
                    site="internal_bank",
                    mfa_type="totp",
                    session_id="mfa-session-xyz",
                )
            ),
        ):
            response = client.post(
                "/connect",
                json={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "mfa_required"
        assert payload["job_id"]
        assert payload["session_id"] == "mfa-session-xyz"
        assert payload["mfa_type"] == "totp"

        status_response = client.get(
            f"/access_jobs/{payload['job_id']}",
            params={"session_id": "mfa-session-xyz"},
        )
        assert status_response.status_code == 200
        job_payload = status_response.json()
        assert job_payload["status"] == "mfa_required"
        assert job_payload["session_id"] == "mfa-session-xyz"

    def test_connect_returns_pending_for_background_execution(self, client):
        async def slow_connect(**kwargs):
            await asyncio.sleep(0.15)
            return {"status": "connected", "data": {"profile_status": "ready"}}

        with (
            patch("src.routers.connection.connect_to_site", slow_connect),
            patch("src.routers.connection._CONNECT_COMPLETION_WAIT_SECONDS", 0.01),
            patch("src.routers.connection._CONNECT_MFA_DISCOVERY_WAIT_SECONDS", 0.01),
        ):
            response = client.post(
                "/connect",
                json={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "pending"
        assert payload["job_id"]

    def test_connect_background_mfa_updates_access_job_status(self, client):
        async def waiting_mfa_connect(**kwargs):
            mfa_manager = get_mfa_manager()
            session = await mfa_manager.create_session(
                session_id=kwargs["session_id"],
                site=kwargs["site"],
                mfa_type="totp",
                metadata={"prompt": "Enter the one-time code"},
            )
            code = await session.wait_for_code(timeout=2)
            if not code:
                await mfa_manager.remove_session(kwargs["session_id"])
                raise MFARequiredError(
                    site=kwargs["site"],
                    mfa_type="totp",
                    session_id=kwargs["session_id"],
                )
            await mfa_manager.remove_session(kwargs["session_id"])
            return {"status": "connected", "data": {"verification": code}}

        with (
            patch("src.routers.connection.connect_to_site", waiting_mfa_connect),
            patch("src.routers.connection._CONNECT_COMPLETION_WAIT_SECONDS", 0.01),
            patch("src.routers.connection._CONNECT_MFA_DISCOVERY_WAIT_SECONDS", 0.2),
        ):
            response = client.post(
                "/connect",
                json={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "mfa_required"
            assert payload["job_id"]
            assert payload["session_id"]


class TestAccessJobLocking:
    @pytest.mark.asyncio
    async def test_run_access_job_blocks_overlapping_scope(self):
        db1 = TestSessionLocal()
        db2 = TestSessionLocal()
        verifier = TestSessionLocal()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_executor(**kwargs):
            started.set()
            await release.wait()
            return {"status": "connected", "data": {"balance": "$10.00"}}

        task = asyncio.create_task(
            run_access_job(
                db1,
                site="internal_bank",
                job_type="connect",
                executor=slow_executor,
                executor_kwargs={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
                principal_hint="test_user",
            )
        )

        try:
            await started.wait()

            with pytest.raises(ConcurrentAccessError):
                await run_access_job(
                    db2,
                    site="internal_bank",
                    job_type="connect",
                    executor=slow_executor,
                    executor_kwargs={
                        "site": "internal_bank",
                        "username": "test_user",
                        "password": "secret123",
                    },
                    principal_hint="test_user",
                )

            release.set()
            job, result = await task

            assert job.status == "completed"
            assert result["status"] == "connected"

            jobs = verifier.query(AccessJob).order_by(AccessJob.created_at).all()
            assert len(jobs) == 2
            statuses = sorted(job.status for job in jobs)
            assert statuses == ["blocked", "completed"]
            blocked_job = next(job for job in jobs if job.status == "blocked")
            assert blocked_job.error_message
            assert "already in progress" in blocked_job.error_message
        finally:
            release.set()
            if not task.done():
                await task
            db1.close()
            db2.close()
            verifier.close()


class TestDetachedAccessJobExecution:
    @pytest.mark.asyncio
    async def test_start_access_job_completes_in_background(self):
        db = TestSessionLocal()
        verifier = TestSessionLocal()

        async def slow_connect(**kwargs):
            await asyncio.sleep(0.05)
            return {"status": "connected", "data": {"profile_status": "ready"}}

        try:
            job, task = await start_access_job(
                db,
                site="internal_bank",
                job_type="connect",
                executor=slow_connect,
                executor_kwargs={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
                principal_hint="test_user",
            )

            assert job.status == "pending"
            completed_job, result = await task
            assert completed_job.id == job.id
            assert result["status"] == "connected"

            verifier.expire_all()
            stored_job = verifier.query(AccessJob).filter_by(id=job.id).first()
            assert stored_job is not None
            assert stored_job.status == "completed"
            assert "connected" in stored_job.metadata_json
            assert "profile_status" in stored_job.result_json
        finally:
            db.close()
            verifier.close()

    def test_fetch_data_persists_read_only_policy_metadata(self, client, auth_headers):
        policy_result = {
            "status": "connected",
            "data": {"balance": "$150.00"},
            "metadata": {
                "read_only_policy": {
                    "enabled": True,
                    "final_phase": "read",
                    "blocked_action_count": 1,
                    "blocked_actions": [
                        {
                            "phase": "read",
                            "action": "request",
                            "reason": "navigation POST requests are blocked",
                            "target": "https://example.test/pay",
                        }
                    ],
                }
            },
        }

        with patch("src.routers.links.connect_to_site", AsyncMock(return_value=policy_result)):
            link_response = client.post(
                "/create_link",
                params={"site": "internal_bank"},
                headers=auth_headers,
            )
            link_token = link_response.json()["link_token"]

            credential_response = client.post(
                "/submit_credentials",
                params={
                    "link_token": link_token,
                    "username": "test_user",
                    "password": "secret123",
                },
                headers=auth_headers,
            )
            access_token = credential_response.json()["access_token"]

            fetch_response = client.get(
                "/fetch_data",
                params={"access_token": access_token},
                headers=auth_headers,
            )

        assert fetch_response.status_code == 200
        job_id = fetch_response.json()["job_id"]

        db = TestSessionLocal()
        try:
            job = db.query(AccessJob).filter_by(id=job_id).first()
            assert job is not None
            assert job.metadata_json is not None
            assert "read_only_policy_blocked_count" in job.metadata_json

            audit_entry = (
                db.query(AuditLog)
                .filter_by(
                    event_type="access_job",
                    action="read_only_policy_blocked",
                    resource=job_id,
                )
                .first()
            )
            assert audit_entry is not None
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_run_access_job_persists_policy_failure_metadata(self):
        async def blocked_executor(**kwargs):
            raise ReadOnlyPolicyViolationError(
                "blocked risky click",
                metadata={
                    "read_only_policy": {
                        "enabled": True,
                        "final_phase": "read",
                        "blocked_action_count": 1,
                        "blocked_actions": [
                            {
                                "phase": "read",
                                "action": "click",
                                "reason": "click target matched a risky action pattern",
                                "target": "#pay-now",
                            }
                        ],
                    }
                },
            )

        db = TestSessionLocal()
        try:
            with pytest.raises(ReadOnlyPolicyViolationError):
                await run_access_job(
                    db,
                    site="internal_bank",
                    job_type="fetch_data",
                    executor=blocked_executor,
                    executor_kwargs={"site": "internal_bank"},
                    metadata={"agent_id": "agent-test"},
                )

            job = db.query(AccessJob).order_by(AccessJob.created_at.desc()).first()
            assert job is not None
            assert job.status == "failed"
            assert job.metadata_json is not None
            assert "read_only_policy" in job.metadata_json

            audit_entry = (
                db.query(AuditLog)
                .filter_by(
                    event_type="access_job",
                    action="read_only_policy_blocked",
                    resource=job.id,
                )
                .first()
            )
            assert audit_entry is not None
            assert audit_entry.agent_id == "agent-test"
        finally:
            db.close()


class TestRedisBackedAccessJobExecution:
    @pytest.mark.asyncio
    async def test_start_access_job_queues_redis_dispatch(self):
        db = TestSessionLocal()
        fake_redis = FakeRedisStream()

        async def connect_to_site(**kwargs):
            return {"status": "connected", "data": {"profile_status": "ready"}}

        try:
            from src import access_jobs as access_jobs_module

            with (
                patch.object(access_jobs_module.settings, "access_job_execution_mode", "redis-worker"),
                patch("src.access_jobs.session_store._redis", return_value=fake_redis),
            ):
                job, observer = await start_access_job(
                    db,
                    site="internal_bank",
                    job_type="connect",
                    executor=connect_to_site,
                    executor_name="connect_to_site",
                    executor_kwargs={
                        "site": "internal_bank",
                        "username": "test_user",
                        "password": "secret123",
                    },
                    principal_hint="test_user",
                )

                assert fake_redis.get(access_jobs_module._dispatch_payload_key(job.id)) is not None
                assert fake_redis.streams[access_jobs_module.settings.access_job_stream_key]
                assert observer.done() is False
                assert access_jobs_module._BACKGROUND_TASKS[job.id] is observer
                observer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await observer
                await asyncio.sleep(0)
                assert job.id not in access_jobs_module._BACKGROUND_TASKS
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_worker_executes_dispatched_access_job(self):
        db = TestSessionLocal()
        verifier = TestSessionLocal()
        fake_redis = FakeRedisStream()

        async def connect_to_site(**kwargs):
            return {"status": "connected", "data": {"profile_status": "ready"}}

        try:
            from src import access_jobs as access_jobs_module

            with (
                patch.object(access_jobs_module.settings, "access_job_execution_mode", "redis-worker"),
                patch("src.access_jobs.session_store._redis", return_value=fake_redis),
            ):
                job, observer = await start_access_job(
                    db,
                    site="internal_bank",
                    job_type="connect",
                    executor=connect_to_site,
                    executor_name="connect_to_site",
                    executor_kwargs={
                        "site": "internal_bank",
                        "username": "test_user",
                        "password": "secret123",
                    },
                    principal_hint="test_user",
                )

                processed = await process_dispatched_access_job(
                    consumer_name="test-worker",
                    executor_overrides={"connect_to_site": connect_to_site},
                )
                assert processed is True

                completed_job, result = await asyncio.wait_for(observer, timeout=0.5)
                assert completed_job.id == job.id
                assert result["status"] == "connected"

                verifier.expire_all()
                stored_job = verifier.query(AccessJob).filter_by(id=job.id).first()
                assert stored_job is not None
                assert stored_job.status == "completed"
        finally:
            db.close()
            verifier.close()

    @pytest.mark.asyncio
    async def test_worker_handles_dispatched_mfa_job(self):
        db = TestSessionLocal()
        verifier = TestSessionLocal()
        fake_redis = FakeRedisStream()

        async def connect_to_site(**kwargs):
            mfa_manager = get_mfa_manager()
            session = await mfa_manager.create_session(
                session_id=kwargs["session_id"],
                site=kwargs["site"],
                mfa_type="totp",
                metadata={"prompt": "Enter the one-time code"},
            )
            code = await session.wait_for_code(timeout=2)
            if not code:
                await mfa_manager.remove_session(kwargs["session_id"])
                raise MFARequiredError(
                    site=kwargs["site"],
                    mfa_type="totp",
                    session_id=kwargs["session_id"],
                )
            await mfa_manager.remove_session(kwargs["session_id"])
            return {"status": "connected", "data": {"verification": code}}

        try:
            from src import access_jobs as access_jobs_module

            with (
                patch.object(access_jobs_module.settings, "access_job_execution_mode", "redis-worker"),
                patch("src.access_jobs.session_store._redis", return_value=fake_redis),
            ):
                job, observer = await start_access_job(
                    db,
                    site="internal_bank",
                    job_type="connect",
                    executor=connect_to_site,
                    executor_name="connect_to_site",
                    executor_kwargs={
                        "site": "internal_bank",
                        "username": "test_user",
                        "password": "secret123",
                    },
                    principal_hint="test_user",
                )

                worker_task = asyncio.create_task(
                    process_dispatched_access_job(
                        consumer_name="test-worker",
                        executor_overrides={"connect_to_site": connect_to_site},
                    )
                )

                session_payload = await wait_for_mfa_session(job.session_id, timeout=0.5)
                assert session_payload is not None
                assert session_payload["mfa_type"] == "totp"

                mfa_submit_result = await get_mfa_manager().submit_code(job.session_id, "654321")
                assert mfa_submit_result is True

                completed_job, result = await asyncio.wait_for(observer, timeout=0.5)
                assert completed_job.id == job.id
                assert result["data"]["verification"] == "654321"

                assert await worker_task is True
                verifier.expire_all()
                stored_job = verifier.query(AccessJob).filter_by(id=job.id).first()
                assert stored_job is not None
                assert stored_job.status == "completed"
        finally:
            db.close()
            verifier.close()

    @pytest.mark.asyncio
    async def test_shutdown_access_jobs_marks_running_job_cancelled(self):
        db = TestSessionLocal()
        verifier = TestSessionLocal()
        release = asyncio.Event()

        async def long_running_connect(**kwargs):
            await release.wait()
            return {"status": "connected", "data": {"profile_status": "ready"}}

        try:
            job, _task = await start_access_job(
                db,
                site="internal_bank",
                job_type="connect",
                executor=long_running_connect,
                executor_kwargs={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
                principal_hint="test_user",
            )

            await asyncio.sleep(0)
            await shutdown_access_jobs(timeout=0.5)

            verifier.expire_all()
            stored_job = verifier.query(AccessJob).filter_by(id=job.id).first()
            assert stored_job is not None
            assert stored_job.status == "cancelled"
            assert stored_job.error_message == "Access job cancelled before completion."
        finally:
            release.set()
            db.close()
            verifier.close()

    @pytest.mark.asyncio
    async def test_start_access_job_resumes_after_mfa_submission(self):
        db = TestSessionLocal()
        verifier = TestSessionLocal()

        async def waiting_mfa_connect(**kwargs):
            mfa_manager = get_mfa_manager()
            session = await mfa_manager.create_session(
                session_id=kwargs["session_id"],
                site=kwargs["site"],
                mfa_type="totp",
                metadata={"prompt": "Enter the one-time code"},
            )
            code = await session.wait_for_code(timeout=2)
            if not code:
                await mfa_manager.remove_session(kwargs["session_id"])
                raise MFARequiredError(
                    site=kwargs["site"],
                    mfa_type="totp",
                    session_id=kwargs["session_id"],
                )
            await mfa_manager.remove_session(kwargs["session_id"])
            return {"status": "connected", "data": {"verification": code}}

        try:
            job, task = await start_access_job(
                db,
                site="internal_bank",
                job_type="connect",
                executor=waiting_mfa_connect,
                executor_kwargs={
                    "site": "internal_bank",
                    "username": "test_user",
                    "password": "secret123",
                },
                principal_hint="test_user",
            )

            session_payload = await wait_for_mfa_session(job.session_id, timeout=0.5)
            assert session_payload is not None
            assert session_payload["mfa_type"] == "totp"

            stored_running_job = verifier.query(AccessJob).filter_by(id=job.id).first()
            assert stored_running_job is not None
            assert stored_running_job.status == "running"

            mfa_submit_result = await get_mfa_manager().submit_code(job.session_id, "654321")
            assert mfa_submit_result is True

            completed_job, result = await task
            assert completed_job.id == job.id
            assert result["data"]["verification"] == "654321"

            verifier.expire_all()
            stored_job = verifier.query(AccessJob).filter_by(id=job.id).first()
            assert stored_job is not None
            assert stored_job.status == "completed"
            assert "connected" in stored_job.metadata_json
            assert "654321" in stored_job.result_json
        finally:
            db.close()
            verifier.close()
