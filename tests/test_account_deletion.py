"""Tests for GDPR account deletion (DELETE /auth/me)."""

from datetime import datetime, timedelta, timezone

_PASSWORD = "Secure@pass123"


def _db(client):
    from src.database import get_db

    gen = client.app.dependency_overrides[get_db]()
    return next(gen), gen


def _user_id(client, username="testuser"):
    from src.database import User

    db, gen = _db(client)
    try:
        return db.query(User).filter(User.username == username).first().id
    finally:
        gen.close()


def _seed_owned_rows(client, user_id):
    """Insert representative owned rows across credential + token tables."""
    from src.database import AccessToken, ApiKey, Link, RefreshToken

    db, gen = _db(client)
    try:
        db.add(Link(link_token="link-del-1", site="demo", user_id=user_id))
        db.add(
            AccessToken(
                token="acc-del-1",
                link_token="link-del-1",
                username_encrypted="x",
                password_encrypted="y",
                user_id=user_id,
            )
        )
        db.add(
            RefreshToken(
                token="refresh-del-1",
                user_id=user_id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        db.add(
            ApiKey(
                id="key-del-1",
                name="k",
                key_hash="h" * 64,
                key_prefix="pfx",
                user_id=user_id,
            )
        )
        db.commit()
    finally:
        gen.close()


def _counts(client, user_id):
    from src.database import AccessToken, ApiKey, Link, RefreshToken, User

    db, gen = _db(client)
    try:
        return {
            "user": db.query(User).filter(User.id == user_id).count(),
            "links": db.query(Link).filter(Link.user_id == user_id).count(),
            "access_tokens": db.query(AccessToken).filter(AccessToken.user_id == user_id).count(),
            "refresh_tokens": db.query(RefreshToken).filter(RefreshToken.user_id == user_id).count(),
            "api_keys": db.query(ApiKey).filter(ApiKey.user_id == user_id).count(),
        }
    finally:
        gen.close()


def test_delete_account_erases_owned_data(client, auth_headers):
    uid = _user_id(client)
    _seed_owned_rows(client, uid)

    before = _counts(client, uid)
    assert before["user"] == 1
    assert before["links"] == 1
    assert before["access_tokens"] == 1
    assert before["api_keys"] == 1
    assert before["refresh_tokens"] >= 1  # registration token + seeded

    resp = client.request("DELETE", "/auth/me", json={"password": _PASSWORD}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["removed"]["links"] == 1
    assert body["removed"]["access_tokens"] == 1
    assert body["removed"]["api_keys"] == 1

    after = _counts(client, uid)
    assert after == {
        "user": 0,
        "links": 0,
        "access_tokens": 0,
        "refresh_tokens": 0,
        "api_keys": 0,
    }


def test_delete_account_preserves_audit_log(client, auth_headers):
    from src.database import AuditLog

    uid = _user_id(client)
    resp = client.request("DELETE", "/auth/me", json={"password": _PASSWORD}, headers=auth_headers)
    assert resp.status_code == 200

    db, gen = _db(client)
    try:
        entry = db.query(AuditLog).filter(AuditLog.user_id == uid, AuditLog.action == "account_deleted").first()
        assert entry is not None
    finally:
        gen.close()


def test_delete_account_requires_correct_password(client, auth_headers):
    uid = _user_id(client)

    wrong = client.request("DELETE", "/auth/me", json={"password": "WrongPass!"}, headers=auth_headers)
    assert wrong.status_code == 403

    missing = client.request("DELETE", "/auth/me", json={}, headers=auth_headers)
    assert missing.status_code == 403

    assert _counts(client, uid)["user"] == 1


def test_delete_account_requires_auth(client):
    resp = client.request("DELETE", "/auth/me", json={"password": "x"})
    assert resp.status_code == 401


def test_old_token_rejected_after_deletion(client, auth_headers):
    resp = client.request("DELETE", "/auth/me", json={"password": _PASSWORD}, headers=auth_headers)
    assert resp.status_code == 200

    # JWT signature is still valid but the user no longer exists.
    me = client.get("/auth/me", headers=auth_headers)
    assert me.status_code == 401
