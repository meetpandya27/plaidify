"""Shared helpers for the access-job, link-session, refresh and webhook tests.

The Redis-backed paths run only when PLAIDIFY_TEST_REDIS_URL points at a
disposable Redis (the tests delete ``plaidify:*`` keys). Import the fixtures
you use into the test module (``redis_mode_fixture`` provides ``redis_mode``,
``receiver_fixture`` provides ``receiver``).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import pytest

from src.database import AccessToken, Link, User, create_user_dek, encrypt_credential_for_user, get_current_key_version

REDIS_URL = os.environ.get("PLAIDIFY_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="set PLAIDIFY_TEST_REDIS_URL to run against a real Redis")


def _flush_plaidify_keys() -> None:
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        keys = list(client.scan_iter("plaidify:*", count=500))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@pytest.fixture(name="redis_mode")
async def redis_mode_fixture(monkeypatch):
    """Run the link-session store, locks, MFA sessions and the dispatcher on a real Redis.

    Uses a stream and consumer group of its own, and short heartbeats so the
    liveness paths run in test time. Yields the ``src.access_jobs`` settings
    object for further tweaks.
    """
    if not REDIS_URL:
        pytest.skip("set PLAIDIFY_TEST_REDIS_URL to run against a real Redis")
    from src import access_jobs, background_services, session_store
    from src.core import async_redis as core_async_redis
    from src.core import mfa_manager as mfa_module
    from src.routers import link_sessions, webhooks

    _flush_plaidify_keys()
    suffix = uuid.uuid4().hex[:8]
    for module_settings in {
        id(s): s
        for s in (
            session_store.settings,
            access_jobs.settings,
            core_async_redis.settings,
            background_services.settings,
            link_sessions.settings,
            webhooks.settings,
        )
    }.values():
        monkeypatch.setattr(module_settings, "redis_url", REDIS_URL)
    monkeypatch.setattr(access_jobs.settings, "access_job_stream_key", f"plaidify:test:{suffix}:stream")
    monkeypatch.setattr(access_jobs.settings, "access_job_consumer_group", f"plaidify-test-{suffix}")
    monkeypatch.setattr(access_jobs.settings, "access_job_worker_block_ms", 200)
    monkeypatch.setattr(access_jobs.settings, "access_job_heartbeat_seconds", 0.3)
    monkeypatch.setattr(session_store, "_redis_client", None)
    monkeypatch.setattr(mfa_module, "_manager", None)
    session_store.clear_all()
    try:
        yield access_jobs.settings
    finally:
        await session_store.close_async_redis()
        await core_async_redis.close_async_redis()
        client = session_store._redis_client
        if client is not None:
            client.close()
        _flush_plaidify_keys()
        mfa_module._manager = None


def make_user(db, username: str = "owner", *, admin: bool = False) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        encrypted_dek=create_user_dek(),
        is_admin=admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_access_token(
    db,
    user: User,
    *,
    site: str = "internal_bank",
    username: str = "site-user",
    password: str = "site-pass",
) -> AccessToken:
    link = Link(link_token=f"link-{uuid.uuid4()}", site=site, user_id=user.id)
    db.add(link)
    token = AccessToken(
        token=str(uuid.uuid4()),
        link_token=link.link_token,
        username_encrypted=encrypt_credential_for_user(user, username),
        password_encrypted=encrypt_credential_for_user(user, password),
        user_id=user.id,
        key_version=get_current_key_version(),
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


class Receiver:
    """A local HTTP endpoint that records webhook requests and answers with the queued status codes."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.statuses: List[int] = []
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with receiver.lock:
                    receiver.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                    status = receiver.statuses.pop(0) if receiver.statuses else 200
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def json_bodies(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [json.loads(item["body"]) for item in self.requests]

    def start(self) -> "Receiver":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(name="receiver")
def receiver_fixture():
    server = Receiver().start()
    try:
        yield server
    finally:
        server.stop()


def header(request: Dict[str, Any], name: str) -> Optional[str]:
    for key, value in request["headers"].items():
        if key.lower() == name.lower():
            return value
    return None
