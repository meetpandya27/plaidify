"""
Tests for the agent-integration endpoints:
- Hosted Link sessions (POST /link/sessions, GET /link/sessions/.../status, POST /link/sessions/.../event)
- Hosted Link page (GET /link)
- Webhook system (POST /webhooks/register, GET /webhooks, DELETE /webhooks, POST /webhooks/test)
- SSE event stream (GET /link/events/...)
- Public token exchange (POST /exchange/public_token)
- MCP server tools
"""

from unittest.mock import AsyncMock, patch

import pytest

from src import session_store


@pytest.fixture(autouse=True)
def clear_link_sessions():
    """Clear in-memory stores between tests."""
    session_store.clear_all()
    yield
    session_store.clear_all()


# ── Hosted Link Page ──────────────────────────────────────────────────────────


class TestHostedLinkPage:
    def test_link_page_returns_html(self, client):
        resp = client.get("/link?token=some-token")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Plaidify" in resp.text
        assert "/ui/link-page.css" in resp.text
        assert "/ui/link-page.js" in resp.text

    def test_link_page_without_token(self, client):
        resp = client.get("/link")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_link_page_allows_bootstrap_origin_in_frame_ancestors(self, client, auth_headers):
        bootstrap_resp = client.post(
            "/link/bootstrap",
            json={"allowed_origin": "https://app.example.com"},
            headers=auth_headers,
        )
        launch_token = bootstrap_resp.json()["launch_token"]

        exchange_resp = client.post(
            "/link/sessions/bootstrap",
            json={"launch_token": launch_token},
            headers={"Origin": "https://app.example.com"},
        )
        link_token = exchange_resp.json()["link_token"]

        resp = client.get(f"/link?token={link_token}")
        assert resp.status_code == 200
        assert "frame-ancestors 'self' https://app.example.com" in resp.headers["content-security-policy"]
        assert "x-frame-options" not in {k.lower() for k in resp.headers.keys()}

    def test_ui_root_does_not_serve_console(self, client):
        resp = client.get("/ui/")
        assert resp.status_code == 404

    def test_ui_index_is_not_served(self, client):
        resp = client.get("/ui/index.html")
        assert resp.status_code == 404


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
        resp = client.post("/link/sessions?site=hydro_one", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["link_token"]

    def test_create_link_session_requires_auth(self, client):
        resp = client.post("/link/sessions")
        assert resp.status_code in (401, 403)

    def test_create_public_link_session(self, client):
        resp = client.post("/link/sessions/public")
        assert resp.status_code == 200
        data = resp.json()
        assert "link_token" in data
        assert data["expires_in"] > 0

        status_resp = client.get(f"/link/sessions/{data['link_token']}/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "awaiting_institution"

    def test_create_public_link_session_disabled_in_production(self, client):
        with patch("src.routers.link_sessions.settings.env", "production"), patch(
            "src.routers.link_sessions.settings.public_link_sessions_enabled", False
        ):
            resp = client.post("/link/sessions/public")

        assert resp.status_code == 403
        assert "disabled in production" in resp.json()["detail"]

    def test_create_public_link_session_allows_configured_origin(self, client):
        with patch("src.routers.link_sessions.settings.public_link_allowed_origins", "https://app.example.com"):
            resp = client.post(
                "/link/sessions/public",
                headers={"Origin": "https://app.example.com"},
            )

        assert resp.status_code == 200
        assert "link_token" in resp.json()

    def test_create_public_link_session_rejects_unapproved_origin(self, client):
        with patch("src.routers.link_sessions.settings.public_link_allowed_origins", "https://app.example.com"):
            resp = client.post(
                "/link/sessions/public",
                headers={"Origin": "https://evil.example.com"},
            )

        assert resp.status_code == 403
        assert "not allowed" in resp.json()["detail"]

    def test_create_link_bootstrap(self, client, auth_headers):
        resp = client.post(
            "/link/bootstrap",
            json={
                "site": "hydro_one",
                "allowed_origin": "https://app.example.com",
                "scopes": ["read_bill"],
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["launch_token"]
        assert data["expires_in"] > 0
        assert data["site"] == "hydro_one"
        assert data["allowed_origin"] == "https://app.example.com"

    def test_create_link_bootstrap_rejects_non_origin_allowed_origin(self, client, auth_headers):
        resp = client.post(
            "/link/bootstrap",
            json={
                "allowed_origin": "https://app.example.com/callback?next=1",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        assert "allowed_origin" in resp.text

    def test_exchange_link_bootstrap(self, client, auth_headers):
        bootstrap_resp = client.post(
            "/link/bootstrap",
            json={
                "site": "hydro_one",
                "allowed_origin": "https://app.example.com",
            },
            headers=auth_headers,
        )
        launch_token = bootstrap_resp.json()["launch_token"]

        resp = client.post(
            "/link/sessions/bootstrap",
            json={"launch_token": launch_token},
            headers={"Origin": "https://app.example.com"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["link_token"]
        status_resp = client.get(f"/link/sessions/{data['link_token']}/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "awaiting_institution"
        assert status_resp.json()["site"] == "hydro_one"

    def test_exchange_link_bootstrap_rejects_reuse(self, client, auth_headers):
        bootstrap_resp = client.post("/link/bootstrap", json={}, headers=auth_headers)
        launch_token = bootstrap_resp.json()["launch_token"]

        first = client.post("/link/sessions/bootstrap", json={"launch_token": launch_token})
        second = client.post("/link/sessions/bootstrap", json={"launch_token": launch_token})

        assert first.status_code == 200
        assert second.status_code == 410
        assert "already been used" in second.json()["detail"]

    def test_exchange_link_bootstrap_rejects_wrong_origin(self, client, auth_headers):
        bootstrap_resp = client.post(
            "/link/bootstrap",
            json={"allowed_origin": "https://app.example.com"},
            headers=auth_headers,
        )
        launch_token = bootstrap_resp.json()["launch_token"]

        resp = client.post(
            "/link/sessions/bootstrap",
            json={"launch_token": launch_token},
            headers={"Origin": "https://evil.example.com"},
        )

        assert resp.status_code == 403
        assert "not allowed to redeem" in resp.json()["detail"]

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
            json={"event": "INSTITUTION_SELECTED", "site": "hydro_one"},
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
            ("ERROR", "error"),
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
        session_store._mem_link_sessions[token]["created_at"] = 0

        resp = client.post(f"/link/sessions/{token}/event", json={"event": "TEST"})
        assert resp.status_code == 410


# ── Webhooks ──────────────────────────────────────────────────────────────────


class TestWebhooks:
    def test_register_webhook(self, client, auth_headers):
        # Create session first
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        resp = client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/webhook",
                "secret": "test-secret-key",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "webhook_id" in data
        assert data["status"] == "registered"

    def test_register_webhook_missing_fields(self, client, auth_headers):
        resp = client.post("/webhooks/register", json={"link_token": "abc"}, headers=auth_headers)
        assert resp.status_code == 422

    def test_register_webhook_invalid_url(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        resp = client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://not-localhost:9999/webhook",
                "secret": "s",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_register_webhook_session_not_found(self, client, auth_headers):
        resp = client.post(
            "/webhooks/register",
            json={
                "link_token": "nonexistent",
                "url": "https://example.com/hook",
                "secret": "s",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_test_webhook_not_found(self, client, auth_headers):
        resp = client.post("/webhooks/test", json={"webhook_id": "nope"}, headers=auth_headers)
        assert resp.status_code == 404

    @patch("src.routers.webhooks._deliver_webhook", new_callable=AsyncMock, return_value=True)
    def test_test_webhook_delivers(self, mock_deliver, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        reg_resp = client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/hook",
                "secret": "secret",
            },
            headers=auth_headers,
        )
        webhook_id = reg_resp.json()["webhook_id"]

        resp = client.post("/webhooks/test", json={"webhook_id": webhook_id}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "delivered"
        mock_deliver.assert_called_once()

    def test_list_webhooks(self, client, auth_headers):
        # Create session and register two webhooks
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/hook1",
                "secret": "s1",
            },
            headers=auth_headers,
        )
        client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/hook2",
                "secret": "s2",
            },
            headers=auth_headers,
        )

        resp = client.get("/webhooks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert len(data["webhooks"]) == 2

    def test_list_webhooks_empty(self, client, auth_headers):
        resp = client.get("/webhooks", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_webhooks_requires_auth(self, client):
        resp = client.get("/webhooks")
        assert resp.status_code in (401, 403)

    def test_delete_webhook(self, client, auth_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        reg_resp = client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/hook",
                "secret": "s",
            },
            headers=auth_headers,
        )
        webhook_id = reg_resp.json()["webhook_id"]

        resp = client.delete(f"/webhooks/{webhook_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Confirm it's gone
        resp = client.get("/webhooks", headers=auth_headers)
        assert resp.json()["count"] == 0

    def test_delete_webhook_not_found(self, client, auth_headers):
        resp = client.delete("/webhooks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_webhook_wrong_user(self, client, auth_headers, second_user_headers):
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        reg_resp = client.post(
            "/webhooks/register",
            json={
                "link_token": token,
                "url": "http://localhost:9999/hook",
                "secret": "s",
            },
            headers=auth_headers,
        )
        webhook_id = reg_resp.json()["webhook_id"]

        # Second user can't delete first user's webhook
        resp = client.delete(f"/webhooks/{webhook_id}", headers=second_user_headers)
        assert resp.status_code == 404


# ── Public Token Exchange ─────────────────────────────────────────────────────


class TestPublicTokenExchange:
    def _create_completed_session(self, client, auth_headers):
        """Helper: create a hosted link session and complete it through /connect."""
        create_resp = client.post("/link/sessions", headers=auth_headers)
        token = create_resp.json()["link_token"]

        connect_resp = client.post(
            "/connect",
            json={
                "link_token": token,
                "password": "Secret@pass123",
                "site": "hydro_one",
                "username": "demo-user",
            },
        )
        assert connect_resp.status_code == 200
        assert connect_resp.json()["status"] == "connected"

        status_resp = client.get(f"/link/sessions/{token}/status")
        assert status_resp.status_code == 200
        return token, status_resp.json()

    def test_completed_session_returns_public_token(self, client, auth_headers):
        token, data = self._create_completed_session(client, auth_headers)
        assert "public_token" in data
        assert data["public_token"].startswith("public-")

    def test_session_status_includes_public_token(self, client, auth_headers):
        token, data = self._create_completed_session(client, auth_headers)
        status_resp = client.get(f"/link/sessions/{token}/status")
        assert status_resp.json()["public_token"] == data["public_token"]

    def test_session_status_does_not_expose_result_payload(self, client, auth_headers):
        token, _ = self._create_completed_session(client, auth_headers)
        status_resp = client.get(f"/link/sessions/{token}/status")
        assert "result" not in status_resp.json()

    def test_exchange_public_token(self, client, auth_headers):
        token, data = self._create_completed_session(client, auth_headers)
        public_token = data["public_token"]

        resp = client.post(
            "/exchange/public_token",
            json={
                "public_token": public_token,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_exchange_public_token_single_use(self, client, auth_headers):
        token, data = self._create_completed_session(client, auth_headers)
        public_token = data["public_token"]

        # First exchange succeeds
        resp1 = client.post(
            "/exchange/public_token",
            json={
                "public_token": public_token,
            },
            headers=auth_headers,
        )
        assert resp1.status_code == 200

        # Second exchange fails (already used)
        resp2 = client.post(
            "/exchange/public_token",
            json={
                "public_token": public_token,
            },
            headers=auth_headers,
        )
        assert resp2.status_code == 410

    def test_exchange_public_token_invalid(self, client, auth_headers):
        resp = client.post(
            "/exchange/public_token",
            json={
                "public_token": "invalid-token",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_exchange_public_token_missing(self, client, auth_headers):
        resp = client.post("/exchange/public_token", json={}, headers=auth_headers)
        assert resp.status_code == 422

    def test_exchange_public_token_requires_auth(self, client):
        resp = client.post("/exchange/public_token", json={"public_token": "abc"})
        assert resp.status_code in (401, 403)

    def test_exchange_public_token_wrong_user(self, client, auth_headers, second_user_headers):
        token, data = self._create_completed_session(client, auth_headers)
        public_token = data["public_token"]

        # Second user can't exchange first user's token
        resp = client.post(
            "/exchange/public_token",
            json={
                "public_token": public_token,
            },
            headers=second_user_headers,
        )
        assert resp.status_code == 403


# ── SDK Models ────────────────────────────────────────────────────────────────


class TestSDKModels:
    def test_link_session_model(self):
        import os
        import sys

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
        import os
        import sys

        sdk_path = os.path.join(os.path.dirname(__file__), "..", "sdk")
        sys.path.insert(0, sdk_path)
        try:
            from plaidify.models import LinkEvent

            event = LinkEvent(event="CONNECTED", data={"public_token": "public-xyz"})
            assert event.event == "CONNECTED"
        finally:
            sys.path.pop(0)

    def test_webhook_registration_model(self):
        import os
        import sys

        sdk_path = os.path.join(os.path.dirname(__file__), "..", "sdk")
        sys.path.insert(0, sdk_path)
        try:
            from plaidify.models import WebhookRegistration

            reg = WebhookRegistration(webhook_id="wh-123")
            assert reg.status == "registered"
        finally:
            sys.path.pop(0)
