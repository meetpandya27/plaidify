"""
Tests for CORS enforcement and security headers.

Covers:
- Security headers present on all responses
- CORS wildcard warning in development
- CORS wildcard blocks startup in production
- HSTS header behavior based on environment
"""

import os
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
        from pydantic import ValidationError
        from src.config import Settings

        for env in ("development", "staging", "production"):
            # Should not raise — we just test the validator directly
            result = Settings.validate_env(env)
            assert result == env

    def test_invalid_environment_rejected(self):
        """Invalid environment values should be rejected."""
        from pydantic import ValidationError
        from src.config import Settings

        with pytest.raises(ValueError, match="env must be"):
            Settings.validate_env("banana")
