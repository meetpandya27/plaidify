"""Tests for the PlaidifySync (synchronous) client."""

import httpx
import respx
import pytest

from plaidify.client import PlaidifySync


BASE = "http://test-server:8000"


class TestSyncClient:
    @respx.mock
    def test_health(self):
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={
            "status": "healthy",
            "version": "0.2.0",
        }))
        with PlaidifySync(server_url=BASE) as pfy:
            h = pfy.health()
        assert h.status == "healthy"

    @respx.mock
    def test_connect(self):
        respx.post(f"{BASE}/connect").mock(return_value=httpx.Response(200, json={
            "status": "connected",
            "data": {"balance": 42.0},
        }))
        with PlaidifySync(server_url=BASE) as pfy:
            result = pfy.connect("bank", username="u", password="p")
        assert result.connected
        assert result.data["balance"] == 42.0

    @respx.mock
    def test_list_blueprints(self):
        respx.get(f"{BASE}/blueprints").mock(return_value=httpx.Response(200, json={
            "blueprints": [
                {"site": "gg", "name": "GG", "domain": "gg.com", "tags": [], "has_mfa": False, "schema_version": "2"},
            ],
            "count": 1,
        }))
        with PlaidifySync(server_url=BASE) as pfy:
            result = pfy.list_blueprints()
        assert result.count == 1

    @respx.mock
    def test_get_blueprint(self):
        respx.get(f"{BASE}/blueprints/test").mock(return_value=httpx.Response(200, json={
            "name": "Test",
            "domain": "test.com",
            "tags": [],
            "has_mfa": False,
            "extract_fields": ["data"],
            "schema_version": "2",
        }))
        with PlaidifySync(server_url=BASE) as pfy:
            bp = pfy.get_blueprint("test")
        assert bp.name == "Test"

    @respx.mock
    def test_context_manager(self):
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={
            "status": "ok",
        }))
        with PlaidifySync(server_url=BASE) as pfy:
            h = pfy.health()
            assert h.status == "ok"
        # After exit, loop should be closed
        assert pfy._loop is None

    @respx.mock
    def test_manual_close(self):
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={
            "status": "ok",
        }))
        pfy = PlaidifySync(server_url=BASE)
        h = pfy.health()
        assert h.status == "ok"
        pfy.close()
        assert pfy._loop is None
