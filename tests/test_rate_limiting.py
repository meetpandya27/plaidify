"""
Tests for rate limiting on auth and connect endpoints.

Covers:
- Login rate limiting (5/minute)
- Registration rate limiting (3/minute)
- Rate limit headers in responses
- Rate limit bypass when disabled
"""

import pytest


@pytest.fixture(autouse=True)
def enable_rate_limiter():
    """Enable the rate limiter with fresh storage for this test module only."""
    from limits.storage.memory import MemoryStorage

    from src.dependencies import limiter

    limiter._limiter.storage = MemoryStorage()
    limiter.enabled = True
    yield
    limiter.enabled = False
    limiter._limiter.storage = MemoryStorage()


class TestAuthRateLimiting:
    """Tests for rate limiting on authentication endpoints."""

    def test_login_rate_limit_returns_429(self, client):
        """Exceeding login rate limit should return 429."""
        # Register a user first
        client.post(
            "/auth/register",
            json={
                "username": "ratelimituser",
                "email": "ratelimit@example.com",
                "password": "Secure@pass123",
            },
        )

        # Make requests up to the limit (5/minute)
        for _ in range(5):
            client.post(
                "/auth/token",
                data={"username": "ratelimituser", "password": "Secure@pass123"},
            )

        # The 6th request should be rate limited
        response = client.post(
            "/auth/token",
            data={"username": "ratelimituser", "password": "Secure@pass123"},
        )
        assert response.status_code == 429

    def test_register_rate_limit_returns_429(self, client):
        """Exceeding registration rate limit (3/minute) should return 429."""
        for i in range(3):
            client.post(
                "/auth/register",
                json={
                    "username": f"reguser{i}",
                    "email": f"reguser{i}@example.com",
                    "password": "Secure@pass123",
                },
            )

        # The 4th request should be rate limited
        response = client.post(
            "/auth/register",
            json={
                "username": "reguser_blocked",
                "email": "reguser_blocked@example.com",
                "password": "Secure@pass123",
            },
        )
        assert response.status_code == 429

    def test_rate_limit_does_not_affect_other_endpoints(self, client):
        """Non-rate-limited endpoints should still work under default limits."""
        # Health and status endpoints should tolerate many requests
        for _ in range(10):
            response = client.get("/health")
            assert response.status_code in (200, 503)  # 503 if DB unhealthy


class TestConnectRateLimiting:
    """Tests for rate limiting on the /connect endpoint."""

    def test_connect_rate_limit_returns_429(self, client):
        """Exceeding connect rate limit should return 429."""
        # The connect endpoint will fail (no blueprint), but rate limiting
        # happens before the handler logic, so we can still test the limit.
        for _ in range(10):
            client.post(
                "/connect",
                json={
                    "site": "nonexistent",
                    "username": "test",
                    "password": "test",
                },
            )

        # The 11th request should be rate limited
        response = client.post(
            "/connect",
            json={
                "site": "nonexistent",
                "username": "test",
                "password": "test",
            },
        )
        assert response.status_code == 429


class TestRateLimitDisabled:
    """Tests for when rate limiting is disabled."""

    def test_no_429_when_disabled(self, client):
        """When rate limiting is disabled, no 429 responses should occur."""
        from src.dependencies import limiter

        limiter.enabled = False

        try:
            for i in range(10):
                response = client.post(
                    "/auth/register",
                    json={
                        "username": f"nolimit{i}",
                        "email": f"nolimit{i}@example.com",
                        "password": "Secure@pass123",
                    },
                )
                # Should get 200 (success) or 400 (duplicate), never 429
                assert response.status_code != 429
        finally:
            limiter.enabled = True


class TestDefaultLimit:
    """RATE_LIMIT_DEFAULT covers every route without its own limit, per client address and path."""

    def test_undecorated_route_gets_the_default_limit(self, client, auth_headers):
        from unittest.mock import patch

        from src import app as appmod

        with patch.object(appmod.settings, "rate_limit_default", "3/minute"):
            codes = [client.get("/links", headers=auth_headers).status_code for _ in range(4)]
            blocked = client.get("/links", headers=auth_headers)
            other_path = client.get("/tokens", headers=auth_headers)
        assert codes[:3] == [200, 200, 200] and codes[3] == 429
        assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1
        assert other_path.status_code == 200

    def test_a_browser_can_read_the_429_and_preflights_do_not_count(self, client, auth_headers):
        from unittest.mock import patch

        from src import app as appmod

        origin = {"Origin": "http://localhost:3000"}
        preflight = {
            **origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        }
        with patch.object(appmod.settings, "rate_limit_default", "2/minute"):
            for _ in range(5):
                assert client.options("/links", headers=preflight).status_code == 200
            codes = [client.get("/links", headers={**auth_headers, **origin}).status_code for _ in range(3)]
            blocked = client.get("/links", headers={**auth_headers, **origin})
        assert codes == [200, 200, 429]
        assert blocked.status_code == 429
        assert blocked.headers["access-control-allow-origin"] == "http://localhost:3000"

    def test_probes_and_the_hosted_pages_polling_are_exempt(self, client):
        from unittest.mock import patch

        from src import app as appmod

        with patch.object(appmod.settings, "rate_limit_default", "2/minute"):
            for _ in range(6):
                assert client.get("/health").status_code in (200, 503)
                assert client.get("/link/sessions/no-such-token/status").status_code != 429
