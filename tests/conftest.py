"""
Shared test fixtures and configuration.

Database: the whole suite uses ONE engine — ``src.database.engine``, built from
``DATABASE_URL`` — for request handlers (``get_db``) and for everything that
opens its own session (background jobs, audit, access jobs). Without an
explicit ``DATABASE_URL`` each run gets a fresh SQLite file in a temporary
directory; set ``DATABASE_URL=postgresql://...`` to run the suite against
PostgreSQL. Tables are created once per session and emptied after every test,
so no test depends on another having run first.
"""

import atexit
import gc
import json
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Set test environment variables BEFORE importing app modules
if not os.environ.get("DATABASE_URL"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="plaidify-tests-")
    atexit.register(shutil.rmtree, _TEST_DB_DIR, True)
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TEST_DB_DIR, "test.db")
os.environ.setdefault("ENCRYPTION_KEY", "s790nQg9kGoAVQGqXreKUbG8Q0OA-A4HASTbyd-ruuQ=")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-not-production")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_FORMAT", "text")

import src.database as _database  # noqa: E402
from src.core.llm_provider import LLMResponse, TokenUsage  # noqa: E402
from src.database import Base, get_db  # noqa: E402
from src.main import app  # noqa: E402

# ── Test Database Setup ───────────────────────────────────────────────────────

# The application's own engine and session factory: modules that imported
# SessionLocal by name hold these very objects, so requests, background work
# and tests all see the same database.
TEST_DATABASE_URL = os.environ["DATABASE_URL"]
test_engine = _database.engine
TestSessionLocal = _database.SessionLocal

# Every table is emptied after each test. Refuse a database that does not
# look disposable unless the caller says it is.
if not (
    test_engine.dialect.name == "sqlite"
    or "test" in (test_engine.url.database or "").lower()
    or os.environ.get("PLAIDIFY_TEST_DB_DISPOSABLE") == "1"
):
    raise pytest.UsageError(
        f"The test suite empties every table of DATABASE_URL ({test_engine.url.render_as_string(hide_password=True)}). "
        "Use a database whose name contains 'test', or set PLAIDIFY_TEST_DB_DISPOSABLE=1 for a throwaway database."
    )


def override_get_db():
    """Request-scoped session on the shared test engine."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


def _truncate_postgres(tables, lock_timeout: str) -> None:
    with test_engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        names = ", ".join(conn.dialect.identifier_preparer.format_table(t) for t in tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def _empty_all_tables() -> None:
    tables = list(reversed(Base.metadata.sorted_tables))
    if test_engine.dialect.name != "postgresql":
        with test_engine.begin() as conn:
            for table in tables:
                conn.execute(table.delete())
        return

    # TRUNCATE waits for every open transaction that touched these tables. A
    # test that dropped a session without closing it (``db = next(get_db())``)
    # leaves one open until the session is garbage collected.
    gc.collect()
    try:
        _truncate_postgres(tables, "5s")
    except OperationalError:
        # Still blocked: end transactions a finished test left idle. The
        # database is disposable (checked above); running statements are spared.
        with test_engine.begin() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%'"
                )
            )
        _truncate_postgres(tables, "30s")


@pytest.fixture(scope="session", autouse=True)
def _create_test_schema():
    """Build the schema fresh for the run.

    create_all alone keeps a table that already exists with an older shape,
    so a local test database left over from before a model change would fail
    on the new columns. The database is disposable (see the name guard above).
    """
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    _empty_all_tables()
    yield
    test_engine.dispose()


@pytest.fixture(autouse=True)
def setup_test_db(_create_test_schema):
    """Every test starts from empty tables and a fresh key-rotation sweep."""
    _database.reset_key_rotation_state()
    yield
    _database.reset_key_rotation_state()
    _empty_all_tables()


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
    response = client.post(
        "/auth/register",
        json={
            "username": "testuser",
            "email": "test@example.com",
            "password": "Secure@pass123",
        },
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def second_user_headers(client):
    """Register a second user and return auth headers."""
    response = client.post(
        "/auth/register",
        json={
            "username": "seconduser",
            "email": "second@example.com",
            "password": "Secure@pass456",
        },
    )
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

    known_sites = {"internal_bank", "hydro_one"}
    if site not in known_sites:
        raise BlueprintNotFoundError(site=site)
    return _MOCK_CONNECT_RESPONSE


@pytest.fixture(autouse=True)
def mock_browser_engine(request):
    """Mock connect_to_site in all routers to prevent Playwright browser launch.

    Browser-automation tests still keep this mock by default so hosted-link
    UI coverage stays deterministic. Tests that need the real connector backend
    should use:
        @pytest.mark.real_connector
    """
    marker_names = {marker.name for marker in request.node.iter_markers()}
    if "real_connector" in marker_names:
        yield
        return

    mock = AsyncMock(side_effect=_mock_connect_to_site)
    with patch("src.routers.connection.connect_to_site", mock), patch("src.routers.links.connect_to_site", mock):
        yield mock


# ── Shared LLM / Playwright Mocks ────────────────────────────────────────────
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
    page.content = AsyncMock(return_value="<html><body><div id='balance'>$1,234.56</div></body></html>")
    return page
