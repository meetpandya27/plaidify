"""Hosted-link sessions: the event stream, atomic session writes, emit-once events,
EXIT cancelling the job, MFA prompts, and embedding origins."""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import access_jobs as access_jobs_module
from src import session_store
from src.access_jobs import start_access_job
from src.core.mfa_manager import get_mfa_manager
from src.database import AccessJob, User, Webhook, WebhookDelivery, encrypt_webhook_secret
from src.main import app
from src.routers import link_sessions
from src.routers.link_sessions import _push_link_session_event, end_link_sessions_for_jobs, reconcile_link_session
from tests.conftest import TestSessionLocal
from tests.jobs_support import REDIS_URL, make_user, receiver_fixture, redis_mode_fixture, requires_redis  # noqa: F401

CREDS = {"site": "internal_bank", "username": "hl_user", "password": "pw"}


@pytest.fixture(autouse=True)
def _clean_store():
    session_store.clear_all()
    yield
    session_store.clear_all()
    access_jobs_module._LOCAL_LOCK_OWNERS.clear()


def _new_session(user_id=None, **fields):
    token = str(uuid.uuid4())
    record = {
        "status": "awaiting_institution",
        "allowed_origin": None,
        "allowed_origins": [],
        "current_job_id": None,
        "site": "internal_bank",
        "user_id": user_id,
        "events": [],
        "access_token": None,
        "public_token": None,
        "session_id": None,
    }
    record.update(fields)
    session_store.create_link_session(token, record)
    return token


def _asgi_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _events(token):
    return [event["event"] for event in session_store.get_link_session(token)["events"]]


# ── Event stream (JOB-02) ─────────────────────────────────────────────────────


@pytest.fixture
async def live_server():
    """The API on an ephemeral port, in this test's event loop (so a blocked loop would stall the test)."""
    import uvicorn
    from sse_starlette.sse import AppStatus

    # sse-starlette keeps one process-wide exit event, bound to the first event
    # loop that streamed; each test has its own loop.
    AppStatus.should_exit_event = None
    AppStatus.should_exit = False
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def _read_stream(base_url, token, received, *, stop_after=None):
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("GET", f"{base_url}/link/events/{token}") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:]:
                    event = json.loads(line[6:])
                    received.append(event["event"])
                    if stop_after and event["event"] == stop_after:
                        return


async def _count_ticks(seconds):
    ticks = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ticks += 1
        await asyncio.sleep(0.01)
    return ticks


async def _stream_scenario(base_url):
    token = _new_session()
    await _push_link_session_event(token, "OPEN", data={})
    received = []
    reader = asyncio.create_task(_read_stream(base_url, token, received))

    # While the stream sits idle, the event loop keeps running everything else.
    ticks = await _count_ticks(1.5)
    assert ticks >= 75, f"event loop stalled while a stream was open ({ticks} ticks)"
    assert received == ["OPEN"]

    await _push_link_session_event(token, "INSTITUTION_SELECTED", data={"site": "internal_bank"})
    await _push_link_session_event(token, "CONNECTED", data={"job_id": "j1"}, updates={"status": "completed"})
    await asyncio.wait_for(reader, timeout=10)
    assert received == ["OPEN", "INSTITUTION_SELECTED", "CONNECTED"]
    return token


@pytest.mark.asyncio
async def test_event_stream_without_redis_delivers_and_ends(live_server):
    token = await _stream_scenario(live_server)
    assert session_store.local_subscriber_count(token) == 0


@requires_redis
@pytest.mark.asyncio
async def test_event_stream_on_redis_never_blocks_the_loop_and_unsubscribes(redis_mode, live_server):
    token = await _stream_scenario(live_server)
    client = session_store.async_redis()
    channel = f"plaidify:link_events:{token}"
    for _ in range(100):
        if (await client.pubsub_numsub(channel))[0][1] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await client.pubsub_numsub(channel))[0][1] == 0


@requires_redis
@pytest.mark.asyncio
async def test_a_client_that_goes_away_releases_its_subscription(redis_mode, live_server):
    token = _new_session()
    received = []
    reader = asyncio.create_task(_read_stream(live_server, token, received))
    client = session_store.async_redis()
    channel = f"plaidify:link_events:{token}"
    for _ in range(100):
        if (await client.pubsub_numsub(channel))[0][1] == 1:
            break
        await asyncio.sleep(0.05)
    assert (await client.pubsub_numsub(channel))[0][1] == 1
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    for _ in range(100):
        if (await client.pubsub_numsub(channel))[0][1] == 0:
            break
        await asyncio.sleep(0.05)
    assert (await client.pubsub_numsub(channel))[0][1] == 0


def test_event_stream_404_and_410(client, monkeypatch):
    assert client.get("/link/events/nope").status_code == 404
    token = _new_session(created_at=time.time() - session_store.LINK_SESSION_TTL - 5)
    assert client.get(f"/link/events/{token}").status_code == 410


# ── Atomic writes and emit-once (JOB-15) ──────────────────────────────────────


async def _concurrent_writes_are_not_lost():
    token = _new_session()
    await asyncio.gather(
        *(
            session_store.atransition_link_session(token, event={"event": f"E{i}", "event_id": str(i)})
            for i in range(25)
        )
    )
    assert sorted(_events(token)) == sorted(f"E{i}" for i in range(25))


async def _terminal_events_are_emitted_once():
    token = _new_session(status="connecting")
    results = await asyncio.gather(
        *(
            _push_link_session_event(token, "CONNECTED", data={"job_id": "j"}, updates={"status": "completed"})
            for _ in range(10)
        )
    )
    assert sum(result is not None for result in results) == 1
    assert _events(token).count("CONNECTED") == 1


@pytest.mark.asyncio
async def test_concurrent_writes_are_not_lost_in_memory():
    await _concurrent_writes_are_not_lost()
    await _terminal_events_are_emitted_once()


@requires_redis
@pytest.mark.asyncio
async def test_concurrent_writes_are_not_lost_on_redis(redis_mode):
    await _concurrent_writes_are_not_lost()
    await _terminal_events_are_emitted_once()


@requires_redis
@pytest.mark.asyncio
async def test_an_expired_session_is_never_written_back(redis_mode, monkeypatch):
    monkeypatch.setattr(session_store, "LINK_SESSION_TTL", 1)
    token = _new_session()
    await asyncio.sleep(1.3)
    assert session_store.update_link_session(token, {"status": "connecting"}) is False
    assert await session_store.aupdate_link_session(token, {"status": "connecting"}) is False
    assert session_store._redis().exists(f"plaidify:link_session:{token}") == 0


def test_an_expired_in_memory_session_is_never_written_back():
    token = _new_session(created_at=time.time() - session_store.LINK_SESSION_TTL - 1)
    assert session_store.update_link_session(token, {"status": "connecting"}) is False
    assert session_store.get_link_session(token)["status"] == "expired"


def test_scopes_are_consumed_once():
    session_store.set_link_scopes("lt", '["balance"]')
    assert session_store.pop_link_scopes("lt") == '["balance"]'
    assert session_store.pop_link_scopes("lt") is None


class TestSharedStateRequirements:
    def test_several_workers_outside_development_need_redis(self, monkeypatch):
        monkeypatch.setattr(session_store.settings, "redis_url", None)
        monkeypatch.setattr(session_store.settings, "env", "staging")
        monkeypatch.setenv("GUNICORN_WORKERS", "3")
        with pytest.raises(RuntimeError, match="REDIS_URL is required when more than one worker runs"):
            session_store.check_shared_state_requirements()

    def test_gunicorn_defaults_to_several_workers(self, monkeypatch):
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/25.3.0")
        assert session_store.web_worker_count() == 2

    def test_one_worker_or_development_may_use_memory(self, monkeypatch):
        monkeypatch.setattr(session_store.settings, "redis_url", None)
        monkeypatch.setattr(session_store.settings, "env", "staging")
        monkeypatch.setenv("GUNICORN_WORKERS", "1")
        session_store.check_shared_state_requirements()
        monkeypatch.setattr(session_store.settings, "env", "development")
        monkeypatch.setenv("GUNICORN_WORKERS", "4")
        session_store.check_shared_state_requirements()

    def test_production_always_needs_redis(self, monkeypatch):
        monkeypatch.setattr(session_store.settings, "redis_url", None)
        monkeypatch.setattr(session_store.settings, "env", "production")
        with pytest.raises(RuntimeError):
            session_store.check_shared_state_requirements()


# ── Page events ───────────────────────────────────────────────────────────────


def test_a_page_error_never_overrides_a_completed_session(client):
    token = _new_session(status="completed", public_token="public-1")
    response = client.post(f"/link/sessions/{token}/event", json={"event": "ERROR", "error": "boom"})
    assert response.json() == {"status": "ignored"}
    status = client.get(f"/link/sessions/{token}/status").json()
    assert status["status"] == "completed"
    assert status["public_token"] == "public-1"


def test_exit_after_completion_keeps_the_session_completed_and_is_sent_once(client):
    token = _new_session(status="completed")
    assert client.post(f"/link/sessions/{token}/event", json={"event": "EXIT"}).json() == {"status": "ok"}
    assert client.post(f"/link/sessions/{token}/event", json={"event": "EXIT"}).json() == {"status": "ignored"}
    assert client.get(f"/link/sessions/{token}/status").json()["status"] == "completed"
    assert _events(token).count("EXIT") == 1


def test_malformed_event_bodies_are_422(client):
    token = _new_session()
    bad_json = client.post(
        f"/link/sessions/{token}/event", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert bad_json.status_code == 422
    assert client.post(f"/link/sessions/{token}/event", json={"event": ["x"]}).status_code == 422


@pytest.mark.asyncio
async def test_exit_cancels_the_running_job_and_frees_its_lock():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def endless(**kwargs):
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()

    token = _new_session()
    job, task = await start_access_job(
        None,
        site="internal_bank",
        job_type="connect",
        executor=endless,
        executor_kwargs=dict(CREDS),
        metadata={"link_token": token},
    )
    session_store.update_link_session(token, {"current_job_id": job.id, "status": "connecting"})
    await started.wait()

    async with _asgi_client() as client:
        response = await client.post(f"/link/sessions/{token}/event", json={"event": "EXIT", "reason": "user_closed"})
    assert response.json() == {"status": "ok"}
    await asyncio.wait_for(stopped.wait(), timeout=3)
    await asyncio.gather(task, return_exceptions=True)
    with TestSessionLocal() as db:
        assert db.get(AccessJob, job.id).status == "cancelled"
    assert access_jobs_module._LOCAL_LOCK_OWNERS == {}
    assert session_store.get_link_session(token)["status"] == "exited"


# ── MFA prompts (JOB-10) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_required_is_sent_once_per_challenge_with_the_reason_for_a_new_one(receiver):
    with TestSessionLocal() as db:
        owner = make_user(db)
        owner_id = owner.id
    token = _new_session(user_id=owner_id)
    with TestSessionLocal() as db:
        db.add(
            Webhook(
                id="wh-mfa",
                link_token=token,
                url=receiver.url,
                secret=encrypt_webhook_secret(db.get(User, owner_id), "s3cret"),
                user_id=owner_id,
            )
        )
        db.commit()

    steps = {"asked": asyncio.Event(), "checked": asyncio.Event(), "reopened": asyncio.Event()}

    async def mfa_executor(**kwargs):
        manager = get_mfa_manager()
        session = await manager.create_session(kwargs["session_id"], kwargs["site"], "otp", {"message": "Code?"})
        steps["asked"].set()
        await session.wait_for_code(timeout=10)
        await steps["checked"].wait()
        await manager.reopen_session(kwargs["session_id"], {"mfa_error": "invalid_code", "attempts_remaining": 2})
        steps["reopened"].set()
        await session.wait_for_code(timeout=10)
        await manager.remove_session(kwargs["session_id"])
        return {"status": "connected", "data": {}}

    job, task = await start_access_job(
        None,
        site="internal_bank",
        job_type="connect",
        executor=mfa_executor,
        executor_kwargs=dict(CREDS),
        metadata={"link_token": token},
    )
    session_store.update_link_session(token, {"current_job_id": job.id, "status": "connecting"})
    await steps["asked"].wait()

    async with _asgi_client() as client:
        first = (await client.get(f"/link/sessions/{token}/status")).json()
        again = (await client.get(f"/link/sessions/{token}/status")).json()
        assert first["status"] == again["status"] == "mfa_required"
        # The page echoes the prompt it was shown: not a new challenge.
        echo = await client.post(
            f"/link/sessions/{token}/event", json={"event": "MFA_REQUIRED", "session_id": job.session_id}
        )
        assert echo.json() == {"status": "ignored"}
        assert _events(token).count("MFA_REQUIRED") == 1

        await get_mfa_manager().submit_code(job.session_id, "000000")
        for _ in range(100):
            if get_mfa_manager()._sessions[job.session_id].consumed:
                break
            await asyncio.sleep(0.02)
        verifying = (await client.get(f"/link/sessions/{token}/status")).json()
        assert verifying["status"] == "verifying_mfa"

        steps["checked"].set()
        await steps["reopened"].wait()
        reprompt = (await client.get(f"/link/sessions/{token}/status")).json()
        assert reprompt["status"] == "mfa_required"
        assert reprompt["metadata"]["mfa_error"] == "invalid_code"
        prompts = [e for e in session_store.get_link_session(token)["events"] if e["event"] == "MFA_REQUIRED"]
        assert len(prompts) == 2
        assert prompts[1]["data"]["mfa_error"] == "invalid_code"
        assert prompts[1]["data"]["attempts_remaining"] == 2

        await get_mfa_manager().submit_code(job.session_id, "123456")
        await task
        done = (await client.get(f"/link/sessions/{token}/status")).json()
        assert done["status"] == "completed"

    for _ in range(200):
        events = [body["event"] for body in receiver.json_bodies()]
        if events.count("MFA_REQUIRED") == 2 and "LINK_COMPLETE" in events:
            break
        await asyncio.sleep(0.05)
    events = [body["event"] for body in receiver.json_bodies()]
    assert events.count("MFA_REQUIRED") == 2
    assert events.count("LINK_COMPLETE") == 1
    reprompt_hook = [body for body in receiver.json_bodies() if body["event"] == "MFA_REQUIRED"][1]
    assert reprompt_hook["data"]["mfa_error"] == "invalid_code"


@pytest.mark.asyncio
async def test_an_mfa_timeout_ends_the_session_with_its_code():
    from src.core.mfa_manager import MFATimeoutError

    async def executor(**kwargs):
        raise MFATimeoutError(site="internal_bank", mfa_type="otp", session_id=kwargs["session_id"])

    token = _new_session()
    job, task = await start_access_job(
        None, site="internal_bank", job_type="connect", executor=executor, executor_kwargs=dict(CREDS)
    )
    session_store.update_link_session(token, {"current_job_id": job.id, "status": "connecting"})
    await asyncio.gather(task, return_exceptions=True)
    with TestSessionLocal() as db:
        session = await reconcile_link_session(db, link_token=token)
    assert session["status"] == "error"
    assert session["error_code"] == "mfa_timeout"
    error_event = [e for e in session["events"] if e["event"] == "ERROR"][0]
    assert error_event["data"]["error_code"] == "mfa_timeout"


@pytest.mark.asyncio
async def test_reaped_jobs_end_their_hosted_sessions():
    token = _new_session(status="connecting")
    now = datetime.now(timezone.utc)
    with TestSessionLocal() as db:
        db.add(
            AccessJob(
                id="ajob-reaped",
                site="internal_bank",
                job_type="connect",
                status="running",
                lock_scope="cred:x:site:internal_bank",
                session_id="s-reaped",
                metadata_json=json.dumps({"link_token": token}),
                created_at=now - timedelta(minutes=10),
                started_at=now - timedelta(minutes=10),
                heartbeat_at=now - timedelta(minutes=10),
            )
        )
        db.commit()
    session_store.update_link_session(token, {"current_job_id": "ajob-reaped"})
    reaped = await access_jobs_module.reap_stuck_access_jobs()
    await end_link_sessions_for_jobs(reaped)
    session = session_store.get_link_session(token)
    assert session["status"] == "error"
    assert "stopped" in session["error_message"]
    assert _events(token) == ["ERROR"]


# ── Embedding origins (LNK-10, SEC-20) ────────────────────────────────────────


def test_backend_sessions_take_allowed_origins(client, auth_headers):
    response = client.post(
        "/link/sessions",
        json={"site": "internal_bank", "allowed_origins": ["https://app.example.com/", "https://app.example.com"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    token = response.json()["link_token"]
    status = client.get(f"/link/sessions/{token}/status").json()
    assert status["allowed_origins"] == ["https://app.example.com"]
    assert status["site"] == "internal_bank"
    page = client.get(f"/link?token={token}")
    assert "https://app.example.com" in page.headers["content-security-policy"]


def test_status_lists_no_origins_when_there_are_none(client, auth_headers):
    token = client.post("/link/sessions", headers=auth_headers).json()["link_token"]
    assert client.get(f"/link/sessions/{token}/status").json()["allowed_origins"] == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://app.example.com",
        "https://app.example.com/path",
        "https://user:pw@app.example.com",
        "javascript:alert(1)",
        "*",
    ],
)
def test_bad_origins_are_refused(client, auth_headers, origin):
    response = client.post("/link/sessions", json={"allowed_origins": [origin]}, headers=auth_headers)
    assert response.status_code == 422


def test_localhost_origins_only_outside_production(client, auth_headers, monkeypatch):
    ok = client.post("/link/sessions", json={"allowed_origins": ["http://localhost:3000"]}, headers=auth_headers)
    assert ok.status_code == 200
    monkeypatch.setattr(link_sessions.settings, "env", "production")
    refused = client.post("/link/sessions", json={"allowed_origins": ["http://localhost:3000"]}, headers=auth_headers)
    assert refused.status_code == 422


def test_session_creation_logs_a_fingerprint_not_the_token(client, auth_headers, caplog):
    with caplog.at_level(logging.INFO, logger="plaidify.api.link_sessions"):
        token = client.post("/link/sessions", headers=auth_headers).json()["link_token"]
    assert token not in caplog.text


def test_webhook_payloads_never_carry_extracted_data(client, auth_headers):
    """The session keeps no copy of the job result (it lived in Redis before)."""
    token = client.post("/link/sessions", headers=auth_headers).json()["link_token"]
    assert "result" not in session_store.get_link_session(token)


@pytest.mark.asyncio
async def test_link_webhooks_are_stored_in_the_outbox(monkeypatch):
    from src.routers import webhooks

    monkeypatch.setattr(webhooks, "_deliver_soon", lambda delivery_ids: None)
    with TestSessionLocal() as db:
        owner = make_user(db)
        owner_id = owner.id
        token = _new_session(user_id=owner_id)
        db.add(
            Webhook(
                id="wh-out",
                link_token=token,
                url="https://hooks.example.com/x",
                secret=encrypt_webhook_secret(owner, "s"),
                user_id=owner_id,
            )
        )
        db.commit()
    await _push_link_session_event(token, "OPEN", data={})
    with TestSessionLocal() as db:
        delivery = db.query(WebhookDelivery).one()
    assert delivery.event == "LINK_OPEN"
    assert delivery.webhook_id == "wh-out"


@requires_redis
def test_redis_url_is_reachable_for_these_tests():
    assert REDIS_URL


# ── Agent site restrictions and the session's site (R5) ───────────────────────


def _agent_headers(client, auth_headers, sites):
    agent = client.post("/agents", json={"name": "Site bot", "allowed_sites": sites}, headers=auth_headers).json()
    return {"X-API-Key": agent["api_key"]}


def test_an_agents_sessions_carry_its_allowed_sites(client, auth_headers):
    headers = _agent_headers(client, auth_headers, ["internal_bank", "hydro_one"])
    token = client.post("/link/sessions", headers=headers).json()["link_token"]
    assert session_store.get_link_session(token)["allowed_sites"] == ["hydro_one", "internal_bank"]
    # A user's own session is unrestricted.
    own = client.post("/link/sessions", headers=auth_headers).json()["link_token"]
    assert session_store.get_link_session(own)["allowed_sites"] is None


def test_a_bootstrapped_session_keeps_the_agents_allowed_sites(client, auth_headers):
    headers = _agent_headers(client, auth_headers, ["internal_bank"])
    launch = client.post("/link/bootstrap", json={}, headers=headers).json()["launch_token"]
    token = client.post("/link/sessions/bootstrap", json={"launch_token": launch}).json()["link_token"]
    assert session_store.get_link_session(token)["allowed_sites"] == ["internal_bank"]


def test_the_picker_cannot_change_a_preset_site_or_pick_a_disallowed_one(client, auth_headers):
    preset = _new_session(site="internal_bank")
    client.post(f"/link/sessions/{preset}/event", json={"event": "INSTITUTION_SELECTED", "site": "hydro_one"})
    status = client.get(f"/link/sessions/{preset}/status").json()
    assert status["site"] == "internal_bank" and status["status"] == "awaiting_credentials"

    restricted = _new_session(site=None, allowed_sites=["internal_bank"])
    client.post(f"/link/sessions/{restricted}/event", json={"event": "INSTITUTION_SELECTED", "site": "hydro_one"})
    assert session_store.get_link_session(restricted)["site"] is None
    client.post(f"/link/sessions/{restricted}/event", json={"event": "INSTITUTION_SELECTED", "site": "Internal_Bank"})
    assert session_store.get_link_session(restricted)["site"] == "Internal_Bank"


def test_a_session_knows_its_jobs():
    token = _new_session()
    session_store.update_link_session(token, {"current_job_id": "ajob-42"})
    assert session_store.link_token_for_job("ajob-42") == token
    assert session_store.link_token_for_job("ajob-unknown") is None
