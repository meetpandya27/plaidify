"""Tests for admin RBAC and session management."""

from unittest.mock import patch


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

        after = client.get("/auth/sessions", headers=headers).json()
        assert after["count"] == 0

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
