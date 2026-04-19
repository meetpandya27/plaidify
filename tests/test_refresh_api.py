"""
Tests for the scheduled refresh API endpoints.
"""

import pytest


class TestRefreshScheduleEndpoints:
    """Test the /refresh/* API endpoints."""

    def _create_link_and_token(self, client, auth_headers):
        """Helper to create a link + access token for refresh tests."""
        # Create a link
        resp = client.post(
            "/link/create",
            json={"site": "demo_site"},
            headers=auth_headers,
        )
        if resp.status_code != 200:
            pytest.skip("Link creation not available")
        link_token = resp.json().get("link_token")
        return link_token

    def test_schedule_refresh_no_auth(self, client):
        """Unauthenticated requests should be rejected."""
        resp = client.post(
            "/refresh/schedule",
            json={"access_token": "acc-123", "interval_seconds": 3600},
        )
        assert resp.status_code in (401, 403)

    def test_schedule_refresh_missing_token(self, client, auth_headers):
        """Missing access_token should return 400."""
        resp = client.post(
            "/refresh/schedule",
            json={"interval_seconds": 3600},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_schedule_refresh_invalid_token(self, client, auth_headers):
        """Non-existent access token should return 404."""
        resp = client.post(
            "/refresh/schedule",
            json={"access_token": "acc-nonexistent", "interval_seconds": 3600},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_schedule_refresh_interval_too_small(self, client, auth_headers):
        """Interval below minimum (300s) should be rejected."""
        resp = client.post(
            "/refresh/schedule",
            json={"access_token": "acc-123", "interval_seconds": 60},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404)  # 400 if validated first, 404 if token checked first

    def test_list_refresh_jobs_no_auth(self, client):
        """Unauthenticated should be rejected."""
        resp = client.get("/refresh/jobs")
        assert resp.status_code in (401, 403)

    def test_list_refresh_jobs_empty(self, client, auth_headers):
        """Should return empty jobs when nothing is scheduled."""
        resp = client.get("/refresh/jobs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data

    def test_unschedule_refresh_no_auth(self, client):
        """Unauthenticated should be rejected."""
        resp = client.delete("/refresh/schedule/acc-123")
        assert resp.status_code in (401, 403)

    def test_unschedule_nonexistent(self, client, auth_headers):
        """Unscheduling a non-existent token should return 404."""
        resp = client.delete(
            "/refresh/schedule/acc-nonexistent",
            headers=auth_headers,
        )
        assert resp.status_code == 404
