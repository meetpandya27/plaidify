"""Tests for the Plaidify async client."""

import pytest
import httpx
import respx

from plaidify.client import Plaidify, _raise_for_api_error
from plaidify.models import ConnectResult, BlueprintInfo, HealthStatus, LinkResult, MFAChallenge
from plaidify.exceptions import (
    PlaidifyError,
    ConnectionError,
    BlueprintNotFoundError,
    InvalidTokenError,
    RateLimitedError,
    ServerError,
    MFARequiredError,
)


BASE = "http://test-server:8000"


# ── Error translation ─────────────────────────────────────────────────────────


class TestRaiseForApiError:
    def test_success_no_raise(self):
        r = httpx.Response(200, json={"ok": True})
        _raise_for_api_error(r)  # Should not raise

    def test_401_raises_invalid_token(self):
        r = httpx.Response(401, json={"detail": "Bad token"})
        with pytest.raises(InvalidTokenError):
            _raise_for_api_error(r)

    def test_404_raises_not_found(self):
        r = httpx.Response(404, json={"detail": "Blueprint not found: xyz"})
        with pytest.raises(BlueprintNotFoundError):
            _raise_for_api_error(r)

    def test_429_raises_rate_limited(self):
        r = httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "30"})
        with pytest.raises(RateLimitedError) as exc_info:
            _raise_for_api_error(r)
        assert exc_info.value.retry_after == 30

    def test_502_raises_connection_error(self):
        r = httpx.Response(502, json={"detail": "upstream fail"})
        with pytest.raises(ConnectionError):
            _raise_for_api_error(r)

    def test_500_raises_server_error(self):
        r = httpx.Response(500, json={"detail": "internal"})
        with pytest.raises(ServerError):
            _raise_for_api_error(r)

    def test_422_raises_generic(self):
        r = httpx.Response(422, json={"detail": "validation error"})
        with pytest.raises(PlaidifyError) as exc_info:
            _raise_for_api_error(r)
        assert exc_info.value.status_code == 422


# ── Health ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHealth:
    @respx.mock
    async def test_health_success(self):
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={
            "status": "healthy",
            "version": "0.2.0",
            "database": "connected",
        }))
        async with Plaidify(server_url=BASE) as pfy:
            h = await pfy.health()
        assert isinstance(h, HealthStatus)
        assert h.status == "healthy"
        assert h.version == "0.2.0"

    @respx.mock
    async def test_health_server_down(self):
        respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(ConnectionError):
                await pfy.health()


# ── Blueprints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBlueprints:
    @respx.mock
    async def test_list_blueprints(self):
        respx.get(f"{BASE}/blueprints").mock(return_value=httpx.Response(200, json={
            "blueprints": [
                {"site": "gg", "name": "GG", "domain": "gg.com", "tags": ["util"], "has_mfa": True, "schema_version": "2"},
                {"site": "tb", "name": "TB", "domain": "tb.com", "tags": [], "has_mfa": False, "schema_version": "2"},
            ],
            "count": 2,
        }))
        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.list_blueprints()
        assert result.count == 2
        assert result.blueprints[0].site == "gg"
        assert result.blueprints[0].has_mfa is True

    @respx.mock
    async def test_get_blueprint(self):
        respx.get(f"{BASE}/blueprints/greengrid").mock(return_value=httpx.Response(200, json={
            "name": "GreenGrid",
            "domain": "greengrid.com",
            "tags": ["energy"],
            "has_mfa": True,
            "extract_fields": ["bill", "usage"],
            "schema_version": "2",
        }))
        async with Plaidify(server_url=BASE) as pfy:
            bp = await pfy.get_blueprint("greengrid")
        assert isinstance(bp, BlueprintInfo)
        assert bp.name == "GreenGrid"
        assert bp.extract_fields == ["bill", "usage"]

    @respx.mock
    async def test_get_blueprint_not_found(self):
        respx.get(f"{BASE}/blueprints/nonexistent").mock(
            return_value=httpx.Response(404, json={"detail": "Blueprint not found: nonexistent"})
        )
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(BlueprintNotFoundError):
                await pfy.get_blueprint("nonexistent")


# ── Connect ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConnect:
    @respx.mock
    async def test_connect_success(self):
        respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={
            "status": "connected",
            "data": {"balance": 100.50, "account": "A123"},
        }))
        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.connect("test_bank", username="user", password="pass")
        assert isinstance(result, ConnectResult)
        assert result.connected is True
        assert result.data["balance"] == 100.50

    @respx.mock
    async def test_connect_with_extract_fields(self):
        route = respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={
            "status": "connected",
            "data": {"balance": 50.0},
        }))
        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.connect(
                "test_bank",
                username="user",
                password="pass",
                extract_fields=["balance"],
            )
        assert result.connected
        # Verify the request included extract_fields
        sent = route.calls[0].request
        import json
        body = json.loads(sent.content)
        assert body["extract_fields"] == ["balance"]

    @respx.mock
    async def test_connect_mfa_no_handler_raises(self):
        respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={
            "status": "mfa_required",
            "session_id": "sess-abc",
            "mfa_type": "otp",
            "metadata": {"message": "Enter OTP"},
        }))
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(MFARequiredError) as exc_info:
                await pfy.connect("bank", username="u", password="p")
            assert exc_info.value.session_id == "sess-abc"
            assert exc_info.value.mfa_type == "otp"

    @respx.mock
    async def test_connect_mfa_with_handler(self):
        # First call returns MFA required
        connect_route = respx.post(f"{BASE}/connect")
        connect_route.side_effect = [
            httpx.Response(200, json={
                "status": "mfa_required",
                "session_id": "sess-abc",
                "mfa_type": "otp",
                "metadata": {"message": "Enter OTP"},
            }),
            httpx.Response(200, json={
                "status": "connected",
                "data": {"balance": 200.0},
            }),
        ]
        # MFA submit
        respx.post(f"{BASE}/mfa/submit").mock(
            return_value=httpx.Response(200, json={
                "status": "mfa_submitted",
                "message": "Code accepted.",
            })
        )

        async def handler(challenge):
            assert challenge.mfa_type == "otp"
            return "123456"

        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.connect("bank", username="u", password="p", mfa_handler=handler)
        assert result.connected is True
        assert result.data["balance"] == 200.0

    @respx.mock
    async def test_connect_server_unreachable(self):
        respx.post(f"{BASE}/connect").mock(side_effect=httpx.ConnectError("refused"))
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(ConnectionError):
                await pfy.connect("bank", username="u", password="p")


# ── MFA ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMFA:
    @respx.mock
    async def test_submit_mfa(self):
        respx.post(f"{BASE}/mfa/submit").mock(return_value=httpx.Response(200, json={
            "status": "mfa_submitted",
            "message": "Accepted",
        }))
        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.submit_mfa("sess-1", "123456")
        assert result.status == "mfa_submitted"

    @respx.mock
    async def test_mfa_status(self):
        respx.get(f"{BASE}/mfa/status/sess-1").mock(return_value=httpx.Response(200, json={
            "session_id": "sess-1",
            "site": "bank",
            "mfa_type": "otp",
            "metadata": None,
        }))
        async with Plaidify(server_url=BASE) as pfy:
            challenge = await pfy.mfa_status("sess-1")
        assert isinstance(challenge, MFAChallenge)
        assert challenge.site == "bank"


# ── Link flow ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLinkFlow:
    @respx.mock
    async def test_create_link(self):
        respx.post(f"{BASE}/create_link").mock(
            return_value=httpx.Response(200, json={"link_token": "lt-abc"})
        )
        async with Plaidify(server_url=BASE, api_key="jwt-test") as pfy:
            link = await pfy.create_link("bank")
        assert isinstance(link, LinkResult)
        assert link.link_token == "lt-abc"
        assert link.site == "bank"

    @respx.mock
    async def test_submit_credentials(self):
        respx.post(f"{BASE}/submit_credentials").mock(
            return_value=httpx.Response(200, json={"access_token": "at-xyz"})
        )
        async with Plaidify(server_url=BASE, api_key="jwt-test") as pfy:
            link = await pfy.submit_credentials("lt-abc", "user", "pass")
        assert link.access_token == "at-xyz"

    @respx.mock
    async def test_fetch_data(self):
        respx.get(f"{BASE}/fetch_data").mock(
            return_value=httpx.Response(200, json={
                "status": "connected",
                "data": {"bill": "$50.00"},
            })
        )
        async with Plaidify(server_url=BASE, api_key="jwt-test") as pfy:
            result = await pfy.fetch_data("at-xyz")
        assert result.connected
        assert result.data["bill"] == "$50.00"


# ── Auth ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAuth:
    @respx.mock
    async def test_register(self):
        respx.post(f"{BASE}/auth/register").mock(
            return_value=httpx.Response(200, json={
                "access_token": "jwt-new",
                "token_type": "bearer",
            })
        )
        async with Plaidify(server_url=BASE) as pfy:
            token = await pfy.register("alice", "alice@example.com", "secretpass")
        assert token.access_token == "jwt-new"

    @respx.mock
    async def test_login(self):
        respx.post(f"{BASE}/auth/token").mock(
            return_value=httpx.Response(200, json={
                "access_token": "jwt-login",
                "token_type": "bearer",
            })
        )
        async with Plaidify(server_url=BASE) as pfy:
            token = await pfy.login("alice", "secretpass")
        assert token.access_token == "jwt-login"

    @respx.mock
    async def test_me(self):
        respx.get(f"{BASE}/auth/me").mock(
            return_value=httpx.Response(200, json={
                "id": 1,
                "username": "alice",
                "email": "alice@example.com",
                "is_active": True,
            })
        )
        async with Plaidify(server_url=BASE, api_key="jwt-test") as pfy:
            profile = await pfy.me()
        assert profile.id == 1
        assert profile.username == "alice"


# ── Context manager ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestContextManager:
    async def test_async_context_manager(self):
        async with Plaidify(server_url=BASE) as pfy:
            assert pfy._client is not None
        assert pfy._client is None

    async def test_manual_close(self):
        pfy = Plaidify(server_url=BASE)
        pfy._ensure_client()
        assert pfy._client is not None
        await pfy.close()
        assert pfy._client is None


# ── Config ────────────────────────────────────────────────────────────────────


class TestClientConfig:
    def test_default_config(self):
        pfy = Plaidify()
        assert pfy._config.server_url == "http://localhost:8000"
        assert pfy._config.api_key is None
        assert pfy._config.timeout == 60.0

    def test_custom_config(self):
        pfy = Plaidify(
            server_url="https://api.example.com",
            api_key="my-key",
            timeout=30.0,
            max_retries=5,
            headers={"X-Custom": "value"},
        )
        assert pfy._config.server_url == "https://api.example.com"
        assert pfy._config.api_key == "my-key"
        assert pfy._config.max_retries == 5

    def test_trailing_slash_stripped(self):
        pfy = Plaidify(server_url="http://localhost:8000/")
        assert pfy._config.server_url == "http://localhost:8000"

    def test_auth_header_included(self):
        pfy = Plaidify(api_key="my-jwt")
        headers = pfy._config.base_headers()
        assert headers["Authorization"] == "Bearer my-jwt"

    def test_no_auth_header_without_key(self):
        pfy = Plaidify()
        headers = pfy._config.base_headers()
        assert "Authorization" not in headers
