"""
Tests for the Blueprint Registry feature.
"""

import json
import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_blueprint(name="Test Utility", domain="testutil.example.com", **overrides):
    """Build a minimal valid V2 blueprint dict for testing."""
    bp = {
        "schema_version": "2.0",
        "name": name,
        "domain": domain,
        "tags": overrides.pop("tags", ["utility", "test"]),
        "auth": {
            "type": "form",
            "steps": [
                {"action": "goto", "url": f"http://{domain}/login"},
                {"action": "fill", "selector": "#username", "value": "{{username}}"},
                {"action": "fill", "selector": "#password", "value": "{{password}}"},
                {"action": "click", "selector": "#login-btn", "wait_for_navigation": True},
                {"action": "wait", "selector": "#dashboard", "timeout": 5000},
            ],
        },
        "extract": {
            "balance": {"selector": "#balance", "type": "currency"},
            "account_number": {"selector": "#acct-num", "type": "text"},
        },
    }
    if overrides.get("mfa"):
        bp["mfa"] = overrides.pop("mfa")
    bp.update(overrides)
    return bp


# ── Publish Tests ─────────────────────────────────────────────────────────────


class TestRegistryPublish:
    def test_publish_blueprint(self, client, auth_headers):
        bp = _make_blueprint()
        resp = client.post("/registry/publish", json={
            "blueprint": bp,
            "description": "A test utility blueprint",
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["version"] == "1.0.0"
        assert data["quality_tier"] == "community"
        assert "site" in data

    def test_publish_requires_auth(self, client):
        bp = _make_blueprint()
        resp = client.post("/registry/publish", json={"blueprint": bp})
        assert resp.status_code == 401

    def test_publish_missing_blueprint(self, client, auth_headers):
        resp = client.post("/registry/publish", json={}, headers=auth_headers)
        assert resp.status_code == 422

    def test_publish_invalid_blueprint(self, client, auth_headers):
        resp = client.post("/registry/publish", json={
            "blueprint": "not a json object",
        }, headers=auth_headers)
        assert resp.status_code == 422

    def test_publish_update_same_user(self, client, auth_headers):
        bp = _make_blueprint()
        # First publish
        resp1 = client.post("/registry/publish", json={
            "blueprint": bp, "description": "v1",
        }, headers=auth_headers)
        assert resp1.status_code == 200
        assert resp1.json()["version"] == "1.0.0"

        # Update
        resp2 = client.post("/registry/publish", json={
            "blueprint": bp, "description": "v2",
        }, headers=auth_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "updated"
        assert data2["version"] == "1.0.1"

    def test_publish_blocked_for_other_user(self, client, auth_headers, second_user_headers):
        bp = _make_blueprint()
        # User 1 publishes
        resp1 = client.post("/registry/publish", json={
            "blueprint": bp,
        }, headers=auth_headers)
        assert resp1.status_code == 200

        # User 2 tries to overwrite
        resp2 = client.post("/registry/publish", json={
            "blueprint": bp,
        }, headers=second_user_headers)
        assert resp2.status_code == 403


# ── Search Tests ──────────────────────────────────────────────────────────────


class TestRegistrySearch:
    def _publish(self, client, headers, name="Test Utility", domain="testutil.example.com", **kw):
        bp = _make_blueprint(name=name, domain=domain, **kw)
        resp = client.post("/registry/publish", json={
            "blueprint": bp,
            "description": kw.get("description", ""),
        }, headers=headers)
        assert resp.status_code == 200
        return resp.json()

    def test_search_empty(self, client):
        resp = client.get("/registry/search")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_search_by_name(self, client, auth_headers):
        self._publish(client, auth_headers, name="Alpha Energy", domain="alpha.example.com")
        self._publish(client, auth_headers, name="Beta Water", domain="beta.example.com")

        resp = client.get("/registry/search", params={"q": "alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["results"][0]["name"] == "Alpha Energy"

    def test_search_by_tag(self, client, auth_headers):
        self._publish(client, auth_headers, name="Tagged BP", domain="tagged.example.com",
                      tags=["solar", "green"])

        resp = client.get("/registry/search", params={"tag": "solar"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

        resp2 = client.get("/registry/search", params={"tag": "nonexistent"})
        assert resp2.status_code == 200
        assert resp2.json()["count"] == 0

    def test_search_invalid_tier(self, client):
        resp = client.get("/registry/search", params={"tier": "invalid"})
        assert resp.status_code == 422

    def test_search_all(self, client, auth_headers):
        self._publish(client, auth_headers, name="First", domain="first.example.com")
        self._publish(client, auth_headers, name="Second", domain="second.example.com")
        resp = client.get("/registry/search")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2


# ── Download Tests ────────────────────────────────────────────────────────────


class TestRegistryDownload:
    def test_download_blueprint(self, client, auth_headers):
        bp = _make_blueprint()
        pub = client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers)
        site = pub.json()["site"]

        resp = client.get(f"/registry/{site}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Utility"
        assert "blueprint" in data
        assert data["blueprint"]["name"] == "Test Utility"
        assert data["downloads"] == 1

    def test_download_increments_counter(self, client, auth_headers):
        bp = _make_blueprint()
        pub = client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers)
        site = pub.json()["site"]

        client.get(f"/registry/{site}")
        client.get(f"/registry/{site}")
        resp = client.get(f"/registry/{site}")
        assert resp.json()["downloads"] == 3

    def test_download_not_found(self, client):
        resp = client.get("/registry/nonexistent_site")
        assert resp.status_code == 404


# ── Delete Tests ──────────────────────────────────────────────────────────────


class TestRegistryDelete:
    def test_delete_own_blueprint(self, client, auth_headers):
        bp = _make_blueprint()
        pub = client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers)
        site = pub.json()["site"]

        resp = client.delete(f"/registry/{site}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify it's gone
        resp2 = client.get(f"/registry/{site}")
        assert resp2.status_code == 404

    def test_delete_other_users_blueprint(self, client, auth_headers, second_user_headers):
        bp = _make_blueprint()
        pub = client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers)
        site = pub.json()["site"]

        resp = client.delete(f"/registry/{site}", headers=second_user_headers)
        assert resp.status_code == 403

    def test_delete_not_found(self, client, auth_headers):
        resp = client.delete("/registry/nonexistent_site", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_requires_auth(self, client):
        resp = client.delete("/registry/some_site")
        assert resp.status_code == 401
