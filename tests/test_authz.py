"""Tests for admin RBAC and session management."""

from unittest.mock import patch

import pytest


def _register(client, username, password="TestPass123!"):
    r = client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@plaidify.dev", "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _db_session(client):
    """Open a session on the test database (same engine the API uses)."""
    from src.database import get_db

    gen = client.app.dependency_overrides[get_db]()
    return next(gen), gen


def _make_admin(client, username):
    from src.database import User

    db, gen = _db_session(client)
    try:
        user = db.query(User).filter(User.username == username).first()
        user.is_admin = True
        db.commit()
    finally:
        gen.close()


def _user_id(client, username):
    from src.database import User

    db, gen = _db_session(client)
    try:
        return db.query(User).filter(User.username == username).first().id
    finally:
        gen.close()


# ── Admin RBAC ───────────────────────────────────────────────────────────────


class TestAdminRBAC:
    def test_new_user_is_not_admin(self, client):
        token = _register(client, "rbac_normal")["access_token"]
        r = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_admin_can_list_users(self, client):
        token = _register(client, "rbac_admin")["access_token"]
        _make_admin(client, "rbac_admin")
        r = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_admin_can_promote_user(self, client):
        admin_token = _register(client, "rbac_admin2")["access_token"]
        _make_admin(client, "rbac_admin2")
        _register(client, "rbac_target")
        target_id = _user_id(client, "rbac_target")

        r = client.post(
            f"/admin/users/{target_id}/promote",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200

        listing = client.get("/admin/users", headers={"Authorization": f"Bearer {admin_token}"}).json()
        promoted = next(u for u in listing["users"] if u["id"] == target_id)
        assert promoted["is_admin"] is True

    def test_admin_cannot_deactivate_self(self, client):
        admin_token = _register(client, "rbac_admin3")["access_token"]
        _make_admin(client, "rbac_admin3")
        admin_id = _user_id(client, "rbac_admin3")
        r = client.post(
            f"/admin/users/{admin_id}/set-active?active=false",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 400


# ── Session management ───────────────────────────────────────────────────────


class TestSessionManagement:
    def test_list_and_revoke_all_sessions(self, client):
        token = _register(client, "sess_user")["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        sessions = client.get("/auth/sessions", headers=headers).json()
        assert sessions["count"] >= 1

        revoked = client.post("/auth/sessions/revoke-all", headers=headers)
        assert revoked.status_code == 200
        assert revoked.json()["count"] >= 1

        # "Sign out everywhere" ends this very access token too.
        assert client.get("/auth/sessions", headers=headers).status_code == 401
        fresh = client.post("/auth/token", data={"username": "sess_user", "password": "TestPass123!"}).json()
        after = client.get("/auth/sessions", headers={"Authorization": f"Bearer {fresh['access_token']}"}).json()
        assert after["count"] == 1

    def test_revoke_all_invalidates_refresh_token(self, client):
        reg = _register(client, "sess_user2")
        headers = {"Authorization": f"Bearer {reg['access_token']}"}
        client.post("/auth/sessions/revoke-all", headers=headers)
        r = client.post("/auth/refresh", json={"refresh_token": reg["refresh_token"]})
        assert r.status_code == 401


# ── Bootstrap user becomes admin ─────────────────────────────────────────────


class TestBootstrapAdmin:
    def test_bootstrap_user_is_admin(self, client):
        import src.app as appmod
        from src.database import get_db

        override = appmod.app.dependency_overrides.get(get_db)
        with (
            patch.object(appmod.settings, "bootstrap_user_username", "boot_admin"),
            patch.object(appmod.settings, "bootstrap_user_email", "boot_admin@plaidify.dev"),
            patch.object(appmod.settings, "bootstrap_user_password", "BootPass123!"),
            patch.object(appmod, "get_db", override),
        ):
            appmod._bootstrap_user()

        token = client.post("/auth/token", data={"username": "boot_admin", "password": "BootPass123!"}).json()[
            "access_token"
        ]
        r = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# ── Deactivation (SEC-06) ────────────────────────────────────────────────────


def _admin_headers(client, username="deact_admin"):
    token = _register(client, username)["access_token"]
    _make_admin(client, username)
    return {"Authorization": f"Bearer {token}"}


def _set_active_in_db(client, username, active):
    from src.database import User

    db, gen = _db_session(client)
    try:
        db.query(User).filter(User.username == username).update({"is_active": active})
        db.commit()
    finally:
        gen.close()


class TestDeactivation:
    def test_deactivation_closes_every_way_in(self, client):
        admin = _admin_headers(client)
        victim = _register(client, "deact_user")
        victim_headers = {"Authorization": f"Bearer {victim['access_token']}"}
        api_key = client.post("/api-keys", json={"name": "k"}, headers=victim_headers).json()["key"]
        agent_key = client.post("/agents", json={"name": "bot"}, headers=victim_headers).json()["api_key"]

        r = client.post(f"/admin/users/{_user_id(client, 'deact_user')}/set-active?active=false", headers=admin)
        assert r.status_code == 200

        assert client.get("/auth/me", headers=victim_headers).status_code == 401
        assert (
            client.post("/auth/token", data={"username": "deact_user", "password": "TestPass123!"}).status_code == 403
        )
        assert client.post("/auth/refresh", json={"refresh_token": victim["refresh_token"]}).status_code == 401
        assert client.post("/link/sessions", headers={"X-API-Key": api_key}).status_code == 401
        assert client.post("/link/sessions", headers={"X-API-Key": agent_key}).status_code == 401

    def test_reactivation_does_not_bring_back_revoked_credentials(self, client):
        admin = _admin_headers(client)
        victim = _register(client, "deact_again")
        victim_headers = {"Authorization": f"Bearer {victim['access_token']}"}
        api_key = client.post("/api-keys", json={"name": "k"}, headers=victim_headers).json()["key"]
        victim_id = _user_id(client, "deact_again")

        client.post(f"/admin/users/{victim_id}/set-active?active=false", headers=admin)
        client.post(f"/admin/users/{victim_id}/set-active?active=true", headers=admin)

        assert (
            client.post("/auth/token", data={"username": "deact_again", "password": "TestPass123!"}).status_code == 200
        )
        assert client.get("/auth/me", headers=victim_headers).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": victim["refresh_token"]}).status_code == 401
        assert client.post("/link/sessions", headers={"X-API-Key": api_key}).status_code == 401

    def test_inactive_flag_is_enforced_on_its_own(self, client):
        """Every path checks is_active itself, not only through the revocations."""
        victim = _register(client, "flag_only")
        victim_headers = {"Authorization": f"Bearer {victim['access_token']}"}
        api_key = client.post("/api-keys", json={"name": "k"}, headers=victim_headers).json()["key"]

        _set_active_in_db(client, "flag_only", False)

        assert client.get("/auth/me", headers=victim_headers).status_code == 401
        assert client.post("/link/sessions", headers={"X-API-Key": api_key}).status_code == 401
        assert client.post("/auth/token", data={"username": "flag_only", "password": "TestPass123!"}).status_code == 403
        assert client.post("/auth/refresh", json={"refresh_token": victim["refresh_token"]}).status_code == 401

        _set_active_in_db(client, "flag_only", True)
        assert client.get("/auth/me", headers=victim_headers).status_code == 200


# ── Bootstrap never promotes someone else's account (SEC-07) ─────────────────


def _bootstrap(client, *, username, email, password="Operator!Secret1", env=None):
    import src.app as appmod
    from src.database import get_db

    override = appmod.app.dependency_overrides.get(get_db)
    patches = [
        patch.object(appmod.settings, "bootstrap_user_username", username),
        patch.object(appmod.settings, "bootstrap_user_email", email),
        patch.object(appmod.settings, "bootstrap_user_password", password),
        patch.object(appmod, "get_db", override),
    ]
    if env:
        patches.append(patch.object(appmod.settings, "env", env))
    for p in patches:
        p.start()
    try:
        appmod._bootstrap_user()
    finally:
        for p in reversed(patches):
            p.stop()


def _is_admin(client, username):
    from src.database import User

    db, gen = _db_session(client)
    try:
        user = db.query(User).filter(User.username == username).first()
        return None if user is None else bool(user.is_admin)
    finally:
        gen.close()


class TestBootstrapConflicts:
    def test_squatted_username_and_email_are_not_promoted(self, client):
        _register(client, "ops")  # the attacker gets there first, as ops@plaidify.dev
        _bootstrap(client, username="ops", email="ops@plaidify.dev")

        assert _is_admin(client, "ops") is False
        # The operator's configured password was never applied.
        assert client.post("/auth/token", data={"username": "ops", "password": "Operator!Secret1"}).status_code == 400

    def test_squatted_email_alone_is_a_conflict(self, client):
        client.post(
            "/auth/register",
            json={"username": "squatter", "email": "boss@plaidify.dev", "password": "TestPass123!"},
        )
        _bootstrap(client, username="boss", email="boss@plaidify.dev")

        assert _is_admin(client, "boss") is None  # not created
        assert _is_admin(client, "squatter") is False

    def test_conflict_stops_startup_in_production(self, client):
        _register(client, "ops2")
        with pytest.raises(RuntimeError, match="NOT promoted"):
            _bootstrap(client, username="ops2", email="ops2@plaidify.dev", env="production")
        assert _is_admin(client, "ops2") is False

    def test_existing_bootstrap_admin_is_left_alone(self, client):
        _bootstrap(client, username="root_admin", email="root_admin@plaidify.dev")
        _bootstrap(client, username="root_admin", email="root_admin@plaidify.dev", env="production")
        assert _is_admin(client, "root_admin") is True

    def test_overlong_password_is_refused(self, client):
        _bootstrap(client, username="long_pw", email="long_pw@plaidify.dev", password="Aa1!" + "x" * 70)
        assert _is_admin(client, "long_pw") is None
        with pytest.raises(RuntimeError, match="72 bytes"):
            _bootstrap(
                client, username="long_pw", email="long_pw@plaidify.dev", password="Aa1!" + "x" * 70, env="production"
            )
