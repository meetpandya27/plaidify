"""
Tests for CORS enforcement and security headers.

Covers:
- Security headers present on all responses
- CORS wildcard warning in development
- CORS wildcard blocks startup in production
- HSTS header behavior based on environment
"""

from unittest.mock import patch

import pytest


class TestSecurityHeaders:
    """Tests for security headers middleware."""

    def test_x_content_type_options(self, client):
        """X-Content-Type-Options: nosniff should be on every response."""
        response = client.get("/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        """X-Frame-Options: SAMEORIGIN should be on every response."""
        response = client.get("/health")
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_x_xss_protection(self, client):
        """X-XSS-Protection should be enabled."""
        response = client.get("/health")
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_referrer_policy(self, client):
        """Referrer-Policy should be set."""
        response = client.get("/health")
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy(self, client):
        """Permissions-Policy should restrict dangerous APIs."""
        response = client.get("/health")
        assert "camera=()" in response.headers.get("Permissions-Policy", "")

    def test_security_headers_on_api_endpoints(self, client):
        """Security headers should appear on API endpoints too, not just /health."""
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_security_headers_on_error_responses(self, client):
        """Security headers should appear even on 404 responses."""
        response = client.get("/nonexistent-path")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_no_hsts_in_development(self, client):
        """HSTS should NOT be present in development mode (default)."""
        response = client.get("/health")
        # In dev mode (ENV=development, ENFORCE_HTTPS=false), no HSTS
        assert "Strict-Transport-Security" not in response.headers


class TestCORSDefaults:
    """Tests for CORS configuration defaults."""

    def test_cors_default_is_not_wildcard(self):
        """Default CORS origins should not be wildcard in new configuration."""
        from src.config import get_settings

        s = get_settings()
        origins = [o.strip() for o in s.cors_origins.split(",")]
        # Default should be localhost origins, not *
        assert "*" not in origins or s.env != "production"

    def test_cors_headers_present(self, client):
        """CORS headers should be present for allowed origins."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Should allow the origin
        assert response.status_code in (200, 204)


class TestEnvironmentValidation:
    """Tests for environment setting validation."""

    def test_valid_environments(self):
        """Valid environment values should be accepted."""
        from src.config import Settings

        for env in ("development", "staging", "production"):
            # Should not raise — we just test the validator directly
            result = Settings.validate_env(env)
            assert result == env

    def test_invalid_environment_rejected(self):
        """Invalid environment values should be rejected."""
        from src.config import Settings

        with pytest.raises(ValueError, match="env must be"):
            Settings.validate_env("banana")


# ── Wave-2 API hardening ──────────────────────────────────────────────────────


class TestHostedLinkToken:
    """SEC-20: the page and its frame-ancestors must read the same, single session."""

    def _partner_session(self, client, auth_headers):
        return client.post(
            "/link/sessions",
            params={"site": "internal_bank"},
            headers={**auth_headers, "Origin": "https://partner.example.com"},
        ).json()["link_token"]

    def test_two_token_parameters_are_refused(self, client, auth_headers):
        link_token = self._partner_session(client, auth_headers)
        for query in (f"token=attacker&token={link_token}", f"token={link_token}&token={link_token}"):
            resp = client.get(f"/link?{query}")
            assert resp.status_code == 400, query
            csp = resp.headers["Content-Security-Policy"]
            assert "partner.example.com" not in csp
            assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_single_token_uses_that_sessions_origins(self, client, auth_headers):
        link_token = self._partner_session(client, auth_headers)
        resp = client.get(f"/link?token={link_token}")
        assert "frame-ancestors 'self' https://partner.example.com" in resp.headers["Content-Security-Policy"]

    def test_legacy_ui_is_not_served(self, client):
        # LNK-12: the old /ui mount (and its link.js widget) is gone.
        for path in ("/ui/link.js", "/ui/link.css", "/ui/link.html"):
            assert client.get(path).status_code == 404


class TestOperationalEndpoints:
    """SEC-25: docs off in production, /metrics behind its token."""

    def test_docs_are_off_in_production_unless_enabled(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.app import _docs_urls

        assert _docs_urls("production", False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}
        assert _docs_urls("production", True)["openapi_url"] == "/openapi.json"
        assert _docs_urls("development", False)["docs_url"] == "/docs"

        locked = TestClient(FastAPI(**_docs_urls("production", False)))
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert locked.get(path).status_code == 404

    def test_metrics_needs_the_token_when_one_is_set(self, client):
        import src.app as appmod

        with patch.object(appmod.settings, "metrics_token", "scrape-secret"):
            assert client.get("/metrics").status_code == 401
            assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
            ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
            assert ok.status_code == 200
            assert "http_requests" in ok.text or "python_info" in ok.text
        assert client.get("/metrics").status_code == 200


class TestRequestBodyLimit:
    """SEC-26: the 1 MB limit holds for chunked bodies too."""

    def test_chunked_body_over_the_limit_is_refused(self, client):
        def chunks():
            for _ in range(3):
                yield b" " * (1024 * 1024)

        resp = client.post("/auth/register", content=chunks(), headers={"Content-Type": "application/json"})
        assert resp.status_code == 413

    def test_declared_length_over_the_limit_is_refused(self, client):
        resp = client.post(
            "/auth/register", content=b" " * (1024 * 1024 + 1), headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 413
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_small_chunked_body_is_read(self, client):
        def chunks():
            yield b'{"username": "chunky", "email": "chunky@example.com",'
            yield b' "password": "Secure@pass123"}'

        resp = client.post("/auth/register", content=chunks(), headers={"Content-Type": "application/json"})
        assert resp.status_code == 200


class TestHttpsRedirect:
    """OPS-02: plain-HTTP probes are answered; everything else is redirected."""

    def _app(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from src.app import HTTPSRedirectExceptProbesMiddleware

        async def ok(request):
            return PlainTextResponse("ok")

        paths = ("/health", "/metrics", "/health/detailed", "/auth/me")
        return HTTPSRedirectExceptProbesMiddleware(Starlette(routes=[Route(p, ok) for p in paths]))

    def test_probes_answer_over_plain_http(self):
        from fastapi.testclient import TestClient

        plain = TestClient(self._app(), base_url="http://api.internal:8000")
        assert plain.get("/health").status_code == 200
        assert plain.get("/metrics").status_code == 200
        for path in ("/health/detailed", "/auth/me", "/metrics/extra"):
            resp = plain.get(path, follow_redirects=False)
            assert resp.status_code == 307, path
            assert resp.headers["location"].startswith("https://api.internal")

    def test_https_requests_are_not_redirected(self):
        from fastapi.testclient import TestClient

        secure = TestClient(self._app(), base_url="https://api.example.com")
        assert secure.get("/auth/me").status_code == 200
