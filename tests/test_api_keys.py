"""API keys: validated scopes and expiry, stored as JSON, and restrictions that fail closed (SEC-04)."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from src.database import ApiKey, utcnow
from tests.conftest import TestSessionLocal


def _create(client, headers, **body):
    return client.post("/api-keys", json={"name": "k", **body}, headers=headers)


class TestCreate:
    def test_scopes_are_stored_as_a_json_list(self, client, auth_headers):
        created = _create(client, auth_headers, scopes=["balance", "read:usage_kwh", "balance"])
        assert created.status_code == 200
        assert created.json()["scopes"] == ["balance", "read:usage_kwh"]
        with TestSessionLocal() as db:
            stored = db.get(ApiKey, created.json()["id"]).scopes
        assert json.loads(stored) == ["balance", "read:usage_kwh"]

        listed = client.get("/api-keys", headers=auth_headers).json()
        assert listed[0]["scopes"] == ["balance", "read:usage_kwh"]

    def test_scopes_as_a_string_are_rejected(self, client, auth_headers):
        # A string used to be stored as-is and read back as "unrestricted".
        assert _create(client, auth_headers, scopes="balance").status_code == 422
        assert _create(client, auth_headers, scopes='["balance"]').status_code == 422

    def test_unknown_scope_forms_are_rejected(self, client, auth_headers):
        for bad in (["*"], ["write:balance"], [""], [123]):
            assert _create(client, auth_headers, scopes=bad).status_code == 422, bad

    def test_expiry_is_bounded_and_misnamed_fields_are_rejected(self, client, auth_headers):
        assert _create(client, auth_headers, expires_days=0).status_code == 422
        assert _create(client, auth_headers, expires_days=100_000).status_code == 422
        assert _create(client, auth_headers, expires_days="soon").status_code == 422
        # The old SDK field name must not silently mean "never expires".
        assert _create(client, auth_headers, expires_in_days=7).status_code == 422

        created = _create(client, auth_headers, expires_days=7).json()
        with TestSessionLocal() as db:
            expires_at = db.get(ApiKey, created["id"]).expires_at
        assert timedelta(days=6, hours=23) < expires_at - utcnow() <= timedelta(days=7)

    def test_bad_bodies_are_422_not_500(self, client, auth_headers):
        assert client.post("/api-keys", content=b"not json", headers=auth_headers).status_code == 422
        assert client.post("/api-keys", json=["a", "list"], headers=auth_headers).status_code == 422
        assert _create(client, auth_headers, name="").status_code == 422

    def test_expired_key_is_refused(self, client, auth_headers):
        created = _create(client, auth_headers, expires_days=1).json()
        with TestSessionLocal() as db:
            db.get(ApiKey, created["id"]).expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert client.get("/links", headers={"X-API-Key": created["key"]}).status_code == 401


class TestRestrictionsFailClosed:
    def test_empty_scope_list_allows_no_field(self, client, auth_headers):
        key = _create(client, auth_headers, scopes=[]).json()["key"]
        resp = client.post("/create_link?site=internal_bank", json={"scopes": ["balance"]}, headers={"X-API-Key": key})
        assert resp.status_code == 403

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_unreadable_stored_scopes_allow_nothing(self, mock_connect, client, auth_headers):
        mock_connect.return_value = {"status": "connected", "data": {"balance": "$1", "ssn": "123"}}
        created = _create(client, auth_headers).json()
        key = {"X-API-Key": created["key"]}
        link_token = client.post("/create_link?site=internal_bank", headers=key).json()["link_token"]
        access_token = client.post(
            "/submit_credentials", json={"link_token": link_token, "username": "u", "password": "p"}, headers=key
        ).json()["access_token"]

        for garbage in ("balance", "", "{}", '["ok", "*"]'):
            with TestSessionLocal() as db:
                db.get(ApiKey, created["id"]).scopes = garbage
                db.commit()
            resp = client.post("/fetch_data", json={"access_token": access_token}, headers=key)
            assert resp.status_code == 200, garbage
            assert resp.json()["data"] == {}, garbage
            assert mock_connect.await_args.kwargs["extract_fields"] == [], garbage


class TestListing:
    def test_list_is_paginated(self, client, auth_headers):
        ids = [_create(client, auth_headers, name=f"k{i}").json()["id"] for i in range(3)]
        first = client.get("/api-keys", params={"limit": 2}, headers=auth_headers).json()
        rest = client.get("/api-keys", params={"limit": 2, "offset": 2}, headers=auth_headers).json()
        assert len(first) == 2 and len(rest) == 1
        assert {k["id"] for k in first + rest} == set(ids)
        assert client.get("/api-keys", params={"limit": 0}, headers=auth_headers).status_code == 422
        assert client.get("/api-keys", params={"limit": 10_000}, headers=auth_headers).status_code == 422

    def test_keys_are_stored_as_digests(self, client, auth_headers):
        from src.dependencies import hash_api_key

        created = _create(client, auth_headers).json()
        with TestSessionLocal() as db:
            row = db.get(ApiKey, created["id"])
        assert row.key_hash == hash_api_key(created["key"])
        assert created["key"] not in (row.key_hash, row.key_prefix)
