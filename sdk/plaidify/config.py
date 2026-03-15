"""
Plaidify SDK configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

DEFAULT_SERVER_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 60.0
SDK_USER_AGENT = "plaidify-python-sdk/0.3.0a1"


@dataclass
class ClientConfig:
    """Configuration for the Plaidify SDK client.

    Attributes:
        server_url: Base URL of the Plaidify API server.
        api_key: Optional API key for authenticated endpoints.
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
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.headers)
        return h
