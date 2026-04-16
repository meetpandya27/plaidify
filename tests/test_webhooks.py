"""
Tests for webhook delivery history and security improvements.
"""

import pytest


class TestWebhookDeliveryHistory:
    """Test GET /webhooks/{id}/deliveries endpoint."""

    def _create_link_session(self, client, auth_headers):
        """Create a link session to get a valid link_token."""
        resp = client.post("/link/sessions", headers=auth_headers)
        return resp.json()["link_token"]

    def _register_webhook(self, client, auth_headers, link_token):
        """Register a webhook for a link session."""
        resp = client.post("/webhooks/register", json={
            "url": "https://example.com/hook",
            "link_token": link_token,
            "secret": "test-webhook-secret",
        }, headers=auth_headers)
        return resp.json()

    def test_deliveries_requires_auth(self, client):
        """Should require authentication."""
        resp = client.get("/webhooks/fake-id/deliveries")
        assert resp.status_code == 401

    def test_deliveries_not_found(self, client, auth_headers):
        """Should return 404 for non-existent webhook."""
        resp = client.get("/webhooks/nonexistent/deliveries", headers=auth_headers)
        assert resp.status_code == 404

    def test_deliveries_empty(self, client, auth_headers):
        """New webhook should have empty delivery history."""
        link_token = self._create_link_session(client, auth_headers)
        wh = self._register_webhook(client, auth_headers, link_token)
        webhook_id = wh["webhook_id"]

        resp = client.get(f"/webhooks/{webhook_id}/deliveries", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_id"] == webhook_id
        assert data["deliveries"] == []
        assert data["total"] == 0

    def test_deliveries_cross_user_forbidden(self, client, auth_headers, second_user_headers):
        """Users cannot view other users' webhook deliveries."""
        link_token = self._create_link_session(client, auth_headers)
        wh = self._register_webhook(client, auth_headers, link_token)
        webhook_id = wh["webhook_id"]

        resp = client.get(f"/webhooks/{webhook_id}/deliveries", headers=second_user_headers)
        assert resp.status_code == 404


class TestWebhookPayloadSecurity:
    """Test that webhook payloads don't leak sensitive tokens."""

    def test_webhook_payload_excludes_access_token(self):
        """Verify fire_webhooks_for_session doesn't include access_token in payload."""
        import inspect
        from src.routers.webhooks import fire_webhooks_for_session

        source = inspect.getsource(fire_webhooks_for_session)
        # The function should NOT set access_token in payload
        assert 'payload["access_token"]' not in source
        # It SHOULD reference public_token instead
        assert 'public_token' in source
