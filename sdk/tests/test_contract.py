"""Request-shape contract tests for the Python SDK.

Each test pins the exact request the SDK sends — method, path, query,
auth header, body — to what the server route reads (src/routers/*.py),
and parses a response shaped like the server's real reply.
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from _support import decrypt_credential, mock_encryption_session
from plaidify.client import Plaidify
from plaidify.exceptions import BlueprintNotFoundError, NotFoundError, PlaidifyError
from plaidify.models import RegistrationPending

BASE = "http://test-server:8000"


def _json(request: httpx.Request) -> dict:
    assert request.headers["content-type"] == "application/json"
    return json.loads(request.content)


def _assert_no_secrets_in_url(request: httpx.Request, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in str(request.url)


# ── Authentication ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAuthHeaders:
    @respx.mock
    async def test_api_key_is_sent_in_x_api_key(self):
        route = respx.post(f"{BASE}/link/sessions").mock(
            return_value=httpx.Response(200, json={"link_token": "lt-1", "link_url": "/link?token=lt-1"})
        )
        async with Plaidify(server_url=BASE, api_key="pk_live_abc") as pfy:
            await pfy.create_link_session()
        request = route.calls[0].request
        assert request.headers["x-api-key"] == "pk_live_abc"
        assert "authorization" not in request.headers

    @respx.mock
    async def test_agent_key_is_sent_in_x_api_key(self):
        route = respx.get(f"{BASE}/access_jobs").mock(return_value=httpx.Response(200, json={"jobs": [], "count": 0}))
        async with Plaidify(server_url=BASE, api_key="pk_agent_abc") as pfy:
            await pfy.list_access_jobs()
        assert route.calls[0].request.headers["x-api-key"] == "pk_agent_abc"

    @respx.mock
    async def test_user_token_is_sent_as_bearer(self):
        route = respx.get(f"{BASE}/auth/me").mock(
            return_value=httpx.Response(200, json={"id": 1, "username": "a", "email": None, "is_active": True})
        )
        async with Plaidify(server_url=BASE, api_key="eyJhbGciOi.jwt") as pfy:
            await pfy.me()
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer eyJhbGciOi.jwt"
        assert "x-api-key" not in request.headers

    @respx.mock
    async def test_login_posts_the_password_form_and_switches_to_bearer(self):
        login = respx.post(f"{BASE}/auth/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "jwt-1", "refresh_token": "r", "token_type": "bearer"}
            )
        )
        me = respx.get(f"{BASE}/auth/me").mock(
            return_value=httpx.Response(200, json={"id": 1, "username": "alice", "email": None, "is_active": True})
        )
        # Started with an API key: after login the user token replaces it.
        async with Plaidify(server_url=BASE, api_key="pk_old") as pfy:
            await pfy.login("alice", "p@ss word&=")
            await pfy.me()

        request = login.calls[0].request
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert parse_qs(request.content.decode()) == {"username": ["alice"], "password": ["p@ss word&="]}
        _assert_no_secrets_in_url(request, "p@ss")
        me_request = me.calls[0].request
        assert me_request.headers["authorization"] == "Bearer jwt-1"
        assert "x-api-key" not in me_request.headers

    @respx.mock
    async def test_register_sends_username_email_password(self):
        route = respx.post(f"{BASE}/auth/register").mock(
            return_value=httpx.Response(200, json={"access_token": "jwt-new", "token_type": "bearer"})
        )
        async with Plaidify(server_url=BASE) as pfy:
            await pfy.register("alice", "alice@example.com", "Secure@pass123")
        assert _json(route.calls[0].request) == {
            "username": "alice",
            "email": "alice@example.com",
            "password": "Secure@pass123",
        }

    @respx.mock
    async def test_a_verification_reply_to_register_sets_no_credential(self):
        respx.post(f"{BASE}/auth/register").mock(
            return_value=httpx.Response(
                202,
                json={"status": "verification_sent", "detail": "If the address can be used, we sent it a link."},
            )
        )
        me = respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(401, json={"detail": "Not authenticated"}))
        async with Plaidify(server_url=BASE) as pfy:
            assert isinstance(await pfy.register("alice", "alice@example.com", "Secure@pass123"), RegistrationPending)
            with pytest.raises(PlaidifyError):
                await pfy.me()
        assert "authorization" not in me.calls[0].request.headers

    @respx.mock
    async def test_verify_email_sends_the_token_in_the_body_and_keeps_the_new_token(self):
        verify = respx.post(f"{BASE}/auth/verify-email").mock(
            return_value=httpx.Response(
                200, json={"access_token": "jwt-2", "refresh_token": "r-2", "token_type": "bearer"}
            )
        )
        me = respx.get(f"{BASE}/auth/me").mock(
            return_value=httpx.Response(200, json={"id": 1, "username": "alice", "email": None, "is_active": True})
        )
        async with Plaidify(server_url=BASE) as pfy:
            await pfy.verify_email("mailed-token-1")
            await pfy.me()
        request = verify.calls[0].request
        assert _json(request) == {"token": "mailed-token-1"}
        _assert_no_secrets_in_url(request, "mailed-token-1")
        assert me.calls[0].request.headers["authorization"] == "Bearer jwt-2"


# ── Connect ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConnectEncryption:
    @respx.mock
    async def test_credentials_are_encrypted_to_the_session_key(self):
        mock_encryption_session(BASE, link_token="enc-lt-1")
        route = respx.post(f"{BASE}/connect").mock(
            return_value=httpx.Response(200, json={"status": "connected", "job_id": "ajob-1", "data": {}})
        )
        async with Plaidify(server_url=BASE) as pfy:
            await pfy.connect("hydro_one", username="alice", password="hunter22")

        body = _json(route.calls[0].request)
        assert set(body) == {"site", "link_token", "encrypted_username", "encrypted_password"}
        assert body["site"] == "hydro_one"
        assert body["link_token"] == "enc-lt-1"
        assert decrypt_credential(body["encrypted_username"]) == "alice"
        assert decrypt_credential(body["encrypted_password"]) == "hunter22"

    @respx.mock
    async def test_no_silent_plaintext_fallback(self):
        respx.post(f"{BASE}/encryption/session").mock(return_value=httpx.Response(503, json={"detail": "down"}))
        connect = respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={"status": "connected"}))
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(PlaidifyError):
                await pfy.connect("hydro_one", username="alice", password="hunter22")
        assert not connect.called

    @respx.mock
    async def test_plaintext_only_when_asked(self):
        route = respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={"status": "connected"}))
        async with Plaidify(server_url=BASE) as pfy:
            await pfy.connect("hydro_one", username="alice", password="hunter22", encrypt=False)
        assert _json(route.calls[0].request) == {"site": "hydro_one", "username": "alice", "password": "hunter22"}

    @respx.mock
    async def test_unknown_site_is_a_missing_blueprint(self):
        mock_encryption_session(BASE)
        respx.post(f"{BASE}/connect").mock(
            return_value=httpx.Response(404, json={"error": "Blueprint not found", "error_code": "BLUEPRINT_NOT_FOUND"})
        )
        async with Plaidify(server_url=BASE) as pfy:
            with pytest.raises(BlueprintNotFoundError) as exc_info:
                await pfy.connect("nope", username="u", password="p")
        assert exc_info.value.site == "nope"

    @respx.mock
    async def test_submit_mfa_sends_a_json_body(self):
        route = respx.post(f"{BASE}/mfa/submit").mock(
            return_value=httpx.Response(200, json={"status": "error", "error": "MFA session not found or expired."})
        )
        async with Plaidify(server_url=BASE) as pfy:
            result = await pfy.submit_mfa("sess-1", "123456")
        request = route.calls[0].request
        assert _json(request) == {"session_id": "sess-1", "code": "123456"}
        _assert_no_secrets_in_url(request, "123456", "sess-1")
        assert result.status == "error"
        assert result.error == "MFA session not found or expired."


# ── Link flow ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLinkFlowContract:
    @respx.mock
    async def test_submit_credentials_sends_a_json_body(self):
        route = respx.post(f"{BASE}/submit_credentials").mock(
            return_value=httpx.Response(200, json={"access_token": "at-1"})
        )
        async with Plaidify(server_url=BASE, api_key="pk_key") as pfy:
            link = await pfy.submit_credentials("lt-1", "alice", "hunter22")
        request = route.calls[0].request
        assert request.url.params == httpx.QueryParams()
        _assert_no_secrets_in_url(request, "hunter22", "alice")
        assert _json(request) == {"link_token": "lt-1", "username": "alice", "password": "hunter22"}
        assert link.access_token == "at-1"

    @respx.mock
    async def test_fetch_data_posts_a_json_body(self):
        route = respx.post(f"{BASE}/fetch_data").mock(
            return_value=httpx.Response(200, json={"status": "connected", "job_id": "ajob-7", "data": {"bill": 1}})
        )
        async with Plaidify(server_url=BASE, api_key="pk_key") as pfy:
            result = await pfy.fetch_data("at-1", consent_token="consent-1")
        request = route.calls[0].request
        _assert_no_secrets_in_url(request, "at-1", "consent-1")
        assert _json(request) == {"access_token": "at-1", "consent_token": "consent-1"}
        assert result.data == {"bill": 1}
        assert result.job_id == "ajob-7"

    @respx.mock
    async def test_fetch_data_without_consent(self):
        route = respx.post(f"{BASE}/fetch_data").mock(return_value=httpx.Response(200, json={"status": "connected"}))
        async with Plaidify(server_url=BASE, api_key="pk_key") as pfy:
            await pfy.fetch_data("at-1")
        assert _json(route.calls[0].request) == {"access_token": "at-1"}

    @respx.mock
    async def test_create_link_sends_scopes_in_the_body(self):
        route = respx.post(f"{BASE}/create_link").mock(
            return_value=httpx.Response(200, json={"link_token": "lt-1", "public_key": "pem"})
        )
        async with Plaidify(server_url=BASE, api_key="pk_key") as pfy:
            await pfy.create_link("hydro_one", scopes=["read:current_bill"])
        request = route.calls[0].request
        assert request.url.params["site"] == "hydro_one"
        assert _json(request) == {"scopes": ["read:current_bill"]}

    @respx.mock
    async def test_poll_link_status_keeps_the_public_token(self):
        respx.get(f"{BASE}/link/sessions/lt-1/status").mock(
            side_effect=[
                httpx.Response(200, json={"status": "connecting", "site": "hydro_one", "events": ["OPEN"]}),
                httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "site": "hydro_one",
                        "events": ["OPEN", "CONNECTED"],
                        "public_token": "public-123",
                    },
                ),
            ]
        )
        async with Plaidify(server_url=BASE) as pfy:
            session = await pfy.poll_link_status("lt-1", interval=0, timeout=5)
        assert session.status == "completed"
        assert session.public_token == "public-123"
        assert session.events == ["OPEN", "CONNECTED"]

    @respx.mock
    async def test_poll_link_status_stops_when_the_user_exits(self):
        respx.get(f"{BASE}/link/sessions/lt-1/status").mock(
            return_value=httpx.Response(200, json={"status": "exited", "events": ["OPEN", "EXIT"]})
        )
        async with Plaidify(server_url=BASE) as pfy:
            session = await pfy.poll_link_status("lt-1", interval=0, timeout=5)
        assert session.status == "exited"
        assert session.public_token is None

    @respx.mock
    async def test_register_webhook(self):
        route = respx.post(f"{BASE}/webhooks/register").mock(
            return_value=httpx.Response(200, json={"webhook_id": "wh-1", "status": "registered"})
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            reg = await pfy.register_webhook("lt-1", "https://example.com/hook", "whsec")
        assert _json(route.calls[0].request) == {
            "link_token": "lt-1",
            "url": "https://example.com/hook",
            "secret": "whsec",
        }
        assert reg.webhook_id == "wh-1"


# ── API keys ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestApiKeysContract:
    @respx.mock
    async def test_create_api_key_sends_expires_days_and_a_scope_list(self):
        route = respx.post(f"{BASE}/api-keys").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "key-1",
                    "name": "ci",
                    "key": "pk_raw",
                    "key_prefix": "pk_raw",
                    "expires_at": "2026-10-29T00:00:00+00:00",
                    "created_at": "2026-09-29T00:00:00+00:00",
                },
            )
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            key = await pfy.create_api_key("ci", scopes=["read:current_bill"], expires_days=30)
        assert _json(route.calls[0].request) == {
            "name": "ci",
            "scopes": ["read:current_bill"],
            "expires_days": 30,
        }
        assert key.raw_key == "pk_raw"
        assert key.expires_at == "2026-10-29T00:00:00+00:00"

    async def test_scopes_must_be_a_list(self):
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            with pytest.raises(TypeError):
                await pfy.create_api_key("ci", scopes="read:current_bill")  # type: ignore[arg-type]

    @respx.mock
    async def test_list_api_keys_reads_the_bare_list(self):
        respx.get(f"{BASE}/api-keys").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "key-1",
                        "name": "scoped",
                        "key_prefix": "pk_1",
                        "scopes": '["read:current_bill"]',
                        "expires_at": None,
                        "last_used_at": None,
                        "created_at": None,
                    },
                    {
                        "id": "key-2",
                        "name": "open",
                        "key_prefix": "pk_2",
                        "scopes": None,
                        "expires_at": None,
                        "last_used_at": None,
                        "created_at": None,
                    },
                ],
            )
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            keys = await pfy.list_api_keys()
        assert [k.id for k in keys] == ["key-1", "key-2"]
        assert keys[0].scopes == ["read:current_bill"]
        assert keys[1].scopes is None

    @respx.mock
    async def test_revoking_a_missing_key_is_not_a_blueprint_error(self):
        respx.delete(f"{BASE}/api-keys/key-x").mock(
            return_value=httpx.Response(404, json={"detail": "API key not found."})
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            with pytest.raises(NotFoundError) as exc_info:
                await pfy.revoke_api_key("key-x")
        assert not isinstance(exc_info.value, BlueprintNotFoundError)


# ── Consent ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConsentContract:
    @respx.mock
    async def test_request_consent_reads_request_id(self):
        route = respx.post(f"{BASE}/consent/request").mock(
            return_value=httpx.Response(
                200,
                json={
                    "request_id": "creq-1",
                    "agent_name": "agent-x",
                    "scopes": ["read:current_bill"],
                    "duration_seconds": 600,
                    "status": "pending",
                },
            )
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            req = await pfy.request_consent("at-1", ["read:current_bill"], "agent-x", duration_seconds=600)
        assert _json(route.calls[0].request) == {
            "access_token": "at-1",
            "scopes": ["read:current_bill"],
            "agent_name": "agent-x",
            "duration_seconds": 600,
        }
        assert req.id == "creq-1"
        assert req.duration_seconds == 600

    @respx.mock
    async def test_approve_consent_uses_the_request_id(self):
        route = respx.post(f"{BASE}/consent/creq-1/approve").mock(
            return_value=httpx.Response(
                200,
                json={
                    "consent_token": "consent-1",
                    "scopes": ["read:current_bill"],
                    "expires_at": "2026-09-29T01:00:00+00:00",
                    "status": "approved",
                },
            )
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            grant = await pfy.approve_consent("creq-1")
        assert route.called
        assert grant.consent_token == "consent-1"

    @respx.mock
    async def test_list_consents(self):
        respx.get(f"{BASE}/consent").mock(
            return_value=httpx.Response(200, json={"grants": [{"consent_token": "c"}], "count": 1})
        )
        async with Plaidify(server_url=BASE, api_key="jwt") as pfy:
            grants = await pfy.list_consents()
        assert grants == [{"consent_token": "c"}]
