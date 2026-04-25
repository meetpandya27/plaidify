"""
Tests for system endpoints: /, /health, /status, /connect, /disconnect.
"""

from unittest.mock import AsyncMock, patch


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
        browser_pool = AsyncMock(return_value=object())

        with (
            patch("src.routers.system.settings.health_check_token", None),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["browser_pool"] == "ok"
        browser_pool.assert_awaited_once()

    def test_detailed_health_requires_valid_token_when_configured(self, client):
        browser_pool = AsyncMock(return_value=object())

        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed")

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid health check token or authentication."
        browser_pool.assert_not_awaited()

    def test_detailed_health_accepts_configured_token(self, client):
        browser_pool = AsyncMock(return_value=object())

        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get(
                "/health/detailed",
                headers={"Authorization": "Bearer health-secret"},
            )

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"
        browser_pool.assert_awaited_once()

    def test_detailed_health_accepts_authenticated_user_when_token_configured(self, client, auth_headers):
        browser_pool = AsyncMock(return_value=object())

        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"
        browser_pool.assert_awaited_once()

    def test_detailed_health_accepts_api_key_when_token_configured(self, client, auth_headers):
        create_key = client.post(
            "/api-keys",
            json={"name": "health-check"},
            headers=auth_headers,
        )
        assert create_key.status_code == 200
        api_key = create_key.json()["key"]

        browser_pool = AsyncMock(return_value=object())

        with (
            patch("src.routers.system.settings.health_check_token", "health-secret"),
            patch("src.routers.system.get_browser_pool", new=browser_pool),
        ):
            response = client.get("/health/detailed", headers={"X-API-Key": api_key})

        assert response.status_code == 200
        assert response.json()["checks"]["browser_pool"] == "ok"
        browser_pool.assert_awaited_once()


class TestConnectEndpoint:
    """Tests for the POST /connect endpoint."""

    def test_connect_internal_fixture(self, client):
        response = client.post(
            "/connect",
            json={
                "site": "internal_bank",
                "username": "test_user",
                "password": "secret123",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        assert "data" in data
        assert data["data"]["profile_status"] == "active"
        assert data["data"]["last_synced"] == "2025-04-17T12:00:00Z"

    def test_connect_public_connector(self, client):
        response = client.post(
            "/connect",
            json={
                "site": "hydro_one",
                "username": "mock_user",
                "password": "mock_password",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        assert "mock_status" in data["data"]
        assert "mock_synced" in data["data"]

    def test_connect_nonexistent_site(self, client):
        response = client.post(
            "/connect",
            json={
                "site": "nonexistent_site_xyz",
                "username": "user",
                "password": "pass",
            },
        )
        assert response.status_code == 404
        assert "error" in response.json()

    def test_connect_missing_fields(self, client):
        response = client.post("/connect", json={"site": "internal_bank"})
        assert response.status_code == 422  # Pydantic validation error

    def test_disconnect(self, client):
        response = client.post("/disconnect")
        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"
