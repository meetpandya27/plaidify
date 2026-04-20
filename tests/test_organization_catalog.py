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
