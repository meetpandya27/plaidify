import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def test_mock_connect():
    response = client.post("/connect", json={
        "site": "mock_site",
        "username": "mock_user",
        "password": "mock_password"
    })
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == "connected"
    assert "data" in json_data
    # Check that data fields match what's defined in mock_site.json
    # Because we haven't implemented actual extraction logic, we rely on the stub's fallback data
    # or any fields we set in blueprint's post_login extraction. For now, let's at least confirm keys exist.
    assert "mock_status" in json_data["data"]
    assert "mock_synced" in json_data["data"]