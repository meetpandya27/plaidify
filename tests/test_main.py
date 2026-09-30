from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_connect(auth_headers):
    """
    Test connecting to internal_bank.

    The autouse mock_browser_engine fixture mocks connect_to_site
    to return stub data without launching Playwright.
    """
    response = client.post(
        "/connect",
        json={"site": "internal_bank", "username": "test_user", "password": "secret123"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "connected"
    assert "data" in data


def test_connect_requires_authentication():
    response = client.post("/connect", json={"site": "internal_bank", "username": "test_user", "password": "secret123"})
    assert response.status_code == 401


def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert "status" in response.json()


def test_disconnect_unknown_link(auth_headers):
    response = client.post("/disconnect", json={"link_token": "no-such-link"}, headers=auth_headers)
    assert response.status_code == 404
