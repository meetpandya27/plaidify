import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def test_connect():
    """
    Test connecting to demo_site.

    Now that the engine uses Playwright (Phase 1), connecting to demo_site
    will attempt real navigation to demo.example.com (which doesn't resolve).
    A 502 is the correct response — the engine tried and the site is unreachable.
    """
    response = client.post("/connect", json={
        "site": "demo_site",
        "username": "demo_user",
        "password": "secret123"
    })
    # Engine now attempts real Playwright navigation; demo.example.com is not reachable
    assert response.status_code == 502
    assert "error" in response.json()

def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert "status" in response.json()

def test_disconnect():
    response = client.post("/disconnect")
    assert response.status_code == 200
    assert response.json() == {"status": "disconnected"}