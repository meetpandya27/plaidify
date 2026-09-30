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
async def test_connect_utility_account_without_credentials_says_how_to_authenticate():
    """No more fake "unauthenticated" link: an encryption session is not a hosted-link session."""
    from src.mcp_server import connect_utility_account

    for status_code, expected in ((401, "PLAIDIFY_API_KEY"), (403, "not allowed")):
        response = _mock_response({"detail": "nope"}, status_code)
        error = httpx.HTTPStatusError("", request=MagicMock(), response=response)
        with patch("src.mcp_server._api", new_callable=AsyncMock, side_effect=error) as mock_api:
            result = await connect_utility_account("hydro_one")
        assert expected in result
        assert all(call.args[1] != "/encryption/session" for call in mock_api.call_args_list)


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
    # The tokens travel in a POST body, never in the URL.
    mock_api.assert_called_once()
    call_params = mock_api.call_args
    assert call_params.args[:2] == ("POST", "/fetch_data")
    assert call_params.kwargs["json"] == {"access_token": "acc-token-456", "consent_token": "consent-xyz"}
    assert "params" not in call_params.kwargs


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


# ── get_job_status ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_status_reports_an_mfa_prompt_and_a_rejected_code():
    from src.mcp_server import get_job_status

    job = {
        "job_id": "ajob-1",
        "site": "demo_bank",
        "status": "mfa_required",
        "mfa_type": "otp",
        "session_id": "sess-9",
        "metadata": {"message": "Enter the code", "mfa_error": "invalid_code", "attempts_remaining": 2},
    }
    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=job) as mock_api:
        result = await get_job_status("ajob-1")
    mock_api.assert_called_once_with("GET", "/access_jobs/ajob-1")
    assert "mfa_required" in result and "sess-9" in result
    assert "rejected the last code" in result and "2 attempt" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job,expected",
    [
        ({"status": "running", "mfa_state": "verifying"}, "checking it"),
        ({"status": "pending"}, "Still running"),
        ({"status": "completed", "result": {"data": {"balance": "$5"}}}, "$5"),
        ({"status": "completed", "result": None, "metadata": {"result_fields": ["balance"]}}, "balance"),
        ({"status": "mfa_timeout", "error_code": "mfa_timeout", "error_message": "MFA timeout"}, "(mfa_timeout)"),
    ],
)
async def test_get_job_status_states(job, expected):
    from src.mcp_server import get_job_status

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value={"site": "s", **job}):
        assert expected in await get_job_status("ajob-1")


@pytest.mark.asyncio
async def test_get_job_status_not_found():
    from src.mcp_server import get_job_status

    response = _mock_response({"detail": "Access job not found."}, 404)
    with patch(
        "src.mcp_server._api",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=response),
    ):
        assert "not found" in await get_job_status("nope")


@pytest.mark.asyncio
async def test_connect_site_pending_points_at_get_job_status():
    from src.mcp_server import connect_site

    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value={"status": "pending", "job_id": "ajob-7"}):
        result = await connect_site("demo_bank", "u", "p")
    assert "get_job_status('ajob-7')" in result


# ── HTTP transports ───────────────────────────────────────────────────────────


def test_main_runs_sse_on_the_configured_port(monkeypatch):
    """mcp.run(transport="sse", port=...) raised TypeError; the port goes through mcp.settings."""
    import src.mcp_server as mcp_mod

    calls = []
    monkeypatch.setattr(mcp_mod.mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(mcp_mod.mcp.settings, "host", mcp_mod.mcp.settings.host)
    monkeypatch.setattr(mcp_mod.mcp.settings, "port", mcp_mod.mcp.settings.port)
    monkeypatch.setattr(mcp_mod.mcp.settings, "transport_security", mcp_mod.mcp.settings.transport_security)
    mcp_mod.main(["--transport", "sse", "--port", "3999"])
    assert calls == [{"transport": "sse"}]
    assert (mcp_mod.mcp.settings.host, mcp_mod.mcp.settings.port) == ("127.0.0.1", 3999)

    mcp_mod.main(["--transport", "streamable-http", "--port", "4000"])
    assert calls[-1] == {"transport": "streamable-http"}


def test_a_non_loopback_bind_needs_allowed_hosts(monkeypatch):
    import src.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod.mcp, "run", lambda **kwargs: None)
    monkeypatch.setattr(mcp_mod.mcp.settings, "host", mcp_mod.mcp.settings.host)
    monkeypatch.setattr(mcp_mod.mcp.settings, "port", mcp_mod.mcp.settings.port)
    monkeypatch.setattr(mcp_mod.mcp.settings, "transport_security", mcp_mod.mcp.settings.transport_security)
    monkeypatch.delenv("PLAIDIFY_MCP_ALLOWED_HOSTS", raising=False)
    with pytest.raises(SystemExit):
        mcp_mod.main(["--transport", "sse", "--host", "0.0.0.0"])
    mcp_mod.main(["--transport", "sse", "--host", "0.0.0.0", "--allowed-host", "mcp.internal"])
    assert mcp_mod.mcp.settings.transport_security.allowed_hosts == ["mcp.internal:*"]


@pytest.mark.asyncio
async def test_the_sse_transport_serves_an_event_stream():
    """Start the real SSE app on an ephemeral port and open /sse."""
    import asyncio

    import uvicorn
    from sse_starlette.sse import AppStatus

    import src.mcp_server as mcp_mod

    # sse-starlette's process-wide exit event is bound to the first loop that streamed.
    AppStatus.should_exit_event = None
    AppStatus.should_exit = False
    mcp_mod.configure_http_transport("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(mcp_mod.mcp.sse_app(), host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(timeout=5) as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/sse") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        assert line.strip() == "event: endpoint"
                        break
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_check_connection_status_explains_an_error_and_an_exit():
    from src.mcp_server import check_connection_status

    failed = {"status": "error", "site": "demo_bank", "error_message": "MFA timeout", "error_code": "mfa_timeout"}
    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value=failed):
        assert "Error (mfa_timeout): MFA timeout" in await check_connection_status("lt")
    with patch("src.mcp_server._api", new_callable=AsyncMock, return_value={"status": "exited", "site": "s"}):
        assert "closed the link" in await check_connection_status("lt")
