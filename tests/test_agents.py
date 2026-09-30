"""
Tests for agent registration system (CRUD + API key provisioning).
"""

from unittest.mock import AsyncMock, patch

import pytest


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
            json={
                "link_token": link_token,
                "username": "test_user",
                "password": "secret123",
            },
            headers={"X-API-Key": api_key},
        )
        assert cred_resp.status_code == 200
        access_token = cred_resp.json()["access_token"]

        # An agent reads data only with the owner's consent, bound to that agent.
        consent = client.post(
            "/consent/request",
            json={"access_token": access_token, "scopes": ["balance"]},
            headers={"X-API-Key": api_key},
        )
        assert consent.status_code == 200
        grant = client.post(f"/consent/{consent.json()['request_id']}/approve", headers=auth_headers).json()

        fetch_resp = client.post(
            "/fetch_data",
            json={"access_token": access_token, "consent_token": grant["consent_token"]},
            headers={"X-API-Key": api_key},
        )
        assert fetch_resp.status_code == 200
        payload = fetch_resp.json()
        assert payload["data"] == {"balance": "$150.00"}
        assert mock_connect.await_args.kwargs["extract_fields"] == ["balance"]


def _agent(client, headers, **body):
    resp = client.post("/agents", json={"name": "Agent", **body}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _link_and_token(client, headers, site="internal_bank"):
    link_token = client.post("/create_link", params={"site": site}, headers=headers).json()["link_token"]
    resp = client.post(
        "/submit_credentials",
        json={"link_token": link_token, "username": "u", "password": "p"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return link_token, resp.json()["access_token"]


class TestAgentRestrictionsFailClosed:
    """SEC-04: [] allows nothing, strings are rejected, unreadable stored values allow nothing."""

    def test_empty_site_list_allows_no_site(self, client, auth_headers):
        key = _agent(client, auth_headers, allowed_sites=[])["api_key"]
        assert client.post("/create_link?site=internal_bank", headers={"X-API-Key": key}).status_code == 403

    def test_empty_scope_list_allows_no_field(self, client, auth_headers):
        key = _agent(client, auth_headers, allowed_scopes=[])["api_key"]
        resp = client.post("/create_link?site=internal_bank", json={"scopes": ["balance"]}, headers={"X-API-Key": key})
        assert resp.status_code == 403
        # Without requested scopes the link inherits the agent's (empty) list.
        resp = client.post("/create_link?site=internal_bank", headers={"X-API-Key": key})
        assert resp.status_code == 200
        assert resp.json()["scopes"] == []

    def test_lists_given_as_strings_are_rejected(self, client, auth_headers):
        for field in ("allowed_sites", "allowed_scopes"):
            resp = client.post("/agents", json={"name": "a", field: "internal_bank"}, headers=auth_headers)
            assert resp.status_code == 422, field

    def test_unknown_fields_and_bad_scopes_are_rejected(self, client, auth_headers):
        assert (
            client.post("/agents", json={"name": "a", "alowed_sites": ["x"]}, headers=auth_headers).status_code == 422
        )
        assert (
            client.post(
                "/agents", json={"name": "a", "allowed_scopes": ["write:balance"]}, headers=auth_headers
            ).status_code
            == 422
        )

    def test_empty_list_round_trips_as_empty_not_null(self, client, auth_headers):
        agent_id = _agent(client, auth_headers, allowed_sites=[], allowed_scopes=[])["agent_id"]
        detail = client.get(f"/agents/{agent_id}", headers=auth_headers).json()
        assert detail["allowed_sites"] == []
        assert detail["allowed_scopes"] == []

    def test_unreadable_stored_site_list_allows_nothing(self, client, auth_headers):
        from src.database import Agent
        from tests.conftest import TestSessionLocal

        created = _agent(client, auth_headers, allowed_sites=["internal_bank"])
        db = TestSessionLocal()
        try:
            db.query(Agent).filter_by(id=created["agent_id"]).update({"allowed_sites": "internal_bank"})
            db.commit()
        finally:
            db.close()
        resp = client.post("/create_link?site=internal_bank", headers={"X-API-Key": created["api_key"]})
        assert resp.status_code == 403

    def test_patching_scopes_widens_the_key_too(self, client, auth_headers):
        created = _agent(client, auth_headers, allowed_scopes=["balance"])
        key = {"X-API-Key": created["api_key"]}
        assert (
            client.post("/create_link?site=internal_bank", json={"scopes": ["usage"]}, headers=key).status_code == 403
        )

        patched = client.patch(
            f"/agents/{created['agent_id']}", json={"allowed_scopes": ["balance", "usage"]}, headers=auth_headers
        )
        assert patched.status_code == 200
        assert (
            client.post("/create_link?site=internal_bank", json={"scopes": ["usage"]}, headers=key).status_code == 200
        )

    def test_patching_scopes_to_null_lifts_the_restriction(self, client, auth_headers):
        created = _agent(client, auth_headers, allowed_scopes=["balance"])
        client.patch(f"/agents/{created['agent_id']}", json={"allowed_scopes": None}, headers=auth_headers)
        resp = client.post(
            "/create_link?site=internal_bank", json={"scopes": ["anything"]}, headers={"X-API-Key": created["api_key"]}
        )
        assert resp.status_code == 200


class TestAgentRateLimit:
    """SEC-27: an agent's own rate_limit is enforced, per agent."""

    @pytest.fixture(autouse=True)
    def _limiter_on(self):
        from limits.storage.memory import MemoryStorage

        from src.dependencies import limiter

        limiter._limiter.storage = MemoryStorage()
        limiter.enabled = True
        yield
        limiter.enabled = False
        limiter._limiter.storage = MemoryStorage()

    def test_rate_limit_is_enforced_per_agent(self, client, auth_headers):
        slow = {"X-API-Key": _agent(client, auth_headers, rate_limit="2/minute")["api_key"]}
        other = {"X-API-Key": _agent(client, auth_headers, rate_limit="2/minute")["api_key"]}

        assert client.get("/links", headers=slow).status_code == 200
        assert client.get("/links", headers=slow).status_code == 200
        limited = client.get("/links", headers=slow)
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1
        # Another agent has its own budget.
        assert client.get("/links", headers=other).status_code == 200

    def test_agent_without_a_limit_is_not_throttled_by_it(self, client, auth_headers):
        key = {"X-API-Key": _agent(client, auth_headers)["api_key"]}
        for _ in range(5):
            assert client.get("/links", headers=key).status_code == 200

    def test_invalid_rate_limits_are_rejected(self, client, auth_headers):
        for bad in ("fast", "0/minute", "10/minute;100/hour", "10 per fortnight"):
            resp = client.post("/agents", json={"name": "a", "rate_limit": bad}, headers=auth_headers)
            assert resp.status_code == 422, bad
        agent_id = _agent(client, auth_headers)["agent_id"]
        assert client.patch(f"/agents/{agent_id}", json={"rate_limit": "lots"}, headers=auth_headers).status_code == 422


class TestAgentConsent:
    """SEC-19: an agent's /fetch_data needs a grant bound to that agent."""

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_agent_needs_its_own_grant(self, mock_connect, client, auth_headers):
        mock_connect.return_value = {"status": "connected", "data": {"balance": "$1", "ssn": "x"}}
        first = _agent(client, auth_headers)
        second = _agent(client, auth_headers)
        _, access_token = _link_and_token(client, auth_headers)

        no_consent = client.post(
            "/fetch_data", json={"access_token": access_token}, headers={"X-API-Key": first["api_key"]}
        )
        assert no_consent.status_code == 403

        request = client.post(
            "/consent/request",
            json={"access_token": access_token, "scopes": ["read:balance"]},
            headers={"X-API-Key": first["api_key"]},
        ).json()
        assert request["agent_id"] == first["agent_id"]
        grant = client.post(f"/consent/{request['request_id']}/approve", headers=auth_headers).json()
        assert grant["agent_id"] == first["agent_id"]

        # Another agent cannot use the first agent's grant.
        stolen = client.post(
            "/fetch_data",
            json={"access_token": access_token, "consent_token": grant["consent_token"]},
            headers={"X-API-Key": second["api_key"]},
        )
        assert stolen.status_code == 403

        ok = client.post(
            "/fetch_data",
            json={"access_token": access_token, "consent_token": grant["consent_token"]},
            headers={"X-API-Key": first["api_key"]},
        )
        assert ok.status_code == 200
        assert ok.json()["data"] == {"balance": "$1"}

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_owner_grant_without_agent_is_not_usable_by_an_agent(self, mock_connect, client, auth_headers):
        mock_connect.return_value = {"status": "connected", "data": {"balance": "$1"}}
        agent = _agent(client, auth_headers)
        _, access_token = _link_and_token(client, auth_headers)
        request = client.post(
            "/consent/request",
            json={"access_token": access_token, "scopes": ["balance"], "agent_name": "Owner app"},
            headers=auth_headers,
        ).json()
        assert request["agent_id"] is None
        grant = client.post(f"/consent/{request['request_id']}/approve", headers=auth_headers).json()

        resp = client.post(
            "/fetch_data",
            json={"access_token": access_token, "consent_token": grant["consent_token"]},
            headers={"X-API-Key": agent["api_key"]},
        )
        assert resp.status_code == 403
        # The owner may use it (and needs none).
        assert (
            client.post(
                "/fetch_data",
                json={"access_token": access_token, "consent_token": grant["consent_token"]},
                headers=auth_headers,
            ).status_code
            == 200
        )
        assert client.post("/fetch_data", json={"access_token": access_token}, headers=auth_headers).status_code == 200

    def test_agent_cannot_approve_or_deny(self, client, auth_headers):
        agent = _agent(client, auth_headers)
        _, access_token = _link_and_token(client, auth_headers)
        request_id = client.post(
            "/consent/request",
            json={"access_token": access_token, "scopes": ["balance"]},
            headers={"X-API-Key": agent["api_key"]},
        ).json()["request_id"]

        assert client.post(f"/consent/{request_id}/approve", headers={"X-API-Key": agent["api_key"]}).status_code == 403
        assert client.post(f"/consent/{request_id}/deny", headers={"X-API-Key": agent["api_key"]}).status_code == 403

    def test_agent_cannot_ask_beyond_its_scopes(self, client, auth_headers):
        agent = _agent(client, auth_headers, allowed_scopes=["balance"])
        _, access_token = _link_and_token(client, auth_headers)
        resp = client.post(
            "/consent/request",
            json={"access_token": access_token, "scopes": ["balance", "ssn"]},
            headers={"X-API-Key": agent["api_key"]},
        )
        assert resp.status_code == 403

    def test_agent_sees_and_revokes_only_its_grants(self, client, auth_headers):
        mine, theirs = _agent(client, auth_headers), _agent(client, auth_headers)
        _, access_token = _link_and_token(client, auth_headers)
        tokens = {}
        for agent in (mine, theirs):
            request_id = client.post(
                "/consent/request",
                json={"access_token": access_token, "scopes": ["balance"]},
                headers={"X-API-Key": agent["api_key"]},
            ).json()["request_id"]
            tokens[agent["agent_id"]] = client.post(f"/consent/{request_id}/approve", headers=auth_headers).json()[
                "consent_token"
            ]

        listed = client.get("/consent", headers={"X-API-Key": mine["api_key"]}).json()
        assert [g["consent_token"] for g in listed["grants"]] == [tokens[mine["agent_id"]]]
        assert (
            client.delete(f"/consent/{tokens[theirs['agent_id']]}", headers={"X-API-Key": mine["api_key"]}).status_code
            == 404
        )
        assert (
            client.delete(f"/consent/{tokens[mine['agent_id']]}", headers={"X-API-Key": mine["api_key"]}).status_code
            == 200
        )
        assert client.get("/consent", headers=auth_headers).json()["count"] == 1
