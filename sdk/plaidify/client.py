"""
Plaidify Python SDK — async and sync clients.

Usage (async)::

    from plaidify import Plaidify

    async with Plaidify(server_url="http://localhost:8000") as pfy:
        result = await pfy.connect("greengrid_energy", username="demo", password="demo")
        print(result.data)

Usage (sync)::

    from plaidify import PlaidifySync

    with PlaidifySync(server_url="http://localhost:8000") as pfy:
        result = pfy.connect("greengrid_energy", username="demo", password="demo")
        print(result.data)
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

from plaidify.config import ClientConfig, DEFAULT_SERVER_URL
from plaidify.exceptions import (
    AuthenticationError,
    BlueprintNotFoundError,
    ConnectionError,
    InvalidTokenError,
    MFARequiredError,
    PlaidifyError,
    RateLimitedError,
    ServerError,
)
from plaidify.models import (
    AccessJobInfo,
    AccessJobListResult,
    AgentInfo,
    AgentListResult,
    ApiKeyInfo,
    AuditEntry,
    AuditLogResult,
    AuditVerifyResult,
    AuthToken,
    BlueprintInfo,
    BlueprintListResult,
    ConsentGrant,
    ConsentRequest,
    ConnectResult,
    HealthStatus,
    LinkEvent,
    LinkResult,
    LinkSession,
    MFAChallenge,
    MFASubmitResult,
    PublicTokenExchangeResult,
    UserProfile,
    WebhookDeliveryResult,
    WebhookRegistration,
)

# Type alias for the MFA handler callback
MFAHandler = Callable[[MFAChallenge], Awaitable[str]]


def _raise_for_api_error(response: httpx.Response) -> None:
    """Translate HTTP error responses into typed SDK exceptions."""
    if response.is_success:
        return

    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {"detail": response.text}

    detail_msg = body.get("detail") or body.get("message") or body.get("error") or str(body)

    if status == 401:
        raise InvalidTokenError(message=str(detail_msg))
    if status == 404:
        raise BlueprintNotFoundError(site=str(detail_msg))
    if status == 429:
        retry_after = int(response.headers.get("Retry-After", "60"))
        raise RateLimitedError(retry_after=retry_after)
    if status == 502:
        raise ConnectionError(message=f"Connection failed: {detail_msg}")
    if status >= 500:
        raise ServerError(message=f"Server error ({status}): {detail_msg}")
    # Generic fallback
    raise PlaidifyError(message=str(detail_msg), status_code=status)


class Plaidify:
    """Async Plaidify client.

    Args:
        server_url: Base URL of the Plaidify server (default ``http://localhost:8000``).
        api_key: Optional JWT token or API key for authenticated endpoints.
        timeout: Request timeout in seconds.
        max_retries: Number of retries on transient failures (5xx, network).
        headers: Extra headers to send on every request.
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._config = ClientConfig(
            server_url=server_url.rstrip("/"),
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            headers=headers or {},
        )
        self._client: Optional[httpx.AsyncClient] = None

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "Plaidify":
        self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            transport = httpx.AsyncHTTPTransport(retries=self._config.max_retries)
            self._client = httpx.AsyncClient(
                base_url=self._config.server_url,
                headers=self._config.base_headers(),
                timeout=httpx.Timeout(self._config.timeout),
                transport=transport,
            )
        return self._client

    @property
    def _http(self) -> httpx.AsyncClient:
        return self._ensure_client()

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> HealthStatus:
        """Check server health.

        Returns:
            HealthStatus with ``status``, ``version``, and ``database`` fields.

        Raises:
            ConnectionError: If the server is unreachable.
        """
        try:
            r = await self._http.get("/health")
        except httpx.ConnectError as e:
            raise ConnectionError(f"Cannot reach Plaidify at {self._config.server_url}: {e}") from e
        _raise_for_api_error(r)
        d = r.json()
        return HealthStatus(
            status=d.get("status", "unknown"),
            version=d.get("version"),
            database=d.get("database"),
        )

    # ── Blueprints ────────────────────────────────────────────────────────────

    async def list_blueprints(self) -> BlueprintListResult:
        """List all available site blueprints.

        Returns:
            BlueprintListResult with a list of BlueprintInfo objects.
        """
        try:
            r = await self._http.get("/blueprints")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        infos = [
            BlueprintInfo(
                site=b.get("site", ""),
                name=b.get("name", ""),
                domain=b.get("domain", ""),
                tags=b.get("tags", []),
                has_mfa=b.get("has_mfa", False),
                schema_version=b.get("schema_version", "2"),
            )
            for b in d.get("blueprints", [])
        ]
        return BlueprintListResult(blueprints=infos, count=d.get("count", len(infos)))

    async def get_blueprint(self, site: str) -> BlueprintInfo:
        """Get detailed info about a specific blueprint.

        Args:
            site: Blueprint identifier (e.g. ``"greengrid_energy"``).

        Returns:
            BlueprintInfo with all metadata including extract fields.

        Raises:
            BlueprintNotFoundError: If no blueprint exists for the site.
        """
        try:
            r = await self._http.get(f"/blueprints/{site}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return BlueprintInfo(
            site=site,
            name=d.get("name", ""),
            domain=d.get("domain", ""),
            tags=d.get("tags", []),
            has_mfa=d.get("has_mfa", False),
            extract_fields=d.get("extract_fields", []),
            schema_version=d.get("schema_version", "2"),
        )

    # ── Connect (one-shot) ────────────────────────────────────────────────────

    async def connect(
        self,
        site: str,
        *,
        username: str,
        password: str,
        extract_fields: Optional[List[str]] = None,
        mfa_handler: Optional[MFAHandler] = None,
    ) -> ConnectResult:
        """Connect to a site and extract data in one call.

        This is the simplest integration path. If the site requires MFA and
        an ``mfa_handler`` is provided, the handler is called automatically
        to resolve the challenge.

        Args:
            site: Blueprint identifier.
            username: Credentials for the target site.
            password: Credentials for the target site.
            extract_fields: Limit extraction to specific fields (None = all).
            mfa_handler: Async callback ``(MFAChallenge) -> str`` that returns
                the MFA code. Called automatically when MFA is triggered.

        Returns:
            ConnectResult with ``status`` and ``data``.

        Raises:
            MFARequiredError: MFA is needed and no ``mfa_handler`` was provided.
            AuthenticationError: Credentials were rejected.
            BlueprintNotFoundError: The site blueprint doesn't exist.
            ConnectionError: Server unreachable.
        """
        payload: Dict[str, Any] = {"site": site}

        # Attempt client-side encryption via ephemeral session
        try:
            enc_r = await self._http.post("/encryption/session")
            if enc_r.is_success:
                enc_data = enc_r.json()
                pub_key_pem = enc_data["public_key"]
                link_token = enc_data["link_token"]
                payload["encrypted_username"] = self._rsa_encrypt(pub_key_pem, username)
                payload["encrypted_password"] = self._rsa_encrypt(pub_key_pem, password)
                payload["link_token"] = link_token
            else:
                payload["username"] = username
                payload["password"] = password
        except Exception:
            # Fallback to plaintext if encryption endpoint unavailable
            payload["username"] = username
            payload["password"] = password

        if extract_fields:
            payload["extract_fields"] = extract_fields

        try:
            r = await self._http.post("/connect", json=payload)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)

        result = self._parse_connect_response(r.json(), site)

        if result.pending and mfa_handler and result.job_id:
            return await self._resolve_connect_job(site, result.job_id, mfa_handler)

        # Auto-handle MFA if handler provided
        if result.mfa_required and mfa_handler and result.session_id:
            challenge = MFAChallenge(
                session_id=result.session_id,
                site=site,
                mfa_type=result.mfa_type or "unknown",
                metadata=result.metadata,
            )
            code = await mfa_handler(challenge)
            mfa_result = await self.submit_mfa(result.session_id, code)
            if mfa_result.status == "error":
                raise PlaidifyError(
                    message=mfa_result.error or "MFA submission failed.",
                    status_code=400,
                )
            if result.job_id:
                return await self._resolve_connect_job(site, result.job_id, mfa_handler)
            # Fallback for older servers that do not return job_id.
            r2 = await self._http.post("/connect", json=payload)
            _raise_for_api_error(r2)
            result = self._parse_connect_response(r2.json(), site)

        # Raise if MFA required but no handler
        if result.mfa_required and not mfa_handler:
            raise MFARequiredError(
                site=site,
                session_id=result.session_id or "",
                mfa_type=result.mfa_type or "unknown",
                metadata=result.metadata,
            )

        return result

    async def list_access_jobs(
        self,
        *,
        limit: int = 20,
        site: Optional[str] = None,
        status: Optional[str] = None,
        job_type: Optional[str] = None,
    ) -> AccessJobListResult:
        """List tracked access jobs for the authenticated user."""
        params: Dict[str, Any] = {"limit": limit}
        if site is not None:
            params["site"] = site
        if status is not None:
            params["status"] = status
        if job_type is not None:
            params["job_type"] = job_type

        try:
            r = await self._http.get("/access_jobs", params=params)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        jobs = [self._parse_access_job(item) for item in d.get("jobs", [])]
        return AccessJobListResult(jobs=jobs, count=d.get("count", len(jobs)))

    async def get_access_job(self, job_id: str) -> AccessJobInfo:
        """Fetch a single access job by ID."""
        try:
            r = await self._http.get(f"/access_jobs/{job_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e

        if r.status_code == 404:
            raise PlaidifyError(
                message=f"Access job not found: {job_id}",
                status_code=404,
                detail={"job_id": job_id},
            )

        _raise_for_api_error(r)
        return self._parse_access_job(r.json())

    async def wait_for_access_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 0.5,
        timeout: float = 30.0,
    ) -> AccessJobInfo:
        """Poll an access job until it leaves a pending/running state."""
        deadline = time.monotonic() + timeout

        while True:
            job = await self.get_access_job(job_id)
            if not job.pending:
                return job

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaidifyError(
                    message=f"Timed out waiting for access job: {job_id}",
                    status_code=408,
                    detail={"job_id": job_id, "status": job.status},
                )
            await asyncio.sleep(min(poll_interval, remaining))

    # ── MFA ───────────────────────────────────────────────────────────────────

    async def submit_mfa(self, session_id: str, code: str) -> MFASubmitResult:
        """Submit an MFA code for a pending session.

        Args:
            session_id: The session ID from the connect response.
            code: The MFA code entered by the user.

        Returns:
            MFASubmitResult with submission status.
        """
        try:
            r = await self._http.post(
                "/mfa/submit",
                params={"session_id": session_id, "code": code},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return MFASubmitResult(
            status=d.get("status", "unknown"),
            message=d.get("message"),
            error=d.get("error"),
        )

    async def mfa_status(self, session_id: str) -> MFAChallenge:
        """Check the status of a pending MFA session.

        Args:
            session_id: The MFA session ID.

        Returns:
            MFAChallenge with session details.

        Raises:
            PlaidifyError: If the session is not found or expired.
        """
        try:
            r = await self._http.get(f"/mfa/status/{session_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return MFAChallenge(
            session_id=d["session_id"],
            site=d["site"],
            mfa_type=d["mfa_type"],
            metadata=d.get("metadata"),
        )

    # ── Link flow (Plaid-style multi-step) ────────────────────────────────────

    async def create_link(
        self,
        site: str,
        scopes: Optional[List[str]] = None,
    ) -> LinkResult:
        """Create a link token for a site (step 1 of multi-step flow).

        Requires authentication (``api_key`` must be set).

        Args:
            site: Blueprint identifier.
            scopes: Optional list of field names or scope strings to restrict
                what data the resulting access token can retrieve.
                Example: ``["balance", "transactions"]``.
                If ``None``, all fields are allowed.

        Returns:
            LinkResult with the ``link_token``.
        """
        try:
            json_body = {"scopes": scopes} if scopes else None
            r = await self._http.post("/create_link", params={"site": site}, json=json_body)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return LinkResult(link_token=d["link_token"], site=site)

    async def submit_credentials(
        self,
        link_token: str,
        username: str,
        password: str,
    ) -> LinkResult:
        """Submit credentials for a link token (step 2).

        Args:
            link_token: The token from :meth:`create_link`.
            username: Credentials for the target site.
            password: Credentials for the target site.

        Returns:
            LinkResult with both ``link_token`` and ``access_token``.
        """
        try:
            r = await self._http.post(
                "/submit_credentials",
                params={
                    "link_token": link_token,
                    "username": username,
                    "password": password,
                },
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return LinkResult(
            link_token=link_token,
            access_token=d["access_token"],
        )

    async def fetch_data(self, access_token: str) -> ConnectResult:
        """Fetch data using a previously created access token (step 3).

        Args:
            access_token: The token from :meth:`submit_credentials`.

        Returns:
            ConnectResult with extracted data.
        """
        try:
            r = await self._http.get(
                "/fetch_data",
                params={"access_token": access_token},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return ConnectResult(
            status=d.get("status", "connected"),
            data=d.get("data"),
            metadata=d.get("metadata"),
        )

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def register(self, username: str, email: str, password: str) -> AuthToken:
        """Register a new user account.

        Args:
            username: Desired username (3-50 chars).
            email: Valid email address.
            password: Password (min 8 chars).

        Returns:
            AuthToken with JWT access token.
        """
        try:
            r = await self._http.post(
                "/auth/register",
                json={"username": username, "email": email, "password": password},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        token = AuthToken(access_token=d["access_token"], token_type=d.get("token_type", "bearer"))
        # Auto-set for subsequent requests
        self._config.api_key = token.access_token
        self._ensure_client().headers["Authorization"] = f"Bearer {token.access_token}"
        return token

    async def login(self, username: str, password: str) -> AuthToken:
        """Log in and receive a JWT token.

        Args:
            username: Account username.
            password: Account password.

        Returns:
            AuthToken with JWT access token.
        """
        try:
            r = await self._http.post(
                "/auth/token",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        token = AuthToken(access_token=d["access_token"], token_type=d.get("token_type", "bearer"))
        self._config.api_key = token.access_token
        self._ensure_client().headers["Authorization"] = f"Bearer {token.access_token}"
        return token

    async def me(self) -> UserProfile:
        """Get the current user's profile.

        Returns:
            UserProfile with user details.
        """
        try:
            r = await self._http.get("/auth/me")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return UserProfile(
            id=d["id"],
            username=d.get("username"),
            email=d.get("email"),
            is_active=d.get("is_active", True),
        )

    # ── Links & Tokens ────────────────────────────────────────────────────────

    async def list_links(self) -> List[Dict[str, str]]:
        """List all links for the current user."""
        try:
            r = await self._http.get("/links")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def delete_link(self, link_token: str) -> Dict[str, str]:
        """Delete a link and its associated access tokens."""
        try:
            r = await self._http.delete(f"/links/{link_token}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def list_tokens(self) -> List[Dict[str, str]]:
        """List all access tokens for the current user."""
        try:
            r = await self._http.get("/tokens")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def delete_token(self, token: str) -> Dict[str, str]:
        """Delete a specific access token."""
        try:
            r = await self._http.delete(f"/tokens/{token}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    # ── Hosted Link (agent integration) ───────────────────────────────────────

    async def create_link_session(self, site: Optional[str] = None) -> LinkSession:
        """Create a hosted link session and return the link URL.

        The returned ``link_url`` can be opened in a browser or embedded via
        the PlaidifyLink widget for the user to authenticate.

        Args:
            site: Optional blueprint identifier to pre-select.

        Returns:
            LinkSession with ``link_token``, ``link_url``, and ``public_key``.
        """
        params = {}
        if site:
            params["site"] = site
        try:
            r = await self._http.post("/link/sessions", params=params)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return LinkSession(
            link_token=d["link_token"],
            link_url=d.get("link_url", ""),
            public_key=d.get("public_key"),
            expires_in=d.get("expires_in", 1800),
        )

    def get_link_url(self, link_token: str) -> str:
        """Return the full hosted link page URL for a link token.

        Args:
            link_token: Token from :meth:`create_link_session`.

        Returns:
            Absolute URL string for the hosted link page.
        """
        return f"{self._config.server_url}/link?token={link_token}"

    async def register_webhook(
        self,
        link_token: str,
        url: str,
        secret: str,
    ) -> WebhookRegistration:
        """Register a webhook URL for a link session.

        The server will POST events (LINK_COMPLETE, LINK_ERROR, MFA_REQUIRED)
        to the provided URL with an HMAC-SHA256 signature.

        Args:
            link_token: Token identifying the link session.
            url: HTTPS callback URL to receive events.
            secret: Shared secret for HMAC signature verification.

        Returns:
            WebhookRegistration with ``webhook_id``.
        """
        try:
            r = await self._http.post(
                "/webhooks/register",
                json={"link_token": link_token, "url": url, "secret": secret},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return WebhookRegistration(webhook_id=d["webhook_id"], status=d.get("status", "registered"))

    async def poll_link_status(
        self,
        link_token: str,
        *,
        timeout: float = 300.0,
        interval: float = 2.0,
    ) -> LinkSession:
        """Poll a link session until it completes or times out.

        Args:
            link_token: Token identifying the link session.
            timeout: Maximum seconds to wait (default 300).
            interval: Seconds between polls (default 2).

        Returns:
            LinkSession with final status.

        Raises:
            PlaidifyError: If the session times out.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = await self._http.get(f"/link/sessions/{link_token}/status")
            except httpx.ConnectError as e:
                raise ConnectionError() from e
            _raise_for_api_error(r)
            d = r.json()
            status = d.get("status", "unknown")
            if status in ("completed", "error", "expired"):
                return LinkSession(
                    link_token=link_token,
                    status=status,
                    site=d.get("site"),
                    events=d.get("events", []),
                )
            await asyncio.sleep(interval)

        raise PlaidifyError(message="Link session timed out.", status_code=408)

    async def stream_link_events(self, link_token: str):
        """Async generator that yields real-time events from a link session SSE stream.

        Yields:
            LinkEvent objects for each event in the stream.

        Example::

            async for event in pfy.stream_link_events(link_token):
                print(event.event, event.data)
                if event.event in ("CONNECTED", "ERROR"):
                    break
        """
        import json as json_mod

        url = f"{self._config.server_url}/link/events/{link_token}"
        headers = dict(self._config.base_headers())
        headers["Accept"] = "text/event-stream"

        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if not response.is_success:
                    _raise_for_api_error(response)

                event_name = ""
                data_buf = ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf = line[5:].strip()
                    elif line == "":
                        # End of event block
                        if event_name and event_name != "ping":
                            parsed = {}
                            if data_buf:
                                try:
                                    parsed = json_mod.loads(data_buf)
                                except (json_mod.JSONDecodeError, ValueError):
                                    parsed = {"raw": data_buf}
                            yield LinkEvent(
                                event=event_name,
                                timestamp=parsed.get("timestamp"),
                                data=parsed.get("data"),
                            )
                        event_name = ""
                        data_buf = ""

    # ── Agents ────────────────────────────────────────────────────────────────

    async def register_agent(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        allowed_scopes: Optional[List[str]] = None,
        allowed_sites: Optional[List[str]] = None,
        rate_limit: Optional[str] = None,
    ) -> AgentInfo:
        """Register a new AI agent and receive its dedicated API key.

        The ``api_key`` field is only returned on creation — store it securely.

        Args:
            name: Agent display name.
            description: What the agent does.
            allowed_scopes: Scope strings the agent may request (None = all).
            allowed_sites: Site identifiers the agent may connect to (None = all).
            rate_limit: Rate-limit string (e.g. ``"60/minute"``).

        Returns:
            AgentInfo including the one-time ``api_key``.
        """
        body: Dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        if allowed_scopes is not None:
            body["allowed_scopes"] = allowed_scopes
        if allowed_sites is not None:
            body["allowed_sites"] = allowed_sites
        if rate_limit is not None:
            body["rate_limit"] = rate_limit
        try:
            r = await self._http.post("/agents", json=body)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return AgentInfo(
            agent_id=d["agent_id"],
            name=d["name"],
            api_key=d.get("api_key"),
            api_key_prefix=d.get("api_key_prefix"),
            allowed_scopes=d.get("allowed_scopes"),
            allowed_sites=d.get("allowed_sites"),
        )

    async def list_agents(self) -> AgentListResult:
        """List all agents owned by the current user.

        Returns:
            AgentListResult with a list of AgentInfo objects.
        """
        try:
            r = await self._http.get("/agents")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        agents = [
            AgentInfo(
                agent_id=a["agent_id"],
                name=a["name"],
                description=a.get("description"),
                allowed_scopes=a.get("allowed_scopes"),
                allowed_sites=a.get("allowed_sites"),
                rate_limit=a.get("rate_limit"),
                created_at=a.get("created_at"),
            )
            for a in d.get("agents", [])
        ]
        return AgentListResult(agents=agents, count=d.get("count", len(agents)))

    async def get_agent(self, agent_id: str) -> AgentInfo:
        """Get details of a specific agent.

        Args:
            agent_id: The ``agent-xxx`` identifier.

        Returns:
            AgentInfo with full details.
        """
        try:
            r = await self._http.get(f"/agents/{agent_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return AgentInfo(
            agent_id=d["agent_id"],
            name=d["name"],
            description=d.get("description"),
            allowed_scopes=d.get("allowed_scopes"),
            allowed_sites=d.get("allowed_sites"),
            rate_limit=d.get("rate_limit"),
            is_active=d.get("is_active", True),
            created_at=d.get("created_at"),
        )

    async def update_agent(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        allowed_scopes: Optional[List[str]] = None,
        allowed_sites: Optional[List[str]] = None,
        rate_limit: Optional[str] = None,
    ) -> Dict[str, str]:
        """Update an agent's configuration.

        Args:
            agent_id: The ``agent-xxx`` identifier.
            name: New display name.
            description: New description.
            allowed_scopes: New scope list.
            allowed_sites: New site list.
            rate_limit: New rate-limit string.

        Returns:
            Dict with ``status`` and ``agent_id``.
        """
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if allowed_scopes is not None:
            body["allowed_scopes"] = allowed_scopes
        if allowed_sites is not None:
            body["allowed_sites"] = allowed_sites
        if rate_limit is not None:
            body["rate_limit"] = rate_limit
        try:
            r = await self._http.patch(f"/agents/{agent_id}", json=body)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def deactivate_agent(self, agent_id: str) -> Dict[str, str]:
        """Deactivate an agent and revoke its API key.

        Args:
            agent_id: The ``agent-xxx`` identifier.

        Returns:
            Dict with ``status`` and ``agent_id``.
        """
        try:
            r = await self._http.delete(f"/agents/{agent_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    # ── Consent ───────────────────────────────────────────────────────────────

    async def request_consent(
        self,
        access_token: str,
        scopes: List[str],
        agent_name: str,
        duration_seconds: int = 3600,
    ) -> ConsentRequest:
        """Request consent from a user for scoped data access.

        Args:
            access_token: The access token to request consent for.
            scopes: Data scopes to request.
            agent_name: Name of the requesting agent.
            duration_seconds: How long the grant should last.

        Returns:
            ConsentRequest with ``id`` and ``status``.
        """
        try:
            r = await self._http.post(
                "/consent/request",
                json={
                    "access_token": access_token,
                    "scopes": scopes,
                    "agent_name": agent_name,
                    "duration_seconds": duration_seconds,
                },
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return ConsentRequest(
            id=d["consent_request_id"],
            access_token=access_token,
            scopes=scopes,
            agent_name=agent_name,
            status="pending",
        )

    async def approve_consent(self, consent_id: int) -> ConsentGrant:
        """Approve a pending consent request.

        Args:
            consent_id: The consent request ID.

        Returns:
            ConsentGrant with the ``consent_token``.
        """
        try:
            r = await self._http.post(f"/consent/{consent_id}/approve")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return ConsentGrant(
            consent_token=d.get("consent_token", ""),
            scopes=d.get("scopes", []),
            expires_at=d.get("expires_at"),
        )

    async def deny_consent(self, consent_id: int) -> Dict[str, str]:
        """Deny a pending consent request.

        Args:
            consent_id: The consent request ID.

        Returns:
            Dict with ``detail``.
        """
        try:
            r = await self._http.post(f"/consent/{consent_id}/deny")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def list_consents(self) -> List[Dict[str, Any]]:
        """List all consent grants for the current user.

        Returns:
            List of consent grant dicts.
        """
        try:
            r = await self._http.get("/consent")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json().get("grants", r.json() if isinstance(r.json(), list) else [])

    async def revoke_consent(self, consent_token: str) -> Dict[str, str]:
        """Revoke an active consent grant.

        Args:
            consent_token: The consent token to revoke.

        Returns:
            Dict with ``detail``.
        """
        try:
            r = await self._http.delete(f"/consent/{consent_token}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    # ── API Keys ──────────────────────────────────────────────────────────────

    async def create_api_key(
        self,
        name: str,
        *,
        scopes: Optional[str] = None,
        expires_in_days: Optional[int] = None,
    ) -> ApiKeyInfo:
        """Create a new API key.

        Args:
            name: Display name for the key.
            scopes: Comma-separated scope string.
            expires_in_days: Number of days until expiry.

        Returns:
            ApiKeyInfo with ``raw_key`` (only returned once).
        """
        body: Dict[str, Any] = {"name": name}
        if scopes is not None:
            body["scopes"] = scopes
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        try:
            r = await self._http.post("/api-keys", json=body)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return ApiKeyInfo(
            id=d.get("id", ""),
            name=name,
            key_prefix=d.get("key_prefix", ""),
            raw_key=d.get("key"),
        )

    async def list_api_keys(self) -> List[ApiKeyInfo]:
        """List all API keys for the current user.

        Returns:
            List of ApiKeyInfo objects.
        """
        try:
            r = await self._http.get("/api-keys")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        keys = r.json().get("keys", r.json() if isinstance(r.json(), list) else [])
        return [
            ApiKeyInfo(
                id=k.get("id", ""),
                name=k.get("name", ""),
                key_prefix=k.get("key_prefix", ""),
                scopes=k.get("scopes"),
                is_active=k.get("is_active", True),
                expires_at=k.get("expires_at"),
                last_used_at=k.get("last_used_at"),
                created_at=k.get("created_at"),
            )
            for k in keys
        ]

    async def revoke_api_key(self, key_id: str) -> Dict[str, str]:
        """Revoke an API key.

        Args:
            key_id: The key record ID.

        Returns:
            Dict with status.
        """
        try:
            r = await self._http.delete(f"/api-keys/{key_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    # ── Public Token Exchange ─────────────────────────────────────────────────

    async def exchange_public_token(self, public_token: str) -> PublicTokenExchangeResult:
        """Exchange a one-time public token for a permanent access token.

        This is step 3 of the 3-token flow:
        ``link_token`` → ``public_token`` → ``access_token``.

        Args:
            public_token: The single-use public token from link completion.

        Returns:
            PublicTokenExchangeResult with the ``access_token``.
        """
        try:
            r = await self._http.post(
                "/exchange/public_token",
                json={"public_token": public_token},
            )
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return PublicTokenExchangeResult(access_token=d["access_token"])

    # ── Webhooks (extended) ───────────────────────────────────────────────────

    async def list_webhooks(self) -> List[Dict[str, Any]]:
        """List all webhooks for the current user.

        Returns:
            List of webhook dicts.
        """
        try:
            r = await self._http.get("/webhooks")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json().get("webhooks", [])

    async def delete_webhook(self, webhook_id: str) -> Dict[str, str]:
        """Delete a webhook.

        Args:
            webhook_id: The webhook ID to delete.

        Returns:
            Dict with status.
        """
        try:
            r = await self._http.delete(f"/webhooks/{webhook_id}")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def test_webhook(self, webhook_id: str) -> Dict[str, str]:
        """Send a test event to a webhook.

        Args:
            webhook_id: The webhook ID to test.

        Returns:
            Dict with delivery ``status``.
        """
        try:
            r = await self._http.post("/webhooks/test", json={"webhook_id": webhook_id})
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        return r.json()

    async def get_webhook_deliveries(self, webhook_id: str) -> WebhookDeliveryResult:
        """Get delivery history for a webhook.

        Args:
            webhook_id: The webhook ID.

        Returns:
            WebhookDeliveryResult with delivery attempts.
        """
        try:
            r = await self._http.get(f"/webhooks/{webhook_id}/deliveries")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return WebhookDeliveryResult(
            webhook_id=d["webhook_id"],
            url=d.get("url", ""),
            deliveries=d.get("deliveries", []),
            total=d.get("total", 0),
        )

    # ── Audit ─────────────────────────────────────────────────────────────────

    async def get_audit_logs(
        self,
        *,
        event_type: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
    ) -> AuditLogResult:
        """Query audit logs.

        Args:
            event_type: Filter by event type (e.g. ``"auth"``, ``"data_access"``).
            offset: Pagination offset.
            limit: Max entries to return.

        Returns:
            AuditLogResult with paginated entries.
        """
        params: Dict[str, Any] = {"offset": offset, "limit": limit}
        if event_type:
            params["event_type"] = event_type
        try:
            r = await self._http.get("/audit/logs", params=params)
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        entries = [
            AuditEntry(
                id=e["id"],
                event_type=e["event_type"],
                action=e["action"],
                user_id=e.get("user_id"),
                agent_id=e.get("agent_id"),
                resource=e.get("resource"),
                metadata=e.get("metadata"),
                ip_address=e.get("ip_address"),
                timestamp=e.get("timestamp"),
                entry_hash=e.get("entry_hash"),
            )
            for e in d.get("entries", [])
        ]
        return AuditLogResult(
            entries=entries,
            total=d.get("total", 0),
            offset=d.get("offset", offset),
            limit=d.get("limit", limit),
        )

    async def verify_audit_chain(self) -> AuditVerifyResult:
        """Verify the integrity of the audit log hash chain.

        Returns:
            AuditVerifyResult with ``valid``, ``total``, and any ``errors``.
        """
        try:
            r = await self._http.get("/audit/verify")
        except httpx.ConnectError as e:
            raise ConnectionError() from e
        _raise_for_api_error(r)
        d = r.json()
        return AuditVerifyResult(
            valid=d["valid"],
            total=d["total"],
            errors=d.get("errors", []),
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_connect_response(data: Dict[str, Any], site: str) -> ConnectResult:
        """Parse a raw JSON dict into a ConnectResult."""
        return ConnectResult(
            status=data.get("status", "unknown"),
            job_id=data.get("job_id"),
            data=data.get("data"),
            session_id=data.get("session_id"),
            mfa_type=data.get("mfa_type"),
            metadata=data.get("metadata"),
        )

    @staticmethod
    def _parse_access_job(data: Dict[str, Any]) -> AccessJobInfo:
        """Parse a raw JSON dict into an AccessJobInfo."""
        return AccessJobInfo(
            job_id=data["job_id"],
            site=data["site"],
            job_type=data["job_type"],
            status=data["status"],
            session_id=data.get("session_id"),
            mfa_type=data.get("mfa_type"),
            error_message=data.get("error_message"),
            metadata=data.get("metadata"),
            result=data.get("result"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )

    async def _resolve_connect_job(
        self,
        site: str,
        job_id: str,
        mfa_handler: MFAHandler,
        *,
        poll_interval: float = 0.5,
        timeout: float = 60.0,
    ) -> ConnectResult:
        """Resolve a detached connect job, handling MFA on the same job if needed."""
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaidifyError(
                    message=f"Timed out waiting for connect job: {job_id}",
                    status_code=408,
                    detail={"job_id": job_id},
                )

            job = await self.wait_for_access_job(
                job_id,
                poll_interval=poll_interval,
                timeout=remaining,
            )

            if job.completed:
                raw_result = dict(job.result or {})
                raw_result.setdefault("job_id", job.job_id)
                raw_result.setdefault("session_id", job.session_id)
                raw_result.setdefault("mfa_type", job.mfa_type)
                raw_result.setdefault("metadata", job.metadata)
                if not raw_result:
                    raw_result["status"] = "completed"
                return self._parse_connect_response(raw_result, site)

            if job.mfa_required:
                if not job.session_id:
                    raise PlaidifyError(
                        message="Access job requires MFA but no session_id was returned.",
                        status_code=500,
                        detail={"job_id": job.job_id},
                    )

                code = await mfa_handler(
                    MFAChallenge(
                        session_id=job.session_id,
                        site=job.site,
                        mfa_type=job.mfa_type or "unknown",
                        metadata=job.metadata,
                    )
                )
                mfa_result = await self.submit_mfa(job.session_id, code)
                if mfa_result.status == "error":
                    raise PlaidifyError(
                        message=mfa_result.error or "MFA submission failed.",
                        status_code=400,
                    )
                await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))
                continue

            if job.status == "blocked":
                raise PlaidifyError(
                    message=job.error_message or f"Access job blocked: {job.job_id}",
                    status_code=409,
                    detail={"job_id": job.job_id},
                )

            raise PlaidifyError(
                message=job.error_message or f"Access job failed: {job.job_id}",
                status_code=500,
                detail={"job_id": job.job_id, "status": job.status},
            )

    @staticmethod
    def _rsa_encrypt(pem_public_key: str, plaintext: str) -> str:
        """Encrypt plaintext with an RSA-OAEP public key, return base64."""
        public_key = serialization.load_pem_public_key(pem_public_key.encode("ascii"))
        ciphertext = public_key.encrypt(
            plaintext.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(ciphertext).decode("ascii")


# ── Synchronous wrapper ──────────────────────────────────────────────────────


class PlaidifySync:
    """Synchronous Plaidify client.

    Wraps the async :class:`Plaidify` client for use in non-async code.
    Uses the same API surface with blocking calls.

    Usage::

        with PlaidifySync(server_url="http://localhost:8000") as pfy:
            result = pfy.connect("greengrid_energy", username="demo", password="demo")
    """

    def __init__(self, **kwargs: Any) -> None:
        self._async_client = Plaidify(**kwargs)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def __enter__(self) -> "PlaidifySync":
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._async_client.__aenter__())
        return self

    def __exit__(self, *args: Any) -> None:
        if self._loop:
            self._loop.run_until_complete(self._async_client.close())
            self._loop.close()
            self._loop = None

    def _run(self, coro: Awaitable[Any]) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop.run_until_complete(self._async_client.__aenter__())
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        """Close the client."""
        if self._loop:
            self._loop.run_until_complete(self._async_client.close())
            self._loop.close()
            self._loop = None

    # Proxy all public methods synchronously
    def health(self) -> HealthStatus:
        return self._run(self._async_client.health())

    def list_blueprints(self) -> BlueprintListResult:
        return self._run(self._async_client.list_blueprints())

    def get_blueprint(self, site: str) -> BlueprintInfo:
        return self._run(self._async_client.get_blueprint(site))

    def connect(
        self,
        site: str,
        *,
        username: str,
        password: str,
        extract_fields: Optional[List[str]] = None,
    ) -> ConnectResult:
        return self._run(
            self._async_client.connect(
                site,
                username=username,
                password=password,
                extract_fields=extract_fields,
            )
        )

    def list_access_jobs(
        self,
        *,
        limit: int = 20,
        site: Optional[str] = None,
        status: Optional[str] = None,
        job_type: Optional[str] = None,
    ) -> AccessJobListResult:
        return self._run(
            self._async_client.list_access_jobs(
                limit=limit,
                site=site,
                status=status,
                job_type=job_type,
            )
        )

    def get_access_job(self, job_id: str) -> AccessJobInfo:
        return self._run(self._async_client.get_access_job(job_id))

    def wait_for_access_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 0.5,
        timeout: float = 30.0,
    ) -> AccessJobInfo:
        return self._run(
            self._async_client.wait_for_access_job(
                job_id,
                poll_interval=poll_interval,
                timeout=timeout,
            )
        )

    def submit_mfa(self, session_id: str, code: str) -> MFASubmitResult:
        return self._run(self._async_client.submit_mfa(session_id, code))

    def mfa_status(self, session_id: str) -> MFAChallenge:
        return self._run(self._async_client.mfa_status(session_id))

    def create_link(self, site: str) -> LinkResult:
        return self._run(self._async_client.create_link(site))

    def submit_credentials(self, link_token: str, username: str, password: str) -> LinkResult:
        return self._run(self._async_client.submit_credentials(link_token, username, password))

    def fetch_data(self, access_token: str) -> ConnectResult:
        return self._run(self._async_client.fetch_data(access_token))

    def register(self, username: str, email: str, password: str) -> AuthToken:
        return self._run(self._async_client.register(username, email, password))

    def login(self, username: str, password: str) -> AuthToken:
        return self._run(self._async_client.login(username, password))

    def me(self) -> UserProfile:
        return self._run(self._async_client.me())

    def list_links(self) -> List[Dict[str, str]]:
        return self._run(self._async_client.list_links())

    def delete_link(self, link_token: str) -> Dict[str, str]:
        return self._run(self._async_client.delete_link(link_token))

    def list_tokens(self) -> List[Dict[str, str]]:
        return self._run(self._async_client.list_tokens())

    def delete_token(self, token: str) -> Dict[str, str]:
        return self._run(self._async_client.delete_token(token))

    def create_link_session(self, site: Optional[str] = None) -> LinkSession:
        return self._run(self._async_client.create_link_session(site))

    def get_link_url(self, link_token: str) -> str:
        return self._async_client.get_link_url(link_token)

    def register_webhook(self, link_token: str, url: str, secret: str) -> WebhookRegistration:
        return self._run(self._async_client.register_webhook(link_token, url, secret))

    def poll_link_status(
        self,
        link_token: str,
        *,
        timeout: float = 300.0,
        interval: float = 2.0,
    ) -> LinkSession:
        return self._run(
            self._async_client.poll_link_status(link_token, timeout=timeout, interval=interval)
        )

    # ── Agents ────────────────────────────────────────────────────────────────

    def register_agent(self, name: str, **kwargs: Any) -> AgentInfo:
        return self._run(self._async_client.register_agent(name, **kwargs))

    def list_agents(self) -> AgentListResult:
        return self._run(self._async_client.list_agents())

    def get_agent(self, agent_id: str) -> AgentInfo:
        return self._run(self._async_client.get_agent(agent_id))

    def update_agent(self, agent_id: str, **kwargs: Any) -> Dict[str, str]:
        return self._run(self._async_client.update_agent(agent_id, **kwargs))

    def deactivate_agent(self, agent_id: str) -> Dict[str, str]:
        return self._run(self._async_client.deactivate_agent(agent_id))

    # ── Consent ───────────────────────────────────────────────────────────────

    def request_consent(
        self, access_token: str, scopes: List[str], agent_name: str, duration_seconds: int = 3600,
    ) -> ConsentRequest:
        return self._run(
            self._async_client.request_consent(access_token, scopes, agent_name, duration_seconds)
        )

    def approve_consent(self, consent_id: int) -> ConsentGrant:
        return self._run(self._async_client.approve_consent(consent_id))

    def deny_consent(self, consent_id: int) -> Dict[str, str]:
        return self._run(self._async_client.deny_consent(consent_id))

    def list_consents(self) -> List[Dict[str, Any]]:
        return self._run(self._async_client.list_consents())

    def revoke_consent(self, consent_token: str) -> Dict[str, str]:
        return self._run(self._async_client.revoke_consent(consent_token))

    # ── API Keys ──────────────────────────────────────────────────────────────

    def create_api_key(self, name: str, **kwargs: Any) -> ApiKeyInfo:
        return self._run(self._async_client.create_api_key(name, **kwargs))

    def list_api_keys(self) -> List[ApiKeyInfo]:
        return self._run(self._async_client.list_api_keys())

    def revoke_api_key(self, key_id: str) -> Dict[str, str]:
        return self._run(self._async_client.revoke_api_key(key_id))

    # ── Public Token Exchange ─────────────────────────────────────────────────

    def exchange_public_token(self, public_token: str) -> PublicTokenExchangeResult:
        return self._run(self._async_client.exchange_public_token(public_token))

    # ── Webhooks (extended) ───────────────────────────────────────────────────

    def list_webhooks(self) -> List[Dict[str, Any]]:
        return self._run(self._async_client.list_webhooks())

    def delete_webhook(self, webhook_id: str) -> Dict[str, str]:
        return self._run(self._async_client.delete_webhook(webhook_id))

    def test_webhook(self, webhook_id: str) -> Dict[str, str]:
        return self._run(self._async_client.test_webhook(webhook_id))

    def get_webhook_deliveries(self, webhook_id: str) -> WebhookDeliveryResult:
        return self._run(self._async_client.get_webhook_deliveries(webhook_id))

    # ── Audit ─────────────────────────────────────────────────────────────────

    def get_audit_logs(self, **kwargs: Any) -> AuditLogResult:
        return self._run(self._async_client.get_audit_logs(**kwargs))

    def verify_audit_chain(self) -> AuditVerifyResult:
        return self._run(self._async_client.verify_audit_chain())
