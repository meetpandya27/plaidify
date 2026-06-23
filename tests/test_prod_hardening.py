"""Tests for production-hardening changes.

Covers: Prometheus metric wiring, /mfa/submit rate limiting, the registration
enable/disable gate, the env-driven first-user bootstrap, and the bcrypt shim.
"""

from unittest.mock import patch

import pytest
from prometheus_client import generate_latest

# ── Prometheus metrics wiring ────────────────────────────────────────────────


class TestMetrics:
    def test_recorders_expose_values(self):
        from src import metrics

        metrics.record_extraction("metric_site", "success")
        metrics.record_extraction("metric_site", "error")
        metrics.record_mfa_challenge("otp_input")
        metrics.set_browser_pool_active(2)

        out = generate_latest().decode()
        assert 'plaidify_blueprint_extractions_total{site="metric_site",status="success"}' in out
        assert 'plaidify_blueprint_extractions_total{site="metric_site",status="error"}' in out
        assert 'plaidify_mfa_challenges_total{mfa_type="otp_input"}' in out
        assert "plaidify_browser_pool_active_contexts 2.0" in out

    def test_recorders_never_raise(self):
        from src import metrics

        # Empty/odd inputs must be safe — metrics must never break a request flow.
        metrics.record_mfa_challenge("")
        metrics.record_extraction("s", "weird-status")
        metrics.set_browser_pool_active(0)


# ── /mfa/submit rate limiting ────────────────────────────────────────────────


class TestMfaRateLimit:
    @pytest.fixture(autouse=True)
    def _enable_limiter(self):
        from limits.storage.memory import MemoryStorage

        from src.dependencies import limiter

        limiter._limiter.storage = MemoryStorage()
        limiter.enabled = True
        yield
        limiter.enabled = False
        limiter._limiter.storage = MemoryStorage()

    def test_mfa_submit_is_rate_limited(self, client):
        # Default limit is 5/minute. A nonexistent session returns 200 with an
        # error status; the 6th call within the window should be 429.
        for _ in range(5):
            r = client.post("/mfa/submit", params={"session_id": "missing", "code": "000000"})
            assert r.status_code == 200
        blocked = client.post("/mfa/submit", params={"session_id": "missing", "code": "000000"})
        assert blocked.status_code == 429


# ── Registration gate ────────────────────────────────────────────────────────


class TestRegistrationGate:
    def test_registration_disabled_returns_403(self, client):
        with patch("src.routers.auth.settings.registration_enabled", False):
            r = client.post(
                "/auth/register",
                json={"username": "gateuser", "email": "gate@plaidify.dev", "password": "TestPass123!"},
            )
            assert r.status_code == 403

    def test_registration_enabled_by_default(self, client):
        r = client.post(
            "/auth/register",
            json={"username": "gateuser2", "email": "gate2@plaidify.dev", "password": "TestPass123!"},
        )
        assert r.status_code == 200


# ── First-user bootstrap ─────────────────────────────────────────────────────


class TestBootstrapUser:
    def test_bootstrap_creates_user_and_is_idempotent(self, client):
        import src.app as appmod
        from src.database import get_db

        # Run the bootstrap against the same test-db session the API uses.
        override = appmod.app.dependency_overrides.get(get_db)
        assert override is not None

        with (
            patch.object(appmod.settings, "bootstrap_user_username", "bootuser"),
            patch.object(appmod.settings, "bootstrap_user_email", "boot@plaidify.dev"),
            patch.object(appmod.settings, "bootstrap_user_password", "BootPass123!"),
            patch.object(appmod, "get_db", override),
        ):
            appmod._bootstrap_user()
            appmod._bootstrap_user()  # idempotent: must not raise or duplicate

        # The bootstrapped account can authenticate.
        r = client.post("/auth/token", data={"username": "bootuser", "password": "BootPass123!"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_bootstrap_noop_when_unset(self):
        import src.app as appmod

        with patch.object(appmod.settings, "bootstrap_user_username", None):
            appmod._bootstrap_user()  # no-op, no error


# ── bcrypt shim ──────────────────────────────────────────────────────────────


class TestBcryptShim:
    def test_password_hash_and_verify(self):
        from src.dependencies import get_password_hash, verify_password

        hashed = get_password_hash("Sup3r!secret")
        assert verify_password("Sup3r!secret", hashed)
        assert not verify_password("wrong", hashed)

    def test_bcrypt_about_shim_present(self):
        import bcrypt

        # The shim ensures passlib can read the version without warning.
        assert hasattr(bcrypt, "__about__")
        assert hasattr(bcrypt.__about__, "__version__")
