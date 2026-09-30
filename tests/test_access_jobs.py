"""Tests for access job tracking, per-credential locking, liveness and the Redis executor."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src import access_jobs as access_jobs_module
from src.access_jobs import (
    build_lock_scope,
    cancel_access_job,
    credential_identity,
    error_for_job,
    process_dispatched_access_job,
    reap_stuck_access_jobs,
    run_access_job,
    run_access_job_worker,
    serialize_access_job,
    serialize_access_job_runtime,
    shutdown_access_jobs,
    start_access_job,
    wait_for_mfa_session,
)
from src.core.mfa_manager import MFARejectedError, MFATimeoutError, get_mfa_manager
from src.database import AccessJob, AuditLog, User, engine, is_encrypted_json
from src.exceptions import (
    AccessJobCancelledError,
    AuthenticationError,
    ConcurrentAccessError,
    MFARequiredError,
    PlaidifyError,
    ReadOnlyPolicyViolationError,
)
from tests.conftest import TestSessionLocal
from tests.jobs_support import make_user, redis_mode_fixture, requires_redis  # noqa: F401

CREDS = {"site": "internal_bank", "username": "test_user", "password": "secret123"}


def _link_and_token(client, headers, username="test_user"):
    link_token = client.post("/create_link", params={"site": "internal_bank"}, headers=headers).json()["link_token"]
    body = {"link_token": link_token, "username": username, "password": "secret123"}
    response = client.post("/submit_credentials", json=body, headers=headers)
    if response.status_code == 422:  # servers that still take the query-string form
        response = client.post("/submit_credentials", params=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _fetch(client, headers, access_token):
    response = client.post("/fetch_data", json={"access_token": access_token}, headers=headers)
    if response.status_code == 405:  # servers that still take GET /fetch_data
        response = client.get("/fetch_data", params={"access_token": access_token}, headers=headers)
    return response


def _job(job_id):
    with TestSessionLocal() as db:
        job = db.get(AccessJob, job_id)
        if job is not None:
            db.expunge(job)
        return job


def _only_job():
    with TestSessionLocal() as db:
        job = db.query(AccessJob).one()
        db.expunge(job)
        return job


async def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _no_leaked_jobs():
    yield
    access_jobs_module._LOCAL_LOCK_OWNERS.clear()


class TestAccessJobTracking:
    def test_connect_creates_completed_access_job(self, client, auth_headers):
        response = client.post("/connect", json=CREDS, headers=auth_headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "connected"
        assert payload["job_id"]

        with TestSessionLocal() as db:
            jobs = db.query(AccessJob).all()
            assert len(jobs) == 1

            job = jobs[0]
            assert payload["job_id"] == job.id
            assert job.site == "internal_bank"
            assert job.job_type == "connect"
            assert job.status == "completed"
            # One lock per site login, keyed so the login never shows.
            assert job.lock_scope.startswith("cred:")
            assert "test_user" not in job.lock_scope
            assert job.session_id.startswith("access-")
            assert job.started_at is not None
            assert job.completed_at is not None
            # Stored encrypted for an owner; without one only a summary is kept.
            if job.user_id is None:
                assert job.result_json is None
            else:
                assert is_encrypted_json(job.result_json)
            assert json.loads(job.metadata_json)["result_fields"]

    def test_fetch_data_creates_user_scoped_access_job(self, client, auth_headers):
        access_token = _link_and_token(client, auth_headers)
        fetch_response = _fetch(client, auth_headers, access_token)
        assert fetch_response.status_code == 200
        payload = fetch_response.json()
        assert payload["status"] == "connected"
        assert payload["job_id"]

        with TestSessionLocal() as db:
            user = db.query(User).filter_by(username="testuser").first()
            jobs = db.query(AccessJob).all()
            assert len(jobs) == 1

            job = jobs[0]
            assert payload["job_id"] == job.id
            assert job.job_type == "fetch_data"
            assert job.status == "completed"
            assert job.user_id == user.id
            assert job.lock_scope == build_lock_scope(site="internal_bank", credential="login:test_user")
            # Stored encrypted under the owner's key.
            assert is_encrypted_json(job.result_json)
            assert "profile_status" not in job.result_json

    def test_get_access_job_for_authenticated_user(self, client, auth_headers):
        access_token = _link_and_token(client, auth_headers)
        job_id = _fetch(client, auth_headers, access_token).json()["job_id"]

        status_response = client.get(f"/access_jobs/{job_id}", headers=auth_headers)
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["job_id"] == job_id
        assert payload["job_type"] == "fetch_data"
        assert payload["status"] == "completed"
        assert payload["error_code"] is None
        assert payload["metadata"]["result_status"] == "connected"
        assert payload["result"]["status"] == "connected"
        assert payload["result"]["data"]

    def test_list_access_jobs_for_authenticated_user(self, client, auth_headers):
        access_token = _link_and_token(client, auth_headers)
        _fetch(client, auth_headers, access_token)

        list_response = client.get("/access_jobs", headers=auth_headers)
        assert list_response.status_code == 200
        payload = list_response.json()
        assert payload["count"] == 1
        assert payload["jobs"][0]["job_type"] == "fetch_data"
        assert payload["jobs"][0]["result"]["data"]

    def test_access_job_requires_matching_user(self, client, auth_headers, second_user_headers):
        access_token = _link_and_token(client, auth_headers)
        job_id = _fetch(client, auth_headers, access_token).json()["job_id"]

        assert client.get(f"/access_jobs/{job_id}", headers=second_user_headers).status_code == 404
        assert client.get(f"/access_jobs/{job_id}").status_code == 401

    def test_anonymous_access_job_is_retrievable_by_job_id(self, client):
        with TestSessionLocal() as db:
            db.add(
                AccessJob(
                    id="ajob-anonymous-test",
                    user_id=None,
                    site="internal_bank",
                    job_type="connect",
                    status="failed",
                    lock_scope="principal:test:site:internal_bank",
                    session_id="mfa-session-123",
                    result_json=json.dumps({"data": {"secret": "never shown"}}),
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

        found = client.get("/access_jobs/ajob-anonymous-test")
        assert found.status_code == 200
        assert found.json()["job_id"] == "ajob-anonymous-test"
        # A job without an owner never returns a stored result.
        assert found.json()["result"] is None

    def test_executor_mfa_required_is_a_terminal_failure_not_a_prompt(self, client, auth_headers):
        """MFARequiredError only comes from unattended runs: the job fails with error_code mfa_required."""
        with patch(
            "src.routers.connection.connect_to_site",
            AsyncMock(
                side_effect=MFARequiredError(site="internal_bank", mfa_type="totp", session_id="mfa-session-xyz")
            ),
        ):
            response = client.post("/connect", json=CREDS, headers=auth_headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "mfa_required"
        assert payload["job_id"]

        job_payload = client.get(f"/access_jobs/{payload['job_id']}", headers=auth_headers).json()
        assert job_payload["status"] == "failed"
        assert job_payload["error_code"] == "mfa_required"

    def test_connect_returns_pending_for_background_execution(self, client, auth_headers):
        async def slow_connect(**kwargs):
            await asyncio.sleep(0.15)
            return {"status": "connected", "data": {"profile_status": "ready"}}

        with (
            patch("src.routers.connection.connect_to_site", slow_connect),
            patch("src.routers.connection._CONNECT_COMPLETION_WAIT_SECONDS", 0.01),
            patch("src.routers.connection._CONNECT_MFA_DISCOVERY_WAIT_SECONDS", 0.01),
        ):
            response = client.post("/connect", json=CREDS, headers=auth_headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "pending"
        assert payload["job_id"]

    def test_connect_background_mfa_reports_the_prompt_while_it_waits(self, client, auth_headers):
        async def waiting_mfa_connect(**kwargs):
            session = await get_mfa_manager().create_session(
                session_id=kwargs["session_id"],
                site=kwargs["site"],
                mfa_type="totp",
                metadata={"prompt": "Enter the one-time code"},
            )
            try:
                code = await session.wait_for_code(timeout=5)
                return {"status": "connected", "data": {"verification": code}}
            finally:
                await get_mfa_manager().remove_session(kwargs["session_id"])

        with (
            patch("src.routers.connection.connect_to_site", waiting_mfa_connect),
            patch("src.routers.connection._CONNECT_COMPLETION_WAIT_SECONDS", 0.01),
            patch("src.routers.connection._CONNECT_MFA_DISCOVERY_WAIT_SECONDS", 0.5),
        ):
            response = client.post("/connect", json=CREDS, headers=auth_headers)
            payload = response.json()
            assert response.status_code == 200
            assert payload["status"] == "mfa_required"
            assert payload["job_id"]
            assert payload["session_id"]


class TestCredentialLocks:
    @pytest.mark.asyncio
    async def test_two_end_users_of_one_developer_do_not_block_each_other(self):
        release = asyncio.Event()
        started = []

        async def slow_executor(**kwargs):
            started.append(kwargs["username"])
            await release.wait()
            return {"status": "connected", "data": {}}

        first = asyncio.create_task(
            run_access_job(
                None,
                site="internal_bank",
                job_type="fetch_data",
                executor=slow_executor,
                executor_kwargs={**CREDS, "username": "alice"},
                user_id=None,
            )
        )
        second = asyncio.create_task(
            run_access_job(
                None,
                site="internal_bank",
                job_type="fetch_data",
                executor=slow_executor,
                executor_kwargs={**CREDS, "username": "bob"},
                user_id=None,
            )
        )
        try:
            assert await _wait_until(lambda: len(started) == 2)
        finally:
            release.set()
        (job_a, _), (job_b, _) = await asyncio.gather(first, second)
        assert {job_a.status, job_b.status} == {"completed"}
        assert job_a.lock_scope != job_b.lock_scope

    @pytest.mark.asyncio
    async def test_the_same_login_is_blocked_while_its_job_runs(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_executor(**kwargs):
            started.set()
            await release.wait()
            return {"status": "connected", "data": {"balance": "$10.00"}}

        task = asyncio.create_task(
            run_access_job(
                TestSessionLocal(),
                site="internal_bank",
                job_type="connect",
                executor=slow_executor,
                executor_kwargs=dict(CREDS),
                principal_hint="test_user",
            )
        )
        try:
            await started.wait()
            with pytest.raises(ConcurrentAccessError):
                await run_access_job(
                    TestSessionLocal(),
                    site="internal_bank",
                    job_type="connect",
                    executor=slow_executor,
                    executor_kwargs={**CREDS, "username": "  TEST_user "},
                )
        finally:
            release.set()
        job, result = await task
        assert job.status == "completed"
        assert result["status"] == "connected"

        with TestSessionLocal() as verifier:
            statuses = sorted(row.status for row in verifier.query(AccessJob).all())
            blocked = verifier.query(AccessJob).filter_by(status="blocked").one()
        assert statuses == ["blocked", "completed"]
        assert "already in progress" in blocked.error_message
        assert blocked.error_code == "rate_limited"
        assert blocked.error_status == 409

    def test_scope_is_a_keyed_hash(self):
        scope = build_lock_scope(site="internal_bank", credential=credential_identity({"username": "alice"}))
        plain = hashlib.sha256(b"internal_bank:alice").hexdigest()[:16]
        assert scope.startswith("cred:") and scope.endswith(":site:internal_bank")
        assert "alice" not in scope and plain not in scope
        # The token of a stored credential identifies it when there is no login.
        assert credential_identity({}, {"link_token": "lt-1"}) == "link_token:lt-1"
        assert credential_identity({"access_token": "at-1"}) == "access_token:at-1"

    @pytest.mark.asyncio
    async def test_the_lock_belongs_to_the_job(self):
        seen = {}

        async def executor(**kwargs):
            seen["owners"] = dict(access_jobs_module._LOCAL_LOCK_OWNERS)
            return {"status": "connected", "data": {}}

        job, _ = await run_access_job(
            None, site="internal_bank", job_type="connect", executor=executor, executor_kwargs=dict(CREDS)
        )
        assert seen["owners"] == {job.lock_scope: job.id}
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {}


class TestJobStorage:
    @pytest.mark.asyncio
    async def test_owned_results_are_encrypted_without_download_contents(self):
        with TestSessionLocal() as db:
            owner = make_user(db)
            owner_id = owner.id

        result = {
            "status": "connected",
            "data": {"account_number": "0012-3456-789", "balance": "$1.00"},
            "metadata": {
                "sensitive_fields": ["account_number"],
                "downloads": [{"filename": "statement.pdf", "content_base64": "JVBERi0xLjQK"}],
            },
        }
        job, returned = await run_access_job(
            None,
            site="internal_bank",
            job_type="fetch_data",
            executor=AsyncMock(return_value=result),
            executor_kwargs=dict(CREDS),
            user_id=owner_id,
        )
        assert returned["metadata"]["downloads"][0]["content_base64"] == "JVBERi0xLjQK"  # the caller gets it all

        stored = _job(job.id)
        assert is_encrypted_json(stored.result_json)
        for secret in ("0012-3456-789", "JVBERi0xLjQK", "account_number"):
            assert secret not in (stored.result_json or "")

        payload = await serialize_access_job_runtime(stored)
        assert payload["result"]["data"]["account_number"] == "0012-3456-789"
        download = payload["result"]["metadata"]["downloads"][0]
        assert "content_base64" not in download and download["content_omitted"] is True

    @pytest.mark.asyncio
    async def test_a_job_without_an_owner_keeps_only_a_summary(self):
        job, _ = await run_access_job(
            None,
            site="internal_bank",
            job_type="connect",
            executor=AsyncMock(return_value={"status": "connected", "data": {"ssn": "123-45-6789"}}),
            executor_kwargs=dict(CREDS),
        )
        stored = _job(job.id)
        assert stored.result_json is None
        assert json.loads(stored.metadata_json)["result_fields"] == ["ssn"]
        assert serialize_access_job(stored)["result"] is None
        _job_row, outcome = access_jobs_module._load_job_outcome(job.id)
        assert outcome == {
            "status": "connected",
            "metadata": {"result_field_count": 1, "result_fields": ["ssn"], "result_stored": False},
        }


class TestNoTransactionDuringTheRun:
    @pytest.mark.asyncio
    async def test_no_connection_is_checked_out_while_the_browser_runs(self):
        caller_db = TestSessionLocal()
        caller_db.query(AccessJob).count()  # the caller's own reads open a transaction
        checked_out = {}

        async def executor(**kwargs):
            await asyncio.sleep(0.05)
            checked_out["during_run"] = engine.pool.checkedout()
            return {"status": "connected", "data": {}}

        try:
            await run_access_job(
                caller_db,
                site="internal_bank",
                job_type="fetch_data",
                executor=executor,
                executor_kwargs=dict(CREDS),
            )
        finally:
            caller_db.close()
        assert checked_out["during_run"] == 0


class TestCancelAndReap:
    @pytest.mark.asyncio
    async def test_cancel_stops_a_running_job_and_frees_its_lock(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def endless(**kwargs):
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                stopped.set()

        job, task = await start_access_job(
            None, site="internal_bank", job_type="connect", executor=endless, executor_kwargs=dict(CREDS)
        )
        await started.wait()
        assert await cancel_access_job(job.id) is True
        assert await asyncio.wait_for(stopped.wait(), timeout=2)
        with pytest.raises(AccessJobCancelledError):
            await task

        assert _job(job.id).status == "cancelled"
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {}
        # The user can retry at once.
        retry, _ = await run_access_job(
            None,
            site="internal_bank",
            job_type="connect",
            executor=AsyncMock(return_value={"status": "connected", "data": {}}),
            executor_kwargs=dict(CREDS),
        )
        assert retry.status == "completed"
        assert await cancel_access_job(job.id) is False

    @pytest.mark.asyncio
    async def test_a_runner_stops_when_its_job_is_ended_elsewhere(self, monkeypatch):
        monkeypatch.setattr(access_jobs_module.settings, "access_job_heartbeat_seconds", 0.1)
        started = asyncio.Event()

        async def endless(**kwargs):
            started.set()
            await asyncio.sleep(3600)

        job, task = await start_access_job(
            None, site="internal_bank", job_type="connect", executor=endless, executor_kwargs=dict(CREDS)
        )
        await started.wait()
        # Another process (the reaper, a cancel) ends the job; this one only sees the row.
        with TestSessionLocal() as db:
            db.query(AccessJob).filter_by(id=job.id).update({"status": "failed", "error_message": "reaped"})
            db.commit()
        with pytest.raises(PlaidifyError, match="reaped"):
            await asyncio.wait_for(task, timeout=3)

    @pytest.mark.asyncio
    async def test_a_job_past_its_deadline_is_stopped(self, monkeypatch):
        monkeypatch.setattr(access_jobs_module, "_run_budget_seconds", lambda: 0.2)
        stopped = asyncio.Event()

        async def endless(**kwargs):
            try:
                await asyncio.sleep(3600)
            finally:
                stopped.set()

        with pytest.raises(PlaidifyError) as caught:
            await run_access_job(
                None, site="internal_bank", job_type="connect", executor=endless, executor_kwargs=dict(CREDS)
            )
        assert caught.value.status_code == 504
        assert stopped.is_set()
        job = _only_job()
        assert job.status == "failed" and "too long" in job.error_message

    @pytest.mark.asyncio
    async def test_a_timeout_inside_the_executor_is_an_ordinary_failure(self):
        with pytest.raises(TimeoutError):
            await run_access_job(
                None,
                site="internal_bank",
                job_type="connect",
                executor=AsyncMock(side_effect=TimeoutError("site did not answer")),
                executor_kwargs=dict(CREDS),
            )
        job = _only_job()
        assert job.status == "failed" and job.error_code == "network_error"

    @pytest.mark.asyncio
    async def test_reaper_fails_orphaned_overdue_and_unclaimed_jobs_only(self):
        now = datetime.now(timezone.utc)
        rows = {
            "ajob-orphan": {
                "status": "running",
                "heartbeat_at": now - timedelta(minutes=10),
                "started_at": now - timedelta(minutes=11),
            },
            "ajob-alive": {
                "status": "running",
                "heartbeat_at": now - timedelta(seconds=5),
                "started_at": now - timedelta(minutes=5),
            },
            "ajob-overdue": {"status": "running", "heartbeat_at": now, "deadline_at": now - timedelta(seconds=1)},
            "ajob-unclaimed": {"status": "pending", "created_at": now - timedelta(hours=1)},
            "ajob-fresh": {"status": "pending"},
        }
        with TestSessionLocal() as db:
            for job_id, fields in rows.items():
                db.add(
                    AccessJob(
                        id=job_id,
                        site="internal_bank",
                        job_type="connect",
                        lock_scope=f"cred:{job_id}:site:internal_bank",
                        session_id=f"sess-{job_id}",
                        **{"created_at": now, **fields},
                    )
                )
            db.commit()
        access_jobs_module._LOCAL_LOCK_OWNERS["cred:ajob-orphan:site:internal_bank"] = "ajob-orphan"

        reaped = await reap_stuck_access_jobs()

        assert sorted(job.id for job in reaped) == ["ajob-orphan", "ajob-overdue", "ajob-unclaimed"]
        assert _job("ajob-alive").status == "running"
        assert _job("ajob-fresh").status == "pending"
        orphan = _job("ajob-orphan")
        assert orphan.status == "failed" and "stopped" in orphan.error_message
        assert "too long" in _job("ajob-overdue").error_message
        assert "picked up" in _job("ajob-unclaimed").error_message
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {}

    def test_cancel_endpoint_is_owner_only(self, client, auth_headers, second_user_headers):
        with TestSessionLocal() as db:
            owner = db.query(User).filter_by(username="testuser").one()
            db.add(
                AccessJob(
                    id="ajob-to-cancel",
                    user_id=owner.id,
                    site="internal_bank",
                    job_type="connect",
                    status="pending",
                    lock_scope="cred:x:site:internal_bank",
                    session_id="sess-cancel",
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

        assert client.post("/access_jobs/ajob-to-cancel/cancel", headers=second_user_headers).status_code == 404
        response = client.post("/access_jobs/ajob-to-cancel/cancel", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"job_id": "ajob-to-cancel", "cancelled": True, "status": "cancelled"}


class TestMfaStatus:
    @pytest.mark.asyncio
    async def test_prompt_only_while_awaiting_a_code_and_again_after_a_rejected_one(self):
        steps = {"asked": asyncio.Event(), "reopened": asyncio.Event(), "done": asyncio.Event()}

        async def mfa_executor(**kwargs):
            manager = get_mfa_manager()
            session = await manager.create_session(kwargs["session_id"], kwargs["site"], "otp", {"message": "Code?"})
            steps["asked"].set()
            await session.wait_for_code(timeout=5)
            await steps["done"].wait()  # the site is "checking" the first code
            await manager.reopen_session(kwargs["session_id"], {"mfa_error": "invalid_code", "attempts_remaining": 2})
            steps["reopened"].set()
            await session.wait_for_code(timeout=5)
            await manager.remove_session(kwargs["session_id"])
            return {"status": "connected", "data": {}}

        job, task = await start_access_job(
            None, site="internal_bank", job_type="connect", executor=mfa_executor, executor_kwargs=dict(CREDS)
        )
        await steps["asked"].wait()
        running = _job(job.id)
        first = await serialize_access_job_runtime(running)
        assert first["status"] == "mfa_required" and first["mfa_attempts"] == 0

        await get_mfa_manager().submit_code(job.session_id, "000000")
        assert await _wait_until(lambda: get_mfa_manager()._sessions[job.session_id].consumed)
        verifying = await serialize_access_job_runtime(running)
        assert verifying["status"] == "running" and verifying["mfa_state"] == "verifying"

        steps["done"].set()
        await steps["reopened"].wait()
        again = await serialize_access_job_runtime(running)
        assert again["status"] == "mfa_required"
        assert again["metadata"]["mfa_error"] == "invalid_code"
        assert again["metadata"]["attempts_remaining"] == 2
        assert again["mfa_attempts"] == 1

        await get_mfa_manager().submit_code(job.session_id, "123456")
        finished, _ = await task
        assert finished.status == "completed"

    @pytest.mark.asyncio
    async def test_wait_for_mfa_session_ignores_an_answered_challenge(self):
        manager = get_mfa_manager()
        await manager.create_session("sess-answered", "internal_bank", "otp")
        await manager.submit_code("sess-answered", "111111")
        assert await wait_for_mfa_session("sess-answered", timeout=0.1) is None
        await manager.remove_session("sess-answered")

    @pytest.mark.asyncio
    async def test_an_unanswered_challenge_ends_as_mfa_timeout(self):
        async def executor(**kwargs):
            raise MFATimeoutError(site="internal_bank", mfa_type="otp", session_id=kwargs["session_id"])

        with pytest.raises(MFATimeoutError):
            await run_access_job(
                None, site="internal_bank", job_type="connect", executor=executor, executor_kwargs=dict(CREDS)
            )
        job = _only_job()
        assert job.status == "mfa_timeout"
        assert job.error_code == "mfa_timeout"
        error = error_for_job(job)
        assert isinstance(error, MFATimeoutError)
        assert error.status_code == 408 and error.error_code.value == "mfa_timeout"


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_rejected_credentials_keep_their_type_and_status(self):
        rejected = MFARejectedError(site="internal_bank", mfa_type="otp")
        rejected.message = "The site rejected the verification code."  # ASCII: SQL_ASCII test databases
        with pytest.raises(AuthenticationError):
            await run_access_job(
                None,
                site="internal_bank",
                job_type="connect",
                executor=AsyncMock(side_effect=rejected),
                executor_kwargs=dict(CREDS),
            )
        job = _only_job()
        assert (job.status, job.error_code, job.error_type, job.error_status) == (
            "failed",
            "invalid_credentials",
            "MFARejectedError",
            401,
        )
        error = error_for_job(job)
        assert isinstance(error, MFARejectedError) and isinstance(error, AuthenticationError)
        assert error.status_code == 401 and error.error_code.value == "invalid_credentials"

    @pytest.mark.asyncio
    async def test_unexpected_errors_are_not_echoed(self):
        with pytest.raises(RuntimeError):
            await run_access_job(
                None,
                site="internal_bank",
                job_type="connect",
                executor=AsyncMock(side_effect=RuntimeError("psycopg2 at 10.0.0.5:5432 refused")),
                executor_kwargs=dict(CREDS),
            )
        job = _only_job()
        assert job.status == "failed" and job.error_code == "internal_error" and job.error_status == 500
        assert "10.0.0.5" not in job.error_message

    def test_legacy_mfa_required_rows_read_as_a_timeout(self):
        job = AccessJob(
            id="ajob-legacy", site="s", job_type="connect", status="mfa_required", lock_scope="x", session_id="s1"
        )
        assert isinstance(error_for_job(job), MFATimeoutError)

    @pytest.mark.asyncio
    async def test_policy_failures_keep_metadata_and_audit(self):
        async def blocked_executor(**kwargs):
            raise ReadOnlyPolicyViolationError(
                "blocked risky click",
                metadata={
                    "read_only_policy": {
                        "enabled": True,
                        "final_phase": "read",
                        "blocked_action_count": 1,
                        "blocked_actions": [
                            {"phase": "read", "action": "click", "reason": "risky", "target": "#pay-now"}
                        ],
                    }
                },
            )

        with pytest.raises(ReadOnlyPolicyViolationError):
            await run_access_job(
                None,
                site="internal_bank",
                job_type="fetch_data",
                executor=blocked_executor,
                executor_kwargs={"site": "internal_bank"},
                metadata={"agent_id": "agent-test"},
            )

        with TestSessionLocal() as db:
            job = db.query(AccessJob).one()
            assert job.status == "failed"
            assert "read_only_policy" in job.metadata_json
            audit_entry = (
                db.query(AuditLog)
                .filter_by(event_type="access_job", action="read_only_policy_blocked", resource=job.id)
                .first()
            )
            assert audit_entry is not None and audit_entry.agent_id == "agent-test"

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
                        {"phase": "read", "action": "request", "reason": "POST blocked", "target": "https://x.test/pay"}
                    ],
                }
            },
        }
        with patch("src.routers.links.connect_to_site", AsyncMock(return_value=policy_result)):
            access_token = _link_and_token(client, auth_headers)
            job_id = _fetch(client, auth_headers, access_token).json()["job_id"]

        with TestSessionLocal() as db:
            job = db.get(AccessJob, job_id)
            assert "read_only_policy_blocked_count" in job.metadata_json
            assert (
                db.query(AuditLog)
                .filter_by(event_type="access_job", action="read_only_policy_blocked", resource=job_id)
                .first()
                is not None
            )


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_access_jobs_marks_running_job_cancelled(self):
        started = asyncio.Event()

        async def long_running_connect(**kwargs):
            started.set()
            await asyncio.sleep(3600)

        job, _task = await start_access_job(
            None,
            site="internal_bank",
            job_type="connect",
            executor=long_running_connect,
            executor_kwargs=dict(CREDS),
        )
        await started.wait()
        await shutdown_access_jobs(timeout=0.5)

        stored_job = _job(job.id)
        assert stored_job.status == "cancelled"
        assert stored_job.error_message == "Access job cancelled before completion."
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {}

    @pytest.mark.asyncio
    async def test_shutdown_before_the_job_started_cancels_it(self):
        job, _task = await start_access_job(
            None,
            site="internal_bank",
            job_type="connect",
            executor=AsyncMock(return_value={"status": "connected"}),
            executor_kwargs=dict(CREDS),
        )
        await shutdown_access_jobs(timeout=0.5)
        assert await _wait_until(lambda: _job(job.id).status == "cancelled")


# ── Redis-worker mode (real Redis) ────────────────────────────────────────────


async def _dispatch(executor_kwargs=None, *, user_id=None, metadata=None):
    return await start_access_job(
        None,
        site="internal_bank",
        job_type="connect",
        executor=AsyncMock(),
        executor_name="connect_to_site",
        executor_kwargs=dict(executor_kwargs or CREDS),
        user_id=user_id,
        metadata=metadata,
    )


async def _stream_state(settings):
    from src import session_store

    client = session_store.async_redis()
    length = await client.xlen(settings.access_job_stream_key)
    pending = await client.xpending(settings.access_job_stream_key, settings.access_job_consumer_group)
    return length, pending["pending"]


@requires_redis
class TestRedisWorker:
    @pytest.mark.asyncio
    async def test_dispatched_job_runs_on_the_worker_and_is_acked(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        with TestSessionLocal() as db:
            owner_id = make_user(db).id
        job, observer = await _dispatch(user_id=owner_id)
        assert _job(job.id).status == "pending"

        executor = AsyncMock(return_value={"status": "connected", "data": {"profile_status": "ready"}})
        assert await process_dispatched_access_job(
            consumer_name="worker-a", executor_overrides={"connect_to_site": executor}, block_ms=100
        )
        completed_job, result = await asyncio.wait_for(observer, timeout=5)
        assert completed_job.status == "completed"
        assert result["data"]["profile_status"] == "ready"
        # The credentials reached the executor; nothing is left in Redis.
        assert executor.await_args.kwargs["username"] == "test_user"
        assert await _stream_state(redis_mode) == (0, 0)
        from src import session_store

        assert await session_store.async_redis().get(access_jobs_module._dispatch_payload_key(job.id)) is None

    @pytest.mark.asyncio
    async def test_a_long_job_with_two_consumers_is_never_reclaimed(self, redis_mode, monkeypatch):
        """JOB-03: a job running past the reclaim window stays with its worker (heartbeat)."""
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        monkeypatch.setattr(redis_mode, "access_job_reclaim_idle_ms", 1000)
        monkeypatch.setattr(redis_mode, "access_job_worker_concurrency", 2)
        calls = []

        async def long_executor(**kwargs):
            calls.append(kwargs["session_id"])
            await asyncio.sleep(3.5)  # 3.5 reclaim windows
            return {"status": "connected", "data": {}}

        job, observer = await _dispatch()
        statuses = set()

        async def watch():
            while not observer.done():
                statuses.add(_job(job.id).status)
                await asyncio.sleep(0.1)

        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_access_job_worker(
                stop_event=stop, consumer_name="w", executor_overrides={"connect_to_site": long_executor}
            )
        )
        watcher = asyncio.create_task(watch())
        try:
            completed_job, _ = await asyncio.wait_for(observer, timeout=15)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=10)
            await watcher
        assert completed_job.status == "completed"
        assert len(calls) == 1
        assert "blocked" not in statuses and "failed" not in statuses
        assert await _stream_state(redis_mode) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_killed_workers_running_job_is_recovered(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        monkeypatch.setattr(redis_mode, "access_job_reclaim_idle_ms", 200)
        from src import session_store

        client = session_store.async_redis()
        job, observer = await _dispatch()
        # A worker read the message, started the job, took the lock... and died.
        await client.xreadgroup(
            redis_mode.access_job_consumer_group, "dead-worker", {redis_mode.access_job_stream_key: ">"}, count=1
        )
        assert access_jobs_module._claim_job(job.id, "dead-worker")
        await access_jobs_module.acquire_scope_lock(job.lock_scope, owner=job.id)
        with TestSessionLocal() as db:
            db.query(AccessJob).filter_by(id=job.id).update(
                {"heartbeat_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
            )
            db.commit()
        await asyncio.sleep(0.3)

        assert await process_dispatched_access_job(
            consumer_name="worker-b", executor_overrides={"connect_to_site": AsyncMock()}, block_ms=50
        )
        recovered = _job(job.id)
        assert recovered.status == "failed" and "stopped" in recovered.error_message
        assert await client.get(access_jobs_module._lock_key(job.lock_scope)) is None
        assert await _stream_state(redis_mode) == (0, 0)
        with pytest.raises(PlaidifyError):
            await asyncio.wait_for(observer, timeout=5)

    @pytest.mark.asyncio
    async def test_a_job_whose_worker_died_before_starting_it_runs_elsewhere(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        monkeypatch.setattr(redis_mode, "access_job_reclaim_idle_ms", 200)
        from src import session_store

        job, observer = await _dispatch()
        await session_store.async_redis().xreadgroup(
            redis_mode.access_job_consumer_group, "dead-worker", {redis_mode.access_job_stream_key: ">"}, count=1
        )
        await asyncio.sleep(0.3)
        executor = AsyncMock(return_value={"status": "connected", "data": {}})
        assert await process_dispatched_access_job(
            consumer_name="worker-b", executor_overrides={"connect_to_site": executor}, block_ms=50
        )
        completed_job, _ = await asyncio.wait_for(observer, timeout=5)
        assert completed_job.status == "completed"
        assert executor.await_count == 1

    @pytest.mark.asyncio
    async def test_a_redelivered_message_never_reruns_a_finished_job(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        from src import session_store

        job, observer = await _dispatch()
        executor = AsyncMock(return_value={"status": "connected", "data": {}})
        assert await process_dispatched_access_job(
            consumer_name="a", executor_overrides={"connect_to_site": executor}, block_ms=50
        )
        await observer
        await session_store.async_redis().xadd(redis_mode.access_job_stream_key, {"job_id": job.id})
        assert await process_dispatched_access_job(
            consumer_name="b", executor_overrides={"connect_to_site": executor}, block_ms=50
        )
        assert executor.await_count == 1
        assert await _stream_state(redis_mode) == (0, 0)

    @pytest.mark.asyncio
    async def test_worker_failures_keep_their_type_and_status(self, redis_mode, monkeypatch):
        """JOB-18: rejected credentials are a 401 invalid_credentials in redis-worker mode too."""
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        job, observer = await _dispatch()
        executor = AsyncMock(side_effect=AuthenticationError(site="internal_bank"))
        await process_dispatched_access_job(
            consumer_name="a", executor_overrides={"connect_to_site": executor}, block_ms=50
        )
        with pytest.raises(AuthenticationError) as caught:
            await asyncio.wait_for(observer, timeout=5)
        assert caught.value.status_code == 401
        assert caught.value.error_code.value == "invalid_credentials"
        assert caught.value.job_id == job.id

    @pytest.mark.asyncio
    async def test_mfa_through_redis_between_api_and_worker(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")

        async def mfa_executor(**kwargs):
            session = await get_mfa_manager().create_session(kwargs["session_id"], kwargs["site"], "totp")
            code = await session.wait_for_code(timeout=5)
            await get_mfa_manager().remove_session(kwargs["session_id"])
            return {"status": "connected", "data": {"verification": code}}

        job, observer = await _dispatch()
        worker = asyncio.create_task(
            process_dispatched_access_job(
                consumer_name="a", executor_overrides={"connect_to_site": mfa_executor}, block_ms=100
            )
        )
        prompt = await wait_for_mfa_session(job.session_id, timeout=3)
        assert prompt is not None and prompt["mfa_type"] == "totp"
        assert (await serialize_access_job_runtime(_job(job.id)))["status"] == "mfa_required"
        assert await get_mfa_manager().submit_code(job.session_id, "654321")
        completed_job, result = await asyncio.wait_for(observer, timeout=5)
        assert completed_job.status == "completed"
        assert await worker is True

    @pytest.mark.asyncio
    async def test_sigterm_drain_cancels_what_does_not_finish(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        started = asyncio.Event()

        async def endless(**kwargs):
            started.set()
            await asyncio.sleep(3600)

        job, observer = await _dispatch()
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_access_job_worker(
                stop_event=stop, consumer_name="w", executor_overrides={"connect_to_site": endless}, drain_timeout=0.2
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        await asyncio.wait_for(worker, timeout=10)

        cancelled = _job(job.id)
        assert cancelled.status == "cancelled"
        from src import session_store

        assert await session_store.async_redis().get(access_jobs_module._lock_key(job.lock_scope)) is None
        assert await _stream_state(redis_mode) == (0, 0)
        with pytest.raises(AccessJobCancelledError):
            await asyncio.wait_for(observer, timeout=5)

    @pytest.mark.asyncio
    async def test_sigterm_drain_lets_a_short_job_finish(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_execution_mode", "redis-worker")
        started = asyncio.Event()

        async def short(**kwargs):
            started.set()
            await asyncio.sleep(0.5)
            return {"status": "connected", "data": {}}

        job, observer = await _dispatch()
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_access_job_worker(
                stop_event=stop, consumer_name="w", executor_overrides={"connect_to_site": short}, drain_timeout=5
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        await asyncio.wait_for(worker, timeout=10)
        assert _job(job.id).status == "completed"
        await observer

    @pytest.mark.asyncio
    async def test_an_idle_worker_does_not_block_the_event_loop(self, redis_mode, monkeypatch):
        monkeypatch.setattr(redis_mode, "access_job_worker_block_ms", 1500)
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        worker = asyncio.create_task(run_access_job_worker(stop_event=stop, consumer_name="idle"))
        tick_task = asyncio.create_task(ticker())
        await asyncio.sleep(1.0)
        stop.set()
        await asyncio.wait_for(worker, timeout=10)
        await tick_task
        # 10 ms ticks for a second while both consumers sit in a 1.5 s XREADGROUP.
        assert ticks >= 50

    @pytest.mark.asyncio
    async def test_the_redis_lock_holds_the_job_id(self, redis_mode):
        from src import session_store

        seen = {}

        async def executor(**kwargs):
            seen["value"] = await session_store.async_redis().get(access_jobs_module._lock_key(job_scope))
            return {"status": "connected", "data": {}}

        job_scope = build_lock_scope(site="internal_bank", credential="login:test_user")
        job, _ = await run_access_job(
            None, site="internal_bank", job_type="connect", executor=executor, executor_kwargs=dict(CREDS)
        )
        assert seen["value"] == job.id
        assert await session_store.async_redis().get(access_jobs_module._lock_key(job_scope)) is None


class TestAgentRestrictions:
    """SEC-05: an API key or agent sees only its sites' jobs, and only its scopes' fields."""

    def _jobs_for(self, username):
        with TestSessionLocal() as db:
            owner_id = db.query(User).filter_by(username=username).one().id
        result = {
            "status": "connected",
            "data": {"balance": "$1.00", "account_number": "0012-3456", "ssn": "123-45-6789"},
            "metadata": {"sensitive_fields": ["account_number", "ssn"]},
        }

        async def create():
            bank, _ = await run_access_job(
                None,
                site="internal_bank",
                job_type="fetch_data",
                executor=AsyncMock(return_value=result),
                executor_kwargs={**CREDS, "username": "bank-login"},
                user_id=owner_id,
            )
            utility, _ = await run_access_job(
                None,
                site="hydro_one",
                job_type="fetch_data",
                executor=AsyncMock(return_value=result),
                executor_kwargs={**CREDS, "site": "hydro_one", "username": "utility-login"},
                user_id=owner_id,
            )
            return bank.id, utility.id

        return asyncio.run(create())

    def test_agents_see_only_their_sites_and_scopes(self, client, auth_headers):
        agent = client.post(
            "/agents",
            json={"name": "Balance bot", "allowed_sites": ["internal_bank"], "allowed_scopes": ["read:balance"]},
            headers=auth_headers,
        ).json()
        agent_headers = {"X-API-Key": agent["api_key"]}
        bank_id, utility_id = self._jobs_for("testuser")

        listed = client.get("/access_jobs", headers=agent_headers).json()
        assert [job["job_id"] for job in listed["jobs"]] == [bank_id]
        [job] = listed["jobs"]
        assert job["result"]["data"] == {"balance": "$1.00"}
        assert "metadata" not in job["result"]
        assert job["metadata"]["result_fields"] == ["balance"]
        assert "0012-3456" not in json.dumps(listed) and "123-45-6789" not in json.dumps(listed)

        assert client.get(f"/access_jobs/{utility_id}", headers=agent_headers).status_code == 404
        detail = client.get(f"/access_jobs/{bank_id}", headers=agent_headers).json()
        assert detail["result"]["data"] == {"balance": "$1.00"}

        # The owner, signed in, sees everything.
        owner_view = client.get(f"/access_jobs/{bank_id}", headers=auth_headers).json()
        assert owner_view["result"]["data"]["ssn"] == "123-45-6789"
        assert len(client.get("/access_jobs", headers=auth_headers).json()["jobs"]) == 2


class TestLockServiceOutage:
    """R6: a Redis outage fails closed in production, falls back to a local lock elsewhere."""

    class _DownRedis:
        async def eval(self, *args, **kwargs):
            raise ConnectionError("redis is down")

    @pytest.mark.asyncio
    async def test_production_refuses_the_job(self, monkeypatch):
        from src.exceptions import LockServiceUnavailableError

        monkeypatch.setattr(access_jobs_module.session_store, "async_redis", lambda: self._DownRedis())
        monkeypatch.setattr(access_jobs_module.settings, "env", "production")
        with pytest.raises(LockServiceUnavailableError) as caught:
            await access_jobs_module.acquire_scope_lock("cred:x:site:internal_bank", owner="ajob-1")
        assert caught.value.status_code == 503

    @pytest.mark.asyncio
    async def test_development_falls_back_to_a_local_lock_and_releases_it(self, monkeypatch):
        monkeypatch.setattr(access_jobs_module.session_store, "async_redis", lambda: self._DownRedis())
        held = await access_jobs_module.acquire_scope_lock("cred:x:site:internal_bank", owner="ajob-1")
        assert held.backend == "local"
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {"cred:x:site:internal_bank": "ajob-1"}
        await held.release()
        assert access_jobs_module._LOCAL_LOCK_OWNERS == {}
