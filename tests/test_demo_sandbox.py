"""Tests for the bundled demo/sandbox: portal target, connector, and discovery gating."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.core.blueprint import blueprint_is_discoverable, load_blueprint
from src.demo.bank import app as bank_app
from src.demo.portal import MFA_CODE
from src.demo.portal import app as portal_app
from src.demo.saas import app as saas_app

DEMO_CONNECTOR = Path("connectors/demo_utility.json")
DEMO_BANK_CONNECTOR = Path("connectors/demo_bank.json")
DEMO_SAAS_CONNECTOR = Path("connectors/demo_saas.json")


# ── Demo portal target site ──────────────────────────────────────────────────


@pytest.fixture
def portal():
    return TestClient(portal_app)


class TestDemoPortal:
    def test_health(self, portal):
        resp = portal.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_login_page_has_form_fields(self, portal):
        html = portal.get("/login").text
        assert "id='username'" in html
        assert "id='password'" in html
        assert "id='login-btn'" in html

    def test_login_no_mfa_reaches_dashboard(self, portal):
        resp = portal.post(
            "/login",
            data={"username": "demo_user", "password": "demo_pass"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "id='dashboard'" in resp.text
        assert "id='current-bill'" in resp.text

    def test_login_mfa_requires_code(self, portal):
        resp = portal.post(
            "/login",
            data={"username": "demo_mfa", "password": "demo_pass"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "id='otp-input'" in resp.text

        verified = portal.post("/mfa", data={"code": MFA_CODE}, follow_redirects=True)
        assert verified.status_code == 200
        assert "id='dashboard'" in verified.text

    def test_invalid_credentials_rejected(self, portal):
        resp = portal.post(
            "/login",
            data={"username": "demo_user", "password": "wrong"},
            follow_redirects=True,
        )
        assert resp.status_code == 401

    def test_dashboard_requires_auth(self, portal):
        resp = portal.get("/dashboard", follow_redirects=False)
        assert resp.status_code in (302, 303)


# ── Demo connector blueprint ─────────────────────────────────────────────────


class TestDemoConnector:
    def test_connector_file_exists(self):
        assert DEMO_CONNECTOR.exists(), "demo_utility.json connector must ship with the repo"

    def test_connector_loads_and_validates(self):
        bp = load_blueprint(DEMO_CONNECTOR)
        assert bp.mfa is not None
        assert "sandbox" in bp.tags and "demo" in bp.tags

    def test_connector_extract_fields(self):
        bp = load_blueprint(DEMO_CONNECTOR)
        for field in ("current_bill", "account_number", "usage_history", "payments"):
            assert field in bp.extract

    def test_connector_has_ui_schemas(self):
        bp = load_blueprint(DEMO_CONNECTOR)
        assert bp.credential_schema and bp.credential_schema.get("fields")
        assert bp.mfa_schema and "otp_input" in bp.mfa_schema


# ── Discoverability gating helper ────────────────────────────────────────────


class TestDiscoverabilityHelper:
    def test_normal_connector_always_discoverable(self):
        assert blueprint_is_discoverable(["utility", "us"], demo_mode=False)
        assert blueprint_is_discoverable(["utility", "us"], demo_mode=True)

    def test_internal_fixture_never_discoverable(self):
        assert not blueprint_is_discoverable(["internal", "fixture"], demo_mode=False)
        assert not blueprint_is_discoverable(["internal", "fixture"], demo_mode=True)

    def test_sandbox_only_in_demo_mode(self):
        assert not blueprint_is_discoverable(["sandbox", "demo"], demo_mode=False)
        assert blueprint_is_discoverable(["sandbox", "demo"], demo_mode=True)

    def test_empty_tags_discoverable(self):
        assert blueprint_is_discoverable(None, demo_mode=False)
        assert blueprint_is_discoverable([], demo_mode=False)


# ── Discovery API gating (/blueprints) ───────────────────────────────────────


class TestBlueprintDiscoveryAPI:
    def test_demo_connector_hidden_by_default(self, client):
        with patch("src.routers.system.settings.demo_mode", False):
            listing = client.get("/blueprints").json()
            sites = {b["site"] for b in listing["blueprints"]}
            assert "demo_utility" not in sites

            detail = client.get("/blueprints/demo_utility")
            assert detail.status_code == 404

    def test_demo_connector_visible_in_demo_mode(self, client):
        with patch("src.routers.system.settings.demo_mode", True):
            listing = client.get("/blueprints").json()
            sites = {b["site"] for b in listing["blueprints"]}
            assert "demo_utility" in sites

            detail = client.get("/blueprints/demo_utility")
            assert detail.status_code == 200
            assert detail.json()["has_mfa"] is True


# ── Additional diverse demo sites (different DOM / login / MFA) ───────────────


@pytest.fixture
def bank():
    return TestClient(bank_app)


@pytest.fixture
def saas():
    return TestClient(saas_app)


class TestDemoBankSite:
    """Acme Bank — email login + security-question MFA + account/transaction layout."""

    def test_health(self, bank):
        assert bank.get("/health").json()["status"] == "ok"

    def test_login_page_uses_email_field(self, bank):
        html = bank.get("/login").text
        assert "id='email'" in html
        assert "id='passcode'" in html
        assert "id='signin'" in html

    def test_security_question_flow(self, bank):
        verify = bank.post(
            "/auth",
            data={"email": "demo@acme.test", "passcode": "demo_pass"},
            follow_redirects=True,
        )
        assert verify.status_code == 200
        assert "id='mfa-question'" in verify.text
        assert "id='security-answer'" in verify.text

        accounts = bank.post("/verify", data={"answer": "plaidify"}, follow_redirects=True)
        assert accounts.status_code == 200
        assert "id='accounts-page'" in accounts.text
        assert "id='checking-balance'" in accounts.text

    def test_wrong_security_answer_rejected(self, bank):
        bank.post("/auth", data={"email": "demo@acme.test", "passcode": "demo_pass"}, follow_redirects=True)
        resp = bank.post("/verify", data={"answer": "nope"}, follow_redirects=True)
        assert resp.status_code == 401


class TestDemoSaasSite:
    """CloudMail — username login, no MFA, card layout."""

    def test_health(self, saas):
        assert saas.get("/health").json()["status"] == "ok"

    def test_login_then_workspace_no_mfa(self, saas):
        resp = saas.post(
            "/signin",
            data={"user": "demo_saas", "pass": "demo_pass"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "id='workspace'" in resp.text
        assert "id='storage-used'" in resp.text

    def test_invalid_login_rejected(self, saas):
        resp = saas.post("/signin", data={"user": "demo_saas", "pass": "bad"}, follow_redirects=True)
        assert resp.status_code == 401


class TestDiverseConnectors:
    def test_bank_connector_security_question(self):
        bp = load_blueprint(DEMO_BANK_CONNECTOR)
        assert bp.mfa is not None
        assert bp.mfa.type.value == "security_question"
        assert "transactions" in bp.extract

    def test_saas_connector_has_no_mfa(self):
        bp = load_blueprint(DEMO_SAAS_CONNECTOR)
        assert bp.mfa is None
        assert "sign_in_activity" in bp.extract

    def test_all_demo_connectors_discoverable_in_demo_mode(self, client):
        with patch("src.routers.system.settings.demo_mode", True):
            sites = {b["site"] for b in client.get("/blueprints").json()["blueprints"]}
            assert {"demo_utility", "demo_bank", "demo_saas"} <= sites


# ── Hosted-Link picker catalog (organization directory) ──────────────────────


class TestDemoPickerCatalog:
    def _reload_catalog(self, demo_mode: bool):
        """Clear cached catalog and rebuild under the given demo_mode."""
        import src.organization_catalog as cat

        cat._load_connector_templates.cache_clear()
        cat.get_organization_catalog.cache_clear()
        return patch("src.organization_catalog.settings.demo_mode", demo_mode)

    def test_sandbox_entries_absent_by_default(self):
        import src.organization_catalog as cat

        with self._reload_catalog(False):
            results = cat.search_organizations(limit=10)["results"]
            assert all(not r["organization_id"].startswith("sandbox-") for r in results)
        cat._load_connector_templates.cache_clear()
        cat.get_organization_catalog.cache_clear()

    def test_sandbox_entries_present_and_first_in_demo_mode(self):
        import src.organization_catalog as cat

        with self._reload_catalog(True):
            results = cat.search_organizations(limit=10)["results"]
            ids = [r["organization_id"] for r in results]
            assert "sandbox-demo-utility" in ids
            assert "sandbox-demo-bank" in ids
            assert "sandbox-demo-saas" in ids
            # Sandbox entries float to the top of the default (no-query) listing.
            assert ids[0].startswith("sandbox-")
        cat._load_connector_templates.cache_clear()
        cat.get_organization_catalog.cache_clear()

    def test_sandbox_entry_maps_to_connector(self):
        import src.organization_catalog as cat

        with self._reload_catalog(True):
            entry = cat.get_organization_by_id("sandbox-demo-bank")
            assert entry is not None
            assert entry["site"] == "demo_bank"
            assert entry["has_mfa"] is True
        cat._load_connector_templates.cache_clear()
        cat.get_organization_catalog.cache_clear()
