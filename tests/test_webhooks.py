"""
Webhooks: ownership, destination checks (SSRF), signing, and the durable outbox.
"""

import asyncio
import ipaddress
import json
import time
from datetime import timedelta

import pytest

from src.database import (
    User,
    Webhook,
    WebhookDelivery,
    decrypt_webhook_secret,
    encrypt_credential,
    utcnow,
)
from src.routers import webhooks
from src.routers.webhooks import (
    WebhookURLError,
    deliver_due_webhooks,
    deliver_webhook,
    enqueue_webhook_event,
    purge_webhook_deliveries,
    validate_webhook_url,
    verify_webhook_signature,
)
from tests.conftest import TestSessionLocal
from tests.jobs_support import header, make_access_token, make_user, receiver_fixture  # noqa: F401


def _create_link_session(client, headers):
    return client.post("/link/sessions", headers=headers).json()["link_token"]


def _register(client, headers, link_token, url="https://example.com/hook", secret="test-webhook-secret"):
    return client.post(
        "/webhooks/register",
        json={"url": url, "link_token": link_token, "secret": secret},
        headers=headers,
    )


_DELIVER_SOON = webhooks._deliver_soon


@pytest.fixture(autouse=True)
def _no_immediate_delivery(monkeypatch):
    """Deliveries go out only when a test sends them (no stray requests to example.com)."""
    monkeypatch.setattr(webhooks, "_deliver_soon", lambda delivery_ids: None)


def _owner_with_webhook(url, *, secret="s3cret-value"):
    with TestSessionLocal() as db:
        owner = make_user(db)
        token = make_access_token(db, owner)
        from src.database import encrypt_webhook_secret

        db.add(
            Webhook(
                id="wh-1",
                link_token=token.link_token,
                url=url,
                secret=encrypt_webhook_secret(owner, secret),
                user_id=owner.id,
            )
        )
        db.commit()
        return owner.id, token.link_token, token.token


def _delivery(delivery_id):
    with TestSessionLocal() as db:
        return db.get(WebhookDelivery, delivery_id)


class TestWebhookDeliveryHistory:
    """GET /webhooks/{id}/deliveries."""

    def test_deliveries_requires_auth(self, client):
        assert client.get("/webhooks/fake-id/deliveries").status_code == 401

    def test_deliveries_not_found(self, client, auth_headers):
        assert client.get("/webhooks/nonexistent/deliveries", headers=auth_headers).status_code == 404

    def test_deliveries_empty(self, client, auth_headers):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token).json()["webhook_id"]

        resp = client.get(f"/webhooks/{webhook_id}/deliveries", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_id"] == webhook_id
        assert data["deliveries"] == []
        assert data["total"] == 0

    def test_deliveries_cross_user_forbidden(self, client, auth_headers, second_user_headers):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token).json()["webhook_id"]
        assert client.get(f"/webhooks/{webhook_id}/deliveries", headers=second_user_headers).status_code == 404

    def test_deliveries_list_the_outbox_and_go_with_the_webhook(self, client, auth_headers):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token).json()["webhook_id"]
        client.post(f"/link/sessions/{link_token}/event", json={"event": "OPEN"})

        deliveries = client.get(f"/webhooks/{webhook_id}/deliveries", headers=auth_headers).json()
        assert deliveries["total"] == 1
        entry = deliveries["deliveries"][0]
        assert entry["event"] == "LINK_OPEN" and entry["status"] == "pending"
        assert set(entry) >= {"delivery_id", "attempts", "last_status_code", "last_error", "next_attempt_at"}

        assert client.delete(f"/webhooks/{webhook_id}", headers=auth_headers).status_code == 200
        with TestSessionLocal() as db:
            assert db.query(WebhookDelivery).count() == 0


class TestWebhookPayloadSecurity:
    def test_webhook_payload_excludes_access_token(self):
        import inspect

        from src.routers.webhooks import fire_webhooks_for_session

        source = inspect.getsource(fire_webhooks_for_session)
        assert 'payload["access_token"]' not in source
        assert "public_token" in source


class TestRegistration:
    def test_only_the_owner_may_subscribe_to_a_session(self, client, auth_headers, second_user_headers):
        link_token = _create_link_session(client, auth_headers)
        assert _register(client, second_user_headers, link_token).status_code == 404
        assert _register(client, auth_headers, link_token).status_code == 200

    def test_create_link_tokens_can_have_webhooks(self, client, auth_headers, second_user_headers):
        link_token = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers).json()[
            "link_token"
        ]
        assert _register(client, auth_headers, link_token).status_code == 200
        assert _register(client, second_user_headers, link_token).status_code == 404

    def test_anonymous_sessions_have_no_owner_to_subscribe(self, client, auth_headers):
        link_token = client.post("/link/sessions/public").json()["link_token"]
        assert _register(client, auth_headers, link_token).status_code == 404

    def test_malformed_bodies_are_422(self, client, auth_headers):
        bad = client.post(
            "/webhooks/register", content=b"{nope", headers={**auth_headers, "Content-Type": "application/json"}
        )
        assert bad.status_code == 422
        assert (
            client.post("/webhooks/register", json={"url": "https://x.test"}, headers=auth_headers).status_code == 422
        )
        assert (
            client.post(
                "/webhooks/test", content=b"[", headers={**auth_headers, "Content-Type": "application/json"}
            ).status_code
            == 422
        )

    def test_the_secret_is_stored_encrypted_for_its_owner(self, client, auth_headers):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token, secret="plain-secret-123").json()["webhook_id"]
        with TestSessionLocal() as db:
            webhook = db.get(Webhook, webhook_id)
            assert webhook.secret != "plain-secret-123"
            assert decrypt_webhook_secret(db, webhook) == "plain-secret-123"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost@169.254.169.254/latest/meta-data",
            "https://user:pass@example.com/hook",
            "http://example.com/hook",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.8/hook",
            "https://[::ffff:10.0.0.8]/hook",
            "https://100.64.1.1/hook",
            "ftp://example.com/hook",
            "https:///nohost",
            "not a url",
        ],
    )
    def test_unsafe_urls_are_refused(self, client, auth_headers, url):
        link_token = _create_link_session(client, auth_headers)
        assert _register(client, auth_headers, link_token, url=url).status_code == 422

    def test_localhost_only_outside_production(self, monkeypatch):
        assert validate_webhook_url("http://localhost:9000/hook") == "http://localhost:9000/hook"
        monkeypatch.setattr(webhooks.settings, "env", "production")
        with pytest.raises(WebhookURLError):
            validate_webhook_url("http://localhost:9000/hook")
        with pytest.raises(WebhookURLError):
            validate_webhook_url("https://127.0.0.1/hook")
        assert validate_webhook_url("https://hooks.example.com/x#frag") == "https://hooks.example.com/x"


class TestDelivery:
    @pytest.mark.asyncio
    async def test_a_delivery_is_signed_with_the_decrypted_secret_and_a_timestamp(self, receiver):
        owner_id, link_token, _ = _owner_with_webhook(receiver.url, secret="s3cret-value")
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {"a": 1}})

        assert await deliver_webhook(delivery_id) == "delivered"
        [request] = receiver.requests
        body = request["body"]
        assert header(request, "X-Plaidify-Delivery") == delivery_id
        assert header(request, "X-Plaidify-Event") == "LINK_OPEN"
        timestamp = header(request, "X-Plaidify-Timestamp")
        assert abs(int(timestamp) - time.time()) < 60
        assert verify_webhook_signature(
            "s3cret-value", body=body, timestamp=timestamp, signature=header(request, "X-Plaidify-Signature")
        )
        # Signing with anything but the plaintext secret (e.g. the stored ciphertext) would not verify.
        with TestSessionLocal() as db:
            stored = db.get(Webhook, "wh-1").secret
        assert not verify_webhook_signature(
            stored, body=body, timestamp=timestamp, signature=header(request, "X-Plaidify-Signature")
        )
        payload = json.loads(body)
        assert payload["delivery_id"] == delivery_id and payload["event"] == "LINK_OPEN"
        record = _delivery(delivery_id)
        assert record.status == "delivered" and record.attempts == 1 and record.last_status_code == 200

    def test_signatures_expire_and_bind_the_timestamp(self):
        body = b'{"a":1}'
        signature = "sha256=" + webhooks.sign_webhook_payload("k", "1000", body)
        assert verify_webhook_signature("k", body=body, timestamp="1000", signature=signature, now=1100)
        assert not verify_webhook_signature("k", body=body, timestamp="1000", signature=signature, now=2000)
        assert not verify_webhook_signature("k", body=body, timestamp="1001", signature=signature, now=1100)
        assert not verify_webhook_signature("k", body=b'{"a":2}', timestamp="1000", signature=signature, now=1100)

    @pytest.mark.asyncio
    async def test_failures_are_retried_with_backoff_under_the_same_delivery_id(self, receiver):
        receiver.statuses = [500, 503]
        _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_ERROR", "data": {}})

        assert await deliver_webhook(delivery_id) == "pending"
        first = _delivery(delivery_id)
        assert first.last_status_code == 500 and first.last_error == "http_error"
        assert first.next_attempt_at > utcnow() + timedelta(seconds=5)
        # Not due yet: the outbox leaves it alone.
        assert await deliver_due_webhooks() == 0

        for _ in range(2):
            with TestSessionLocal() as db:
                db.get(WebhookDelivery, delivery_id).next_attempt_at = utcnow() - timedelta(seconds=1)
                db.commit()
            assert await deliver_due_webhooks() == 1
        record = _delivery(delivery_id)
        assert record.status == "delivered" and record.attempts == 3
        ids = {header(request, "X-Plaidify-Delivery") for request in receiver.requests}
        assert ids == {delivery_id}
        assert record.next_attempt_at is None

    @pytest.mark.asyncio
    async def test_attempts_stop_at_the_limit(self, receiver, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "webhook_max_attempts", 2)
        receiver.statuses = [500, 500, 500]
        _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_ERROR", "data": {}})
        await deliver_webhook(delivery_id)
        with TestSessionLocal() as db:
            db.get(WebhookDelivery, delivery_id).next_attempt_at = utcnow()
            db.commit()
        assert await deliver_webhook(delivery_id) == "failed"
        assert await deliver_webhook(delivery_id) is None
        assert len(receiver.requests) == 2

    @pytest.mark.asyncio
    async def test_redirects_are_not_followed(self, receiver):
        receiver.statuses = [302]
        _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {}})
        assert await deliver_webhook(delivery_id) == "pending"
        assert _delivery(delivery_id).last_error == "redirect_not_followed"
        assert len(receiver.requests) == 1

    @pytest.mark.asyncio
    async def test_a_host_resolving_to_metadata_is_refused_at_send_time(self, monkeypatch):
        """DNS rebinding: the name passed registration, but resolves to 169.254.169.254 when sending."""
        _owner_id, link_token, _ = _owner_with_webhook("https://rebind.example.com/hook")
        connected = []

        async def fake_resolve(self, host, port):
            return [ipaddress.ip_address("169.254.169.254")]

        async def fake_connect(*args, **kwargs):  # never reached
            connected.append(args)
            raise AssertionError("connected to a blocked address")

        monkeypatch.setattr(webhooks._PublicAddressBackend, "_resolve", fake_resolve)
        monkeypatch.setattr(webhooks.httpcore.AnyIOBackend, "connect_tcp", fake_connect)
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {}})
        assert await deliver_webhook(delivery_id) == "pending"
        assert _delivery(delivery_id).last_error == "blocked_destination"
        assert connected == []

    @pytest.mark.asyncio
    async def test_an_undecryptable_secret_skips_delivery(self, receiver, caplog):
        _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
        with TestSessionLocal() as db:
            db.get(Webhook, "wh-1").secret = "not-a-ciphertext"
            db.commit()
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {}})
        assert await deliver_webhook(delivery_id) is None
        record = _delivery(delivery_id)
        assert record.status == "failed" and record.last_error == "secret_unavailable"
        assert receiver.requests == []
        assert any(getattr(record, "extra_data", {}).get("webhook_id") == "wh-1" for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_master_key_secret_from_older_rows_still_signs(self, receiver):
        _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
        with TestSessionLocal() as db:
            db.get(Webhook, "wh-1").secret = encrypt_credential("legacy-secret")
            db.commit()
        [delivery_id] = await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {}})
        assert await deliver_webhook(delivery_id) == "delivered"
        [request] = receiver.requests
        assert verify_webhook_signature(
            "legacy-secret",
            body=request["body"],
            timestamp=header(request, "X-Plaidify-Timestamp"),
            signature=header(request, "X-Plaidify-Signature"),
        )

    @pytest.mark.asyncio
    async def test_payloads_are_encrypted_at_rest(self):
        _owner_id, link_token, _ = _owner_with_webhook("https://hooks.example.com/x")
        [delivery_id] = await enqueue_webhook_event(
            link_token, {"event": "LINK_COMPLETE", "public_token": "public-abc", "data": {}}
        )
        stored = _delivery(delivery_id)
        assert stored.payload_json.startswith("enc:v1:")
        assert "public-abc" not in stored.payload_json

    def test_old_finished_deliveries_are_purged(self):
        _owner_id, link_token, _ = _owner_with_webhook("https://hooks.example.com/x")
        with TestSessionLocal() as db:
            for index, status in enumerate(("delivered", "failed", "pending")):
                db.add(
                    WebhookDelivery(
                        id=f"whd_old_{index}",
                        webhook_id="wh-1",
                        user_id=db.query(User).one().id,
                        event="LINK_OPEN",
                        payload_json="enc:v1:x",
                        status=status,
                        attempts=1,
                        created_at=utcnow() - timedelta(days=30),
                    )
                )
            db.commit()
        assert purge_webhook_deliveries() == 2
        with TestSessionLocal() as db:
            assert [row.id for row in db.query(WebhookDelivery).all()] == ["whd_old_2"]


class TestTestEndpoint:
    def test_test_endpoint_sends_one_signed_attempt(self, client, auth_headers, receiver):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token, url=receiver.url, secret="abc123").json()["webhook_id"]
        response = client.post("/webhooks/test", json={"webhook_id": webhook_id}, headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "delivered" and body["status_code"] == 200
        [request] = receiver.requests
        assert header(request, "X-Plaidify-Delivery") == body["delivery_id"]
        assert verify_webhook_signature(
            "abc123",
            body=request["body"],
            timestamp=header(request, "X-Plaidify-Timestamp"),
            signature=header(request, "X-Plaidify-Signature"),
        )

    def test_test_endpoint_reports_a_failure_by_category(self, client, auth_headers, receiver):
        receiver.statuses = [500]
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token, url=receiver.url).json()["webhook_id"]
        body = client.post("/webhooks/test", json={"webhook_id": webhook_id}, headers=auth_headers).json()
        assert body["status"] == "failed" and body["error"] == "http_error"

    def test_test_endpoint_refuses_an_undecryptable_secret(self, client, auth_headers):
        link_token = _create_link_session(client, auth_headers)
        webhook_id = _register(client, auth_headers, link_token).json()["webhook_id"]
        with TestSessionLocal() as db:
            db.get(Webhook, webhook_id).secret = "garbage"
            db.commit()
        response = client.post("/webhooks/test", json={"webhook_id": webhook_id}, headers=auth_headers)
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_a_new_event_is_sent_at_once(receiver, monkeypatch):
    monkeypatch.setattr(webhooks, "_deliver_soon", _DELIVER_SOON)
    _owner_id, link_token, _ = _owner_with_webhook(receiver.url)
    await enqueue_webhook_event(link_token, {"event": "LINK_OPEN", "data": {}})
    for _ in range(100):
        if receiver.requests:
            break
        await asyncio.sleep(0.05)
    assert len(receiver.requests) == 1
    await webhooks.shutdown_webhook_deliveries()


@pytest.mark.asyncio
async def test_refresh_webhooks_go_through_the_outbox_signed_with_the_plain_secret(receiver):
    """SEC-17: DATA_REFRESHED / REFRESH_FAILED are signed with the decrypted secret."""
    from src.routers.refresh import _on_refresh_webhook

    owner_id, _link_token, access_token = _owner_with_webhook(receiver.url, secret="refresh-secret")
    await _on_refresh_webhook(
        access_token,
        owner_id,
        {"__refresh_failed__": True, "reason": "needs_reauth", "error": "MFA required", "consecutive_failures": 0},
    )
    assert await deliver_due_webhooks() == 1
    [request] = receiver.requests
    payload = json.loads(request["body"])
    assert payload["event"] == "REFRESH_FAILED" and payload["reason"] == "needs_reauth"
    assert payload["access_token_prefix"] == access_token[:12] + "..."
    assert access_token not in request["body"].decode()
    assert verify_webhook_signature(
        "refresh-secret",
        body=request["body"],
        timestamp=header(request, "X-Plaidify-Timestamp"),
        signature=header(request, "X-Plaidify-Signature"),
    )
