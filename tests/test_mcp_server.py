"""
Tests for the Plaidify MCP server (src/mcp_server.py).

Tests each MCP tool by mocking the httpx calls to the Plaidify API.
"""

import json

# Patch environment before importing
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("ENCRYPTION_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-mcp-tests")


def _mock_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.json.return_value = data
    response.text = json.dumps(data)
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=response,
        )
    return response


# ── list_available_sites ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_available_sites_returns_formatted_list():
    from src.mcp_server import list_available_sites

    mock_data = {
        "count": 2,
        "blueprints": [
            {
                "site": "hydro_one",
                "name": "GreenGrid Energy",
                "domain": "greengrid.example.com",
                "has_mfa": False,
                "tags": ["energy"],
            },
            {"site": "internal_bank", "name": "Test Bank", "domain": "testbank.com", "has_mfa": True, "tags": ["bank"]},
        ],
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await list_available_sites()

    assert "GreenGrid Energy" in result
    assert "hydro_one" in result
    assert "Test Bank" in result
    assert "[MFA]" in result
    assert "Available sites (2)" in result


@pytest.mark.asyncio
async def test_list_available_sites_empty():
    from src.mcp_server import list_available_sites

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value={"blueprints": []}):
        result = await list_available_sites()

    assert "No sites available" in result


# ── connect_site ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_site_success():
    from src.mcp_server import connect_site

    mock_data = {
        "status": "connected",
        "data": {"current_bill": "$142.57", "usage_kwh": "1,247"},
        "extraction_method": "selector",
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await connect_site("hydro_one", "user", "pass")

    assert "Connected to hydro_one" in result
    assert "$142.57" in result
    assert "2 fields" in result


@pytest.mark.asyncio
async def test_connect_site_mfa_required():
    from src.mcp_server import connect_site

    mock_data = {
        "status": "mfa_required",
        "session_id": "sess-123",
        "mfa_type": "totp",
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await connect_site("hydro_one", "user", "pass")

    assert "MFA required" in result
    assert "sess-123" in result
    assert "totp" in result


@pytest.mark.asyncio
async def test_connect_site_not_found():
    from src.mcp_server import connect_site

    resp = _mock_response({"detail": "Blueprint not found"}, 404)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await connect_site("nonexistent", "user", "pass")

    assert "not found" in result


@pytest.mark.asyncio
async def test_connect_site_rate_limited():
    from src.mcp_server import connect_site

    resp = _mock_response({"detail": "Rate limited"}, 429)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await connect_site("hydro_one", "user", "pass")

    assert "Rate limited" in result


# ── connect_utility_account ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_utility_account_success():
    from src.mcp_server import connect_utility_account

    mock_data = {
        "link_token": "lnk-abc-123",
        "link_url": "/link?token=lnk-abc-123",
        "expires_in": 1800,
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await connect_utility_account("hydro_one")

    assert "lnk-abc-123" in result
    assert "Link session created" in result
    assert "check_connection_status" in result


@pytest.mark.asyncio
async def test_connect_utility_account_unauthenticated_fallback():
    from src.mcp_server import connect_utility_account

    resp_403 = _mock_response({"detail": "Forbidden"}, 403)
    error_403 = httpx.HTTPStatusError("", request=MagicMock(), response=resp_403)

    fallback_data = {"link_token": "enc-xyz"}

    async def side_effect(method, path, **kwargs):
        if path == "/link/sessions":
            raise error_403
        return fallback_data

    with patch("src.mcp_server._api", new_callable=AsyncMock, side_effect=side_effect):
        result = await connect_utility_account("hydro_one")

    assert "enc-xyz" in result
    assert "unauthenticated mode" in result


# ── check_connection_status ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_connection_status_completed():
    from src.mcp_server import check_connection_status

    mock_data = {
        "status": "completed",
        "site": "hydro_one",
        "events": ["INSTITUTION_SELECTED", "CREDENTIALS_SUBMITTED", "CONNECTED"],
        "public_token": "pub-token-123",
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await check_connection_status("lnk-abc-123")

    assert "completed" in result
    assert "pub-token-123" in result
    assert "exchange_public_token" in result


@pytest.mark.asyncio
async def test_check_connection_status_awaiting():
    from src.mcp_server import check_connection_status

    mock_data = {"status": "awaiting_credentials", "site": "hydro_one", "events": ["INSTITUTION_SELECTED"]}

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await check_connection_status("lnk-abc-123")

    assert "awaiting_credentials" in result
    assert "hasn't entered credentials" in result


@pytest.mark.asyncio
async def test_check_connection_status_not_found():
    from src.mcp_server import check_connection_status

    resp = _mock_response({"detail": "Not found"}, 404)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await check_connection_status("bad-token")

    assert "not found" in result


# ── exchange_public_token ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_public_token_success():
    from src.mcp_server import exchange_public_token

    mock_data = {"access_token": "acc-token-456"}

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await exchange_public_token("pub-token-123")

    assert "acc-token-456" in result
    assert "fetch_data" in result


@pytest.mark.asyncio
async def test_exchange_public_token_already_used():
    from src.mcp_server import exchange_public_token

    resp = _mock_response({"detail": "Already exchanged"}, 410)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await exchange_public_token("used-token")

    assert "already been exchanged" in result


# ── fetch_data ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_data_success():
    from src.mcp_server import fetch_data

    mock_data = {
        "data": {"current_bill": "$142.57", "account_status": "Active"},
        "extraction_method": "llm_adaptive",
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await fetch_data("acc-token-456")

    assert "$142.57" in result
    assert "Active" in result


@pytest.mark.asyncio
async def test_fetch_data_with_consent_token():
    from src.mcp_server import fetch_data

    mock_data = {
        "data": {"current_bill": "$142.57"},
        "scopes_applied": ["read:current_bill"],
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data) as mock_api:
        result = await fetch_data("acc-token-456", consent_token="consent-xyz")

    assert "$142.57" in result
    assert "read:current_bill" in result
    # Verify consent_token was passed
    mock_api.assert_called_once()
    call_params = mock_api.call_args
    assert call_params.kwargs.get("params", {}).get("consent_token") == "consent-xyz" or (
        len(call_params.args) > 2 and "consent_token" in str(call_params)
    )


@pytest.mark.asyncio
async def test_fetch_data_invalid_token():
    from src.mcp_server import fetch_data

    resp = _mock_response({"detail": "Unauthorized"}, 401)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await fetch_data("bad-token")

    assert "Invalid access token" in result


# ── submit_mfa ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_mfa_success():
    from src.mcp_server import submit_mfa

    mock_data = {"status": "mfa_submitted"}

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await submit_mfa("sess-123", "123456")

    assert "submitted successfully" in result


@pytest.mark.asyncio
async def test_submit_mfa_not_found():
    from src.mcp_server import submit_mfa

    resp = _mock_response({"detail": "Not found"}, 404)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await submit_mfa("bad-sess", "123456")

    assert "not found" in result


# ── request_consent ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_consent_success():
    from src.mcp_server import request_consent

    mock_data = {
        "request_id": "cr-789",
        "status": "pending",
    }

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await request_consent(
            "acc-token-456",
            scopes=["read:current_bill", "read:usage_history"],
            agent_name="Test Agent",
            duration_seconds=7200,
        )

    assert "cr-789" in result
    assert "Test Agent" in result
    assert "read:current_bill" in result
    assert "7200" in result


# ── list_connections ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_connections_success():
    from src.mcp_server import list_connections

    mock_data = [
        {"site": "hydro_one", "link_token": "lnk-111"},
        {"site": "internal_bank", "link_token": "lnk-222"},
    ]

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=mock_data):
        result = await list_connections()

    assert "hydro_one" in result
    assert "lnk-111" in result
    assert "Active connections (2)" in result


@pytest.mark.asyncio
async def test_list_connections_empty():
    from src.mcp_server import list_connections

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=[]):
        result = await list_connections()

    assert "No active connections" in result


@pytest.mark.asyncio
async def test_list_connections_unauthenticated():
    from src.mcp_server import list_connections

    resp = _mock_response({"detail": "Unauthorized"}, 401)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=resp),
    ):
        result = await list_connections()

    assert "Authentication required" in result


# ── _format_data helper ──────────────────────────────────────────────────────


def test_format_data_dict():
    from src.mcp_server import _format_data

    result = _format_data({"bill": "$100", "status": "Active"})
    assert "bill: $100" in result
    assert "status: Active" in result


def test_format_data_nested():
    from src.mcp_server import _format_data

    result = _format_data({"history": [{"month": "Jan", "cost": "$50"}]})
    assert "history:" in result
    assert "Jan" in result


def test_format_data_list():
    from src.mcp_server import _format_data

    result = _format_data(["item1", "item2"])
    assert "item1" in result


# ── _headers helper ───────────────────────────────────────────────────────────


def test_headers_with_jwt():
    import src.mcp_server as mcp_mod
    from src.mcp_server import _headers

    original = mcp_mod.PLAIDIFY_API_KEY
    try:
        mcp_mod.PLAIDIFY_API_KEY = "eyJhbGciOi..."
        h = _headers()
        assert h["Authorization"] == "Bearer eyJhbGciOi..."
        assert "X-API-Key" not in h
    finally:
        mcp_mod.PLAIDIFY_API_KEY = original


def test_headers_with_api_key():
    import src.mcp_server as mcp_mod
    from src.mcp_server import _headers

    original = mcp_mod.PLAIDIFY_API_KEY
    try:
        mcp_mod.PLAIDIFY_API_KEY = "pk_test_abc123"
        h = _headers()
        assert h["X-API-Key"] == "pk_test_abc123"
        assert "Authorization" not in h
    finally:
        mcp_mod.PLAIDIFY_API_KEY = original


def test_headers_no_key():
    import src.mcp_server as mcp_mod
    from src.mcp_server import _headers

    original = mcp_mod.PLAIDIFY_API_KEY
    try:
        mcp_mod.PLAIDIFY_API_KEY = ""
        h = _headers()
        assert "Authorization" not in h
        assert "X-API-Key" not in h
    finally:
        mcp_mod.PLAIDIFY_API_KEY = original
