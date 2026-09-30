"""
Plaidify SDK configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

DEFAULT_SERVER_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 60.0
SDK_USER_AGENT = "plaidify-python-sdk/0.3.0a1"

# API keys (``pk_...``, agent keys ``pk_agent_...``) authenticate with the
# X-API-Key header; the server never accepts one as a bearer token.
API_KEY_PREFIX = "pk_"


def auth_headers(credential: Optional[str]) -> Dict[str, str]:
    """Headers that present ``credential`` the way the server expects it.

    API keys travel only in ``X-API-Key``; user access tokens (JWTs) as
    ``Authorization: Bearer``.
    """
    if not credential:
        return {}
    if credential.startswith(API_KEY_PREFIX):
        return {"X-API-Key": credential}
    return {"Authorization": f"Bearer {credential}"}


@dataclass
class ClientConfig:
    """Configuration for the Plaidify SDK client.

    Attributes:
        server_url: Base URL of the Plaidify API server.
        api_key: Optional credential for authenticated endpoints: an API key
            (``pk_...``) or a user access token (JWT).
        timeout: Default request timeout in seconds.
        max_retries: Number of retries on transient failures.
        headers: Additional HTTP headers to include in every request.
    """

    server_url: str = DEFAULT_SERVER_URL
    api_key: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = 3
    headers: Dict[str, str] = field(default_factory=dict)

    def base_headers(self) -> Dict[str, str]:
        """Build the default header set for requests."""
        h: Dict[str, str] = {
            "User-Agent": SDK_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        h.update(auth_headers(self.api_key))
        h.update(self.headers)
        return h
