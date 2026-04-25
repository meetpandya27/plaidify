"""Playwright-backed hosted Link coverage for web and native webview shells."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import uuid

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from src.main import app

pytestmark = pytest.mark.playwright


class _ThreadedUvicornServer(uvicorn.Server):
    def install_signal_handlers(self):
        return


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(base_url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/status", timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - exercised only on startup races
            last_error = exc
            time.sleep(0.1)

    raise RuntimeError(f"Timed out waiting for live test server: {last_error}")


def _request_json(method: str, url: str, *, payload: dict | None = None, headers: dict | None = None) -> dict:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=10.0) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _get_hydro_one_provider_name(base_url: str) -> str:
    payload = _request_json("GET", f"{base_url}/organizations/search?site=hydro_one&limit=1")
    results = payload.get("results") or []
    if not results:
        raise AssertionError("Expected at least one hosted-link organization backed by the hydro_one connector.")
    return str(results[0]["name"])


def _create_authenticated_link_session(base_url: str, *, site: str | None = None) -> str:
    suffix = uuid.uuid4().hex[:10]
    auth_payload = _request_json(
        "POST",
        f"{base_url}/auth/register",
        payload={
            "username": f"e2euser-{suffix}",
            "email": f"e2e-{suffix}@example.com",
            "password": "Secure@pass123",
        },
    )
    session_url = f"{base_url}/link/sessions"
    if site:
        session_url = f"{session_url}?site={site}"

    session_payload = _request_json(
        "POST",
        session_url,
        headers={"Authorization": f"Bearer {auth_payload['access_token']}"},
    )
    return session_payload["link_token"]


def _select_provider(page, provider_name: str) -> None:
    page.locator("#step-select.active").wait_for(timeout=15000)
    page.locator("#institution-search").fill(provider_name)
    provider = page.locator(".institution-item", has_text=provider_name).first
    provider.wait_for(timeout=15000)
    provider.click()
    page.locator("#step-credentials.active").wait_for(timeout=15000)


def _bridge_init_script(bridge_kind: str) -> str:
    if bridge_kind == "react-native":
        return """
        window.__bridgeMessages = [];
        window.ReactNativeWebView = {
          postMessage(payload) {
            window.__bridgeMessages.push(JSON.parse(payload));
          },
        };
        """

    if bridge_kind == "webkit":
        return """
        window.__bridgeMessages = [];
        window.webkit = {
          messageHandlers: {
            plaidifyLink: {
              postMessage(payload) {
                window.__bridgeMessages.push(payload);
              },
            },
          },
        };
        """

    raise ValueError(f"Unsupported bridge kind: {bridge_kind}")


@pytest.fixture
def live_server():
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = _ThreadedUvicornServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(base_url)

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        try:
            browser_instance = playwright.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright browser is unavailable: {exc}")

        try:
            yield browser_instance
        finally:
            browser_instance.close()


def test_hosted_link_web_journey_returns_public_token(live_server, browser):
    link_token = _create_authenticated_link_session(live_server)
    provider_name = _get_hydro_one_provider_name(live_server)
    context = browser.new_context(viewport={"width": 1440, "height": 1100})

    try:
        page = context.new_page()
        page.goto(f"{live_server}/link?token={link_token}", wait_until="domcontentloaded")
        _select_provider(page, provider_name)

        assert page.locator("#provider-name").inner_text() == provider_name

        consent_text = page.locator("#consent-list").inner_text()
        assert "Return a secure completion back to your app when verification finishes." in consent_text
        assert "structured, read-only account data" not in consent_text
        assert "JSON" not in page.locator("body").inner_text()

        page.locator("#link-username").fill("demo-user")
        page.locator("#link-password").fill("Secret@pass123")
        page.locator("#connect-btn").click()

        page.locator("#step-success.active").wait_for(timeout=15000)

        success_text = page.locator("#success-message").inner_text()
        assert "Return to your app" in success_text

        reference_text = page.locator("#access-token-display").inner_text()
        assert "PUBLIC TOKEN" in reference_text
        assert "public-" in reference_text
        assert "access_token" not in reference_text
    finally:
        context.close()


@pytest.mark.parametrize("bridge_kind", ["react-native", "webkit"])
def test_hosted_link_native_bridges_only_receive_safe_payloads(live_server, browser, bridge_kind):
    link_token = _create_authenticated_link_session(live_server)
    provider_name = _get_hydro_one_provider_name(live_server)
    context = browser.new_context(
        viewport={"width": 393, "height": 852},
        has_touch=True,
        is_mobile=True,
    )

    try:
        page = context.new_page()
        page.add_init_script(_bridge_init_script(bridge_kind))
        page.goto(f"{live_server}/link?token={link_token}", wait_until="domcontentloaded")
        _select_provider(page, provider_name)

        page.locator("#link-username").fill("demo-user")
        page.locator("#link-password").fill("Secret@pass123")
        page.locator("#connect-btn").click()

        page.locator("#step-success.active").wait_for(timeout=15000)
        bridge_messages = page.evaluate("window.__bridgeMessages")

        assert any(message["event"] == "INSTITUTION_SELECTED" for message in bridge_messages)
        connected_event = next(message for message in bridge_messages if message["event"] == "CONNECTED")
        assert connected_event["source"] == "plaidify-link"
        assert connected_event["public_token"].startswith("public-")
        assert "data" not in connected_event
        assert "access_token" not in connected_event
    finally:
        context.close()
