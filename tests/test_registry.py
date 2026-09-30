"""
Tests for the Blueprint Registry feature.
"""


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
        resp = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
                "description": "A test utility blueprint",
            },
            headers=auth_headers,
        )
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
        resp = client.post(
            "/registry/publish",
            json={
                "blueprint": "not a json object",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_publish_update_same_user(self, client, auth_headers):
        bp = _make_blueprint()
        # First publish
        resp1 = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
                "description": "v1",
            },
            headers=auth_headers,
        )
        assert resp1.status_code == 200
        assert resp1.json()["version"] == "1.0.0"

        # Update
        resp2 = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
                "description": "v2",
            },
            headers=auth_headers,
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "updated"
        assert data2["version"] == "1.0.1"

    def test_publish_blocked_for_other_user(self, client, auth_headers, second_user_headers):
        bp = _make_blueprint()
        # User 1 publishes
        resp1 = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
            },
            headers=auth_headers,
        )
        assert resp1.status_code == 200

        # User 2 tries to overwrite
        resp2 = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
            },
            headers=second_user_headers,
        )
        assert resp2.status_code == 403


# ── Search Tests ──────────────────────────────────────────────────────────────


class TestRegistrySearch:
    def _publish(self, client, headers, name="Test Utility", domain="testutil.example.com", **kw):
        bp = _make_blueprint(name=name, domain=domain, **kw)
        resp = client.post(
            "/registry/publish",
            json={
                "blueprint": bp,
                "description": kw.get("description", ""),
            },
            headers=headers,
        )
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
        self._publish(client, auth_headers, name="Tagged BP", domain="tagged.example.com", tags=["solar", "green"])

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


# ── Claimed sites, counters and paging (JOB-22) ───────────────────────────────


def _make_admin(client, headers):
    from src.database import User
    from tests.conftest import TestSessionLocal

    user_id = client.get("/auth/me", headers=headers).json()["id"]
    with TestSessionLocal() as db:
        db.get(User, user_id).is_admin = True
        db.commit()


class TestClaimedSites:
    def test_admin_can_update_and_remove_a_claimed_site(self, client, auth_headers, second_user_headers):
        bp = _make_blueprint()
        site = client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers).json()["site"]
        _make_admin(client, second_user_headers)

        updated = client.post("/registry/publish", json={"blueprint": bp}, headers=second_user_headers)
        assert updated.status_code == 200
        assert updated.json()["version"] == "1.0.1"
        # The claim stays with its publisher.
        assert client.post("/registry/publish", json={"blueprint": bp}, headers=auth_headers).status_code == 200
        assert client.delete(f"/registry/{site}", headers=second_user_headers).status_code == 200

    def test_publish_body_is_validated(self, client, auth_headers):
        assert client.post("/registry/publish", json={"blueprint": ["a"]}, headers=auth_headers).status_code == 422
        assert client.post("/registry/publish", json={"blueprint": "[]"}, headers=auth_headers).status_code == 422
        too_long = {"blueprint": _make_blueprint(), "description": "x" * 5000}
        assert client.post("/registry/publish", json=too_long, headers=auth_headers).status_code == 422
        assert client.post("/registry/publish", content=b"{", headers=auth_headers).status_code == 422


class TestCountersAndPaging:
    def test_concurrent_downloads_are_all_counted(self, client, auth_headers):
        import threading

        from fastapi.testclient import TestClient

        from src.main import app

        site = client.post("/registry/publish", json={"blueprint": _make_blueprint()}, headers=auth_headers).json()[
            "site"
        ]
        statuses = []

        def download():
            statuses.append(TestClient(app).get(f"/registry/{site}").status_code)

        threads = [threading.Thread(target=download) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert statuses == [200] * 8
        assert client.get(f"/registry/{site}").json()["downloads"] == 9

    def test_search_is_paginated(self, client, auth_headers):
        for n in range(3):
            client.post(
                "/registry/publish",
                json={"blueprint": _make_blueprint(name=f"Util {n}", domain=f"u{n}.example.com")},
                headers=auth_headers,
            )
        page = client.get("/registry/search", params={"limit": 2}).json()
        rest = client.get("/registry/search", params={"limit": 2, "offset": 2}).json()
        assert page["count"] == 2 and rest["count"] == 1
        assert {r["site"] for r in page["results"] + rest["results"]} == {
            "u0_example_com",
            "u1_example_com",
            "u2_example_com",
        }
        assert client.get("/registry/search", params={"limit": 1000}).status_code == 422
