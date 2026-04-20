"""
Tests for the Link Token flow: create_link → submit_credentials → fetch_data.
Also tests link/token management (list, delete).
"""


class TestLinkTokenFlow:
    """Tests for the full multi-step link flow."""

    def test_full_flow(self, client, auth_headers):
        """Test the complete create_link → submit_credentials → fetch_data flow.

        The autouse mock_browser_engine fixture mocks connect_to_site
        to return stub data without launching Playwright.
        """
        # Step 1: Create link
        r1 = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        assert r1.status_code == 200
        link_token = r1.json()["link_token"]
        assert link_token

        # Step 2: Submit credentials
        r2 = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        assert r2.status_code == 200
        access_token = r2.json()["access_token"]
        assert access_token

        # Step 3: Fetch data — mocked engine returns stub data
        r3 = client.get("/fetch_data", params={"access_token": access_token}, headers=auth_headers)
        assert r3.status_code == 200
        data = r3.json()
        assert data["status"] == "connected"

    def test_create_link_no_auth(self, client):
        response = client.post("/create_link", params={"site": "internal_bank"})
        assert response.status_code == 401

    def test_submit_credentials_invalid_link(self, client, auth_headers):
        response = client.post(
            "/submit_credentials",
            params={
                "link_token": "nonexistent-token",
                "username": "user",
                "password": "pass",
            },
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_fetch_data_invalid_token(self, client, auth_headers):
        response = client.get(
            "/fetch_data",
            params={
                "access_token": "nonexistent-token",
            },
            headers=auth_headers,
        )
        assert response.status_code == 401


class TestInstructions:
    """Tests for the submit_instructions endpoint."""

    def test_submit_and_fetch_with_instructions(self, client, auth_headers):
        """Test instructions flow with mocked engine."""
        # Create link + submit creds
        r1 = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r1.json()["link_token"]

        r2 = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers=auth_headers,
        )
        access_token = r2.json()["access_token"]

        # Submit instructions — this should work
        r3 = client.post(
            "/submit_instructions",
            params={
                "access_token": access_token,
                "instructions": "Extract only active accounts",
            },
            headers=auth_headers,
        )
        assert r3.status_code == 200

        # Fetch data — mocked engine returns stub data
        r4 = client.get("/fetch_data", params={"access_token": access_token}, headers=auth_headers)
        assert r4.status_code == 200

    def test_submit_instructions_invalid_token(self, client, auth_headers):
        response = client.post(
            "/submit_instructions",
            params={
                "access_token": "invalid",
                "instructions": "some instructions",
            },
            headers=auth_headers,
        )
        assert response.status_code == 401


class TestLinkManagement:
    """Tests for listing and deleting links and tokens."""

    def test_list_links(self, client, auth_headers):
        # Create two links
        client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        client.post("/create_link", params={"site": "hydro_one"}, headers=auth_headers)

        response = client.get("/links", headers=auth_headers)
        assert response.status_code == 200
        links = response.json()
        assert len(links) == 2
        sites = {link["site"] for link in links}
        assert "internal_bank" in sites
        assert "hydro_one" in sites

    def test_delete_link(self, client, auth_headers):
        r = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r.json()["link_token"]

        # Delete
        response = client.delete(f"/links/{link_token}", headers=auth_headers)
        assert response.status_code == 200

        # Verify gone
        links = client.get("/links", headers=auth_headers).json()
        assert len(links) == 0

    def test_delete_link_not_found(self, client, auth_headers):
        response = client.delete("/links/nonexistent", headers=auth_headers)
        assert response.status_code == 404

    def test_list_tokens(self, client, auth_headers):
        # Create link and submit credentials
        r1 = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r1.json()["link_token"]
        client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "user",
                "password": "pass",
            },
            headers=auth_headers,
        )

        tokens = client.get("/tokens", headers=auth_headers).json()
        assert len(tokens) == 1

    def test_delete_token(self, client, auth_headers):
        r1 = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r1.json()["link_token"]
        r2 = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "user",
                "password": "pass",
            },
            headers=auth_headers,
        )
        access_token = r2.json()["access_token"]

        # Delete token
        response = client.delete(f"/tokens/{access_token}", headers=auth_headers)
        assert response.status_code == 200

        # Verify gone
        tokens = client.get("/tokens", headers=auth_headers).json()
        assert len(tokens) == 0

    def test_delete_token_not_found(self, client, auth_headers):
        response = client.delete("/tokens/nonexistent", headers=auth_headers)
        assert response.status_code == 404


class TestUserIsolation:
    """Tests ensuring users can't access each other's data."""

    def test_user_cannot_see_other_links(self, client, auth_headers, second_user_headers):
        # User 1 creates a link
        client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)

        # User 2 should see no links
        links = client.get("/links", headers=second_user_headers).json()
        assert len(links) == 0

    def test_user_cannot_delete_other_link(self, client, auth_headers, second_user_headers):
        r = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r.json()["link_token"]

        # User 2 tries to delete user 1's link
        response = client.delete(f"/links/{link_token}", headers=second_user_headers)
        assert response.status_code == 404

    def test_user_cannot_fetch_other_data(self, client, auth_headers, second_user_headers):
        # User 1 creates full flow
        r1 = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        link_token = r1.json()["link_token"]
        r2 = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "user",
                "password": "pass",
            },
            headers=auth_headers,
        )
        access_token = r2.json()["access_token"]

        # User 2 tries to fetch user 1's data
        response = client.get(
            "/fetch_data",
            params={
                "access_token": access_token,
            },
            headers=second_user_headers,
        )
        assert response.status_code == 401
