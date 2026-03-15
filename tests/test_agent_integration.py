"""
Tests for the agent-integration endpoints:
- Hosted Link sessions (POST /link/sessions, GET /link/sessions/.../status, POST /link/sessions/.../event)
- Hosted Link page (GET /link)
- Webhook system (POST /webhooks/register, POST /webhooks/test)
- SSE event stream (GET /link/events/...)
"""

import json
import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from src.main import app, _link_sessions, _link_session_lock, _webhook_registry


@pytest.fixture(autouse=True)
def clear_link_sessions():
    """Clear in-memory stores between tests."""
    _link_sessions.clear()
    _webhook_registry.clear()
    yield
    _link_sessions.clear()
    _webhook_registry.clear()


# ── Hosted Link Page ──────────────────────────────────────────────────────────


class TestHostedLinkPage:
    def test_link_page_returns_html(self, client):
        resp = client.get("/link?token=some-token")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Plaidify" in resp.text

    def test_link_page_without_token(self, client):
        resp = client.get("/link")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


# ── Link Sessions ─────────────────────────────────────────────────────────────


class TestLinkSessions:
    def test_create_link_session(self, client, auth_headers):
        resp = client.post("/link/sessions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "link_token" in data
        assert "link_url" in data
        assert "public_key" in data
        assert data["expires_in"] > 0

    def test_create_link_session_with_site(self, client, auth_headers):
        resp = client.post("/link/sessions?site=greengrid_energy", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["link_token"]

    def test_create_link_session_requires_auth(self, client):
        resp = client.post("/link/sessions")
        assert resp.status_code in (401, 403)

    def test_get_session_status(self, client, auth_headers):
        # Create session
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        # Check status
        resp = client.get(f"/link/sessions/{token}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "awaiting_institution"
        assert data["link_token"] == token

    def test_get_session_status_not_found(self, client):
        resp = client.get("/link/sessions/nonexistent-token/status")
        assert resp.status_code == 404

    def test_post_session_event(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        # Post event
        resp = client.post(
            f"/link/sessions/{token}/event",
            json={"event": "INSTITUTION_SELECTED", "site": "greengrid_energy"},
        )
        assert resp.status_code == 200

        # Verify status updated
        status_resp = client.get(f"/link/sessions/{token}/status")
        assert status_resp.json()["status"] == "awaiting_credentials"
        assert "INSTITUTION_SELECTED" in status_resp.json()["events"]

    def test_session_event_flow(self, client, auth_headers):
        """Walk through the full event flow."""
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        events = [
            ("INSTITUTION_SELECTED", "awaiting_credentials"),
            ("CREDENTIALS_SUBMITTED", "connecting"),
            ("CONNECTED", "completed"),
        ]
        for event_name, expected_status in events:
            client.post(f"/link/sessions/{token}/event", json={"event": event_name})
            status = client.get(f"/link/sessions/{token}/status").json()
            assert status["status"] == expected_status

    def test_session_mfa_flow(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        client.post(f"/link/sessions/{token}/event", json={"event": "INSTITUTION_SELECTED"})
        client.post(f"/link/sessions/{token}/event", json={"event": "CREDENTIALS_SUBMITTED"})
        client.post(f"/link/sessions/{token}/event", json={"event": "MFA_REQUIRED"})

        status = client.get(f"/link/sessions/{token}/status").json()
        assert status["status"] == "mfa_required"

        client.post(f"/link/sessions/{token}/event", json={"event": "MFA_SUBMITTED"})
        status = client.get(f"/link/sessions/{token}/status").json()
        assert status["status"] == "verifying_mfa"

    def test_expired_session_rejects_events(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        # Manually expire the session
        _link_sessions[token]["created_at"] = 0

        resp = client.post(f"/link/sessions/{token}/event", json={"event": "TEST"})
        assert resp.status_code == 410


# ── Webhooks ──────────────────────────────────────────────────────────────────


class TestWebhooks:
    def test_register_webhook(self, client, auth_headers):
        # Create session first
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        resp = client.post("/webhooks/register", json={
            "link_token": token,
            "url": "http://localhost:9999/webhook",
            "secret": "test-secret-key",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "webhook_id" in data
        assert data["status"] == "registered"

    def test_register_webhook_missing_fields(self, client):
        resp = client.post("/webhooks/register", json={"link_token": "abc"})
        assert resp.status_code == 422

    def test_register_webhook_invalid_url(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        resp = client.post("/webhooks/register", json={
            "link_token": token,
            "url": "http://not-localhost:9999/webhook",
            "secret": "s",
        })
        assert resp.status_code == 422

    def test_register_webhook_session_not_found(self, client):
        resp = client.post("/webhooks/register", json={
            "link_token": "nonexistent",
            "url": "https://example.com/hook",
            "secret": "s",
        })
        assert resp.status_code == 404

    def test_test_webhook_not_found(self, client):
        resp = client.post("/webhooks/test", json={"webhook_id": "nope"})
        assert resp.status_code == 404

    @patch("src.main._deliver_webhook", new_callable=AsyncMock, return_value=True)
    def test_test_webhook_delivers(self, mock_deliver, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        reg_resp = client.post("/webhooks/register", json={
            "link_token": token,
            "url": "http://localhost:9999/hook",
            "secret": "secret",
        })
        webhook_id = reg_resp.json()["webhook_id"]

        resp = client.post("/webhooks/test", json={"webhook_id": webhook_id})
        assert resp.status_code == 200
        assert resp.json()["status"] == "delivered"
        mock_deliver.assert_called_once()


# ── SDK Models ────────────────────────────────────────────────────────────────


class TestSDKModels:
    def test_link_session_model(self):
        import sys
        import os
        sdk_path = os.path.join(os.path.dirname(__file__), "..", "sdk")
        sys.path.insert(0, sdk_path)
        try:
            from plaidify.models import LinkSession
            session = LinkSession(link_token="abc", link_url="/link?token=abc")
            assert session.link_token == "abc"
            assert session.status == "awaiting_institution"
        finally:
            sys.path.pop(0)

    def test_link_event_model(self):
        import sys
        import os
        sdk_path = os.path.join(os.path.dirname(__file__), "..", "sdk")
        sys.path.insert(0, sdk_path)
        try:
            from plaidify.models import LinkEvent
            event = LinkEvent(event="CONNECTED", data={"access_token": "xyz"})
            assert event.event == "CONNECTED"
        finally:
            sys.path.pop(0)

    def test_webhook_registration_model(self):
        import sys
        import os
        sdk_path = os.path.join(os.path.dirname(__file__), "..", "sdk")
        sys.path.insert(0, sdk_path)
        try:
            from plaidify.models import WebhookRegistration
            reg = WebhookRegistration(webhook_id="wh-123")
            assert reg.status == "registered"
        finally:
            sys.path.pop(0)
