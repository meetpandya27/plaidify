"""Who may call /connect (JOB-04), per-attempt encryption keys (LNK-02), key generation (JOB-20)."""

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src import session_store
from src.crypto import get_public_key
from src.database import AccessJob, AccessToken, Link, User, decrypt_credential_for_user, utcnow
from src.exceptions import AuthenticationError
from tests.conftest import TestSessionLocal

_CONNECTED = {"status": "connected", "data": {"balance": "$1", "ssn": "123-45-6789"}}


@pytest.fixture(autouse=True)
def _fresh_sessions():
    session_store.clear_all()
    yield
    session_store.clear_all()


def _encrypt(pem: str, value: str) -> str:
    key = serialization.load_pem_public_key(pem.encode())
    ciphertext = key.encrypt(
        value.encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ciphertext).decode()


def _hosted_session(client, headers, site=None):
    params = {"site": site} if site else {}
    resp = client.post("/link/sessions", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["link_token"]


def _page_connect(client, link_token, site="hydro_one", username="demo-user", password="Secret@pass123"):
    """What the hosted page does for one attempt: fetch a key, encrypt, connect (no credentials headers)."""
    pem = client.get(f"/encryption/public_key/{link_token}")
    assert pem.status_code == 200, pem.text
    return client.post(
        "/connect",
        json={
            "link_token": link_token,
            "site": site,
            "encrypted_username": _encrypt(pem.json()["public_key"], username),
            "encrypted_password": _encrypt(pem.json()["public_key"], password),
        },
    )


def _stored_username(link_token):
    with TestSessionLocal() as db:
        token = db.query(AccessToken).filter_by(link_token=link_token).one()
        user = db.get(User, token.user_id)
        return decrypt_credential_for_user(user, token.username_encrypted)


class TestWhoMayConnect:
    def test_anonymous_connect_needs_a_live_hosted_session(self, client):
        body = {"site": "internal_bank", "username": "u", "password": "p"}
        assert client.post("/connect", json=body).status_code == 401
        # An encryption-session token is not a hosted session.
        enc = client.post("/encryption/session").json()
        assert client.post("/connect", json={**body, "link_token": enc["link_token"]}).status_code == 401
        assert client.post("/connect", json={**body, "link_token": "made-up"}).status_code == 401

    def test_bad_credentials_are_not_ignored(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        resp = client.post(
            "/connect",
            json={"site": "hydro_one", "username": "u", "password": "p", "link_token": link_token},
            headers={"X-API-Key": "pk_not-a-key"},
        )
        assert resp.status_code == 401

    def test_hosted_page_connects_with_its_link_token(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        resp = _page_connect(client, link_token)
        assert resp.status_code == 200
        assert resp.json()["status"] == "connected"
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "completed"
        assert status["public_token"].startswith("public-")

    def test_completed_session_never_overwrites_its_credentials(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        assert _page_connect(client, link_token).status_code == 200

        # Someone else holding the link URL tries to swap in their own account.
        resp = client.post(
            "/connect",
            json={"link_token": link_token, "site": "hydro_one", "username": "attacker", "password": "x"},
        )
        assert resp.status_code == 409
        assert client.get(f"/encryption/public_key/{link_token}").status_code == 409
        assert _stored_username(link_token) == "demo-user"

    def test_expired_and_exited_sessions_refuse_credentials(self, client, auth_headers):
        expired = _hosted_session(client, auth_headers)
        session_store._mem_link_sessions[expired]["created_at"] = 0
        body = {"site": "hydro_one", "username": "u", "password": "p"}
        assert client.post("/connect", json={**body, "link_token": expired}).status_code == 410

        exited = _hosted_session(client, auth_headers)
        client.post(f"/link/sessions/{exited}/event", json={"event": "EXIT"})
        assert client.post("/connect", json={**body, "link_token": exited}).status_code == 409

    def test_session_site_must_match(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers, site="hydro_one")
        resp = client.post(
            "/connect",
            json={"link_token": link_token, "site": "internal_bank", "username": "u", "password": "p"},
        )
        assert resp.status_code == 400
        with TestSessionLocal() as db:
            assert db.get(Link, link_token).site == "hydro_one"
            assert db.query(AccessToken).filter_by(link_token=link_token).count() == 0

    def test_attempt_in_progress_blocks_a_second_one(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        with TestSessionLocal() as db:
            db.add(
                AccessJob(
                    id="ajob-running",
                    site="hydro_one",
                    job_type="connect",
                    status="running",
                    lock_scope="test",
                    session_id="access-running",
                    created_at=utcnow(),
                )
            )
            db.commit()
        session_store.update_link_session(link_token, {"status": "connecting", "current_job_id": "ajob-running"})
        resp = client.post(
            "/connect",
            json={"link_token": link_token, "site": "hydro_one", "username": "u", "password": "p"},
        )
        assert resp.status_code == 409

    def test_a_page_event_alone_does_not_block_connecting(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        client.post(f"/link/sessions/{link_token}/event", json={"event": "CREDENTIALS_SUBMITTED"})
        assert client.get(f"/link/sessions/{link_token}/status").json()["status"] == "connecting"
        assert _page_connect(client, link_token).status_code == 200

    def test_another_accounts_login_cannot_use_the_session(self, client, auth_headers, second_user_headers):
        link_token = _hosted_session(client, auth_headers)
        resp = client.post(
            "/connect",
            json={"link_token": link_token, "site": "hydro_one", "username": "u", "password": "p"},
            headers=second_user_headers,
        )
        assert resp.status_code == 403

    def test_agent_restrictions_apply_to_connect(self, client, auth_headers):
        agent = client.post(
            "/agents",
            json={"name": "a", "allowed_sites": ["internal_bank"], "allowed_scopes": ["balance"]},
            headers=auth_headers,
        ).json()
        key = {"X-API-Key": agent["api_key"]}
        body = {"username": "u", "password": "p"}
        assert client.post("/connect", json={**body, "site": "hydro_one"}, headers=key).status_code == 403
        resp = client.post("/connect", json={**body, "site": "internal_bank", "extract_fields": ["ssn"]}, headers=key)
        assert resp.status_code == 403

        with patch("src.routers.connection.connect_to_site", AsyncMock(return_value=_CONNECTED)) as engine:
            ok = client.post("/connect", json={**body, "site": "internal_bank"}, headers=key)
        assert ok.status_code == 200
        assert engine.await_args.kwargs["extract_fields"] == ["balance"]
        assert ok.json()["data"] == {"balance": "$1"}


class TestRetryWithAFreshKey:
    """LNK-02: "Try again" gets a new key, and a key decrypts one submission only."""

    def test_try_again_after_a_failed_attempt(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        engine = AsyncMock(side_effect=[AuthenticationError(site="hydro_one"), _CONNECTED])

        with patch("src.routers.connection.connect_to_site", engine):
            first_key = client.get(f"/encryption/public_key/{link_token}").json()["public_key"]
            failed = client.post(
                "/connect",
                json={
                    "link_token": link_token,
                    "site": "hydro_one",
                    "encrypted_username": _encrypt(first_key, "demo-user"),
                    "encrypted_password": _encrypt(first_key, "wrong"),
                },
            )
            assert failed.status_code == 401
            # The used key is gone...
            assert get_public_key(link_token) is None
            # ...and the page's retry gets a new one.
            retried = _page_connect(client, link_token, password="Secret@pass123")

        assert retried.status_code == 200
        assert retried.json()["status"] == "connected"
        assert engine.await_count == 2
        assert engine.await_args.kwargs["password"] == "Secret@pass123"
        assert client.get(f"/link/sessions/{link_token}/status").json()["status"] == "completed"

    def test_every_request_issues_a_new_key(self, client, auth_headers):
        link_token = _hosted_session(client, auth_headers)
        first = client.get(f"/encryption/public_key/{link_token}").json()["public_key"]
        second = client.get(f"/encryption/public_key/{link_token}").json()["public_key"]
        assert first != second
        assert get_public_key(link_token) == second

    def test_a_key_decrypts_one_submission(self, client, auth_headers):
        enc = client.post("/encryption/session").json()
        body = {
            "site": "internal_bank",
            "link_token": enc["link_token"],
            "encrypted_username": _encrypt(enc["public_key"], "u"),
            "encrypted_password": _encrypt(enc["public_key"], "p"),
        }
        assert client.post("/connect", json=body, headers=auth_headers).status_code == 200
        replay = client.post("/connect", json=body, headers=auth_headers)
        assert replay.status_code == 400


class TestKeyGeneration:
    """JOB-20: RSA generation runs off the event loop and its endpoints have a tight limit."""

    def test_keys_are_generated_off_the_event_loop(self, client, auth_headers):
        import src.routers.connection as connection
        import src.routers.links as links

        threads_with_a_loop = []
        real = connection.generate_keypair

        def recording(link_token):
            try:
                asyncio.get_running_loop()
                threads_with_a_loop.append(True)
            except RuntimeError:
                threads_with_a_loop.append(False)
            return real(link_token)

        with (
            patch.object(connection, "generate_keypair", recording),
            patch.object(links, "generate_keypair", recording),
        ):
            client.post("/encryption/session")
            client.get(f"/encryption/public_key/{_hosted_session(client, auth_headers)}")
            client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)

        assert threads_with_a_loop == [False, False, False]

    def test_encryption_endpoints_are_rate_limited(self, client):
        from limits.storage.memory import MemoryStorage

        from src.dependencies import limiter

        limiter._limiter.storage = MemoryStorage()
        limiter.enabled = True
        try:
            codes = [client.post("/encryption/session").status_code for _ in range(11)]
            assert codes == [200] * 10 + [429]
            key_codes = [client.get("/encryption/public_key/anything").status_code for _ in range(11)]
            assert key_codes == [404] * 10 + [429]
        finally:
            limiter.enabled = False
            limiter._limiter.storage = MemoryStorage()


def test_connect_response_keeps_the_extraction_method(client, auth_headers):
    """JOB-24: ConnectResponse no longer drops extraction_method."""
    result = {"status": "connected", "data": {"a": 1}, "extraction_method": "css_selectors", "metadata": {}}
    with patch("src.routers.connection.connect_to_site", AsyncMock(return_value=result)):
        resp = client.post(
            "/connect", json={"site": "internal_bank", "username": "u", "password": "p"}, headers=auth_headers
        )
    assert resp.status_code == 200
    assert resp.json()["extraction_method"] == "css_selectors"


def test_authenticated_connect_owns_its_job(client, auth_headers):
    """The caller owns a one-shot connect's job, so its (encrypted) result can be read back."""
    me = client.get("/auth/me", headers=auth_headers).json()["id"]
    with patch("src.routers.connection.connect_to_site", AsyncMock(return_value=_CONNECTED)):
        resp = client.post(
            "/connect", json={"site": "internal_bank", "username": "u", "password": "p"}, headers=auth_headers
        )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    with TestSessionLocal() as db:
        assert db.get(AccessJob, job_id).user_id == me

    job = client.get(f"/access_jobs/{job_id}", headers=auth_headers)
    assert job.status_code == 200
    assert job.json()["result"]["data"]["balance"] == "$1"


def test_agent_created_session_keeps_the_agents_sites_on_the_hosted_page(client, auth_headers):
    """The anonymous page can't widen a session an agent created beyond the agent's allowed sites."""
    agent = client.post(
        "/agents",
        json={"name": "a", "allowed_sites": ["internal_bank"], "allowed_scopes": ["balance"]},
        headers=auth_headers,
    ).json()
    link_token = _hosted_session(client, {"X-API-Key": agent["api_key"]})

    assert _page_connect(client, link_token, site="hydro_one").status_code == 403
    with patch("src.routers.connection.connect_to_site", AsyncMock(return_value=_CONNECTED)):
        assert _page_connect(client, link_token, site="internal_bank").status_code == 200
