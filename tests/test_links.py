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


class TestHostedLinkEventSanitization:
    """Ensure hosted-link events never leak access_token / result payloads.

    Enforces the completion contract: /link surfaces only public_token +
    metadata to browser/mobile clients.
    """

    def test_build_event_strips_forbidden_keys(self):
        from src.routers.link_sessions import _build_link_session_event

        event = _build_link_session_event(
            "CONNECTED",
            data={
                "public_token": "public-abc",
                "access_token": "at-should-not-leak",
                "result": {"balance": 1234, "access_token": "nested-leak"},
                "site": "internal_bank",
                "job_id": "job-1",
            },
        )

        assert event["event"] == "CONNECTED"
        assert event["data"]["public_token"] == "public-abc"
        assert event["data"]["site"] == "internal_bank"
        assert event["data"]["job_id"] == "job-1"
        assert "access_token" not in event["data"]
        assert "result" not in event["data"]

    def test_sanitizer_handles_nested_lists(self):
        from src.routers.link_sessions import _sanitize_hosted_event_data

        sanitized = _sanitize_hosted_event_data(
            {
                "items": [
                    {"name": "a", "access_token": "leak-a"},
                    {"name": "b", "password": "pw"},
                ],
                "public_token": "public-xyz",
            }
        )

        assert sanitized["public_token"] == "public-xyz"
        assert sanitized["items"][0] == {"name": "a"}
        assert sanitized["items"][1] == {"name": "b"}

    def test_event_endpoint_strips_forbidden_keys(self, client, auth_headers):
        session = client.post(
            "/link/sessions",
            params={"site": "internal_bank"},
            headers=auth_headers,
        ).json()
        link_token = session["link_token"]

        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={
                "event": "INSTITUTION_SELECTED",
                "site": "internal_bank",
                "access_token": "should-not-persist",
                "result": {"leak": True},
            },
        )
        assert response.status_code == 200

        status = client.get(f"/link/sessions/{link_token}/status").json()
        # Status response never contains access_token or result.
        assert "access_token" not in status
        assert "result" not in status


class TestHostedLinkFrameAncestors:
    """Verify hosted /link CSP frame-ancestors is derived from per-session allowlist."""

    def test_link_html_no_token_defaults_to_self(self, client):
        response = client.get("/link")
        assert response.status_code == 200
        csp = response.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'self'" in csp
        # Hard default stays SAMEORIGIN when no session allowlist is present.
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_link_html_with_unknown_token_defaults_to_self(self, client):
        response = client.get("/link?token=does-not-exist")
        assert response.status_code == 200
        csp = response.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'self'" in csp
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_link_html_uses_session_allowed_origin(self, client, auth_headers):
        session = client.post(
            "/link/sessions",
            params={"site": "internal_bank"},
            headers={**auth_headers, "Origin": "https://partner.example.com"},
        ).json()
        link_token = session["link_token"]

        response = client.get(f"/link?token={link_token}")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'self' https://partner.example.com" in csp
        # X-Frame-Options removed so the document can be framed cross-origin.
        assert "X-Frame-Options" not in response.headers

    def test_bootstrap_multi_origin_allowlist(self, client, auth_headers):
        bootstrap = client.post(
            "/link/bootstrap",
            json={
                "site": "internal_bank",
                "allowed_origins": [
                    "https://partner-a.example.com",
                    "https://partner-b.example.com/",
                    "https://partner-a.example.com",
                ],
            },
            headers=auth_headers,
        )
        assert bootstrap.status_code == 200
        body = bootstrap.json()
        assert body["allowed_origins"] == [
            "https://partner-a.example.com",
            "https://partner-b.example.com",
        ]

        exchange = client.post(
            "/link/sessions/bootstrap",
            json={"launch_token": body["launch_token"]},
            headers={"Origin": "https://partner-b.example.com"},
        )
        assert exchange.status_code == 200
        link_token = exchange.json()["link_token"]

        response = client.get(f"/link?token={link_token}")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "https://partner-a.example.com" in csp
        assert "https://partner-b.example.com" in csp
        assert "X-Frame-Options" not in response.headers

    def test_bootstrap_rejects_origin_outside_allowlist(self, client, auth_headers):
        bootstrap = client.post(
            "/link/bootstrap",
            json={
                "site": "internal_bank",
                "allowed_origins": ["https://partner-a.example.com"],
            },
            headers=auth_headers,
        ).json()

        exchange = client.post(
            "/link/sessions/bootstrap",
            json={"launch_token": bootstrap["launch_token"]},
            headers={"Origin": "https://attacker.example.com"},
        )
        assert exchange.status_code == 403

    def test_bootstrap_rejects_invalid_origin_format(self, client, auth_headers):
        response = client.post(
            "/link/bootstrap",
            json={
                "site": "internal_bank",
                "allowed_origins": ["not-a-valid-origin"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestHostedLinkEventEndpoint:
    """Full lifecycle coverage for POST /link/sessions/{token}/event.

    Verifies that anonymous browser posts (authed by the link_token
    capability) update session_state for every observable event so
    webhooks and SSE cannot silently diverge from what the user saw.
    """

    def _create_session(self, client, auth_headers):
        return client.post(
            "/link/sessions",
            params={"site": "internal_bank"},
            headers=auth_headers,
        ).json()["link_token"]

    def test_unknown_token_returns_404(self, client):
        response = client.post(
            "/link/sessions/does-not-exist/event",
            json={"event": "OPEN"},
        )
        assert response.status_code == 404

    def test_open_event_records_but_does_not_transition(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "OPEN"},
        )
        assert response.status_code == 200
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert "OPEN" in status["events"]
        assert status["status"] == "awaiting_institution"

    def test_institution_selected_transitions_state(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "INSTITUTION_SELECTED", "site": "internal_bank"},
        )
        assert response.status_code == 200
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "awaiting_credentials"

    def test_credentials_submitted_transitions_state(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "CREDENTIALS_SUBMITTED"},
        )
        assert response.status_code == 200
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "connecting"

    def test_mfa_events_transition_state(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "MFA_REQUIRED", "mfa_type": "otp"},
        )
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "mfa_required"

        client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "MFA_SUBMITTED"},
        )
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "verifying_mfa"

    def test_connected_is_authoritative_server_side(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "CONNECTED", "public_token": "should-not-trust"},
        )
        # Browser-reported CONNECTED is intentionally ignored so a client
        # cannot lie about completion; page retry queue still sees 2xx.
        assert response.status_code == 200
        assert response.json() == {"status": "ignored"}
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "awaiting_institution"

    def test_exit_event_marks_session_exited(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "EXIT", "reason": "user_closed"},
        )
        assert response.status_code == 200
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "exited"

    def test_error_event_marks_session_error(self, client, auth_headers):
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "ERROR", "error": "provider refused"},
        )
        assert response.status_code == 200
        status = client.get(f"/link/sessions/{link_token}/status").json()
        assert status["status"] == "error"
        assert status["error_message"] == "provider refused"

    def test_event_endpoint_requires_no_user_auth(self, client, auth_headers):
        # The endpoint is deliberately authed by the link_token capability,
        # not by a user JWT, because it is called from the anonymous
        # hosted page.
        link_token = self._create_session(client, auth_headers)
        response = client.post(
            f"/link/sessions/{link_token}/event",
            json={"event": "INSTITUTION_SELECTED", "site": "internal_bank"},
            # no auth headers at all
        )
        assert response.status_code == 200


class TestHostedLinkFrontend:
    """The React bundle under frontend-next/dist/ is the only supported
    hosted-link frontend (#65). When the build is missing, GET /link
    returns a 500 with a clear error so deployment failures are loud.
    """

    def test_link_page_serves_react_bundle(self, client, monkeypatch, tmp_path):
        from src.routers import link_sessions as link_sessions_module

        fake_dist = tmp_path / "dist"
        fake_dist.mkdir()
        (fake_dist / "index.html").write_text(
            '<!doctype html><html><body><div id="root"></div>'
            '<script type="module" src="/ui-next/assets/index.js"></script></body></html>',
            encoding="utf-8",
        )
        monkeypatch.setattr(link_sessions_module, "FRONTEND_NEXT_DIST", fake_dist)

        response = client.get("/link")
        assert response.status_code == 200
        assert 'id="root"' in response.text

    def test_link_page_returns_500_when_bundle_missing(
        self, client, monkeypatch, tmp_path, caplog
    ):
        from src.routers import link_sessions as link_sessions_module

        monkeypatch.setattr(
            link_sessions_module, "FRONTEND_NEXT_DIST", tmp_path / "does-not-exist"
        )

        with caplog.at_level("ERROR"):
            response = client.get("/link")
        assert response.status_code == 500
        assert any(
            "frontend-next/dist/index.html is missing" in record.getMessage()
            for record in caplog.records
        )

