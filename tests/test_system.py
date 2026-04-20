"""
Tests for system endpoints: /, /health, /status, /connect, /disconnect.
"""


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
