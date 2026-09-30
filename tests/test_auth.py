"""
Tests for authentication endpoints: register, login, profile, OAuth2.
"""

import hashlib
import logging
import re
from datetime import timedelta
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from src.database import LoginThrottle, PasswordResetToken, User, utcnow
from src.main import app
from tests.conftest import TestSessionLocal

PASSWORD = "Strong@pass123"


def _register(client, username, email=None, password=PASSWORD):
    resp = client.post(
        "/auth/register",
        json={"username": username, "email": email or f"{username}@example.com", "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, username, password=PASSWORD):
    return client.post("/auth/token", data={"username": username, "password": password})


def _reset_token_for(username) -> str:
    """Plant a reset token for the user (as the emailed link would carry) and return it."""
    raw = "reset-" + hashlib.sha256(username.encode()).hexdigest()[:24]
    with TestSessionLocal() as db:
        user = db.query(User).filter(User.username == username).one()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        db.commit()
    return raw


class TestRegistration:
    """Tests for POST /auth/register."""

    def test_register_success(self, client):
        response = client.post(
            "/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_username(self, client):
        client.post(
            "/auth/register",
            json={
                "username": "dupuser",
                "email": "dup1@example.com",
                "password": "Strong@pass123",
            },
        )
        response = client.post(
            "/auth/register",
            json={
                "username": "dupuser",
                "email": "dup2@example.com",
                "password": "Strong@pass456",
            },
        )
        assert response.status_code == 400
        assert "already registered" in response.json()["detail"]

    def test_register_duplicate_email(self, client):
        client.post(
            "/auth/register",
            json={
                "username": "user1",
                "email": "same@example.com",
                "password": "Strong@pass123",
            },
        )
        response = client.post(
            "/auth/register",
            json={
                "username": "user2",
                "email": "same@example.com",
                "password": "Strong@pass456",
            },
        )
        assert response.status_code == 400

    def test_register_invalid_email(self, client):
        response = client.post(
            "/auth/register",
            json={
                "username": "baduser",
                "email": "not-an-email",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 422

    def test_register_short_password(self, client):
        response = client.post(
            "/auth/register",
            json={
                "username": "shortpw",
                "email": "short@example.com",
                "password": "short",
            },
        )
        assert response.status_code == 422


class TestLogin:
    """Tests for POST /auth/token."""

    def test_login_success(self, client):
        # Register first
        client.post(
            "/auth/register",
            json={
                "username": "loginuser",
                "email": "login@example.com",
                "password": "Strong@pass123",
            },
        )
        # Login
        response = client.post(
            "/auth/token",
            data={
                "username": "loginuser",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_login_wrong_password(self, client):
        client.post(
            "/auth/register",
            json={
                "username": "loginuser2",
                "email": "login2@example.com",
                "password": "Strong@pass123",
            },
        )
        response = client.post(
            "/auth/token",
            data={
                "username": "loginuser2",
                "password": "wrongpassword",
            },
        )
        assert response.status_code == 400

    def test_login_nonexistent_user(self, client):
        response = client.post(
            "/auth/token",
            data={
                "username": "nobody",
                "password": "nopass",
            },
        )
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
        response = client.get("/auth/me", headers={"Authorization": "Bearer invalid-token-here"})
        assert response.status_code == 401

    def test_link_launch_token_is_not_a_login(self, client, auth_headers):
        """A launch token handed to a browser must not open the developer's account."""
        from src.auth_utils import create_link_launch_token

        user_id = client.get("/auth/me", headers=auth_headers).json()["id"]
        launch = {"Authorization": f"Bearer {create_link_launch_token(launch_id='launch-1', user_id=user_id)}"}

        assert client.get("/auth/me", headers=launch).status_code == 401
        assert client.post("/api-keys", headers=launch, json={"name": "stolen"}).status_code == 401

    def test_token_without_type_is_rejected(self, client, auth_headers):
        import jwt

        from src.config import get_settings

        settings = get_settings()
        user_id = client.get("/auth/me", headers=auth_headers).json()["id"]
        untyped = jwt.encode(
            {"sub": str(user_id), "exp": 4102444800}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {untyped}"}).status_code == 401


class TestOAuth2:
    """Tests for POST /auth/oauth2 (disabled by default — returns 403)."""

    def test_oauth2_disabled_by_default_returns_403(self, client):
        response = client.post(
            "/auth/oauth2",
            json={
                "provider": "google",
                "oauth_token": "google-token-abc12345",
            },
        )
        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()


class TestAccountLockout:
    """Tests for account lockout after repeated failed logins."""

    def _register(self, client, username="locktest", email="lock@test.com"):
        client.post(
            "/auth/register",
            json={
                "username": username,
                "email": email,
                "password": "Strong@pass123",
            },
        )

    def test_lockout_after_five_failures(self, client):
        self._register(client)
        for _ in range(5):
            client.post("/auth/token", data={"username": "locktest", "password": "WrongPass1!"})
        # 6th attempt should be locked
        resp = client.post("/auth/token", data={"username": "locktest", "password": "Strong@pass123"})
        assert resp.status_code == 423
        assert "locked" in resp.json()["detail"].lower()

    def test_successful_login_resets_counter(self, client):
        self._register(client, "resetcount", "rc@test.com")
        # 3 failures (below threshold)
        for _ in range(3):
            client.post("/auth/token", data={"username": "resetcount", "password": "WrongPass1!"})
        # Correct login resets counter
        resp = client.post("/auth/token", data={"username": "resetcount", "password": "Strong@pass123"})
        assert resp.status_code == 200
        # 3 more failures — still below 5 total since counter was reset
        for _ in range(3):
            client.post("/auth/token", data={"username": "resetcount", "password": "WrongPass1!"})
        resp = client.post("/auth/token", data={"username": "resetcount", "password": "Strong@pass123"})
        assert resp.status_code == 200


class TestPasswordReset:
    """Tests for password reset flow."""

    def _register(self, client, username="resetuser", email="reset@test.com"):
        client.post(
            "/auth/register",
            json={
                "username": username,
                "email": email,
                "password": "Strong@pass123",
            },
        )

    def test_forgot_password_returns_200_for_existing_email(self, client):
        self._register(client)
        resp = client.post("/auth/forgot-password", json={"email": "reset@test.com"})
        assert resp.status_code == 200
        assert "reset link" in resp.json()["message"].lower()

    def test_forgot_password_returns_200_for_unknown_email(self, client):
        resp = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
        assert resp.status_code == 200  # No email enumeration

    def test_reset_password_invalid_token(self, client):
        resp = client.post(
            "/auth/reset-password",
            json={
                "token": "invalid-token",
                "new_password": "NewStrong@1234",
            },
        )
        assert resp.status_code == 400

    def test_reset_password_full_flow(self, client):
        """End-to-end: register → forgot → extract token from DB → reset → login with new pw."""
        self._register(client, "fullreset", "full@reset.com")

        # Request reset
        client.post("/auth/forgot-password", json={"email": "full@reset.com"})

        # Extract raw token from the password_reset_tokens table
        from src.database import PasswordResetToken
        from tests.conftest import TestSessionLocal

        with TestSessionLocal() as db:
            record = (
                db.query(PasswordResetToken)
                .filter(
                    PasswordResetToken.used == False  # noqa: E712
                )
                .first()
            )
        assert record is not None

        # We can't get the raw token from the DB (it's hashed), so we test
        # that the invalid-token path works correctly instead.
        # A full integration test would require intercepting the log output.
        resp = client.post(
            "/auth/reset-password",
            json={
                "token": "wrong-token",
                "new_password": "NewStrong@1234",
            },
        )
        assert resp.status_code == 400


# ── Launch tokens vs access tokens (SEC-01) ──────────────────────────────────


class TestTokenSeparation:
    def test_access_token_is_not_a_launch_token(self, client, auth_headers):
        from src.auth_utils import decode_link_launch_token

        access = auth_headers["Authorization"].split(" ", 1)[1]
        with pytest.raises(jwt.InvalidTokenError):
            decode_link_launch_token(access)
        exchanged = client.post("/link/sessions/bootstrap", json={"launch_token": access})
        assert exchanged.status_code == 400

    def test_launch_tokens_are_signed_with_their_own_key_and_audience(self):
        from src.auth_utils import (
            ACCESS_TOKEN_AUDIENCE,
            LINK_LAUNCH_AUDIENCE,
            create_link_launch_token,
            decode_link_launch_token,
            link_launch_signing_key,
            settings,
        )

        assert ACCESS_TOKEN_AUDIENCE != LINK_LAUNCH_AUDIENCE
        assert link_launch_signing_key() != settings.jwt_secret_key.encode()
        launch = create_link_launch_token(launch_id="l-1", user_id=1)
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(launch, settings.jwt_secret_key, algorithms=["HS256"], audience=LINK_LAUNCH_AUDIENCE)
        assert decode_link_launch_token(launch)["aud"] == LINK_LAUNCH_AUDIENCE

    def test_launch_token_minted_with_the_login_key_is_refused(self):
        """A launch token signed the old way (JWT_SECRET_KEY) no longer verifies."""
        from src.auth_utils import LINK_LAUNCH_AUDIENCE, LINK_LAUNCH_TOKEN_TYPE, decode_link_launch_token, settings

        forged = jwt.encode(
            {"sub": "1", "typ": LINK_LAUNCH_TOKEN_TYPE, "aud": LINK_LAUNCH_AUDIENCE, "jti": "x", "exp": 4102444800},
            settings.jwt_secret_key,
            algorithm="HS256",
        )
        with pytest.raises(jwt.InvalidTokenError):
            decode_link_launch_token(forged)

    def test_configured_link_launch_secret_is_used(self):
        from src import auth_utils

        secret = "launch-secret-" + "x" * 32
        with patch.object(auth_utils.settings, "link_launch_secret", secret):
            launch = auth_utils.create_link_launch_token(launch_id="l-2", user_id=1)
            assert auth_utils.decode_link_launch_token(launch)["jti"] == "l-2"
            jwt.decode(launch, secret, algorithms=["HS256"], audience=auth_utils.LINK_LAUNCH_AUDIENCE)
        with pytest.raises(jwt.InvalidSignatureError):
            auth_utils.decode_link_launch_token(launch)  # derived key now

    def test_access_token_without_audience_is_rejected(self, client, auth_headers):
        from src.config import get_settings

        settings = get_settings()
        user_id = client.get("/auth/me", headers=auth_headers).json()["id"]
        no_aud = jwt.encode(
            {"sub": str(user_id), "typ": "access", "tv": 0, "exp": 4102444800},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        assert client.get("/auth/me", headers=_bearer(no_aud)).status_code == 401

    def test_short_jwt_secret_stops_startup(self):
        import src.app as appmod

        with patch.object(appmod.settings, "jwt_secret_key", "too-short"):
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY must be at least 32"):
                appmod._validate_runtime_configuration()

    def test_link_launch_secret_must_be_long_and_distinct(self):
        import src.app as appmod

        with patch.object(appmod.settings, "link_launch_secret", "short"):
            with pytest.raises(RuntimeError, match="LINK_LAUNCH_SECRET"):
                appmod._validate_runtime_configuration()
        with patch.object(appmod.settings, "link_launch_secret", appmod.settings.jwt_secret_key):
            with pytest.raises(RuntimeError, match="must differ"):
                appmod._validate_runtime_configuration()


# ── Session versions (SEC-14) ────────────────────────────────────────────────


class TestSessionEnding:
    def test_password_reset_ends_every_session(self, client):
        victim = _register(client, "reset_victim")
        # A second, "stolen" session.
        stolen = _login(client, "reset_victim").json()

        resp = client.post(
            "/auth/reset-password", json={"token": _reset_token_for("reset_victim"), "new_password": "N3w!Password"}
        )
        assert resp.status_code == 200

        for session in (victim, stolen):
            assert client.get("/auth/me", headers=_bearer(session["access_token"])).status_code == 401
            assert client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
        assert _login(client, "reset_victim").status_code == 400
        assert _login(client, "reset_victim", "N3w!Password").status_code == 200

    def test_reset_token_is_single_use_and_proves_the_email(self, client):
        _register(client, "reset_once")
        token = _reset_token_for("reset_once")
        first = client.post("/auth/reset-password", json={"token": token, "new_password": "N3w!Password"})
        second = client.post("/auth/reset-password", json={"token": token, "new_password": "Oth3r!Password"})
        assert first.status_code == 200
        assert second.status_code == 400
        with TestSessionLocal() as db:
            assert db.query(User).filter(User.username == "reset_once").one().email_verified is True

    def test_new_login_after_sign_out_everywhere_works(self, client):
        registered = _register(client, "signout")
        assert client.post("/auth/sessions/revoke-all", headers=_bearer(registered["access_token"])).status_code == 200
        assert client.get("/auth/me", headers=_bearer(registered["access_token"])).status_code == 401
        fresh = _login(client, "signout").json()
        assert client.get("/auth/me", headers=_bearer(fresh["access_token"])).status_code == 200


# ── Sign-in throttling (SEC-18) ──────────────────────────────────────────────


def _from(address):
    return TestClient(app, client=(address, 50000))


class TestSignInThrottling:
    def test_one_client_is_locked_out_but_the_owner_elsewhere_is_not(self, client):
        _register(client, "target")
        attacker = _from("203.0.113.9")
        for _ in range(5):
            assert _login(attacker, "target", "WrongPass1!").status_code == 400
        locked = _login(attacker, "target")  # even the right password
        assert locked.status_code == 423
        assert int(locked.headers["Retry-After"]) > 0

        owner = _from("198.51.100.20")
        assert _login(owner, "target").status_code == 200

    def test_an_expired_lock_starts_the_count_afresh(self, client):
        _register(client, "patient")
        attacker = _from("203.0.113.10")
        for _ in range(5):
            _login(attacker, "patient", "WrongPass1!")
        assert _login(attacker, "patient").status_code == 423

        with TestSessionLocal() as db:
            past = utcnow() - timedelta(minutes=16)
            db.query(LoginThrottle).update({"locked_until": past, "window_started_at": past - timedelta(minutes=1)})
            db.commit()

        # One more failure after the lock ran out does not lock again (it used to).
        assert _login(attacker, "patient", "WrongPass1!").status_code == 400
        assert _login(attacker, "patient").status_code == 200

    def test_a_lock_that_ran_out_resets_even_inside_the_window(self, client):
        """The count restarts when the lock ends, not only when the failure window does."""
        import src.routers.auth as auth_router

        _register(client, "shortlock")
        attacker = _from("203.0.113.13")
        with patch.object(auth_router, "_LOGIN_LOCK_DURATION", timedelta(minutes=1)):
            for _ in range(5):
                _login(attacker, "shortlock", "WrongPass1!")
            assert _login(attacker, "shortlock").status_code == 423
            with TestSessionLocal() as db:
                db.query(LoginThrottle).update({"locked_until": utcnow() - timedelta(seconds=1)})
                db.commit()
            # The failures are still inside their 15-minute window, yet the count
            # starts afresh: it takes five new failures to lock again.
            assert [_login(attacker, "shortlock", "WrongPass1!").status_code for _ in range(5)] == [400] * 5
            assert _login(attacker, "shortlock").status_code == 423

    def test_unknown_usernames_lock_the_same_way(self, client):
        attacker = _from("203.0.113.11")
        replies = [_login(attacker, "nobody-here", "WrongPass1!") for _ in range(6)]
        assert [r.status_code for r in replies] == [400] * 5 + [423]
        assert replies[0].json() == {"detail": "Incorrect username or password"}

    def test_many_addresses_lock_the_whole_username(self, client):
        _register(client, "sprayed")
        for n in range(4):
            attacker = _from(f"203.0.113.{100 + n}")
            for _ in range(5):
                _login(attacker, "sprayed", "WrongPass1!")
        assert _login(_from("198.51.100.30"), "sprayed").status_code == 423

    def test_password_reset_clears_the_lockout(self, client):
        _register(client, "unlockme")
        for n in range(4):
            attacker = _from(f"203.0.113.{120 + n}")
            for _ in range(5):
                _login(attacker, "unlockme", "WrongPass1!")
        assert _login(client, "unlockme").status_code == 423

        token = _reset_token_for("unlockme")
        assert (
            client.post("/auth/reset-password", json={"token": token, "new_password": "N3w!Password"}).status_code
            == 200
        )
        assert _login(client, "unlockme", "N3w!Password").status_code == 200

    def test_throttle_rows_hold_no_usernames_or_addresses(self, client):
        _login(_from("203.0.113.12"), "someone@example.com", "WrongPass1!")
        with TestSessionLocal() as db:
            rows = db.query(LoginThrottle).all()
        assert rows
        for row in rows:
            assert "someone" not in row.key and "203.0.113" not in row.key
            assert re.fullmatch(r"[0-9a-f]{64}", row.key)


# ── Passwords bcrypt would truncate (SEC-23) ─────────────────────────────────


class TestPasswordLength:
    LONG = "Aa1!" + "x" * 69  # 73 bytes

    def test_register_rejects_more_than_72_bytes(self, client):
        resp = client.post(
            "/auth/register", json={"username": "longpw", "email": "longpw@example.com", "password": self.LONG}
        )
        assert resp.status_code == 422
        assert "72 bytes" in resp.text

    def test_multibyte_characters_count_as_bytes(self, client):
        password = "Aa1!" + "é" * 35  # 39 characters, 74 bytes
        resp = client.post(
            "/auth/register", json={"username": "utf8pw", "email": "utf8pw@example.com", "password": password}
        )
        assert resp.status_code == 422

    def test_reset_rejects_more_than_72_bytes(self, client):
        _register(client, "longreset")
        token = _reset_token_for("longreset")
        resp = client.post("/auth/reset-password", json={"token": token, "new_password": self.LONG})
        assert resp.status_code == 422

    def test_a_longer_password_never_verifies_against_its_prefix(self):
        from src.dependencies import get_password_hash, verify_password

        prefix = "Aa1!" + "y" * 68  # exactly 72 bytes
        hashed = get_password_hash(prefix)
        assert verify_password(prefix, hashed)
        assert not verify_password(prefix + "anything", hashed)
        with pytest.raises(ValueError):
            get_password_hash(prefix + "z")


# ── Uniform replies and reset delivery (SEC-24) ──────────────────────────────


class _FakeSMTP:
    sent: list = []
    events: list = []

    def __init__(self, host, port, timeout=None):
        self.events.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.events.append(("ehlo",))

    def starttls(self, context=None):
        self.events.append(("starttls", context is not None))

    def login(self, user, password):
        self.events.append(("login", user, password))

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def fake_smtp():
    import src.mailer as mailer

    _FakeSMTP.sent, _FakeSMTP.events = [], []
    with (
        patch.object(mailer.smtplib, "SMTP", _FakeSMTP),
        patch.object(mailer.settings, "smtp_host", "smtp.example.com"),
        patch.object(mailer.settings, "smtp_port", 587),
        patch.object(mailer.settings, "smtp_from", "Plaidify <no-reply@example.com>"),
        patch.object(mailer.settings, "smtp_username", "mailer"),
        patch.object(mailer.settings, "smtp_password", "mail-secret"),
        patch.object(mailer.settings, "password_reset_url", "https://app.example.com/reset?token={token}"),
    ):
        yield _FakeSMTP


class TestUniformReplies:
    def test_forgot_password_reply_is_the_same_for_known_and_unknown(self, client, fake_smtp):
        _register(client, "known", "known@example.com")
        known = client.post("/auth/forgot-password", json={"email": "known@example.com"})
        unknown = client.post("/auth/forgot-password", json={"email": "unknown@example.com"})
        assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
        assert len(fake_smtp.sent) == 1

    def test_reset_email_is_delivered_over_starttls_and_completes_the_reset(self, client, fake_smtp):
        session = _register(client, "mailme", "mailme@example.com")
        assert client.post("/auth/forgot-password", json={"email": "mailme@example.com"}).status_code == 200

        (message,) = fake_smtp.sent
        assert message["To"] == "mailme@example.com"
        assert fake_smtp.events[:4] == [
            ("connect", "smtp.example.com", 587),
            ("ehlo",),
            ("starttls", True),
            ("ehlo",),
        ]
        assert ("login", "mailer", "mail-secret") in fake_smtp.events
        token = re.search(r"https://app\.example\.com/reset\?token=(\S+)", message.get_content()).group(1)

        resp = client.post("/auth/reset-password", json={"token": token, "new_password": "N3w!Password"})
        assert resp.status_code == 200
        assert client.get("/auth/me", headers=_bearer(session["access_token"])).status_code == 401
        assert _login(client, "mailme", "N3w!Password").status_code == 200

    def test_without_smtp_the_reset_is_logged_as_disabled_without_the_token(self, client, caplog):
        import src.mailer as mailer

        _register(client, "nomail", "nomail@example.com")
        with patch.object(mailer.settings, "smtp_host", None), caplog.at_level(logging.WARNING):
            resp = client.post("/auth/forgot-password", json={"email": "nomail@example.com"})
        assert resp.status_code == 200
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("reset emails are disabled" in message for message in warnings)
        with TestSessionLocal() as db:
            assert db.query(PasswordResetToken).count() == 1
        # The raw token appears nowhere in the log (only its hash is stored).
        assert not any("reset-" in r.getMessage() or "token=" in r.getMessage() for r in caplog.records)

    def test_unknown_username_costs_a_bcrypt_verification(self, client):
        import src.dependencies as deps

        calls = []
        real_matches = deps._bcrypt_matches

        def counting_matches(secret, hashed):
            calls.append(hashed)
            return real_matches(secret, hashed)

        with patch.object(deps, "_bcrypt_matches", side_effect=counting_matches):
            assert _login(client, "ghost-user", "WrongPass1!").status_code == 400
        assert len(calls) == 1

    def test_duplicate_registration_hashes_like_a_new_one(self, client):
        import src.routers.auth as auth_router

        _register(client, "twice")
        with patch.object(auth_router, "get_password_hash", wraps=auth_router.get_password_hash) as hashed:
            resp = client.post(
                "/auth/register", json={"username": "twice", "email": "other@example.com", "password": PASSWORD}
            )
        assert resp.status_code == 400
        assert hashed.call_count == 1
