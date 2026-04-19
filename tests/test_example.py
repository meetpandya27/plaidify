from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_mock_connect():
    """
    Test connecting to mock_site.

    The autouse mock_browser_engine fixture mocks connect_to_site
    to return stub data without launching Playwright.
    """
    response = client.post("/connect", json={"site": "mock_site", "username": "mock_user", "password": "mock_password"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "connected"
    assert "data" in data
