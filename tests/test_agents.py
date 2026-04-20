"""
Tests for agent registration system (CRUD + API key provisioning).
"""

from unittest.mock import AsyncMock, patch


class TestAgentRegistration:
    """Test agent CRUD endpoints."""

    def test_register_agent(self, client, auth_headers):
        """Should register a new agent and return API key."""
        resp = client.post(
            "/agents",
            json={
                "name": "My Test Agent",
                "description": "A test agent for integration",
                "allowed_scopes": ["billing", "usage"],
                "allowed_sites": ["internal_bank"],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Test Agent"
        assert "agent_id" in data
        assert data["agent_id"].startswith("agent-")
        assert "api_key" in data  # Raw key returned once
        assert data["api_key"].startswith("pk_agent_")
        assert data["allowed_scopes"] == ["billing", "usage"]
        assert data["allowed_sites"] == ["internal_bank"]

    def test_register_agent_requires_auth(self, client):
        """Agent registration should require authentication."""
        resp = client.post(
            "/agents",
            json={
                "name": "Unauthorized Agent",
            },
        )
        assert resp.status_code == 401

    def test_register_agent_requires_name(self, client, auth_headers):
        """Agent registration should require a name."""
        resp = client.post("/agents", json={}, headers=auth_headers)
        assert resp.status_code == 422

    def test_list_agents(self, client, auth_headers):
        """Should list user's agents."""
        # Register two agents
        client.post("/agents", json={"name": "Agent A"}, headers=auth_headers)
        client.post("/agents", json={"name": "Agent B"}, headers=auth_headers)

        resp = client.get("/agents", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        names = {a["name"] for a in data["agents"]}
        assert names == {"Agent A", "Agent B"}

    def test_list_agents_isolated_per_user(self, client, auth_headers, second_user_headers):
        """Users should only see their own agents."""
        client.post("/agents", json={"name": "User1 Agent"}, headers=auth_headers)
        client.post("/agents", json={"name": "User2 Agent"}, headers=second_user_headers)

        resp1 = client.get("/agents", headers=auth_headers)
        resp2 = client.get("/agents", headers=second_user_headers)
        assert resp1.json()["count"] == 1
        assert resp1.json()["agents"][0]["name"] == "User1 Agent"
        assert resp2.json()["count"] == 1
        assert resp2.json()["agents"][0]["name"] == "User2 Agent"

    def test_get_agent_by_id(self, client, auth_headers):
        """Should retrieve a specific agent by ID."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Detail Agent",
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]

        resp = client.get(f"/agents/{agent_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Detail Agent"
        assert resp.json()["agent_id"] == agent_id

    def test_get_agent_not_found(self, client, auth_headers):
        """Should return 404 for non-existent agent."""
        resp = client.get("/agents/agent-nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    def test_get_agent_cross_user_forbidden(self, client, auth_headers, second_user_headers):
        """Users cannot access other users' agents."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Private Agent",
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]

        resp = client.get(f"/agents/{agent_id}", headers=second_user_headers)
        assert resp.status_code == 404

    def test_update_agent(self, client, auth_headers):
        """Should update allowed_scopes and rate_limit."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Updatable Agent",
                "allowed_scopes": ["billing"],
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]

        resp = client.patch(
            f"/agents/{agent_id}",
            json={
                "allowed_scopes": ["billing", "usage", "identity"],
                "rate_limit": "120/minute",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify via GET
        get_resp = client.get(f"/agents/{agent_id}", headers=auth_headers)
        data = get_resp.json()
        assert set(data["allowed_scopes"]) == {"billing", "usage", "identity"}
        assert data["rate_limit"] == "120/minute"

    def test_delete_agent(self, client, auth_headers):
        """Should deactivate agent and revoke its API key."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Deletable Agent",
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]

        resp = client.delete(f"/agents/{agent_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deactivated"

        # Agent should no longer appear in active list
        list_resp = client.get("/agents", headers=auth_headers)
        active_ids = {a["agent_id"] for a in list_resp.json()["agents"]}
        assert agent_id not in active_ids

    def test_delete_agent_cross_user_forbidden(self, client, auth_headers, second_user_headers):
        """Users cannot delete other users' agents."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Protected Agent",
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]

        resp = client.delete(f"/agents/{agent_id}", headers=second_user_headers)
        assert resp.status_code == 404


class TestAgentAPIKey:
    """Test that agent API key works for authentication."""

    def test_agent_api_key_authenticates(self, client, auth_headers):
        """Agent's API key should work for X-API-Key authentication."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Auth Agent",
            },
            headers=auth_headers,
        )
        api_key = create_resp.json()["api_key"]

        # Use the agent's API key to create a link (uses get_current_user_or_api_key)
        resp = client.post("/create_link?site=internal_bank", headers={"X-API-Key": api_key})
        assert resp.status_code == 200
        assert "link_token" in resp.json()

    def test_deleted_agent_key_revoked(self, client, auth_headers):
        """After deleting an agent, its API key should be revoked."""
        create_resp = client.post(
            "/agents",
            json={
                "name": "Revoke Agent",
            },
            headers=auth_headers,
        )
        agent_id = create_resp.json()["agent_id"]
        api_key = create_resp.json()["api_key"]

        # Delete the agent
        client.delete(f"/agents/{agent_id}", headers=auth_headers)

        # API key should no longer work for authenticated endpoints
        resp = client.post("/create_link?site=internal_bank", headers={"X-API-Key": api_key})
        assert resp.status_code == 401

    def test_agent_api_key_restricts_allowed_sites(self, client, auth_headers):
        create_resp = client.post(
            "/agents",
            json={
                "name": "Scoped Agent",
                "allowed_sites": ["internal_bank"],
            },
            headers=auth_headers,
        )
        api_key = create_resp.json()["api_key"]

        allowed = client.post("/create_link?site=internal_bank", headers={"X-API-Key": api_key})
        blocked = client.post("/create_link?site=hydro_one", headers={"X-API-Key": api_key})

        assert allowed.status_code == 200
        assert blocked.status_code == 403

    def test_agent_api_key_rejects_requested_scopes_outside_policy(self, client, auth_headers):
        create_resp = client.post(
            "/agents",
            json={
                "name": "Field Agent",
                "allowed_scopes": ["balance"],
            },
            headers=auth_headers,
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post(
            "/create_link?site=internal_bank",
            json={"scopes": ["balance", "transactions"]},
            headers={"X-API-Key": api_key},
        )

        assert resp.status_code == 403

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_agent_api_key_scope_narrows_fetch_execution(self, mock_connect, client, auth_headers):
        mock_connect.return_value = {
            "status": "connected",
            "data": {
                "balance": "$150.00",
                "transactions": [{"amount": 50}],
            },
        }

        create_resp = client.post(
            "/agents",
            json={
                "name": "Narrow Agent",
                "allowed_scopes": ["balance"],
                "allowed_sites": ["internal_bank"],
            },
            headers=auth_headers,
        )
        api_key = create_resp.json()["api_key"]

        link_resp = client.post(
            "/create_link?site=internal_bank",
            headers={"X-API-Key": api_key},
        )
        assert link_resp.status_code == 200
        assert link_resp.json()["scopes"] == ["balance"]
        link_token = link_resp.json()["link_token"]

        cred_resp = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers={"X-API-Key": api_key},
        )
        assert cred_resp.status_code == 200
        access_token = cred_resp.json()["access_token"]

        fetch_resp = client.get(
            "/fetch_data",
            params={"access_token": access_token},
            headers={"X-API-Key": api_key},
        )
        assert fetch_resp.status_code == 200
        payload = fetch_resp.json()
        assert payload["data"] == {"balance": "$150.00"}
        assert mock_connect.await_args.kwargs["extract_fields"] == ["balance"]
