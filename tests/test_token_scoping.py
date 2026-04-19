"""
Tests for Access Token Scoping feature.
"""

from unittest.mock import AsyncMock, patch

# ── Helpers ───────────────────────────────────────────────────────────────────


def _create_scoped_link(client, auth_headers, site="test_site", scopes=None):
    """Create a link with optional scopes and return link_token."""
    if scopes is not None:
        resp = client.post(
            f"/create_link?site={site}",
            json={"scopes": scopes},
            headers=auth_headers,
        )
    else:
        resp = client.post(f"/create_link?site={site}", headers=auth_headers)
    assert resp.status_code == 200
    return resp.json()


def _submit_creds(client, auth_headers, link_token):
    """Submit credentials and return response data."""
    resp = client.post(
        f"/submit_credentials?link_token={link_token}&username=demo&password=demo",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    return resp.json()


MOCK_SITE_DATA = {
    "status": "connected",
    "data": {
        "balance": "$150.00",
        "transactions": [{"date": "2026-01-01", "amount": 50}],
        "account_number": "12345",
        "usage_kwh": "500",
    },
}


# ── Create Link with Scopes ──────────────────────────────────────────────────


class TestCreateLinkScopes:
    def test_create_link_without_scopes(self, client, auth_headers):
        data = _create_scoped_link(client, auth_headers)
        assert "link_token" in data
        assert "scopes" not in data  # No scopes = all allowed

    def test_create_link_with_scopes(self, client, auth_headers):
        data = _create_scoped_link(client, auth_headers, scopes=["balance", "transactions"])
        assert "link_token" in data
        assert data["scopes"] == ["balance", "transactions"]

    def test_scopes_propagate_to_access_token(self, client, auth_headers):
        link_data = _create_scoped_link(client, auth_headers, scopes=["balance"])
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        assert "access_token" in cred_data
        assert cred_data["scopes"] == ["balance"]

    def test_no_scopes_no_propagation(self, client, auth_headers):
        link_data = _create_scoped_link(client, auth_headers)
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        assert "access_token" in cred_data
        assert "scopes" not in cred_data


# ── Scope Enforcement on /fetch_data ──────────────────────────────────────────


class TestScopeEnforcement:
    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_fetch_with_scoped_token(self, mock_connect, client, auth_headers):
        mock_connect.return_value = MOCK_SITE_DATA.copy()

        # Create scoped link + access token
        link_data = _create_scoped_link(client, auth_headers, scopes=["balance", "usage_kwh"])
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        access_token = cred_data["access_token"]

        resp = client.get(
            f"/fetch_data?access_token={access_token}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should only return balance and usage_kwh
        assert "balance" in data.get("data", {})
        assert "usage_kwh" in data.get("data", {})
        assert "transactions" not in data.get("data", {})
        assert "account_number" not in data.get("data", {})
        assert set(data["scopes_applied"]) == {"balance", "usage_kwh"}
        assert set(mock_connect.await_args.kwargs["extract_fields"]) == {"balance", "usage_kwh"}

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_fetch_without_scoped_token(self, mock_connect, client, auth_headers):
        mock_connect.return_value = MOCK_SITE_DATA.copy()

        # Create unscoped link + access token
        link_data = _create_scoped_link(client, auth_headers)
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        access_token = cred_data["access_token"]

        resp = client.get(
            f"/fetch_data?access_token={access_token}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # All fields should be present
        assert "balance" in data.get("data", {})
        assert "transactions" in data.get("data", {})
        assert "account_number" in data.get("data", {})
        assert "usage_kwh" in data.get("data", {})
        # No scopes_applied key
        assert "scopes_applied" not in data

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_scoped_token_with_read_prefix(self, mock_connect, client, auth_headers):
        mock_connect.return_value = MOCK_SITE_DATA.copy()

        # Create with read: prefixed scopes
        link_data = _create_scoped_link(client, auth_headers, scopes=["read:balance", "read:account_number"])
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        access_token = cred_data["access_token"]

        resp = client.get(
            f"/fetch_data?access_token={access_token}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "balance" in data.get("data", {})
        assert "account_number" in data.get("data", {})
        assert "transactions" not in data.get("data", {})

    @patch("src.routers.links.connect_to_site", new_callable=AsyncMock)
    def test_empty_scopes_returns_nothing(self, mock_connect, client, auth_headers):
        mock_connect.return_value = MOCK_SITE_DATA.copy()

        # Create with empty scopes list (restricts to nothing)
        link_data = _create_scoped_link(client, auth_headers, scopes=[])
        cred_data = _submit_creds(client, auth_headers, link_data["link_token"])
        access_token = cred_data["access_token"]

        resp = client.get(
            f"/fetch_data?access_token={access_token}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Empty scopes means no fields allowed
        assert data.get("data", {}) == {}
        assert mock_connect.await_args.kwargs["extract_fields"] == []


# ── Backward Compatibility ────────────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_existing_tokens_work(self, client, auth_headers):
        """Tokens created before scoping should still work (scopes=NULL)."""
        # Standard link creation without scopes
        resp = client.post("/create_link?site=test_site", headers=auth_headers)
        assert resp.status_code == 200
        link_token = resp.json()["link_token"]

        resp = client.post(
            f"/submit_credentials?link_token={link_token}&username=u&password=p",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()
        # No scopes key in response
        assert "scopes" not in resp.json()
