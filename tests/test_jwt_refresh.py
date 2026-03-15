"""
Tests for JWT refresh token rotation (Issue #11).
"""

import time
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import pytest


class TestTokenResponseIncludesRefresh:
    """Verify that register, login, and oauth2 return refresh tokens."""

    def test_register_returns_refresh_token(self, client):
        response = client.post("/auth/register", json={
            "username": "refreshuser",
            "email": "refresh@example.com",
            "password": "strongpass123",
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert len(data["refresh_token"]) > 20

    def test_login_returns_refresh_token(self, client):
        # Register first
        client.post("/auth/register", json={
            "username": "loginrefresh",
            "email": "loginrefresh@example.com",
            "password": "strongpass123",
        })
        # Login
        response = client.post("/auth/token", data={
            "username": "loginrefresh",
            "password": "strongpass123",
        })
        assert response.status_code == 200
        data = response.json()
        assert "refresh_token" in data
        assert len(data["refresh_token"]) > 20

    def test_oauth2_returns_refresh_token(self, client):
        response = client.post("/auth/oauth2", json={
            "provider": "google",
            "oauth_token": "fake-google-token-12345678",
        })
        assert response.status_code == 200
        data = response.json()
        assert "refresh_token" in data


class TestRefreshEndpoint:
    """Tests for POST /auth/refresh."""

    def _register_and_get_tokens(self, client, username="refreshtest"):
        response = client.post("/auth/register", json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "strongpass123",
        })
        return response.json()

    def test_refresh_success(self, client):
        tokens = self._register_and_get_tokens(client)
        response = client.post("/auth/refresh", json={
            "refresh_token": tokens["refresh_token"],
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        # Refresh token must be different (rotation)
        assert data["refresh_token"] != tokens["refresh_token"]

    def test_refresh_rotates_token(self, client):
        """Old refresh token should be revoked after use (rotation)."""
        tokens = self._register_and_get_tokens(client, "rotateuser")
        old_refresh = tokens["refresh_token"]

        # Use the refresh token
        response = client.post("/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert response.status_code == 200

        # Try to reuse the old refresh token — should fail
        response = client.post("/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert response.status_code == 401
        assert "Invalid or revoked" in response.json()["detail"]

    def test_refresh_invalid_token(self, client):
        response = client.post("/auth/refresh", json={
            "refresh_token": "completely-invalid-token",
        })
        assert response.status_code == 401

    def test_refresh_expired_token(self, client):
        """Expired refresh tokens should be rejected."""
        tokens = self._register_and_get_tokens(client, "expireduser")

        # Manually expire the token in the database
        from src.database import RefreshToken
        from tests.conftest import TestSessionLocal
        db = TestSessionLocal()
        try:
            rt = db.query(RefreshToken).filter_by(token=tokens["refresh_token"]).first()
            rt.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        response = client.post("/auth/refresh", json={
            "refresh_token": tokens["refresh_token"],
        })
        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_new_access_token_works(self, client):
        """The new access token from refresh should authenticate requests."""
        tokens = self._register_and_get_tokens(client, "newtokenuser")

        # Refresh
        response = client.post("/auth/refresh", json={
            "refresh_token": tokens["refresh_token"],
        })
        new_tokens = response.json()

        # Use the new access token
        headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
        response = client.get("/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["username"] == "newtokenuser"

    def test_refresh_chain(self, client):
        """Can chain multiple refresh operations."""
        tokens = self._register_and_get_tokens(client, "chainuser")

        for i in range(3):
            response = client.post("/auth/refresh", json={
                "refresh_token": tokens["refresh_token"],
            })
            assert response.status_code == 200
            tokens = response.json()
            assert "access_token" in tokens
            assert "refresh_token" in tokens


class TestShortAccessTokenExpiry:
    """Verify access tokens have short expiry times."""

    def test_access_token_default_expiry(self, client):
        """Access tokens should have a short default expiry (15 minutes)."""
        import jwt as pyjwt
        from src.config import get_settings

        response = client.post("/auth/register", json={
            "username": "expiryuser",
            "email": "expiry@example.com",
            "password": "strongpass123",
        })
        token = response.json()["access_token"]

        settings = get_settings()
        payload = pyjwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)

        # Should expire within ~15 minutes (with some tolerance)
        diff_minutes = (exp - now).total_seconds() / 60
        assert diff_minutes <= 16  # 15 min + 1 min tolerance
        assert diff_minutes >= 13  # at least 13 min
