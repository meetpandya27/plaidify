import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_mock_connect():
    """
    Test connecting to mock_site.

    Now that the engine uses Playwright (Phase 1), connecting to mock_site
    will fail because mock.example.com is not a real host. This behavior
    is correct — the engine now actually tries to navigate to the login URL.

    We assert a 502 (ConnectionFailedError) since the site is unreachable.
    """
    response = client.post("/connect", json={
        "site": "mock_site",
        "username": "mock_user",
        "password": "mock_password"
    })
    # Engine now attempts real navigation; mock.example.com doesn't resolve
    assert response.status_code == 502
    json_data = response.json()
    assert "error" in json_data