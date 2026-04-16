"""
Shared test fixtures and configuration.
"""

import os
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set test environment variables BEFORE importing app modules
os.environ.setdefault("ENCRYPTION_KEY", "s790nQg9kGoAVQGqXreKUbG8Q0OA-A4HASTbyd-ruuQ=")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-not-production")
os.environ.setdefault("DATABASE_URL", "sqlite:///test_plaidify.db")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_FORMAT", "text")

from src.database import Base, get_db
from src.main import app


# ── Test Database Setup ───────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite:///test_plaidify.db"
test_engine = create_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    """Override the database dependency with test database."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create fresh tables before each test, drop after."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Disable rate limiting by default in tests to prevent cross-test interference.

    Tests that specifically test rate limiting should re-enable it via:
        limiter.enabled = True
    """
    from limits.storage.memory import MemoryStorage
    from src.dependencies import limiter
    limiter.enabled = False
    # Replace storage with a fresh instance to guarantee no stale counters
    limiter._limiter.storage = MemoryStorage()
    yield
    limiter.enabled = False
    limiter._limiter.storage = MemoryStorage()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    """Register a user and return auth headers with a valid JWT."""
    response = client.post("/auth/register", json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "securepassword123",
    })
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def second_user_headers(client):
    """Register a second user and return auth headers."""
    response = client.post("/auth/register", json={
        "username": "seconduser",
        "email": "second@example.com",
        "password": "securepassword456",
    })
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── Mock Browser Engine ───────────────────────────────────────────────────────

_MOCK_CONNECT_RESPONSE = {
    "status": "connected",
    "data": {
        "profile_status": "active",
        "last_synced": "2025-04-17T12:00:00Z",
        "mock_status": "active",
        "mock_synced": "2025-04-17T12:00:00Z",
    },
}


async def _mock_connect_to_site(site, username=None, password=None, **kwargs):
    """Mock connect_to_site that returns stub data for known sites.

    Raises BlueprintNotFoundError for unknown sites, matching real behavior.
    """
    from src.exceptions import BlueprintNotFoundError

    known_sites = {"demo_site", "mock_site", "test_bank", "greengrid_energy",
                   "greengrid_energy_v3", "hydro_one"}
    if site not in known_sites:
        raise BlueprintNotFoundError(site=site)
    return _MOCK_CONNECT_RESPONSE


@pytest.fixture(autouse=True)
def mock_browser_engine(request):
    """Mock connect_to_site in all routers to prevent Playwright browser launch.

    Tests that need real browser automation should use:
        @pytest.mark.playwright
    """
    if "playwright" in [m.name for m in request.node.iter_markers()]:
        yield
        return

    mock = AsyncMock(side_effect=_mock_connect_to_site)
    with patch("src.routers.connection.connect_to_site", mock), \
         patch("src.routers.links.connect_to_site", mock):
        yield mock


# ── Shared LLM / Playwright Mocks ────────────────────────────────────────────


import json
from unittest.mock import AsyncMock, MagicMock

from src.core.llm_provider import LLMResponse, TokenUsage


FAKE_SCREENSHOT = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def make_llm_response(
    data: dict,
    selectors: dict | None = None,
    confidence: float = 0.9,
    **overrides,
) -> LLMResponse:
    """Create a mock LLM response with optional selector map."""
    payload: dict = {"data": data, "confidence": confidence}
    if selectors is not None:
        payload["selectors"] = selectors
    return LLMResponse(
        content=json.dumps(payload),
        model=overrides.get("model", "gpt-4o-mini"),
        usage=TokenUsage(
            prompt_tokens=overrides.get("prompt_tokens", 500),
            completion_tokens=overrides.get("completion_tokens", 200),
            total_tokens=overrides.get("total_tokens", 700),
        ),
        latency_ms=overrides.get("latency_ms", 1234.5),
        provider=overrides.get("provider", "openai"),
    )


def make_mock_llm_provider(response_data: dict) -> MagicMock:
    """Create a mock LLM provider that returns the given data as an LLMResponse.

    Args:
        response_data: The full JSON response dict (e.g. {"data": {...}, "confidence": 0.9}).
                       Serialized directly as the LLM response content.
    """
    from src.core.llm_provider import BaseLLMProvider

    provider = MagicMock(spec=BaseLLMProvider)
    provider.provider_name = "mock"
    provider.model = "mock-vision"
    provider.max_tokens = 4096
    provider.temperature = 0.0
    provider.timeout = 60.0

    response = LLMResponse(
        content=json.dumps(response_data),
        model="mock-vision",
        usage=TokenUsage(prompt_tokens=500, completion_tokens=200, total_tokens=700),
        latency_ms=1234.5,
        provider="mock",
    )
    provider._call = AsyncMock(return_value=response)
    return provider


def make_mock_playwright_page(
    url: str = "http://example.com/dashboard",
    viewport_width: int = 1280,
    viewport_height: int = 800,
) -> MagicMock:
    """Create a mock Playwright page for testing."""
    page = AsyncMock()
    page.url = url
    page.viewport_size = {"width": viewport_width, "height": viewport_height}
    page.screenshot = AsyncMock(return_value=FAKE_SCREENSHOT)
    page.set_viewport_size = AsyncMock()
    page.content = AsyncMock(
        return_value="<html><body><div id='balance'>$1,234.56</div></body></html>"
    )
    return page
