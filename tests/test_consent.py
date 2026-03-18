"""
Tests for the Consent Engine feature.
"""

import json
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _create_access_token(client, auth_headers):
    """Create a link + access token for testing consent flows.

    Returns (link_token, access_token).
    """
    # Create a link
    resp = client.post("/create_link?site=test_site", headers=auth_headers)
    assert resp.status_code == 200
    link_token = resp.json()["link_token"]

    # Submit credentials to get an access token (query params, not JSON)
    resp = client.post(
        f"/submit_credentials?link_token={link_token}&username=demo_user&password=demo_pass",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    access_token = resp.json()["access_token"]
    return link_token, access_token


def _request_consent(client, auth_headers, access_token, **overrides):
    """Request consent and return the response JSON."""
    body = {
        "agent_name": overrides.get("agent_name", "TestAgent"),
        "scopes": overrides.get("scopes", ["read:current_bill", "read:usage_kwh"]),
        "access_token": access_token,
        "duration_seconds": overrides.get("duration_seconds", 3600),
    }
    if "agent_description" in overrides:
        body["agent_description"] = overrides["agent_description"]
    resp = client.post("/consent/request", json=body, headers=auth_headers)
    return resp


# ── Consent Request Tests ────────────────────────────────────────────────────


class TestConsentRequest:
    def test_request_consent(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        resp = _request_consent(client, auth_headers, access_token)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["agent_name"] == "TestAgent"
        assert data["scopes"] == ["read:current_bill", "read:usage_kwh"]
        assert data["duration_seconds"] == 3600
        assert "request_id" in data

    def test_request_requires_auth(self, client):
        resp = client.post("/consent/request", json={
            "agent_name": "Agent",
            "scopes": ["read:balance"],
            "access_token": "fake",
        })
        assert resp.status_code == 401

    def test_request_missing_agent_name(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        resp = client.post("/consent/request", json={
            "scopes": ["read:balance"],
            "access_token": access_token,
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_request_missing_scopes(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        resp = client.post("/consent/request", json={
            "agent_name": "Agent",
            "access_token": access_token,
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_request_invalid_access_token(self, client, auth_headers):
        resp = _request_consent(client, auth_headers, "nonexistent_token")
        assert resp.status_code == 401

    def test_request_duration_too_long(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        resp = _request_consent(client, auth_headers, access_token,
                                duration_seconds=31 * 24 * 3600)  # 31 days
        assert resp.status_code == 422

    def test_request_duration_too_short(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        resp = _request_consent(client, auth_headers, access_token,
                                duration_seconds=10)  # < 60s
        assert resp.status_code == 422


# ── Consent Approve / Deny Tests ─────────────────────────────────────────────


class TestConsentApproval:
    def test_approve_consent(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        request_id = req_resp.json()["request_id"]

        resp = client.post(f"/consent/{request_id}/approve", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert "consent_token" in data
        assert "expires_at" in data
        assert data["scopes"] == ["read:current_bill", "read:usage_kwh"]

    def test_deny_consent(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        request_id = req_resp.json()["request_id"]

        resp = client.post(f"/consent/{request_id}/deny", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"

    def test_approve_already_denied(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        request_id = req_resp.json()["request_id"]

        client.post(f"/consent/{request_id}/deny", headers=auth_headers)
        resp = client.post(f"/consent/{request_id}/approve", headers=auth_headers)
        assert resp.status_code == 409

    def test_deny_already_approved(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        request_id = req_resp.json()["request_id"]

        client.post(f"/consent/{request_id}/approve", headers=auth_headers)
        resp = client.post(f"/consent/{request_id}/deny", headers=auth_headers)
        assert resp.status_code == 409

    def test_approve_not_found(self, client, auth_headers):
        resp = client.post("/consent/nonexistent/approve", headers=auth_headers)
        assert resp.status_code == 404

    def test_deny_not_found(self, client, auth_headers):
        resp = client.post("/consent/nonexistent/deny", headers=auth_headers)
        assert resp.status_code == 404

    def test_approve_other_users_request(self, client, auth_headers, second_user_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        request_id = req_resp.json()["request_id"]

        # Second user tries to approve
        resp = client.post(f"/consent/{request_id}/approve", headers=second_user_headers)
        assert resp.status_code == 404

    def test_approve_requires_auth(self, client):
        resp = client.post("/consent/some-request/approve")
        assert resp.status_code == 401


# ── List Consents Tests ──────────────────────────────────────────────────────


class TestListConsents:
    def test_list_empty(self, client, auth_headers):
        resp = client.get("/consent", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_active_grants(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)

        # Create and approve two consents
        r1 = _request_consent(client, auth_headers, access_token,
                              agent_name="Agent1", scopes=["read:balance"])
        client.post(f"/consent/{r1.json()['request_id']}/approve", headers=auth_headers)

        r2 = _request_consent(client, auth_headers, access_token,
                              agent_name="Agent2", scopes=["read:usage"])
        client.post(f"/consent/{r2.json()['request_id']}/approve", headers=auth_headers)

        resp = client.get("/consent", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        agent_names = {g["agent_name"] for g in data["grants"]}
        assert agent_names == {"Agent1", "Agent2"}

    def test_list_excludes_revoked(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)

        r1 = _request_consent(client, auth_headers, access_token)
        approve_resp = client.post(
            f"/consent/{r1.json()['request_id']}/approve", headers=auth_headers)
        consent_token = approve_resp.json()["consent_token"]

        # Revoke it
        client.delete(f"/consent/{consent_token}", headers=auth_headers)

        resp = client.get("/consent", headers=auth_headers)
        assert resp.json()["count"] == 0

    def test_list_requires_auth(self, client):
        resp = client.get("/consent")
        assert resp.status_code == 401


# ── Revoke Consent Tests ─────────────────────────────────────────────────────


class TestRevokeConsent:
    def test_revoke_consent(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        approve_resp = client.post(
            f"/consent/{req_resp.json()['request_id']}/approve", headers=auth_headers)
        consent_token = approve_resp.json()["consent_token"]

        resp = client.delete(f"/consent/{consent_token}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

    def test_revoke_already_revoked(self, client, auth_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        approve_resp = client.post(
            f"/consent/{req_resp.json()['request_id']}/approve", headers=auth_headers)
        consent_token = approve_resp.json()["consent_token"]

        client.delete(f"/consent/{consent_token}", headers=auth_headers)
        resp = client.delete(f"/consent/{consent_token}", headers=auth_headers)
        assert resp.status_code == 409

    def test_revoke_not_found(self, client, auth_headers):
        resp = client.delete("/consent/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    def test_revoke_other_users_grant(self, client, auth_headers, second_user_headers):
        _, access_token = _create_access_token(client, auth_headers)
        req_resp = _request_consent(client, auth_headers, access_token)
        approve_resp = client.post(
            f"/consent/{req_resp.json()['request_id']}/approve", headers=auth_headers)
        consent_token = approve_resp.json()["consent_token"]

        resp = client.delete(f"/consent/{consent_token}", headers=second_user_headers)
        assert resp.status_code == 404

    def test_revoke_requires_auth(self, client):
        resp = client.delete("/consent/some-token")
        assert resp.status_code == 401
