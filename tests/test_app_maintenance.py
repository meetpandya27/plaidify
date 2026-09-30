"""The app's periodic maintenance, startup checks and import-time wiring.

Covers: one process per maintenance job (lease), which rows the cleanup
removes, the audit/job-result retention, the key-rotation step's error
handling, the init_db startup race, and tracing set up at import time.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

import src.app as appmod
from src.audit import record_audit_event, verify_audit_chain
from src.database import (
    AccessJob,
    AuditLog,
    KeyRotationIncomplete,
    LoginThrottle,
    MaintenanceLease,
    PasswordResetToken,
    RefreshToken,
    User,
    utcnow,
)
from tests.conftest import TestSessionLocal

REPO_ROOT = Path(__file__).resolve().parents[1]


def _user(db, name="maint"):
    user = User(username=name, email=f"{name}@example.com")
    db.add(user)
    db.commit()
    return user


class TestMaintenanceLease:
    def test_only_one_process_holds_a_live_lease(self):
        assert appmod._claim_maintenance_lease("job", 60) is True
        with patch.object(appmod, "_LEASE_HOLDER", "other-host:1:abc"):
            assert appmod._claim_maintenance_lease("job", 60) is False
        # Different jobs have different leases.
        with patch.object(appmod, "_LEASE_HOLDER", "other-host:1:abc"):
            assert appmod._claim_maintenance_lease("another-job", 60) is True

    def test_an_expired_lease_can_be_taken_over(self):
        assert appmod._claim_maintenance_lease("job", 60) is True
        with TestSessionLocal() as db:
            db.get(MaintenanceLease, "job").expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        with patch.object(appmod, "_LEASE_HOLDER", "other-host:1:abc"):
            assert appmod._claim_maintenance_lease("job", 60) is True
        with TestSessionLocal() as db:
            assert db.get(MaintenanceLease, "job").holder == "other-host:1:abc"

    @pytest.mark.asyncio
    async def test_loop_runs_the_job_only_with_the_lease(self):
        import asyncio

        ran = []
        with patch.object(appmod, "_claim_maintenance_lease", side_effect=[False, True, False]):
            task = asyncio.create_task(appmod._maintenance_loop("job", 0.01, lambda: ran.append(1)))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert ran == [1]


class TestAuthCleanup:
    def test_only_expired_refresh_tokens_are_deleted(self):
        now = utcnow()
        with TestSessionLocal() as db:
            user = _user(db)
            db.add_all(
                [
                    RefreshToken(token="expired", user_id=user.id, expires_at=now - timedelta(minutes=1)),
                    RefreshToken(token="rotated", user_id=user.id, expires_at=now + timedelta(days=1), revoked=True),
                    RefreshToken(token="live", user_id=user.id, expires_at=now + timedelta(days=1)),
                    PasswordResetToken(user_id=user.id, token_hash="old", expires_at=now - timedelta(minutes=1)),
                    PasswordResetToken(user_id=user.id, token_hash="new", expires_at=now + timedelta(minutes=30)),
                ]
            )
            db.commit()

        appmod._purge_expired_auth_rows()

        with TestSessionLocal() as db:
            # A rotated token stays until it expires: presenting it again reveals theft.
            assert db.query(RefreshToken).count() == 2
            assert db.query(RefreshToken).filter_by(revoked=True).count() == 1
            assert [r.token_hash for r in db.query(PasswordResetToken)] == ["new"]

    def test_stale_sign_in_throttles_are_deleted_but_live_locks_stay(self):
        now = utcnow()
        old = now - timedelta(days=2)
        with TestSessionLocal() as db:
            db.add_all(
                [
                    LoginThrottle(key="a" * 64, subject="s", failures=1, window_started_at=old, updated_at=old),
                    LoginThrottle(
                        key="b" * 64,
                        subject="s",
                        failures=5,
                        window_started_at=old,
                        updated_at=old,
                        locked_until=now + timedelta(minutes=5),
                    ),
                    LoginThrottle(key="c" * 64, subject="s", failures=1, window_started_at=now, updated_at=now),
                ]
            )
            db.commit()

        appmod._purge_expired_auth_rows()

        with TestSessionLocal() as db:
            assert sorted(row.key[0] for row in db.query(LoginThrottle)) == ["b", "c"]


class TestRetention:
    def test_audit_pruning_keeps_the_chain_verifiable_and_job_results_are_erased(self):
        with TestSessionLocal() as db:
            for n in range(3):
                record_audit_event(db, "auth", f"old-{n}")
            db.query(AuditLog).update({"timestamp": utcnow() - timedelta(days=10_000)})
            db.commit()
            record_audit_event(db, "auth", "recent")
            db.add(
                AccessJob(
                    id="ajob-old",
                    site="s",
                    job_type="connect",
                    status="completed",
                    lock_scope="x",
                    result_json=json.dumps({"data": {"balance": 1}}),
                    created_at=utcnow() - timedelta(days=365),
                    completed_at=utcnow() - timedelta(days=365),
                )
            )
            db.commit()

        appmod._apply_retention()

        with TestSessionLocal() as db:
            actions = [row.action for row in db.query(AuditLog).order_by(AuditLog.id)]
            assert "old-0" not in actions and "recent" in actions
            assert verify_audit_chain(db)["valid"] is True
            assert db.get(AccessJob, "ajob-old").result_json is None


class TestReencryptStep:
    def test_incomplete_rotation_is_logged_not_raised(self, caplog):
        with patch.object(appmod, "re_encrypt_tokens", side_effect=KeyRotationIncomplete(["users:1"], 2)):
            with caplog.at_level("ERROR"):
                appmod._reencrypt_step()
        assert any("keep ENCRYPTION_KEY_PREVIOUS" in r.getMessage() for r in caplog.records)


class TestInitDbRace:
    def test_a_concurrently_created_table_is_tolerated(self):
        import src.database as database

        calls = []

        def racing_create_all(bind):
            calls.append(bind)
            if len(calls) == 1:
                raise OperationalError("CREATE TABLE users", {}, Exception("table users already exists"))

        with patch.object(database.Base.metadata, "create_all", side_effect=racing_create_all):
            database.init_db()
        assert len(calls) == 2

    def test_other_database_errors_still_fail(self):
        import src.database as database

        error = OperationalError("CREATE TABLE users", {}, Exception("disk I/O error"))
        with patch.object(database.Base.metadata, "create_all", side_effect=error):
            with pytest.raises(OperationalError):
                database.init_db()


def test_tracing_is_set_up_when_the_app_is_imported(tmp_path):
    """JOB-17: HTTP spans need the instrumentation in place before the app starts serving."""
    env = {
        **os.environ,
        "OTEL_ENDPOINT": "http://127.0.0.1:4317",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'tracing.db'}",
        "PYTHONPATH": str(REPO_ROOT),
    }
    probe = "import src.app as a; print(getattr(a.app, '_is_instrumented_by_opentelemetry', False))"
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "True"
