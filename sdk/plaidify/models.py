"""
Typed data models for the Plaidify SDK.

All API responses are parsed into these models, giving you
IDE autocomplete and type-safe access to fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ConnectResult:
    """Result of a connect() call.

    Attributes:
        status: Connection outcome — ``"connected"``, ``"mfa_required"``, etc.
        data: Extracted data keyed by field name (None until connected).
        session_id: MFA session ID (present when status is ``"mfa_required"``).
        mfa_type: MFA type (``"otp"``, ``"email_code"``, ``"push"``, etc.).
        metadata: Extra info from the server (MFA prompt text, etc.).
    """

    status: str
    data: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    mfa_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @property
    def connected(self) -> bool:
        """True if the connection succeeded and data was extracted."""
        return self.status == "connected"

    @property
    def mfa_required(self) -> bool:
        """True if multi-factor authentication is needed."""
        return self.status == "mfa_required"


@dataclass(frozen=True)
class MFAChallenge:
    """Describes a pending MFA challenge.

    Returned by :meth:`Plaidify.mfa_status` or raised inside
    :class:`MFARequiredError`.
    """

    session_id: str
    site: str
    mfa_type: str
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class BlueprintInfo:
    """Metadata about a single site blueprint.

    Attributes:
        site: Blueprint identifier (e.g. ``"greengrid_energy"``).
        name: Human-readable site name.
        domain: Target website domain.
        tags: Categorisation tags (``["utility", "energy"]``).
        has_mfa: Whether the site may trigger MFA.
        extract_fields: Data fields this blueprint can extract.
        schema_version: Blueprint schema version string.
    """

    site: str
    name: str
    domain: str
    tags: List[str] = field(default_factory=list)
    has_mfa: bool = False
    extract_fields: List[str] = field(default_factory=list)
    schema_version: str = "2"


@dataclass(frozen=True)
class BlueprintListResult:
    """Result of listing all available blueprints."""

    blueprints: List[BlueprintInfo]
    count: int


@dataclass(frozen=True)
class LinkResult:
    """Result of the multi-step link flow.

    Created by :meth:`Plaidify.create_link`, enriched by
    :meth:`Plaidify.submit_credentials`.
    """

    link_token: str
    access_token: Optional[str] = None
    site: Optional[str] = None


@dataclass(frozen=True)
class MFASubmitResult:
    """Result of an MFA code submission."""

    status: str
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class AuthToken:
    """JWT token pair returned after login / registration."""

    access_token: str
    token_type: str = "bearer"


@dataclass(frozen=True)
class UserProfile:
    """Current user profile."""

    id: int
    username: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True


@dataclass(frozen=True)
class HealthStatus:
    """Server health check result."""

    status: str
    version: Optional[str] = None
    database: Optional[str] = None


@dataclass(frozen=True)
class LinkSession:
    """A hosted link session created via :meth:`Plaidify.create_link_session`.

    Attributes:
        link_token: Unique session identifier.
        link_url: Full URL for the hosted link page.
        public_key: Ephemeral RSA public key in PEM format.
        expires_in: Seconds until the session expires.
        status: Current session status.
        site: Selected site (if any).
        events: List of event names that have occurred.
    """

    link_token: str
    link_url: str = ""
    public_key: Optional[str] = None
    expires_in: int = 1800
    status: str = "awaiting_institution"
    site: Optional[str] = None
    events: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LinkEvent:
    """A single event from a link session SSE stream."""

    event: str
    timestamp: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class WebhookRegistration:
    """Result of registering a webhook."""

    webhook_id: str
    status: str = "registered"
