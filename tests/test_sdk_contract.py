"""The Python SDK against the real app, in-process.

sdk/tests pins each request the SDK sends; this module checks that the
server actually accepts those requests and that the SDK reads the replies.
It drives the SDK through httpx.ASGITransport, so every call goes through
FastAPI's routing, dependencies and models exactly as over the network.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))

from plaidify import Plaidify  # noqa: E402
from plaidify.exceptions import InvalidTokenError  # noqa: E402

from src import session_store  # noqa: E402
from src.database import AccessToken, Link, SessionLocal, User, encrypt_credential_for_user  # noqa: E402
from src.main import app  # noqa: E402

BASE = "http://plaidify.test"
PASSWORD = "Secure@pass123"


def _sdk(credential: str | None = None) -> Plaidify:
    return Plaidify(server_url=BASE, api_key=credential, transport=httpx.ASGITransport(app=app))


async def _registered() -> tuple[Plaidify, str]:
    username = f"sdk-{uuid.uuid4().hex[:10]}"
    pfy = _sdk()
    await pfy.register(username, f"{username}@example.com", PASSWORD)
    return pfy, username


def _route_accepts_body(path: str, method: str) -> bool:
    operation = app.openapi()["paths"].get(path, {}).get(method, {})
    return "requestBody" in operation


def _make_access_token(user_id: int) -> str:
    """An access token as /submit_credentials would store it."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
        link_token = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db.add(Link(link_token=link_token, site="hydro_one", user_id=user_id))
        db.add(
            AccessToken(
                token=token,
                link_token=link_token,
                username_encrypted=encrypt_credential_for_user(user, "alice"),
                password_encrypted=encrypt_credential_for_user(user, "hunter22"),
                user_id=user_id,
            )
        )
        db.commit()
        return token
    finally:
        db.close()


async def test_register_login_and_bearer_auth():
    pfy, username = await _registered()
    async with pfy:
        me = await pfy.me()
    assert me.username == username

    # A fresh client logging in with the OAuth2 password form.
    async with _sdk() as fresh:
        token = await fresh.login(username, PASSWORD)
        assert token.access_token
        assert (await fresh.me()).id == me.id


async def test_api_key_travels_in_x_api_key_and_honours_expires_days():
    pfy, _ = await _registered()
    async with pfy:
        created = await pfy.create_api_key("ci", expires_days=7)
        assert created.raw_key and created.raw_key.startswith("pk_")
        listed = {key.id: key for key in await pfy.list_api_keys()}
    assert listed[created.id].expires_at is not None

    async with _sdk(created.raw_key) as keyed:
        session = await keyed.create_link_session()
    assert session.link_token

    async with pfy:
        await pfy.revoke_api_key(created.id)
    async with _sdk(created.raw_key) as keyed:
        with pytest.raises(InvalidTokenError):
            await keyed.create_link_session()


async def test_consent_request_is_approved_by_its_request_id():
    pfy, _ = await _registered()
    async with pfy:
        me = await pfy.me()
        access_token = _make_access_token(me.id)
        request = await pfy.request_consent(access_token, ["read:current_bill"], "agent-x", duration_seconds=600)
        assert request.id.startswith("creq-")
        grant = await pfy.approve_consent(request.id)
        grants = await pfy.list_consents()
    assert grant.consent_token.startswith("consent-")
    assert [g["consent_token"] for g in grants] == [grant.consent_token]


async def test_poll_link_status_returns_the_public_token():
    pfy, _ = await _registered()
    async with pfy:
        session = await pfy.create_link_session()
        session_store.update_link_session(
            session.link_token, {"status": "completed", "public_token": "public-contract"}
        )
        finished = await pfy.poll_link_status(session.link_token, interval=0, timeout=5)
    assert finished.status == "completed"
    assert finished.public_token == "public-contract"


async def test_webhook_registration_carries_a_secret():
    pfy, _ = await _registered()
    async with pfy:
        session = await pfy.create_link_session()
        registration = await pfy.register_webhook(session.link_token, "https://example.com/hook", "whsec-1")
        hooks = await pfy.list_webhooks()
    assert registration.status == "registered"
    assert [hook["webhook_id"] for hook in hooks] == [registration.webhook_id]


async def test_mfa_submit_body_is_read_by_the_server():
    async with _sdk() as pfy:
        result = await pfy.submit_mfa("no-such-session", "123456")
    # The server parsed the body: it looked the session up and did not find it.
    assert result.status == "error"
    assert "not found" in (result.error or "")


async def test_link_flow_bodies_are_read_by_the_server():
    if not (_route_accepts_body("/submit_credentials", "post") and _route_accepts_body("/fetch_data", "post")):
        pytest.skip("/submit_credentials and POST /fetch_data take JSON bodies once the API side of SEC-02 lands")
    pfy, _ = await _registered()
    async with pfy:
        link = await pfy.create_link("hydro_one")
        creds = await pfy.submit_credentials(link.link_token, "alice", "hunter22")
        result = await pfy.fetch_data(creds.access_token)
    assert result.status == "connected"
    assert result.data


async def test_one_shot_connect_sends_the_credential_and_encrypts():
    """/connect needs an API key or a login; the SDK sends its credential with the encrypted body."""
    pfy, _ = await _registered()
    async with pfy:
        result = await pfy.connect("internal_bank", username="alice", password="hunter22")
    assert result.status == "connected"
    assert result.data

    async with _sdk() as anonymous:
        with pytest.raises(InvalidTokenError):
            await anonymous.connect("internal_bank", username="alice", password="hunter22")


async def test_api_key_scopes_round_trip_as_a_list():
    pfy, _ = await _registered()
    async with pfy:
        created = await pfy.create_api_key("scoped", scopes=["balance", "read:usage_kwh"])
        listed = {key.id: key for key in await pfy.list_api_keys()}
    assert listed[created.id].scopes == ["balance", "read:usage_kwh"]
