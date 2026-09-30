"""
Tests for JWT refresh token rotation (Issue #11).
"""

import json
import threading
from datetime import datetime, timedelta, timezone


class TestTokenResponseIncludesRefresh:
    """Verify that register, login, and oauth2 return refresh tokens."""

    def test_register_returns_refresh_token(self, client):
        response = client.post(
            "/auth/register",
            json={
                "username": "refreshuser",
                "email": "refresh@example.com",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert len(data["refresh_token"]) > 20

    def test_login_returns_refresh_token(self, client):
        # Register first
        client.post(
            "/auth/register",
            json={
                "username": "loginrefresh",
                "email": "loginrefresh@example.com",
                "password": "Strong@pass123",
            },
        )
        # Login
        response = client.post(
            "/auth/token",
            data={
                "username": "loginrefresh",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "refresh_token" in data
        assert len(data["refresh_token"]) > 20

    def test_oauth2_disabled_returns_403(self, client):
        """OAuth2 social login is disabled by default (OAUTH_ENABLED=false)."""
        response = client.post(
            "/auth/oauth2",
            json={
                "provider": "google",
                "oauth_token": "fake-google-token-12345678",
            },
        )
        assert response.status_code == 403


class TestRefreshEndpoint:
    """Tests for POST /auth/refresh."""

    def _register_and_get_tokens(self, client, username="refreshtest"):
        response = client.post(
            "/auth/register",
            json={
                "username": username,
                "email": f"{username}@example.com",
                "password": "Strong@pass123",
            },
        )
        return response.json()

    def test_refresh_success(self, client):
        tokens = self._register_and_get_tokens(client)
        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": tokens["refresh_token"],
            },
        )
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
        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": old_refresh,
            },
        )
        assert response.status_code == 200

        # Try to reuse the old refresh token — should fail
        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": old_refresh,
            },
        )
        assert response.status_code == 401
        assert "Invalid or revoked" in response.json()["detail"]

    def test_refresh_invalid_token(self, client):
        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": "completely-invalid-token",
            },
        )
        assert response.status_code == 401

    def test_refresh_expired_token(self, client):
        """Expired refresh tokens should be rejected."""
        tokens = self._register_and_get_tokens(client, "expireduser")

        # Manually expire the token in the database
        from src.database import RefreshToken, hash_refresh_token
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            rt = db.query(RefreshToken).filter_by(token_hash=hash_refresh_token(tokens["refresh_token"])).first()
            rt.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_presenting_an_expired_token_twice_keeps_other_sessions(self, client):
        """An expired token is just expired: retrying it must not look like theft."""
        from src.database import RefreshToken, hash_refresh_token
        from tests.conftest import TestSessionLocal

        tokens = self._register_and_get_tokens(client, "expiredtwice")
        other = client.post("/auth/token", data={"username": "expiredtwice", "password": "Strong@pass123"}).json()

        db = TestSessionLocal()
        try:
            rt = db.query(RefreshToken).filter_by(token_hash=hash_refresh_token(tokens["refresh_token"])).one()
            rt.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        for _ in range(2):
            response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
            assert response.status_code == 401
            assert "expired" in response.json()["detail"].lower()

        assert client.post("/auth/refresh", json={"refresh_token": other["refresh_token"]}).status_code == 200

    def test_new_access_token_works(self, client):
        """The new access token from refresh should authenticate requests."""
        tokens = self._register_and_get_tokens(client, "newtokenuser")

        # Refresh
        response = client.post(
            "/auth/refresh",
            json={
                "refresh_token": tokens["refresh_token"],
            },
        )
        new_tokens = response.json()

        # Use the new access token
        headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
        response = client.get("/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["username"] == "newtokenuser"

    def test_refresh_chain(self, client):
        """Can chain multiple refresh operations."""
        tokens = self._register_and_get_tokens(client, "chainuser")

        for _ in range(3):
            response = client.post(
                "/auth/refresh",
                json={
                    "refresh_token": tokens["refresh_token"],
                },
            )
            assert response.status_code == 200
            tokens = response.json()
            assert "access_token" in tokens
            assert "refresh_token" in tokens


class TestRefreshTokenStorage:
    """SEC-15: only a hash of each refresh token is stored."""

    def test_raw_token_is_not_stored(self, client):
        from src.database import RefreshToken, hash_refresh_token
        from tests.conftest import TestSessionLocal

        raw = client.post(
            "/auth/register",
            json={"username": "hashstore", "email": "hashstore@example.com", "password": "Strong@pass123"},
        ).json()["refresh_token"]

        db = TestSessionLocal()
        try:
            rows = db.query(RefreshToken).all()
            assert len(rows) == 1
            assert rows[0].token_hash == hash_refresh_token(raw)
            assert rows[0].token_hash != raw
            assert not hasattr(rows[0], "token")
        finally:
            db.close()

    def test_constructor_hashes_a_raw_token(self):
        from src.database import RefreshToken, hash_refresh_token

        row = RefreshToken(token="raw-value", user_id=1, expires_at=datetime.now(timezone.utc))
        assert row.token_hash == hash_refresh_token("raw-value")

    def test_a_stored_hash_is_not_a_usable_refresh_token(self, client):
        from src.database import RefreshToken
        from tests.conftest import TestSessionLocal

        client.post(
            "/auth/register",
            json={"username": "hashreplay", "email": "hashreplay@example.com", "password": "Strong@pass123"},
        )
        db = TestSessionLocal()
        try:
            stored = db.query(RefreshToken).one().token_hash
        finally:
            db.close()
        assert client.post("/auth/refresh", json={"refresh_token": stored}).status_code == 401


class TestRefreshTokenReuse:
    """SEC-15: rotation is atomic and reuse of a rotated token revokes the whole family."""

    def _register(self, client, username):
        return client.post(
            "/auth/register",
            json={"username": username, "email": f"{username}@example.com", "password": "Strong@pass123"},
        ).json()

    def _active_refresh_tokens(self, username):
        from src.database import RefreshToken, User
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = db.query(User).filter_by(username=username).one()
            return db.query(RefreshToken).filter_by(user_id=user.id, revoked=False).count()
        finally:
            db.close()

    def test_reusing_a_rotated_token_revokes_every_session(self, client):
        first = self._register(client, "familyuser")
        second = client.post("/auth/token", data={"username": "familyuser", "password": "Strong@pass123"}).json()

        rotated = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert rotated.status_code == 200
        assert self._active_refresh_tokens("familyuser") == 2  # second + the rotated one

        # The old token shows up again: someone else has a copy of it.
        replay = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert replay.status_code == 401
        assert "Invalid or revoked" in replay.json()["detail"]
        assert self._active_refresh_tokens("familyuser") == 0

        for token in (second["refresh_token"], rotated.json()["refresh_token"]):
            assert client.post("/auth/refresh", json={"refresh_token": token}).status_code == 401

    def test_reuse_is_audited(self, client):
        from src.database import AuditLog
        from tests.conftest import TestSessionLocal

        tokens = self._register(client, "reuseaudit")
        client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

        db = TestSessionLocal()
        try:
            entry = db.query(AuditLog).filter_by(action="refresh_token_reuse").one()
            assert json.loads(entry.metadata_json)["revoked_count"] == 1
        finally:
            db.close()

    def test_unknown_token_revokes_nothing(self, client):
        self._register(client, "unknowntok")
        assert client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"}).status_code == 401
        assert self._active_refresh_tokens("unknowntok") == 1

    def test_concurrent_refreshes_mint_at_most_one_session(self, client):
        """One token presented by many requests at once yields exactly one new pair."""
        from src.database import RefreshToken, User
        from tests.conftest import TestSessionLocal

        tokens = self._register(client, "racer")
        workers = 8
        barrier = threading.Barrier(workers)
        statuses: list[int] = []
        lock = threading.Lock()

        def refresh():
            barrier.wait()
            response = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
            with lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=refresh) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert sorted(statuses) == [200] + [401] * (workers - 1)

        db = TestSessionLocal()
        try:
            user = db.query(User).filter_by(username="racer").one()
            issued = db.query(RefreshToken).filter_by(user_id=user.id).count()
            # The registration token plus exactly one successor — never one per request.
            assert issued == 2
            # The losers presented an already-rotated token, which revokes the family.
            assert db.query(RefreshToken).filter_by(user_id=user.id, revoked=False).count() == 0
        finally:
            db.close()


class TestShortAccessTokenExpiry:
    """Verify access tokens have short expiry times."""

    def test_access_token_default_expiry(self, client):
        """Access tokens should have a short default expiry (15 minutes)."""
        import jwt as pyjwt

        from src.auth_utils import ACCESS_TOKEN_AUDIENCE
        from src.config import get_settings

        response = client.post(
            "/auth/register",
            json={
                "username": "expiryuser",
                "email": "expiry@example.com",
                "password": "Strong@pass123",
            },
        )
        token = response.json()["access_token"]

        settings = get_settings()
        payload = pyjwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm], audience=ACCESS_TOKEN_AUDIENCE
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)

        # Should expire within ~15 minutes (with some tolerance)
        diff_minutes = (exp - now).total_seconds() / 60
        assert diff_minutes <= 16  # 15 min + 1 min tolerance
        assert diff_minutes >= 13  # at least 13 min
