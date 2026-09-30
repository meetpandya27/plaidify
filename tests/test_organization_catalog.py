"""Tests for the organization directory (hosted Link picker).

Only organizations backed by a real connector are supported and routed to a
site; fictional sample organizations exist only in demo mode, are never
supported, and are never routed anywhere (JOB-05).
"""

from contextlib import contextmanager
from unittest.mock import patch

import src.organization_catalog as catalog


@contextmanager
def _demo_mode(enabled: bool):
    catalog.refresh_organization_catalog()
    with patch("src.organization_catalog.settings.demo_mode", enabled):
        try:
            yield
        finally:
            catalog.refresh_organization_catalog()


class TestOrganizationCatalog:
    def test_only_real_connectors_are_listed_outside_demo_mode(self, client):
        with _demo_mode(False):
            payload = client.get("/organizations/search", params={"limit": 100}).json()

        assert payload["count"] >= 1
        for entry in payload["results"]:
            assert entry["supported"] is True
            assert entry["site"]
            assert not entry.get("is_sample")
            assert not entry.get("is_sandbox")
        assert {entry["site"] for entry in payload["results"]} >= {"hydro_one"}

    def test_nothing_fictional_is_routed_to_hydro_one(self, client):
        with _demo_mode(True):
            entries = catalog.get_organization_catalog()
            routed = [entry for entry in entries if entry.get("site") == "hydro_one"]

        assert [entry["name"] for entry in routed] == ["Hydro One"]
        samples = [entry for entry in entries if entry.get("is_sample")]
        assert len(samples) > 10000
        assert all(entry["supported"] is False and entry["site"] is None for entry in samples)

    def test_no_samples_outside_demo_mode(self, client):
        with _demo_mode(False):
            assert not any(entry.get("is_sample") for entry in catalog.get_organization_catalog())
            payload = client.get(
                "/organizations/search", params={"q": "North Harbor Bank", "include_unsupported": True}
            ).json()
        assert payload["count"] == 0

    def test_samples_are_hidden_from_default_search_in_demo_mode(self, client):
        with _demo_mode(True):
            default = client.get("/organizations/search", params={"q": "Maple Trust"}).json()
            browsing = client.get(
                "/organizations/search",
                params={
                    "q": "Maple Trust",
                    "country": "CA",
                    "category": "finance",
                    "limit": 5,
                    "include_unsupported": True,
                },
            ).json()

        assert default["count"] == 0
        assert browsing["count"] >= 1
        first = browsing["results"][0]
        assert first["country_code"] == "CA"
        assert first["category"] == "finance"
        assert "Maple Trust" in first["name"]
        assert first["supported"] is False and first["site"] is None

    def test_real_connector_entry_is_described_from_its_blueprint(self, client):
        with _demo_mode(False):
            payload = client.get("/organizations/search", params={"site": "hydro_one", "limit": 3}).json()

        [entry] = payload["results"]
        assert entry["name"] == "Hydro One"
        assert entry["category"] == "utility"
        assert entry["country_code"] == "CA"
        assert entry["region_code"] == "ON"
        assert entry["has_mfa"] is True
        # Connector-specific schemas from the blueprint win over category defaults.
        assert entry["credential_schema"]["fields"][0]["label"].startswith("Hydro One")
        assert "otp_input" in entry["mfa_schema"]

    def test_summary_counts_supported_entries(self, client):
        with _demo_mode(False):
            summary = client.get("/organizations/summary").json()
        assert summary["total_count"] >= 1
        assert {item["site"] for item in summary["connector_templates"]} >= {"hydro_one"}

    def test_get_single_organization(self, client):
        with _demo_mode(False):
            search_response = client.get("/organizations/search", params={"limit": 1})
            organization_id = search_response.json()["results"][0]["organization_id"]
            response = client.get(f"/organizations/{organization_id}")
        assert response.status_code == 200
        assert response.json()["organization_id"] == organization_id

    def test_search_validates_pagination(self, client):
        response = client.get("/organizations/search", params={"limit": 0})
        assert response.status_code == 422

    def test_results_expose_branding_metadata(self, client):
        with _demo_mode(True):
            results = client.get("/organizations/search", params={"limit": 10, "include_unsupported": True}).json()[
                "results"
            ]
        # #53: every entry ships logo + brand palette + auth affordances.
        for result in results:
            assert result["logo_url"].startswith("data:image/svg+xml;base64,")
            assert result["logo_monogram"] and result["logo_monogram"].isupper()
            assert result["primary_color"].startswith("#") and len(result["primary_color"]) == 7
            assert result["secondary_color"].startswith("#") and len(result["secondary_color"]) == 7
            assert result["accent_color"].startswith("#")
            assert result["hint_copy"]
            assert result["auth_style"] in {"username_password", "email_password", "member_number"}

    def test_credential_schema_defaults_follow_auth_style(self, client):
        with _demo_mode(True):

            def first(category):
                return client.get(
                    "/organizations/search", params={"category": category, "limit": 1, "include_unsupported": True}
                ).json()["results"][0]

            finance, government = first("finance"), first("government")

        for org in (finance, government):
            field_ids = [field["id"] for field in org["credential_schema"]["fields"]]
            assert "username" in field_ids and "password" in field_ids
            assert isinstance(org["mfa_schema"], dict) and org["mfa_schema"]
        government_username = next(f for f in government["credential_schema"]["fields"] if f["id"] == "username")
        assert government_username["label"].lower().startswith("member")
