import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def test_connect():
    response = client.post("/connect", json={
        "site": "demo_site",
        "username": "demo_user",
        "password": "secret123"
    })
    assert response.status_code == 200
    assert response.json() == {
        "status": "connected",
        "data": {
            "profile_status": "active",
            "last_synced": "2025-04-17T12:00:00Z"
        }
    }

def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert "status" in response.json()

def test_disconnect():
    response = client.post("/disconnect")
    assert response.status_code == 200
    assert response.json() == {"status": "disconnected"}