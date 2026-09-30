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
            r = client.post("/mfa/submit", json={"session_id": "missing", "code": "000000"})
            assert r.status_code == 200
        blocked = client.post("/mfa/submit", json={"session_id": "missing", "code": "000000"})
        assert blocked.status_code == 429


# ── Access-log redaction ─────────────────────────────────────────────────────


class TestAccessLogRedaction:
    def _access_record(self, path: str):
        import logging

        # The shape uvicorn's httptools/h11 protocols log for every request.
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "POST", path, "1.1", 200),
            None,
        )

    def test_mfa_code_and_session_are_redacted(self):
        from src.logging_config import AccessLogRedactFilter

        record = self._access_record("/mfa/submit?session_id=access-123&code=654321")
        assert AccessLogRedactFilter().filter(record) is True
        line = record.getMessage()
        assert "654321" not in line
        assert "access-123" not in line
        assert "/mfa/submit?session_id=REDACTED&code=REDACTED" in line

    def test_link_token_redacted_and_other_params_kept(self):
        from src.logging_config import redact_query

        assert redact_query("/link?token=lnk-secret&theme=dark") == "/link?token=REDACTED&theme=dark"
        assert redact_query("/health") == "/health"

    def test_site_credentials_in_legacy_query_are_redacted(self):
        from src.logging_config import redact_query

        line = redact_query("/submit_credentials?link_token=lnk-1&username=alice&password=hunter2")
        assert "alice" not in line and "hunter2" not in line and "lnk-1" not in line
        assert redact_query("/fetch_data?access_token=tok-9&consent_token=c-1") == (
            "/fetch_data?access_token=REDACTED&consent_token=REDACTED"
        )

    def test_credentials_in_the_path_are_redacted(self):
        from src.logging_config import AccessLogRedactFilter, redact_path

        assert redact_path("/tokens/tok-9") == "/tokens/REDACTED"
        assert redact_path("/links/lnk-1") == "/links/REDACTED"
        assert redact_path("/link/sessions/lnk-1/status") == "/link/sessions/REDACTED/status"
        assert redact_path("/link/events/lnk-1") == "/link/events/REDACTED"
        assert redact_path("/encryption/public_key/lnk-1") == "/encryption/public_key/REDACTED"
        assert redact_path("/mfa/status/access-1") == "/mfa/status/REDACTED"
        assert redact_path("/access_jobs/ajob-1") == "/access_jobs/REDACTED"
        assert redact_path("/refresh/schedule/tok-9") == "/refresh/schedule/REDACTED"
        assert redact_path("/consent/ctok-1") == "/consent/REDACTED"
        # Static routes and plain ids stay readable.
        for path in (
            "/link/sessions/bootstrap",
            "/link/sessions/public",
            "/consent/request",
            "/consent/creq-1/approve",
            "/agents/7",
            "/access_jobs",
            "/refresh/schedule",
        ):
            assert redact_path(path) == path

        record = self._access_record("/link/sessions/lnk-secret/status?token=lnk-secret")
        AccessLogRedactFilter().filter(record)
        assert "lnk-secret" not in record.getMessage()

    def test_filter_is_installed_on_the_access_logger(self):
        import logging

        from src.logging_config import AccessLogRedactFilter, setup_logging

        setup_logging(level="WARNING", log_format="text")
        setup_logging(level="WARNING", log_format="text")
        filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, AccessLogRedactFilter)]
        assert len(filters) == 1


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
