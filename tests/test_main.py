import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def test_connect():
    """
    Test connecting to demo_site.

    The autouse mock_browser_engine fixture mocks connect_to_site
    to return stub data without launching Playwright.
    """
    response = client.post("/connect", json={
        "site": "demo_site",
        "username": "demo_user",
        "password": "secret123"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "connected"
    assert "data" in data

def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert "status" in response.json()

def test_disconnect():
    response = client.post("/disconnect")
    assert response.status_code == 200
    assert response.json() == {"status": "disconnected"}