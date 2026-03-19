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
    AuthToken,
    BlueprintInfo,
    BlueprintListResult,
    ConnectResult,
    HealthStatus,
    LinkEvent,
    LinkResult,
    LinkSession,
    MFAChallenge,
    MFASubmitResult,
    UserProfile,
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
            # After MFA, reconnect to get the data
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

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_connect_response(data: Dict[str, Any], site: str) -> ConnectResult:
        """Parse a raw JSON dict into a ConnectResult."""
        return ConnectResult(
            status=data.get("status", "unknown"),
            data=data.get("data"),
            session_id=data.get("session_id"),
            mfa_type=data.get("mfa_type"),
            metadata=data.get("metadata"),
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
