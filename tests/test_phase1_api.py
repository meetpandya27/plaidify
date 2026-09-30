"""
Tests for Phase 1 API endpoints — blueprints, MFA, and enhanced connect.
"""

import os

import pytest

# Ensure test env vars are set before imports
os.environ.setdefault("ENCRYPTION_KEY", "ZY58Cfm5vG7YuExWuJ7uG8eN9_A8v6uLEFncah56324=")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-not-production")
os.environ.setdefault("DATABASE_URL", "sqlite:///test_plaidify.db")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_FORMAT", "text")


# ── Blueprint Endpoint Tests ──────────────────────────────────────────────────


class TestBlueprintEndpoints:
    def test_list_blueprints(self, client):
        """GET /blueprints should return available blueprints."""
        response = client.get("/blueprints")
        assert response.status_code == 200
        data = response.json()
        assert "blueprints" in data
        assert "count" in data
        assert data["count"] >= 0
        # At least internal_bank or internal_bank should exist
        if data["count"] > 0:
            bp = data["blueprints"][0]
            assert "site" in bp
            assert "name" in bp
            assert "domain" in bp

    def test_get_blueprint_info(self, client):
        """GET /blueprints/{site} should return blueprint details."""
        # First check what blueprints are available
        list_resp = client.get("/blueprints")
        blueprints = list_resp.json()["blueprints"]
        if not blueprints:
            pytest.skip("No blueprints available")

        site = blueprints[0]["site"]
        response = client.get(f"/blueprints/{site}")
        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "domain" in data
        assert "extract_fields" in data
        assert isinstance(data["extract_fields"], list)

    def test_get_nonexistent_blueprint(self, client):
        """GET /blueprints/nonexistent should return 404."""
        response = client.get("/blueprints/nonexistent_site_xyz")
        assert response.status_code == 404


# ── MFA Endpoint Tests ───────────────────────────────────────────────────────


class TestMFAEndpoints:
    def test_mfa_status_nonexistent(self, client):
        """GET /mfa/status/{session_id} should return 404 for unknown session."""
        response = client.get("/mfa/status/nonexistent_session")
        assert response.status_code == 404

    def test_mfa_submit_query_form_is_gone(self, client):
        """A code in the query string would land in access logs: only the JSON body is read."""
        response = client.post(
            "/mfa/submit",
            params={"session_id": "nonexistent", "code": "123456"},
        )
        assert response.status_code == 422

    def test_mfa_submit_json_body(self, client):
        """POST /mfa/submit takes the code in a JSON body, out of the URL."""
        response = client.post("/mfa/submit", json={"session_id": "nonexistent", "code": "123456"})
        assert response.status_code == 200
        assert response.json()["status"] == "error"

    def test_mfa_submit_requires_session_and_code(self, client):
        assert client.post("/mfa/submit").status_code == 422
        assert client.post("/mfa/submit", json={"session_id": "nonexistent"}).status_code == 422
        assert client.post("/mfa/submit", params={"session_id": "nonexistent"}).status_code == 422

    def test_mfa_submit_rejects_oversized_code(self, client):
        response = client.post("/mfa/submit", json={"session_id": "nonexistent", "code": "9" * 33})
        assert response.status_code == 422


# ── Connect Endpoint with Enhanced Response ──────────────────────────────────


class TestConnectEndpoint:
    def test_connect_with_extract_fields(self, client, auth_headers):
        """POST /connect should accept extract_fields parameter."""
        response = client.post(
            "/connect",
            json={
                "site": "internal_bank",
                "username": "user",
                "password": "pass",
                "extract_fields": ["profile_status"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
