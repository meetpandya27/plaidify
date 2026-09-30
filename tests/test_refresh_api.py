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
            json={"site": "internal_bank"},
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


def _user_token(client, headers):
    link_token = client.post("/create_link", params={"site": "internal_bank"}, headers=headers).json()["link_token"]
    body = {"link_token": link_token, "username": "refresh-login", "password": "pw"}
    response = client.post("/submit_credentials", json=body, headers=headers)
    if response.status_code == 422:  # servers that still take the query-string form
        response = client.post("/submit_credentials", params=body, headers=headers)
    return response.json()["access_token"]


class TestRefreshJobsAreScoped:
    """SEC-10: /refresh/jobs shows only the caller's schedules, never a full token."""

    def test_each_user_sees_only_their_own_masked_schedules(self, client, auth_headers, second_user_headers):
        mine = _user_token(client, auth_headers)
        theirs = _user_token(client, second_user_headers)
        assert client.post("/refresh/schedule", json={"access_token": mine}, headers=auth_headers).status_code == 200
        assert (
            client.post("/refresh/schedule", json={"access_token": theirs}, headers=second_user_headers).status_code
            == 200
        )

        jobs = client.get("/refresh/jobs", headers=auth_headers).json()["jobs"]
        assert [job["access_token"] for job in jobs] == [mine[:12] + "..."]
        assert mine not in str(jobs) and theirs not in str(jobs)
        assert "user_id" not in jobs[0]

    def test_the_admin_view_needs_an_admin(self, client, auth_headers):
        from src.database import User
        from tests.conftest import TestSessionLocal

        token = _user_token(client, auth_headers)
        client.post("/refresh/schedule", json={"access_token": token}, headers=auth_headers)
        assert client.get("/refresh/admin/jobs", headers=auth_headers).status_code == 403

        with TestSessionLocal() as db:
            db.query(User).filter_by(username="testuser").update({"is_admin": True})
            db.commit()
        response = client.get("/refresh/admin/jobs", headers=auth_headers)
        assert response.status_code == 200
        [job] = response.json()["jobs"]
        assert job["access_token"] == token[:12] + "..." and "user_id" in job
        assert token not in response.text

    def test_other_users_cannot_change_or_remove_a_schedule(self, client, auth_headers, second_user_headers):
        token = _user_token(client, auth_headers)
        client.post("/refresh/schedule", json={"access_token": token}, headers=auth_headers)
        assert (
            client.patch(f"/refresh/schedule/{token}", json={"enabled": False}, headers=second_user_headers).status_code
            == 404
        )
        assert client.delete(f"/refresh/schedule/{token}", headers=second_user_headers).status_code == 404


class TestRefreshValidation:
    def test_a_too_short_interval_is_refused_before_it_is_saved(self, client, auth_headers):
        """JOB-11: PATCH used to save interval_seconds=1 and then answer 400."""
        token = _user_token(client, auth_headers)
        client.post("/refresh/schedule", json={"access_token": token, "interval_seconds": 600}, headers=auth_headers)
        response = client.patch(f"/refresh/schedule/{token}", json={"interval_seconds": 1}, headers=auth_headers)
        assert response.status_code == 400
        [job] = client.get("/refresh/jobs", headers=auth_headers).json()["jobs"]
        assert job["interval_seconds"] == 600

        bad_format = client.patch(
            f"/refresh/schedule/{token}", json={"schedule_format": "quarterly"}, headers=auth_headers
        )
        assert bad_format.status_code == 400
        ok = client.patch(f"/refresh/schedule/{token}", json={"schedule_format": "daily"}, headers=auth_headers)
        assert ok.status_code == 200 and ok.json()["interval_seconds"] == 86400

    def test_malformed_bodies_are_422(self, client, auth_headers):
        headers = {**auth_headers, "Content-Type": "application/json"}
        assert client.post("/refresh/schedule", content=b"{oops", headers=headers).status_code == 422
        assert client.patch("/refresh/schedule/x", content=b"[1,", headers=headers).status_code == 422
        assert (
            client.post(
                "/refresh/schedule", json={"access_token": "t", "interval_seconds": "soon"}, headers=auth_headers
            ).status_code
            == 422
        )

    def test_deleting_a_schedule_removes_its_row(self, client, auth_headers):
        token = _user_token(client, auth_headers)
        client.post("/refresh/schedule", json={"access_token": token}, headers=auth_headers)
        assert client.delete(f"/refresh/schedule/{token}", headers=auth_headers).status_code == 200
        assert client.get("/refresh/jobs", headers=auth_headers).json()["jobs"] == []
        assert client.delete(f"/refresh/schedule/{token}", headers=auth_headers).status_code == 404
