from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_connect(auth_headers):
    response = client.post(
        "/connect",
        json={"site": "internal_bank", "username": "test_user", "password": "secret123"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "connected"
    assert "data" in data


def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert "status" in response.json()


def test_disconnect(auth_headers):
    link_token = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers).json()[
        "link_token"
    ]
    response = client.post("/disconnect", json={"link_token": link_token}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    assert response.json()["revoked_tokens"] == 0
