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
        job_id: Access job ID for polling detached execution.
        data: Extracted data keyed by field name (None until connected).
        session_id: MFA session ID (present when status is ``"mfa_required"``).
        mfa_type: MFA type (``"otp"``, ``"email_code"``, ``"push"``, etc.).
        metadata: Extra info from the server (MFA prompt text, etc.).
    """

    status: str
    job_id: Optional[str] = None
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

    @property
    def pending(self) -> bool:
        """True if the connect job is still executing in the background."""
        return self.status in {"pending", "running"}


@dataclass(frozen=True)
class AccessJobInfo:
    """Status for a tracked server-side access job."""

    job_id: str
    site: str
    job_type: str
    status: str
    session_id: Optional[str] = None
    mfa_type: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def pending(self) -> bool:
        return self.status in {"pending", "running"}

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def mfa_required(self) -> bool:
        return self.status == "mfa_required"


@dataclass(frozen=True)
class AccessJobListResult:
    """Result of listing access jobs."""

    jobs: List[AccessJobInfo]
    count: int


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


# ── Agent models ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentInfo:
    """Agent identity registered with the server.

    Attributes:
        agent_id: Unique ``agent-xxx`` identifier.
        name: Display name.
        description: What the agent does.
        api_key: Raw API key (only returned on creation).
        api_key_prefix: First 16 chars of the key (always returned).
        allowed_scopes: Scope strings the agent may request.
        allowed_sites: Site identifiers the agent may connect to.
        rate_limit: Custom rate-limit string (e.g. ``"60/minute"``).
        is_active: Whether the agent is enabled.
        created_at: ISO timestamp.
    """

    agent_id: str
    name: str
    description: Optional[str] = None
    api_key: Optional[str] = None
    api_key_prefix: Optional[str] = None
    allowed_scopes: Optional[List[str]] = None
    allowed_sites: Optional[List[str]] = None
    rate_limit: Optional[str] = None
    is_active: bool = True
    created_at: Optional[str] = None


@dataclass(frozen=True)
class AgentListResult:
    """Result of listing agents."""

    agents: List[AgentInfo]
    count: int


# ── Consent models ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConsentRequest:
    """A consent request submitted for user approval.

    Attributes:
        id: Unique consent request ID.
        access_token: The token consent is requested for.
        scopes: Requested data scopes.
        agent_name: Name of the requesting agent.
        status: ``pending``, ``approved``, or ``denied``.
        created_at: ISO timestamp.
    """

    id: int
    access_token: str
    scopes: List[str]
    agent_name: str
    status: str = "pending"
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ConsentGrant:
    """An active consent grant with a consent token.

    Attributes:
        consent_token: Opaque token the agent uses with ``fetch_data``.
        scopes: Granted data scopes.
        expires_at: ISO timestamp when the grant expires.
    """

    consent_token: str
    scopes: List[str]
    expires_at: Optional[str] = None


# ── API Key models ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApiKeyInfo:
    """An API key owned by the current user.

    Attributes:
        id: Key record ID.
        name: Display name.
        key_prefix: First characters of the key for identification.
        raw_key: Full key (only present when first created).
        scopes: Comma-separated scopes.
        is_active: Whether the key is active.
        expires_at: ISO expiry timestamp.
        last_used_at: ISO timestamp of last use.
        created_at: ISO timestamp.
    """

    id: str
    name: str
    key_prefix: str = ""
    raw_key: Optional[str] = None
    scopes: Optional[str] = None
    is_active: bool = True
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    created_at: Optional[str] = None


# ── Audit models ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuditEntry:
    """A single tamper-evident audit log entry.

    Attributes:
        id: Auto-increment entry ID.
        event_type: Category (``auth``, ``data_access``, ``token``, etc.).
        user_id: User that triggered the event.
        agent_id: Agent that triggered the event (if any).
        resource: Affected resource identifier.
        action: Specific action string.
        metadata: Extra context dict.
        ip_address: Client IP address.
        timestamp: ISO timestamp.
        entry_hash: SHA-256 hash for chain verification.
    """

    id: int
    event_type: str
    action: str
    user_id: Optional[int] = None
    agent_id: Optional[str] = None
    resource: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    timestamp: Optional[str] = None
    entry_hash: Optional[str] = None


@dataclass(frozen=True)
class AuditLogResult:
    """Paginated audit log query result."""

    entries: List[AuditEntry]
    total: int
    offset: int = 0
    limit: int = 100


@dataclass(frozen=True)
class AuditVerifyResult:
    """Result of verifying the audit hash chain."""

    valid: bool
    total: int
    errors: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WebhookDelivery:
    """A single webhook delivery attempt."""

    status_code: Optional[int] = None
    success: bool = False
    timestamp: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Delivery history for a specific webhook."""

    webhook_id: str
    url: str = ""
    deliveries: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0


@dataclass(frozen=True)
class PublicTokenExchangeResult:
    """Result of exchanging a public token for an access token."""

    access_token: str
