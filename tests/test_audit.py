"""
Tests for tamper-evident audit logging with hash chains.
"""

import base64
import hashlib
import itertools
import json
import os
import threading
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, update

import src.database as _db_mod
from src.audit import (
    CHECKPOINT_ACTION,
    CHECKPOINT_EVENT,
    LEGACY_KEY_ID,
    AuditChainInvalid,
    _compute_hash,
    _signing_key,
    audit_seal_needed,
    compute_entry_hash,
    prune_audit_logs,
    record_audit_event,
    seal_audit_chain,
    seal_audit_chain_if_needed,
    verify_audit_chain,
)
from src.database import AuditChainHead, AuditLog, User, utcnow

db_settings = _db_mod.settings

_ENTRY = dict(
    event_type="auth",
    user_id=1,
    agent_id=None,
    resource=None,
    action="login",
    metadata_json=None,
    ip_address=None,
    prev_hash=None,
)


def _session():
    from tests.conftest import TestSessionLocal

    return TestSessionLocal()


def _new_master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _entries(db):
    return db.query(AuditLog).order_by(AuditLog.id).all()


# ── Hash chain core ──────────────────────────────────────────────────────────


class TestHashChain:
    def _get_test_db(self):
        return _session()

    def test_record_first_entry(self, client):
        """First audit entry should have prev_hash=None."""
        db = self._get_test_db()
        entry = record_audit_event(db, "auth", "test_action", user_id=1)
        assert entry.prev_hash is None
        assert len(entry.entry_hash) == 64  # SHA-256 hex
        db.close()

    def test_chain_links_correctly(self, client):
        """Second entry should reference first entry's hash."""
        db = self._get_test_db()
        first = record_audit_event(db, "auth", "first_action", user_id=1)
        second = record_audit_event(db, "auth", "second_action", user_id=1)
        assert second.prev_hash == first.entry_hash
        db.close()

    def test_hash_is_deterministic(self, client):
        """Same input should produce same hash."""
        key = b"k" * 32
        ts = utcnow()
        h1 = compute_entry_hash(key, key_id="kid", timestamp=ts, **_ENTRY)
        h2 = compute_entry_hash(key, key_id="kid", timestamp=ts, **_ENTRY)
        assert h1 == h2

    def test_hash_changes_with_different_input(self, client):
        key = b"k" * 32
        ts = utcnow()
        h1 = compute_entry_hash(key, key_id="kid", timestamp=ts, **_ENTRY)
        h2 = compute_entry_hash(key, key_id="kid", timestamp=ts, **{**_ENTRY, "user_id": 2})
        assert h1 != h2

    def test_hash_depends_on_the_key(self, client):
        ts = utcnow()
        h1 = compute_entry_hash(b"a" * 32, key_id="kid", timestamp=ts, **_ENTRY)
        h2 = compute_entry_hash(b"b" * 32, key_id="kid", timestamp=ts, **_ENTRY)
        assert h1 != h2

    def test_field_boundaries_cannot_be_shifted(self, client):
        """The legacy '|'-joined payload let one field's text pose as another's."""
        ts = utcnow()
        a = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "resource": "x|y"})
        b = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "agent_id": "x", "resource": "y"})
        assert a != b

    def test_verify_valid_chain(self, client):
        """Valid chain should pass verification."""
        db = self._get_test_db()
        record_audit_event(db, "auth", "action_a", user_id=1)
        record_audit_event(db, "auth", "action_b", user_id=1)
        record_audit_event(db, "data_access", "action_c", user_id=1)

        result = verify_audit_chain(db)
        assert result["valid"] is True
        assert result["total"] == 3
        assert result["errors"] == []
        db.close()

    def test_verify_detects_tampered_hash(self, client):
        """Tampering with an entry_hash should be detected."""
        db = self._get_test_db()
        record_audit_event(db, "auth", "good_a", user_id=1)
        tampered = record_audit_event(db, "auth", "good_b", user_id=1)
        record_audit_event(db, "auth", "good_c", user_id=1)

        # Tamper with the second entry
        tampered.entry_hash = "0" * 64
        db.commit()

        result = verify_audit_chain(db)
        assert result["valid"] is False
        assert len(result["errors"]) > 0
        db.close()

    def test_metadata_stored_as_json(self, client):
        """Metadata dict should be stored as JSON string."""
        db = self._get_test_db()
        entry = record_audit_event(
            db,
            "auth",
            "login",
            user_id=1,
            metadata={"ip": "127.0.0.1", "browser": "test"},
        )
        parsed = json.loads(entry.metadata_json)
        assert parsed["ip"] == "127.0.0.1"
        assert parsed["browser"] == "test"
        db.close()


# ── Keyed chain (SEC-16) ─────────────────────────────────────────────────────


class TestKeyedChain:
    def test_entries_are_hmac_signed_with_the_configured_key(self, client):
        db = _session()
        try:
            entry = record_audit_event(db, "auth", "login", user_id=7, ip_address="10.0.0.1")
            key_id, key = _signing_key()
            assert entry.key_id == key_id
            assert entry.entry_hash == compute_entry_hash(
                key,
                key_id=key_id,
                event_type="auth",
                user_id=7,
                agent_id=None,
                resource=None,
                action="login",
                metadata_json=None,
                ip_address="10.0.0.1",
                timestamp=entry.timestamp,
                prev_hash=None,
            )
        finally:
            db.close()

    def test_editing_an_entry_and_rehashing_without_the_key_is_detected(self, client):
        """Unkeyed SHA-256 let anyone with write access rewrite the chain; HMAC does not."""
        db = _session()
        try:
            record_audit_event(db, "auth", "login", user_id=1)
            victim = record_audit_event(db, "admin", "promote_user", user_id=1)
            after = record_audit_event(db, "auth", "logout", user_id=1)

            forged = hashlib.sha256(b"forged").hexdigest()
            victim.action = "harmless"
            victim.entry_hash = forged
            after.prev_hash = forged  # keep the links consistent
            db.commit()

            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any(e["id"] == victim.id and e["error"] == "entry_hash mismatch" for e in result["errors"])
        finally:
            db.close()

    def test_forged_unkeyed_entry_after_keyed_entries_is_detected(self, client):
        db = _session()
        try:
            first = record_audit_event(db, "auth", "login", user_id=1)
            ts = utcnow()
            legacy_hash = _compute_hash(
                "auth", 1, None, None, "forged", None, None, ts.replace(tzinfo=None).isoformat(), first.entry_hash
            )
            db.add(
                AuditLog(
                    event_type="auth",
                    user_id=1,
                    action="forged",
                    timestamp=ts,
                    prev_hash=first.entry_hash,
                    entry_hash=legacy_hash,
                    key_id=LEGACY_KEY_ID,
                )
            )
            db.commit()

            errors = [e["error"] for e in verify_audit_chain(db)["errors"]]
            assert "unkeyed entry after the chain became keyed" in errors
        finally:
            db.close()

    def test_explicit_audit_hmac_key_signs_new_entries(self, client):
        db = _session()
        try:
            derived_id = _signing_key()[0]
            record_audit_event(db, "auth", "before", user_id=1)
            with patch.object(db_settings, "audit_hmac_key", "a" * 48):
                explicit_id = _signing_key()[0]
                entry = record_audit_event(db, "auth", "after", user_id=1)
                assert entry.key_id == explicit_id != derived_id
                # Entries under the derived key still verify alongside the explicit one.
                assert verify_audit_chain(db)["valid"] is True
        finally:
            db.close()

    def test_a_legacy_unkeyed_prefix_still_verifies_and_is_continued(self, client):
        """Rows written before this change (plain SHA-256) stay valid; new rows chain onto them."""
        db = _session()
        try:
            prev = None
            for action in ("old_a", "old_b"):
                ts = utcnow()
                digest = _compute_hash(
                    "auth", 1, None, None, action, None, None, ts.replace(tzinfo=None).isoformat(), prev
                )
                db.add(
                    AuditLog(
                        event_type="auth",
                        user_id=1,
                        action=action,
                        timestamp=ts,
                        prev_hash=prev,
                        entry_hash=digest,
                        key_id=LEGACY_KEY_ID,
                    )
                )
                db.commit()
                prev = digest

            new = record_audit_event(db, "auth", "new", user_id=1)
            assert new.prev_hash == prev
            result = verify_audit_chain(db)
            assert result["valid"] is True
            assert result["total"] == 3

            oldest = _entries(db)[0]
            oldest.action = "rewritten"
            db.commit()
            assert verify_audit_chain(db)["valid"] is False
        finally:
            db.close()


# ── Deletion and truncation ──────────────────────────────────────────────────


class TestChainDeletion:
    def _chain(self, db, n=4):
        return [record_audit_event(db, "auth", f"action_{i}", user_id=1) for i in range(n)]

    def test_deleting_the_newest_entry_is_detected(self, client):
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[-1].id))
            db.commit()

            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any("do not match the chain head" in e["error"] for e in result["errors"])
        finally:
            db.close()

    def test_deleting_a_middle_entry_is_detected(self, client):
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[1].id))
            db.commit()

            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any(e["id"] == entries[2].id and e["error"] == "prev_hash mismatch" for e in result["errors"])
        finally:
            db.close()

    def test_deleting_the_oldest_entry_is_detected(self, client):
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[0].id))
            db.commit()
            assert verify_audit_chain(db)["valid"] is False
        finally:
            db.close()

    def test_a_new_append_does_not_hide_a_truncation(self, client):
        """The next entry links to the deleted one (via the head), so the gap stays visible."""
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[-1].id))
            db.commit()
            record_audit_event(db, "auth", "later", user_id=1)

            assert verify_audit_chain(db)["valid"] is False
        finally:
            db.close()

    def test_deleting_newest_entries_and_the_head_leaves_evidence(self, client):
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[-1].id))
            db.execute(delete(AuditChainHead))
            db.commit()

            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any(e["error"] == "chain head is missing" for e in result["errors"])

            # The next append restores the head but records that it had to.
            record_audit_event(db, "auth", "later", user_id=1)
            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any("chain head was deleted" in e["error"] for e in result["errors"])
        finally:
            db.close()

    def test_forging_the_chain_head_is_detected(self, client):
        db = _session()
        try:
            entries = self._chain(db)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[-1].id))
            db.execute(
                update(AuditChainHead).values(last_entry_id=entries[-2].id, last_entry_hash=entries[-2].entry_hash)
            )
            db.commit()

            result = verify_audit_chain(db)
            assert result["valid"] is False
            assert any(e["error"] == "chain head MAC mismatch" for e in result["errors"])
        finally:
            db.close()


# ── Serialized appends ───────────────────────────────────────────────────────


class TestConcurrentAppends:
    def test_concurrent_appends_keep_one_chain(self, client):
        """Appends from many threads (own sessions) never fork the chain."""
        threads_n, per_thread = 8, 12
        barrier = threading.Barrier(threads_n)
        failures: list[BaseException] = []

        def worker(n: int) -> None:
            db = _session()
            try:
                barrier.wait()
                for i in range(per_thread):
                    record_audit_event(db, "auth", f"t{n}-{i}", user_id=n)
            except BaseException as exc:  # surfaced below
                failures.append(exc)
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not failures, failures

        db = _session()
        try:
            entries = _entries(db)
            assert len(entries) == threads_n * per_thread
            prev_hashes = [e.prev_hash for e in entries]
            assert len(set(prev_hashes)) == len(prev_hashes), "two entries share a predecessor: the chain forked"
            for earlier, later in itertools.pairwise(entries):
                assert later.prev_hash == earlier.entry_hash
            result = verify_audit_chain(db)
            assert result["valid"] is True, result["errors"][:3]
            assert result["total"] == threads_n * per_thread
        finally:
            db.close()


# ── Retention ────────────────────────────────────────────────────────────────


class TestRetention:
    def test_pruning_keeps_the_chain_verifiable(self, client):
        db = _session()
        try:
            entries = [record_audit_event(db, "auth", f"a{i}", user_id=1) for i in range(5)]
            last_pruned_hash = entries[1].entry_hash
            deleted = prune_audit_logs(db, cutoff=entries[2].timestamp)
            assert deleted == 2

            checkpoint = db.query(AuditLog).filter_by(event_type=CHECKPOINT_EVENT, action=CHECKPOINT_ACTION).one()
            meta = json.loads(checkpoint.metadata_json)
            assert meta["kind"] == "prune"
            assert meta["first_entry_id"] == entries[2].id
            assert meta["anchor_hash"] == last_pruned_hash
            assert meta["pruned_count"] == 2

            result = verify_audit_chain(db)
            assert result["valid"] is True, result["errors"]
            assert result["start_id"] == entries[2].id
            assert result["checkpoint_id"] == checkpoint.id
            assert result["total"] == 4  # three kept entries + the checkpoint
        finally:
            db.close()

    def test_pruning_everything_keeps_the_chain_verifiable(self, client):
        db = _session()
        try:
            for i in range(3):
                record_audit_event(db, "auth", f"a{i}", user_id=1)
            assert prune_audit_logs(db, retention_days=1, now=utcnow() + timedelta(days=2)) == 3

            result = verify_audit_chain(db)
            assert result["valid"] is True, result["errors"]
            assert result["total"] == 1

            record_audit_event(db, "auth", "after", user_id=1)
            assert verify_audit_chain(db)["valid"] is True
        finally:
            db.close()

    def test_repeated_pruning(self, client):
        db = _session()
        try:
            first = [record_audit_event(db, "auth", f"a{i}", user_id=1) for i in range(3)]
            prune_audit_logs(db, cutoff=first[1].timestamp)
            second = [record_audit_event(db, "auth", f"b{i}", user_id=1) for i in range(3)]
            prune_audit_logs(db, cutoff=second[0].timestamp)

            result = verify_audit_chain(db)
            assert result["valid"] is True, result["errors"]
            assert result["start_id"] == second[0].id
        finally:
            db.close()

    def test_nothing_to_prune_writes_no_checkpoint(self, client):
        db = _session()
        try:
            record_audit_event(db, "auth", "recent", user_id=1)
            assert prune_audit_logs(db, retention_days=30) == 0
            assert db.query(AuditLog).filter_by(action=CHECKPOINT_ACTION).count() == 0
        finally:
            db.close()

    def test_deleting_old_rows_without_a_checkpoint_is_detected(self, client):
        """What the old retention job did: a bare DELETE broke the chain for good."""
        db = _session()
        try:
            entries = [record_audit_event(db, "auth", f"a{i}", user_id=1) for i in range(4)]
            db.execute(delete(AuditLog).where(AuditLog.id < entries[2].id))
            db.commit()
            assert verify_audit_chain(db)["valid"] is False
        finally:
            db.close()

    def test_a_tampered_checkpoint_is_not_trusted(self, client):
        db = _session()
        try:
            entries = [record_audit_event(db, "auth", f"a{i}", user_id=1) for i in range(4)]
            prune_audit_logs(db, cutoff=entries[2].timestamp)
            # Hide one more entry by moving the checkpoint's anchor forward.
            checkpoint = db.query(AuditLog).filter_by(action=CHECKPOINT_ACTION).one()
            meta = json.loads(checkpoint.metadata_json)
            meta.update(first_entry_id=entries[3].id, anchor_hash=entries[2].entry_hash)
            checkpoint.metadata_json = json.dumps(meta, sort_keys=True)
            db.execute(delete(AuditLog).where(AuditLog.id == entries[2].id))
            db.commit()

            assert verify_audit_chain(db)["valid"] is False
        finally:
            db.close()


# ── Streaming verification ───────────────────────────────────────────────────


class TestStreamingVerification:
    def test_small_batches_give_the_same_answer(self, client):
        db = _session()
        try:
            entries = [record_audit_event(db, "auth", f"a{i}", user_id=1) for i in range(7)]
            assert verify_audit_chain(db, batch_size=2) == verify_audit_chain(db, batch_size=1000)

            entries[4].action = "edited"
            db.commit()
            small = verify_audit_chain(db, batch_size=2)
            assert small["valid"] is False
            assert small == verify_audit_chain(db, batch_size=1000)
        finally:
            db.close()

    def test_error_list_is_capped(self, client):
        db = _session()
        try:
            for i in range(6):
                record_audit_event(db, "auth", f"a{i}", user_id=1)
            db.execute(update(AuditLog).values(action="edited"))
            db.commit()

            result = verify_audit_chain(db, max_errors=3)
            assert result["valid"] is False
            assert len(result["errors"]) == 3
            assert result["error_count"] >= 6
        finally:
            db.close()


# ── Sealing on signing-key changes ───────────────────────────────────────────


class TestSealing:
    def _rotate(self, old_key):
        new_key = _new_master_key()
        return (
            patch.object(db_settings, "encryption_key", new_key),
            patch.object(db_settings, "encryption_key_previous", old_key),
            new_key,
        )

    def test_rotating_encryption_key_with_a_seal_keeps_old_entries_verifiable(self, client):
        db = _session()
        try:
            old_key = db_settings.encryption_key
            for i in range(3):
                record_audit_event(db, "auth", f"old{i}", user_id=1)
            key_patch, prev_patch, new_key = self._rotate(old_key)
            with key_patch, prev_patch:
                record_audit_event(db, "auth", "new", user_id=1)
                assert audit_seal_needed(db) is True
                seal = seal_audit_chain_if_needed(db)
                assert seal is not None
                assert audit_seal_needed(db) is False
                assert seal_audit_chain_if_needed(db) is None

            # The previous key is gone; the old entries are attested by the seal.
            with patch.object(db_settings, "encryption_key", new_key):
                result = verify_audit_chain(db)
                assert result["valid"] is True, result["errors"]
                assert result["sealed"] == 3
        finally:
            db.close()

    def test_rotating_audit_hmac_key_through_the_rotation_job(self, client):
        """AUDIT_HMAC_KEY -> AUDIT_HMAC_KEY_PREVIOUS, new key, restart; the hourly job seals."""
        from src.database import re_encrypt_tokens

        old_secret, new_secret = "o" * 40, "n" * 40
        db = _session()
        try:
            with patch.object(db_settings, "audit_hmac_key", old_secret):
                for i in range(3):
                    record_audit_event(db, "auth", f"old{i}", user_id=1)
            with (
                patch.object(db_settings, "audit_hmac_key", new_secret),
                patch.object(db_settings, "audit_hmac_key_previous", old_secret),
            ):
                record_audit_event(db, "auth", "new", user_id=1)
                assert verify_audit_chain(db)["valid"] is True  # both keys configured
                re_encrypt_tokens(db)  # what the background job runs
                assert audit_seal_needed(db) is False
            with patch.object(db_settings, "audit_hmac_key", new_secret):
                result = verify_audit_chain(db)
                assert result["valid"] is True, result["errors"]
                assert result["sealed"] == 3
        finally:
            db.close()

    def test_without_a_seal_old_entries_become_unverifiable(self, client):
        db = _session()
        try:
            record_audit_event(db, "auth", "old", user_id=1)
            with patch.object(db_settings, "encryption_key", _new_master_key()):
                result = verify_audit_chain(db)
                assert result["valid"] is False
                assert result["errors"][0]["error"] == "signed with a key that is not configured"
        finally:
            db.close()

    def test_an_invalid_chain_is_not_sealed(self, client):
        db = _session()
        try:
            record_audit_event(db, "auth", "a", user_id=1)
            tampered = record_audit_event(db, "auth", "b", user_id=1)
            tampered.action = "edited"
            db.commit()
            with pytest.raises(AuditChainInvalid):
                seal_audit_chain(db)
            assert db.query(AuditLog).filter_by(action=CHECKPOINT_ACTION).count() == 0
        finally:
            db.close()


# ── API endpoint integration ─────────────────────────────────────────────────


def _make_admin(username="testuser"):
    db = _session()
    try:
        db.query(User).filter_by(username=username).update({"is_admin": True})
        db.commit()
    finally:
        db.close()


class TestAuditEndpoints:
    def test_audit_logs_requires_auth(self, client):
        """Audit logs should require authentication."""
        resp = client.get("/audit/logs")
        assert resp.status_code == 401

    def test_audit_logs_empty(self, client, auth_headers):
        """Fresh system should return empty audit logs for user."""
        resp = client.get("/audit/logs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data

    def test_audit_verify_requires_auth(self, client):
        resp = client.get("/audit/verify")
        assert resp.status_code == 401

    def test_audit_verify_requires_admin(self, client, auth_headers):
        """Verification reads the whole table: administrators only."""
        resp = client.get("/audit/verify", headers=auth_headers)
        assert resp.status_code == 403

    def test_audit_verify_empty_chain(self, client, auth_headers):
        """A chain with only the registration entry is valid."""
        _make_admin()
        resp = client.get("/audit/verify", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

    def test_audit_verify_reports_tampering(self, client, auth_headers):
        _make_admin()
        db = _session()
        try:
            db.execute(update(AuditLog).values(action="edited"))
            db.commit()
        finally:
            db.close()
        data = client.get("/audit/verify", headers=auth_headers).json()
        assert data["valid"] is False
        assert data["error_count"] >= 1

    def test_audit_logs_filter_by_event_type(self, client, auth_headers):
        """Should filter audit logs by event_type."""
        db = _session()
        record_audit_event(db, "auth", "login", user_id=1)
        record_audit_event(db, "data_access", "fetch", user_id=1)
        db.close()

        resp = client.get("/audit/logs?event_type=auth", headers=auth_headers)
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        for e in entries:
            assert e["event_type"] == "auth"


# ── Event instrumentation ────────────────────────────────────────────────────


class TestAuditInstrumentation:
    def test_register_creates_audit_entry(self, client):
        """User registration should create an audit log entry."""
        resp = client.post(
            "/auth/register",
            json={
                "username": "audit_user",
                "email": "audit@test.com",
                "password": "Secure@pass123",
            },
        )
        assert resp.status_code == 200

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="register").first()
        assert entry is not None
        meta = json.loads(entry.metadata_json)
        assert meta["username"] == "audit_user"
        db.close()

    def test_login_creates_audit_entry(self, client, auth_headers):
        """Successful login should create an audit log entry."""
        resp = client.post(
            "/auth/token",
            data={
                "username": "testuser",
                "password": "Secure@pass123",
            },
        )
        assert resp.status_code == 200

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="login").first()
        assert entry is not None
        db.close()

    def test_failed_login_creates_audit_entry(self, client):
        """Failed login should create an audit log entry."""
        resp = client.post(
            "/auth/token",
            data={
                "username": "testuser",
                "password": "wrongpassword",
            },
        )
        assert resp.status_code == 400

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="login_failed").first()
        assert entry is not None
        db.close()

    def test_failed_login_audit_entry_never_holds_the_username(self, client):
        """The chain can't be edited or erased later, so the typed username stays out of it."""
        for _ in range(2):
            client.post("/auth/token", data={"username": "private-person@example.com", "password": "wrong-pass"})

        db = _session()
        entries = db.query(AuditLog).filter_by(event_type="auth", action="login_failed").all()
        db.close()
        assert len(entries) == 2
        for entry in entries:
            assert "private-person" not in (entry.metadata_json or "")
        # Repeat attempts on one username still correlate.
        subjects = {json.loads(entry.metadata_json)["account"] for entry in entries}
        assert len(subjects) == 1

    def test_token_creation_creates_audit_entry(self, client, auth_headers):
        """Submitting credentials should log token creation."""
        # Create link
        resp = client.post("/create_link?site=test_site", headers=auth_headers)
        link_token = resp.json()["link_token"]

        # Submit credentials
        resp = client.post(
            "/submit_credentials",
            json={"link_token": link_token, "username": "u", "password": "p"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="token", action="create").first()
        assert entry is not None
        db.close()

    def test_token_deletion_creates_audit_entry(self, client, auth_headers):
        """Deleting a token should log revocation."""
        # Create link + token
        resp = client.post("/create_link?site=test_site", headers=auth_headers)
        link_token = resp.json()["link_token"]
        resp = client.post(
            "/submit_credentials",
            json={"link_token": link_token, "username": "u", "password": "p"},
            headers=auth_headers,
        )
        access_token = resp.json()["access_token"]

        # Delete token
        resp = client.delete(f"/tokens/{access_token}", headers=auth_headers)
        assert resp.status_code == 200

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="token", action="revoke").first()
        assert entry is not None
        db.close()


# ── Agent ID and IP Address tracking ─────────────────────────────────────────


class TestAuditAgentAndIP:
    def _get_test_db(self):
        return _session()

    def test_record_with_agent_id(self, client):
        """Audit entry should store agent_id."""
        db = self._get_test_db()
        entry = record_audit_event(
            db,
            "data_access",
            "fetch",
            user_id=1,
            agent_id="agent-abc123",
        )
        assert entry.agent_id == "agent-abc123"
        db.close()

    def test_record_with_ip_address(self, client):
        """Audit entry should store ip_address."""
        db = self._get_test_db()
        entry = record_audit_event(
            db,
            "auth",
            "login",
            user_id=1,
            ip_address="192.168.1.100",
        )
        assert entry.ip_address == "192.168.1.100"
        db.close()

    def test_agent_id_included_in_hash(self, client):
        """Entries with different agent_ids should produce different hashes."""
        ts = utcnow()
        h1 = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "agent_id": "agent-a"})
        h2 = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "agent_id": "agent-b"})
        assert h1 != h2

    def test_ip_address_included_in_hash(self, client):
        """Entries with different IPs should produce different hashes."""
        ts = utcnow()
        h1 = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "ip_address": "10.0.0.1"})
        h2 = compute_entry_hash(b"k" * 32, key_id="kid", timestamp=ts, **{**_ENTRY, "ip_address": "10.0.0.2"})
        assert h1 != h2

    def test_chain_valid_with_agent_and_ip(self, client):
        """Chain with agent_id and ip_address entries should verify correctly."""
        db = self._get_test_db()
        record_audit_event(db, "auth", "login", user_id=1, ip_address="10.0.0.1")
        record_audit_event(
            db,
            "data_access",
            "fetch",
            user_id=1,
            agent_id="agent-x",
            ip_address="10.0.0.2",
        )
        record_audit_event(db, "auth", "logout", user_id=1)

        result = verify_audit_chain(db)
        assert result["valid"] is True
        assert result["total"] == 3
        db.close()

    def test_register_logs_ip_address(self, client):
        """Registration audit entry should include IP address."""
        client.post(
            "/auth/register",
            json={
                "username": "ip_user",
                "email": "ip@test.com",
                "password": "Secure@pass123",
            },
        )

        db = _session()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="register").first()
        assert entry is not None
        # TestClient uses "testclient" as host
        assert entry.ip_address is not None
        db.close()

    def test_audit_logs_endpoint_includes_new_fields(self, client, auth_headers):
        """Audit logs endpoint should return agent_id and ip_address."""
        db = _session()
        record_audit_event(
            db,
            "data_access",
            "fetch",
            user_id=1,
            agent_id="agent-test",
            ip_address="1.2.3.4",
        )
        db.close()

        resp = client.get("/audit/logs", headers=auth_headers)
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        # Find the data_access entry
        access_entries = [e for e in entries if e["event_type"] == "data_access"]
        if access_entries:
            assert "agent_id" in access_entries[0]
            assert "ip_address" in access_entries[0]


def test_non_string_fields_do_not_raise_false_alarms(client):
    """The MAC covers what the columns hand back, so an int resource still verifies."""
    db = _session()
    try:
        record_audit_event(db, "agent", "update", user_id="7", resource=12345, agent_id=99)
        result = verify_audit_chain(db)
        assert result["valid"] is True, result["errors"]
    finally:
        db.close()
