"""
Tests for tamper-evident audit logging with hash chains.
"""

import json

from src.audit import _compute_hash, record_audit_event, verify_audit_chain
from src.database import AuditLog

# ── Hash chain core ──────────────────────────────────────────────────────────


class TestHashChain:
    def _get_test_db(self):
        from tests.conftest import TestSessionLocal

        return TestSessionLocal()

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
        h1 = _compute_hash("auth", 1, None, None, "login", None, None, "2026-01-01T00:00:00", None)
        h2 = _compute_hash("auth", 1, None, None, "login", None, None, "2026-01-01T00:00:00", None)
        assert h1 == h2

    def test_hash_changes_with_different_input(self, client):
        h1 = _compute_hash("auth", 1, None, None, "login", None, None, "2026-01-01T00:00:00", None)
        h2 = _compute_hash("auth", 2, None, None, "login", None, None, "2026-01-01T00:00:00", None)
        assert h1 != h2

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


# ── API endpoint integration ─────────────────────────────────────────────────


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

    def test_audit_verify_empty_chain(self, client, auth_headers):
        """Empty chain should be valid."""
        resp = client.get("/audit/verify", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

    def test_audit_logs_filter_by_event_type(self, client, auth_headers):
        """Should filter audit logs by event_type."""
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
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
        from tests.conftest import TestSessionLocal

        resp = client.post(
            "/auth/register",
            json={
                "username": "audit_user",
                "email": "audit@test.com",
                "password": "Secure@pass123",
            },
        )
        assert resp.status_code == 200

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="register").first()
        assert entry is not None
        meta = json.loads(entry.metadata_json)
        assert meta["username"] == "audit_user"
        db.close()

    def test_login_creates_audit_entry(self, client, auth_headers):
        """Successful login should create an audit log entry."""
        from tests.conftest import TestSessionLocal

        resp = client.post(
            "/auth/token",
            data={
                "username": "testuser",
                "password": "Secure@pass123",
            },
        )
        assert resp.status_code == 200

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="login").first()
        assert entry is not None
        db.close()

    def test_failed_login_creates_audit_entry(self, client):
        """Failed login should create an audit log entry."""
        from tests.conftest import TestSessionLocal

        resp = client.post(
            "/auth/token",
            data={
                "username": "testuser",
                "password": "wrongpassword",
            },
        )
        assert resp.status_code == 400

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="login_failed").first()
        assert entry is not None
        db.close()

    def test_token_creation_creates_audit_entry(self, client, auth_headers):
        """Submitting credentials should log token creation."""
        from tests.conftest import TestSessionLocal

        # Create link
        resp = client.post("/create_link?site=test_site", headers=auth_headers)
        link_token = resp.json()["link_token"]

        # Submit credentials
        resp = client.post(
            f"/submit_credentials?link_token={link_token}&username=u&password=p",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="token", action="create").first()
        assert entry is not None
        db.close()

    def test_token_deletion_creates_audit_entry(self, client, auth_headers):
        """Deleting a token should log revocation."""
        from tests.conftest import TestSessionLocal

        # Create link + token
        resp = client.post("/create_link?site=test_site", headers=auth_headers)
        link_token = resp.json()["link_token"]
        resp = client.post(
            f"/submit_credentials?link_token={link_token}&username=u&password=p",
            headers=auth_headers,
        )
        access_token = resp.json()["access_token"]

        # Delete token
        resp = client.delete(f"/tokens/{access_token}", headers=auth_headers)
        assert resp.status_code == 200

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="token", action="revoke").first()
        assert entry is not None
        db.close()


# ── Agent ID and IP Address tracking ─────────────────────────────────────────


class TestAuditAgentAndIP:
    def _get_test_db(self):
        from tests.conftest import TestSessionLocal

        return TestSessionLocal()

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
        h1 = _compute_hash("auth", 1, "agent-a", None, "login", None, None, "2026-01-01T00:00:00", None)
        h2 = _compute_hash("auth", 1, "agent-b", None, "login", None, None, "2026-01-01T00:00:00", None)
        assert h1 != h2

    def test_ip_address_included_in_hash(self, client):
        """Entries with different IPs should produce different hashes."""
        h1 = _compute_hash("auth", 1, None, None, "login", None, "10.0.0.1", "2026-01-01T00:00:00", None)
        h2 = _compute_hash("auth", 1, None, None, "login", None, "10.0.0.2", "2026-01-01T00:00:00", None)
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
        from tests.conftest import TestSessionLocal

        client.post(
            "/auth/register",
            json={
                "username": "ip_user",
                "email": "ip@test.com",
                "password": "Secure@pass123",
            },
        )

        db = TestSessionLocal()
        entry = db.query(AuditLog).filter_by(event_type="auth", action="register").first()
        assert entry is not None
        # TestClient uses "testclient" as host
        assert entry.ip_address is not None
        db.close()

    def test_audit_logs_endpoint_includes_new_fields(self, client, auth_headers):
        """Audit logs endpoint should return agent_id and ip_address."""
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
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
