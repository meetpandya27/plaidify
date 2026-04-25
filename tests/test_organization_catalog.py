"""Tests for the generated organization directory."""


class TestOrganizationCatalog:
    def test_directory_summary_exposes_large_catalog(self, client):
        response = client.get("/organizations/summary")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total_count"] > 10000
        assert any(item["code"] == "US" for item in payload["countries"])
        assert any(item["code"] == "CA" for item in payload["countries"])
        assert any(item["key"] == "finance" for item in payload["categories"])
        assert any(item["key"] == "utility" for item in payload["categories"])

    def test_search_returns_filtered_results(self, client):
        response = client.get(
            "/organizations/search",
            params={"q": "Maple Trust", "country": "CA", "category": "finance", "limit": 5},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] >= 1
        assert payload["returned"] <= 5
        assert payload["results"]
        first = payload["results"][0]
        assert first["country_code"] == "CA"
        assert first["category"] == "finance"
        assert "Maple Trust" in first["name"]

    def test_search_can_filter_by_site(self, client):
        response = client.get("/organizations/search", params={"site": "hydro_one", "limit": 3})

        assert response.status_code == 200
        payload = response.json()
        assert payload["results"]
        assert all(item["site"] == "hydro_one" for item in payload["results"])

    def test_get_single_organization(self, client):
        search_response = client.get("/organizations/search", params={"limit": 1})
        organization_id = search_response.json()["results"][0]["organization_id"]

        response = client.get(f"/organizations/{organization_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["organization_id"] == organization_id

    def test_search_validates_pagination(self, client):
        response = client.get("/organizations/search", params={"limit": 0})
        assert response.status_code == 422

    def test_results_expose_branding_metadata(self, client):
        response = client.get("/organizations/search", params={"category": "finance", "limit": 1})
        assert response.status_code == 200
        result = response.json()["results"][0]
        # #53: every entry ships logo + brand palette + auth affordances.
        assert result["logo_url"].startswith("data:image/svg+xml;base64,")
        assert result["logo_monogram"] and result["logo_monogram"].isupper()
        assert result["primary_color"].startswith("#") and len(result["primary_color"]) == 7
        assert result["secondary_color"].startswith("#") and len(result["secondary_color"]) == 7
        assert result["accent_color"].startswith("#")
        assert result["hint_copy"]
        assert result["auth_style"] in {"username_password", "email_password", "member_number"}

    def test_branding_differs_across_categories(self, client):
        finance = client.get("/organizations/search", params={"category": "finance", "limit": 1}).json()["results"][0]
        utility = client.get("/organizations/search", params={"category": "utility", "limit": 1}).json()["results"][0]
        assert finance["primary_color"] != utility["primary_color"]
        assert finance["auth_style"] != utility["auth_style"] or finance["hint_copy"] != utility["hint_copy"]

    def test_credential_schema_defaults_follow_auth_style(self, client):
        finance = client.get("/organizations/search", params={"category": "finance", "limit": 1}).json()["results"][0]
        utility = client.get("/organizations/search", params={"category": "utility", "limit": 1}).json()["results"][0]
        government = client.get("/organizations/search", params={"category": "government", "limit": 1}).json()[
            "results"
        ][0]

        # Every org ships a credential_schema + mfa_schema (#54).
        for org in (finance, utility, government):
            schema = org["credential_schema"]
            assert isinstance(schema.get("fields"), list) and schema["fields"]
            field_ids = [field["id"] for field in schema["fields"]]
            assert "username" in field_ids and "password" in field_ids
            mfa_schema = org["mfa_schema"]
            assert isinstance(mfa_schema, dict) and mfa_schema

        finance_username = next(field for field in finance["credential_schema"]["fields"] if field["id"] == "username")
        utility_username = next(field for field in utility["credential_schema"]["fields"] if field["id"] == "username")
        government_username = next(
            field for field in government["credential_schema"]["fields"] if field["id"] == "username"
        )
        assert finance_username["type"] == "text"
        assert utility_username["type"] == "email"
        assert government_username["label"].lower().startswith("member")
