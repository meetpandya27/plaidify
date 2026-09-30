"""
Tests for system endpoints: /, /health, /status, /connect, /disconnect.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_HEALTHY_POOL = SimpleNamespace(is_healthy=True, _running=True)


class TestSystemEndpoints:
    """Tests for root, health, and status endpoints."""

    def test_root(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "Plaidify" in data["message"]
        assert "version" in data

    def test_health(self, client):
        """Simple public health probe returns just status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_status(self, client):
        response = client.get("/status")
        assert response.status_code == 200
        assert response.json()["status"] == "API is running"

    def test_detailed_health_is_public_when_token_unset(self, client):
        browser_pool = AsyncMock(return_value=_HEALTHY_POOL)

        with (
            patch("src.routers.system.settings.health_check_token", None),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["browser_pool"] == "ok"
        browser_pool.assert_not_awaited()

    def test_detailed_health_never_starts_the_browser_pool(self, client):
        # A probe that launched Chromium was a cheap way to make the server start browsers.
        browser_pool = AsyncMock(return_value=_HEALTHY_POOL)

        with (
            patch("src.routers.system.settings.health_check_token", None),
            patch("src.core.browser_pool._pool", None),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "not_started"
        browser_pool.assert_not_awaited()

    def test_detailed_health_reports_a_disconnected_browser(self, client):
        # A crashed Chromium used to leave health saying "ok" while every connection hung.
        with (
            patch("src.routers.system.settings.health_check_token", None),
            patch("src.core.browser_pool._pool", SimpleNamespace(is_healthy=False, _running=True)),
        ):
            response = client.get("/health/detailed")

        assert response.json()["checks"]["browser_pool"] == "disconnected"

    def test_detailed_health_reports_kms(self, client):
        """KMS provider round-trip is probed and reported healthy by default."""
        with (
            patch("src.routers.system.settings.health_check_token", None),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 200
        assert response.json()["checks"]["kms"] == "ok"

    def test_detailed_health_requires_valid_token_when_configured(self, client):
        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid health check token or authentication."

    def test_detailed_health_accepts_configured_token(self, client):
        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            response = client.get(
                "/health/detailed",
                headers={"Authorization": "Bearer health-secret"},
            )

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"

    def test_detailed_health_accepts_authenticated_user_when_token_configured(self, client, auth_headers):
        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            response = client.get("/health/detailed", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"

    def test_detailed_health_accepts_api_key_when_token_configured(self, client, auth_headers):
        create_key = client.post(
            "/api-keys",
            json={"name": "health-check"},
            headers=auth_headers,
        )
        assert create_key.status_code == 200
        api_key = create_key.json()["key"]

        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            response = client.get("/health/detailed", headers={"X-API-Key": api_key})

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"

    def test_detailed_health_is_off_in_production_without_a_token(self, client, auth_headers):
        with (
            patch("src.routers.system.settings.env", "production"),
            patch("src.routers.system.settings.health_check_token", None),
        ):
            anonymous = client.get("/health/detailed")
            logged_in = client.get("/health/detailed", headers=auth_headers)

        assert anonymous.status_code == 404
        assert logged_in.status_code == 404

    def test_detailed_health_in_production_needs_the_token(self, client):
        with (
            patch("src.routers.system.settings.env", "production"),
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.core.browser_pool._pool", _HEALTHY_POOL),
        ):
            refused = client.get("/health/detailed")
            allowed = client.get("/health/detailed", headers={"Authorization": "Bearer health-secret"})

        assert refused.status_code == 401
        assert allowed.status_code == 200

    def test_blueprint_path_stays_inside_the_connectors_directory(self, client, tmp_path):
        # A file resolving into a sibling that shares the prefix ("connectors-evil")
        # passed the old string-prefix check.
        connectors = tmp_path / "connectors"
        connectors.mkdir()
        outside = tmp_path / "connectors-evil"
        outside.mkdir()
        (outside / "escape.json").write_text("{}")
        (connectors / "escape.json").symlink_to(outside / "escape.json")
        with patch("src.routers.system.settings.connectors_dir", str(connectors)):
            assert client.get("/blueprints/escape").status_code == 400
            assert client.get("/blueprints/missing_site").status_code == 404


class TestConnectEndpoint:
    """Tests for the POST /connect endpoint."""

    def test_connect_internal_fixture(self, client, auth_headers):
        response = client.post(
            "/connect",
            json={
                "site": "internal_bank",
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        assert "data" in data
        assert data["data"]["profile_status"] == "active"
        assert data["data"]["last_synced"] == "2025-04-17T12:00:00Z"

    def test_connect_public_connector(self, client, auth_headers):
        response = client.post(
            "/connect",
            json={
                "site": "hydro_one",
                "username": "mock_user",
                "password": "mock_password",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        assert "mock_status" in data["data"]
        assert "mock_synced" in data["data"]

    def test_connect_nonexistent_site(self, client, auth_headers):
        response = client.post(
            "/connect",
            json={
                "site": "nonexistent_site_xyz",
                "username": "user",
                "password": "pass",
            },
            headers=auth_headers,
        )
        assert response.status_code == 404
        assert "error" in response.json()

    def test_connect_missing_fields(self, client, auth_headers):
        response = client.post("/connect", json={"site": "internal_bank"}, headers=auth_headers)
        assert response.status_code == 422  # neither plaintext nor encrypted credentials

    def test_connect_requires_authentication(self, client):
        response = client.post(
            "/connect",
            json={"site": "internal_bank", "username": "user", "password": "pass"},
        )
        assert response.status_code == 401

    def test_disconnect(self, client, auth_headers):
        link_token = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers).json()[
            "link_token"
        ]
        client.post(
            "/submit_credentials",
            json={"link_token": link_token, "username": "user", "password": "pass"},
            headers=auth_headers,
        )

        response = client.post("/disconnect", json={"link_token": link_token}, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"
        assert response.json()["revoked_tokens"] == 1
        assert client.get("/tokens", headers=auth_headers).json() == []

    def test_disconnect_requires_authentication(self, client):
        assert client.post("/disconnect", json={"link_token": "x"}).status_code == 401
