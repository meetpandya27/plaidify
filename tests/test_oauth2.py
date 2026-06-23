"""Tests for OAuth2 social login (POST /auth/oauth2) and provider verification."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.oauth_providers import (
    OAuthIdentity,
    OAuthVerificationError,
    verify_oauth_token,
)


def _db(client):
    from src.database import get_db

    gen = client.app.dependency_overrides[get_db]()
    return next(gen), gen


def _identity(provider="google", subject="sub-123", email="alice@example.com", verified=True, username="alice"):
    return OAuthIdentity(
        provider=provider,
        subject=subject,
        email=email,
        email_verified=verified,
        username=username,
    )


# ── Endpoint behavior ─────────────────────────────────────────────────────────


def test_oauth2_disabled_returns_403(client):
    with patch("src.routers.auth.settings.oauth_enabled", False):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 403


def test_oauth2_unsupported_provider_returns_400(client):
    with patch("src.routers.auth.settings.oauth_enabled", True):
        resp = client.post("/auth/oauth2", json={"provider": "myspace", "oauth_token": "t"})
    assert resp.status_code == 400


def test_oauth2_verification_failure_returns_401(client):
    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", side_effect=OAuthVerificationError("bad")),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 401


def test_oauth2_unverified_email_returns_403(client):
    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity(verified=False)),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 403


def test_oauth2_auto_registers_new_user(client):
    from src.database import User

    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})

    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()

    db, gen = _db(client)
    try:
        user = db.query(User).filter(User.email == "alice@example.com").first()
        assert user is not None
        assert user.oauth_provider == "google"
        assert user.oauth_sub == "sub-123"
        assert user.hashed_password is None
    finally:
        gen.close()


def test_oauth2_repeat_login_does_not_duplicate(client):
    from src.database import User

    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        first = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
        second = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})

    assert first.status_code == 200
    assert second.status_code == 200

    db, gen = _db(client)
    try:
        count = db.query(User).filter(User.email == "alice@example.com").count()
        assert count == 1
    finally:
        gen.close()


def test_oauth2_links_existing_email_account(client, auth_headers):
    """A verified OAuth email matching an existing user links rather than duplicates."""
    from src.database import User

    # auth_headers registered testuser with test@example.com
    identity = _identity(email="test@example.com", subject="gh-999", provider="github", username="testuser")
    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=identity),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "github", "oauth_token": "t"})

    assert resp.status_code == 200
    db, gen = _db(client)
    try:
        users = db.query(User).filter(User.email == "test@example.com").all()
        assert len(users) == 1
        assert users[0].oauth_provider == "github"
        assert users[0].oauth_sub == "gh-999"
    finally:
        gen.close()


def test_oauth2_no_autoregister_without_account_returns_403(client):
    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.settings.oauth_auto_register", False),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 403


# ── Provider module unit tests ────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_verify_google_success_with_audience_check():
    settings = SimpleNamespace(oauth_google_client_id="client-abc")
    payload = {"sub": "g-1", "aud": "client-abc", "email": "bob@example.com", "email_verified": "true"}

    with patch("src.oauth_providers.httpx.get", return_value=_FakeResp(200, payload)):
        identity = verify_oauth_token("google", "id-token", settings)

    assert identity.provider == "google"
    assert identity.subject == "g-1"
    assert identity.email == "bob@example.com"
    assert identity.email_verified is True
    assert identity.username == "bob"


def test_verify_google_audience_mismatch_raises():
    settings = SimpleNamespace(oauth_google_client_id="expected-client")
    payload = {"sub": "g-1", "aud": "someone-elses-client", "email": "bob@example.com", "email_verified": "true"}

    with patch("src.oauth_providers.httpx.get", return_value=_FakeResp(200, payload)):
        with pytest.raises(OAuthVerificationError):
            verify_oauth_token("google", "id-token", settings)


def test_verify_github_uses_verified_primary_email():
    settings = SimpleNamespace()

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/user"):
            return _FakeResp(200, {"id": 42, "login": "octocat", "email": None})
        if url.endswith("/user/emails"):
            return _FakeResp(
                200,
                [
                    {"email": "secondary@example.com", "primary": False, "verified": True},
                    {"email": "octo@example.com", "primary": True, "verified": True},
                ],
            )
        return _FakeResp(404, {})

    with patch("src.oauth_providers.httpx.get", side_effect=fake_get):
        identity = verify_oauth_token("github", "gh-token", settings)

    assert identity.subject == "42"
    assert identity.username == "octocat"
    assert identity.email == "octo@example.com"
    assert identity.email_verified is True


def test_verify_unsupported_provider_raises():
    with pytest.raises(OAuthVerificationError):
        verify_oauth_token("myspace", "t", SimpleNamespace())
