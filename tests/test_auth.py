"""
Tests for authentication endpoints: register, login, profile, OAuth2.
"""

import pytest


class TestRegistration:
    """Tests for POST /auth/register."""

    def test_register_success(self, client):
        response = client.post("/auth/register", json={
            "username": "newuser",
            "email": "new@example.com",
            "password": "strongpass123",
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_username(self, client):
        client.post("/auth/register", json={
            "username": "dupuser",
            "email": "dup1@example.com",
            "password": "strongpass123",
        })
        response = client.post("/auth/register", json={
            "username": "dupuser",
            "email": "dup2@example.com",
            "password": "strongpass456",
        })
        assert response.status_code == 400
        assert "already registered" in response.json()["detail"]

    def test_register_duplicate_email(self, client):
        client.post("/auth/register", json={
            "username": "user1",
            "email": "same@example.com",
            "password": "strongpass123",
        })
        response = client.post("/auth/register", json={
            "username": "user2",
            "email": "same@example.com",
            "password": "strongpass456",
        })
        assert response.status_code == 400

    def test_register_invalid_email(self, client):
        response = client.post("/auth/register", json={
            "username": "baduser",
            "email": "not-an-email",
            "password": "strongpass123",
        })
        assert response.status_code == 422

    def test_register_short_password(self, client):
        response = client.post("/auth/register", json={
            "username": "shortpw",
            "email": "short@example.com",
            "password": "short",
        })
        assert response.status_code == 422


class TestLogin:
    """Tests for POST /auth/token."""

    def test_login_success(self, client):
        # Register first
        client.post("/auth/register", json={
            "username": "loginuser",
            "email": "login@example.com",
            "password": "strongpass123",
        })
        # Login
        response = client.post("/auth/token", data={
            "username": "loginuser",
            "password": "strongpass123",
        })
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_login_wrong_password(self, client):
        client.post("/auth/register", json={
            "username": "loginuser2",
            "email": "login2@example.com",
            "password": "strongpass123",
        })
        response = client.post("/auth/token", data={
            "username": "loginuser2",
            "password": "wrongpassword",
        })
        assert response.status_code == 400

    def test_login_nonexistent_user(self, client):
        response = client.post("/auth/token", data={
            "username": "nobody",
            "password": "nopass",
        })
        assert response.status_code == 400


class TestProfile:
    """Tests for GET /auth/me."""

    def test_get_profile(self, client, auth_headers):
        response = client.get("/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["email"] == "test@example.com"
        assert data["is_active"] is True

    def test_get_profile_no_auth(self, client):
        response = client.get("/auth/me")
        assert response.status_code == 401

    def test_get_profile_invalid_token(self, client):
        response = client.get("/auth/me", headers={
            "Authorization": "Bearer invalid-token-here"
        })
        assert response.status_code == 401


class TestOAuth2:
    """Tests for POST /auth/oauth2."""

    def test_oauth2_login_creates_user(self, client):
        response = client.post("/auth/oauth2", json={
            "provider": "google",
            "oauth_token": "google-token-abc12345",
        })
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_oauth2_login_returns_same_user(self, client):
        # First login
        r1 = client.post("/auth/oauth2", json={
            "provider": "github",
            "oauth_token": "github-token-xyz98765",
        })
        token1 = r1.json()["access_token"]

        # Second login with same token prefix
        r2 = client.post("/auth/oauth2", json={
            "provider": "github",
            "oauth_token": "github-token-xyz98765",
        })
        token2 = r2.json()["access_token"]

        # Both should resolve to same user
        me1 = client.get("/auth/me", headers={"Authorization": f"Bearer {token1}"}).json()
        me2 = client.get("/auth/me", headers={"Authorization": f"Bearer {token2}"}).json()
        assert me1["id"] == me2["id"]
