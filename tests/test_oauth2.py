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


def test_oauth2_never_links_into_an_unverified_password_account(client, auth_headers):
    """Anyone can register a password account with someone else's email: never link into it."""
    from src.database import User

    # auth_headers registered testuser with test@example.com (unverified: never proven)
    identity = _identity(email="Test@Example.com", subject="gh-999", provider="github", username="testuser")
    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=identity),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "github", "oauth_token": "t"})

    assert resp.status_code == 409
    assert "not been verified" in resp.json()["detail"]
    db, gen = _db(client)
    try:
        users = db.query(User).filter(User.email == "test@example.com").all()
        assert len(users) == 1
        assert users[0].oauth_sub is None
    finally:
        gen.close()


def test_oauth2_links_an_account_whose_email_is_verified(client, auth_headers):
    """Once the address is proven (e.g. by an emailed password reset), the identity links."""
    from src.database import User

    db, gen = _db(client)
    try:
        db.query(User).filter(User.email == "test@example.com").update({"email_verified": True})
        db.commit()
    finally:
        gen.close()

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


def test_oauth2_sign_up_honours_registration_enabled(client):
    from src.database import User

    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.settings.registration_enabled", False),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})

    assert resp.status_code == 403
    assert "registration is disabled" in resp.json()["detail"].lower()
    db, gen = _db(client)
    try:
        assert db.query(User).filter(User.email == "alice@example.com").count() == 0
    finally:
        gen.close()


def test_oauth2_created_accounts_have_a_verified_email(client):
    from src.database import User

    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        assert client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"}).status_code == 200

    db, gen = _db(client)
    try:
        assert db.query(User).filter(User.email == "alice@example.com").one().email_verified is True
    finally:
        gen.close()


def test_oauth2_disabled_account_is_refused(client):
    from src.database import User

    with (
        patch("src.routers.auth.settings.oauth_enabled", True),
        patch("src.routers.auth.verify_oauth_token", return_value=_identity()),
    ):
        assert client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"}).status_code == 200
        db, gen = _db(client)
        try:
            db.query(User).filter(User.email == "alice@example.com").update({"is_active": False})
            db.commit()
        finally:
            gen.close()
        resp = client.post("/auth/oauth2", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 403


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


_GITHUB_SETTINGS = SimpleNamespace(oauth_github_client_id="Iv1.plaidify", oauth_github_client_secret="gh-secret")


def _github_emails(url, params=None, headers=None, timeout=None):
    if url.endswith("/user/emails"):
        assert headers["Authorization"] == "Bearer gh-token"
        return _FakeResp(
            200,
            [
                {"email": "secondary@example.com", "primary": False, "verified": True},
                {"email": "octo@example.com", "primary": True, "verified": True},
            ],
        )
    return _FakeResp(404, {})


def test_verify_github_checks_the_token_against_plaidifys_app():
    calls = []

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        calls.append((url, json, auth))
        return _FakeResp(200, {"app": {"client_id": "Iv1.plaidify"}, "user": {"id": 42, "login": "octocat"}})

    with (
        patch("src.oauth_providers.httpx.post", side_effect=fake_post),
        patch("src.oauth_providers.httpx.get", side_effect=_github_emails),
    ):
        identity = verify_oauth_token("github", "gh-token", _GITHUB_SETTINGS)

    assert calls == [
        (
            "https://api.github.com/applications/Iv1.plaidify/token",
            {"access_token": "gh-token"},
            ("Iv1.plaidify", "gh-secret"),
        )
    ]
    assert identity.subject == "42"
    assert identity.username == "octocat"
    assert identity.email == "octo@example.com"
    assert identity.email_verified is True


def test_verify_github_rejects_a_token_of_another_app():
    # GitHub answers 404 when the token was not issued to this client id.
    with (
        patch("src.oauth_providers.httpx.post", return_value=_FakeResp(404, {"message": "Not Found"})),
        patch("src.oauth_providers.httpx.get", side_effect=_github_emails) as get,
    ):
        with pytest.raises(OAuthVerificationError):
            verify_oauth_token("github", "gh-token", _GITHUB_SETTINGS)
    get.assert_not_called()


def test_verify_github_rejects_a_mismatched_app_in_the_answer():
    answer = {"app": {"client_id": "Iv1.someone-else"}, "user": {"id": 42, "login": "octocat"}}
    with patch("src.oauth_providers.httpx.post", return_value=_FakeResp(200, answer)):
        with pytest.raises(OAuthVerificationError):
            verify_oauth_token("github", "gh-token", _GITHUB_SETTINGS)


def test_verify_github_without_app_credentials_fails_closed():
    with patch("src.oauth_providers.httpx.post") as post:
        with pytest.raises(OAuthVerificationError):
            verify_oauth_token("github", "gh-token", SimpleNamespace(oauth_github_client_id="Iv1.plaidify"))
    post.assert_not_called()


def test_verify_google_without_client_id_fails_closed():
    payload = {"sub": "g-1", "aud": "any-app", "email": "bob@example.com", "email_verified": "true"}
    with patch("src.oauth_providers.httpx.get", return_value=_FakeResp(200, payload)) as get:
        with pytest.raises(OAuthVerificationError):
            verify_oauth_token("google", "id-token", SimpleNamespace(oauth_google_client_id=None))
    get.assert_not_called()


def test_enabled_oauth_names_missing_client_credentials():
    from src.oauth_providers import missing_oauth_configuration

    base = dict(oauth_enabled=True, oauth_allowed_providers="google,github")
    assert missing_oauth_configuration(SimpleNamespace(**base)) == [
        "OAUTH_GOOGLE_CLIENT_ID",
        "OAUTH_GITHUB_CLIENT_ID",
        "OAUTH_GITHUB_CLIENT_SECRET",
    ]
    complete = SimpleNamespace(
        **base, oauth_google_client_id="g", oauth_github_client_id="gh", oauth_github_client_secret="s"
    )
    assert missing_oauth_configuration(complete) == []
    assert missing_oauth_configuration(SimpleNamespace(oauth_enabled=False)) == []


def test_app_refuses_to_start_with_oauth_but_no_client_id():
    import src.app as appmod

    with (
        patch.object(appmod.settings, "oauth_enabled", True),
        patch.object(appmod.settings, "oauth_allowed_providers", "google"),
        patch.object(appmod.settings, "oauth_google_client_id", None),
    ):
        with pytest.raises(RuntimeError, match="OAUTH_GOOGLE_CLIENT_ID"):
            appmod._validate_runtime_configuration()


def test_verify_unsupported_provider_raises():
    with pytest.raises(OAuthVerificationError):
        verify_oauth_token("myspace", "t", SimpleNamespace())
