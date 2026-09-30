"""
Pydantic request/response models for the Plaidify API.
"""

import re
from typing import Any, Dict, Optional, Union
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator

# ── Scopes and site keys ──────────────────────────────────────────────────────
#
# A scope names one data field a caller may read, as the bare field name
# ("balance") or with the only action there is ("read:balance"). Anything
# else (other prefixes, wildcards, empty strings) is rejected rather than
# silently widened or ignored.

SCOPE_READ_PREFIX = "read:"
MAX_SCOPES = 100
_SCOPE_FIELD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_SITE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_RATE_LIMIT_MAX_AMOUNT = 100_000


def scope_field(scope: Any) -> str:
    """The data field a scope grants.

    Raises:
        ValueError: ``scope`` is not a scope string ("<field>" or "read:<field>").
    """
    if not isinstance(scope, str):
        raise ValueError("A scope must be a string.")
    value = scope.strip()
    if value.startswith(SCOPE_READ_PREFIX):
        value = value[len(SCOPE_READ_PREFIX) :]
    if not _SCOPE_FIELD_RE.match(value):
        raise ValueError(f"Unknown scope {scope!r}: use a field name such as 'balance' or 'read:balance'.")
    return value


def validate_scope_list(values: Optional[list[str]]) -> Optional[list[str]]:
    """Check every scope, strip whitespace and drop duplicates (order kept). ``None`` stays ``None``."""
    if values is None:
        return None
    cleaned: list[str] = []
    for value in values:
        scope_field(value)
        stripped = value.strip()
        if stripped not in cleaned:
            cleaned.append(stripped)
    return cleaned


def validate_site_list(values: Optional[list[str]]) -> Optional[list[str]]:
    """Check every site key (a blueprint name such as 'hydro_one') and drop duplicates."""
    if values is None:
        return None
    cleaned: list[str] = []
    for value in values:
        stripped = value.strip()
        if not _SITE_KEY_RE.match(stripped):
            raise ValueError(f"Invalid site {value!r}: use a site key such as 'hydro_one'.")
        if stripped not in cleaned:
            cleaned.append(stripped)
    return cleaned


def validate_rate_limit(value: Optional[str]) -> Optional[str]:
    """Check a single 'N/period' rate limit (e.g. '30/minute'); returns it stripped."""
    if value is None:
        return None
    from limits import parse

    stripped = value.strip()
    try:
        item = parse(stripped)
    except ValueError:
        raise ValueError("rate_limit must look like 'N/period', e.g. '30/minute' or '1000/hour'.") from None
    if ";" in stripped or "," in stripped or not 1 <= item.amount <= _RATE_LIMIT_MAX_AMOUNT:
        raise ValueError(f"rate_limit must be one 'N/period' limit with 1 <= N <= {_RATE_LIMIT_MAX_AMOUNT}.")
    return stripped


# ── Passwords ─────────────────────────────────────────────────────────────────

# bcrypt reads only the first 72 bytes: a longer password would be silently
# truncated, and different passwords sharing a 72-byte prefix would both verify.
MAX_PASSWORD_BYTES = 72


def validate_new_password(value: str) -> str:
    """Reject a password bcrypt would truncate, then apply the strength rules."""
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded "
            "(fewer characters if it uses non-ASCII letters)."
        )
    if not re.search(r"[A-Z]", value):
        raise ValueError("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", value):
        raise ValueError("Password must contain at least one lowercase letter.")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one digit.")
    if not re.search(r"[^A-Za-z0-9]", value):
        raise ValueError("Password must contain at least one special character.")
    return value


# ── Connection Models ─────────────────────────────────────────────────────────


class ConnectRequest(BaseModel):
    """Request body for POST /connect.

    Credentials can be sent as plaintext (username/password) or encrypted
    (encrypted_username/encrypted_password + link_token). If encrypted fields
    are present they take precedence.
    """

    site: str = Field(..., min_length=1, max_length=64, description="Site identifier matching a blueprint name.")
    username: Optional[str] = Field(default=None, max_length=256, description="Plaintext username (omit if encrypted).")
    password: Optional[str] = Field(
        default=None, max_length=4096, description="Plaintext password (omit if encrypted)."
    )
    encrypted_username: Optional[str] = Field(
        default=None, max_length=4096, description="Base64-encoded RSA-OAEP encrypted username."
    )
    encrypted_password: Optional[str] = Field(
        default=None, max_length=4096, description="Base64-encoded RSA-OAEP encrypted password."
    )
    link_token: Optional[str] = Field(
        default=None, max_length=256, description="Link token whose ephemeral key encrypts the credentials."
    )
    extract_fields: Optional[list[str]] = Field(
        default=None,
        max_length=100,
        description="Specific fields to extract (None = all defined in blueprint).",
    )


class ConnectResponse(BaseModel):
    """Response from POST /connect."""

    status: str = Field(..., description="Connection status (e.g., 'connected', 'mfa_required').")
    job_id: Optional[str] = Field(default=None, description="Access job ID for tracking execution status.")
    data: Optional[Dict[str, Any]] = Field(default=None, description="Extracted data from the target site.")
    extraction_method: Optional[str] = Field(
        default=None, description="How the data was extracted (e.g. 'css_selectors', 'llm_adaptive')."
    )
    session_id: Optional[str] = Field(default=None, description="Session ID for MFA continuation.")
    mfa_type: Optional[str] = Field(default=None, description="Type of MFA required (if status is 'mfa_required').")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional metadata (e.g., MFA question text)."
    )


class DisconnectRequest(BaseModel):
    """Request body for POST /disconnect."""

    link_token: str = Field(..., min_length=1, max_length=256, description="The link whose access to revoke.")


class DisconnectResponse(BaseModel):
    """Response from POST /disconnect."""

    status: str
    link_token: str
    revoked_tokens: int = Field(..., description="Access tokens (with their stored credentials) deleted.")
    message: Optional[str] = None


# ── Link Flow Models ──────────────────────────────────────────────────────────


class RefreshScheduleDirective(BaseModel):
    """A refresh schedule registered when /submit_credentials mints the access token."""

    schedule_format: Optional[str] = Field(
        default=None,
        max_length=32,
        validation_alias=AliasChoices("schedule_format", "format"),
        description="interval | hourly | daily | weekly",
    )
    interval_seconds: int = Field(default=3600, ge=1, le=366 * 86400)


class CreateLinkRequest(BaseModel):
    """Optional JSON body for POST /create_link."""

    scopes: Optional[list[str]] = Field(
        default=None,
        max_length=MAX_SCOPES,
        description="Fields the access token may read ('balance' or 'read:balance'); omit for all, [] for none.",
    )
    refresh_schedule: Optional[RefreshScheduleDirective] = None

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)


class SubmitCredentialsRequest(BaseModel):
    """Request body for POST /submit_credentials. Credentials never travel in the URL."""

    link_token: str = Field(..., min_length=1, max_length=256)
    username: Optional[str] = Field(default=None, max_length=256)
    password: Optional[str] = Field(default=None, max_length=4096)
    encrypted_username: Optional[str] = Field(default=None, max_length=4096)
    encrypted_password: Optional[str] = Field(default=None, max_length=4096)


class SubmitInstructionsRequest(BaseModel):
    """Request body for POST /submit_instructions."""

    access_token: str = Field(..., min_length=1, max_length=256)
    instructions: str = Field(..., max_length=4000)


class FetchDataRequest(BaseModel):
    """Request body for POST /fetch_data. Tokens never travel in the URL."""

    access_token: str = Field(..., min_length=1, max_length=256)
    consent_token: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Consent grant narrowing the fields returned; required when an agent's API key calls.",
    )


class PublicTokenExchangeRequest(BaseModel):
    """Request body for POST /exchange/public_token."""

    public_token: str = Field(..., min_length=1, max_length=256)


# ── API Key and Agent Models ─────────────────────────────────────────────────


class ApiKeyCreateRequest(BaseModel):
    """Request body for POST /api-keys. Unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="default", min_length=1, max_length=100)
    scopes: Optional[list[str]] = Field(
        default=None,
        max_length=MAX_SCOPES,
        description="Fields the key may read ('balance' or 'read:balance'); omit for all, [] for none.",
    )
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650, description="Days until the key expires.")

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)


class AgentCreateRequest(BaseModel):
    """Request body for POST /agents. Unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    allowed_scopes: Optional[list[str]] = Field(
        default=None, max_length=MAX_SCOPES, description="Omit for every scope; [] allows none."
    )
    allowed_sites: Optional[list[str]] = Field(
        default=None, max_length=MAX_SCOPES, description="Omit for every site; [] allows none."
    )
    rate_limit: Optional[str] = Field(default=None, max_length=64, description="e.g. '30/minute'")

    @field_validator("allowed_scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)

    @field_validator("allowed_sites")
    @classmethod
    def check_sites(cls, value):
        return validate_site_list(value)

    @field_validator("rate_limit")
    @classmethod
    def check_rate_limit(cls, value):
        return validate_rate_limit(value)


class AgentUpdateRequest(BaseModel):
    """Request body for PATCH /agents/{agent_id}; only the fields sent change.

    An explicit ``null`` for allowed_scopes/allowed_sites/rate_limit removes
    that restriction; ``[]`` allows nothing.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    allowed_scopes: Optional[list[str]] = Field(default=None, max_length=MAX_SCOPES)
    allowed_sites: Optional[list[str]] = Field(default=None, max_length=MAX_SCOPES)
    rate_limit: Optional[str] = Field(default=None, max_length=64)

    @field_validator("allowed_scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)

    @field_validator("allowed_sites")
    @classmethod
    def check_sites(cls, value):
        return validate_site_list(value)

    @field_validator("rate_limit")
    @classmethod
    def check_rate_limit(cls, value):
        return validate_rate_limit(value)


# ── Consent Models ────────────────────────────────────────────────────────────

MIN_CONSENT_DURATION_SECONDS = 60
MAX_CONSENT_DURATION_SECONDS = 30 * 24 * 3600


class ConsentRequestCreate(BaseModel):
    """Request body for POST /consent/request."""

    agent_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Display name; required unless an agent's API key asks (its registered name is used).",
    )
    agent_description: Optional[str] = Field(default=None, max_length=1000)
    scopes: list[str] = Field(..., min_length=1, max_length=MAX_SCOPES)
    access_token: str = Field(..., min_length=1, max_length=256)
    duration_seconds: int = Field(default=3600, ge=MIN_CONSENT_DURATION_SECONDS, le=MAX_CONSENT_DURATION_SECONDS)

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)


# ── Registry Models ───────────────────────────────────────────────────────────


class RegistryPublishRequest(BaseModel):
    """Request body for POST /registry/publish."""

    blueprint: Union[Dict[str, Any], str] = Field(..., description="The full blueprint (object or JSON text).")
    description: Optional[str] = Field(default=None, max_length=2000)


# ── Auth Models ───────────────────────────────────────────────────────────────


class UserRegisterRequest(BaseModel):
    """Request body for POST /auth/register."""

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value):
        return validate_new_password(value)


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    """Request body for POST /auth/refresh."""

    refresh_token: str = Field(..., max_length=256)


class DeleteAccountRequest(BaseModel):
    """Request body for DELETE /auth/me (GDPR account erasure)."""

    password: str | None = Field(
        default=None,
        max_length=128,
        description="Current password — required to confirm deletion for password-based accounts.",
    )


class OAuth2LoginRequest(BaseModel):
    """Request body for POST /auth/oauth2."""

    provider: str = Field(..., max_length=64, description="OAuth2 provider name (e.g., 'google', 'github').")
    oauth_token: str = Field(..., max_length=4096, description="OAuth2 token from the provider.")


class ForgotPasswordRequest(BaseModel):
    """Request body for POST /auth/forgot-password."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Request body for POST /auth/reset-password."""

    token: str = Field(..., max_length=256)
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, value):
        return validate_new_password(value)


class UserProfileResponse(BaseModel):
    """Response from GET /auth/me."""

    id: int
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: bool


# ── MFA Models ────────────────────────────────────────────────────────────────


class MFASubmitRequest(BaseModel):
    """Request body for POST /mfa/submit."""

    session_id: str = Field(..., max_length=256, description="The MFA session ID from the connect response.")
    code: str = Field(..., max_length=32, description="The MFA code entered by the user.")


class MFAStatusResponse(BaseModel):
    """Response from GET /mfa/status/{session_id}."""

    session_id: str
    site: str
    mfa_type: str
    metadata: Optional[Dict[str, Any]] = None


# ── Hosted Link Bootstrap Models ────────────────────────────────────────────


def _normalize_allowed_origin_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("allowed_origin must be an http(s) origin without a path, query, or fragment.")

    return f"{parsed.scheme}://{parsed.netloc}"


class HostedLinkBootstrapRequest(BaseModel):
    """Request body for POST /link/bootstrap."""

    site: Optional[str] = Field(default=None, min_length=1, max_length=64)
    allowed_origin: Optional[str] = Field(default=None, max_length=512)
    allowed_origins: Optional[list[str]] = Field(default=None, max_length=20)
    scopes: Optional[list[str]] = Field(default=None, max_length=MAX_SCOPES)

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, value):
        return validate_scope_list(value)

    @field_validator("allowed_origin")
    @classmethod
    def normalize_allowed_origin(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_allowed_origin_value(value)

    @field_validator("allowed_origins")
    @classmethod
    def normalize_allowed_origins(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return value
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            entry = _normalize_allowed_origin_value(item)
            if entry and entry not in seen:
                seen.add(entry)
                normalized.append(entry)
        return normalized or None


class HostedLinkBootstrapResponse(BaseModel):
    """Response from POST /link/bootstrap."""

    launch_token: str
    expires_in: int
    site: Optional[str] = None
    allowed_origin: Optional[str] = None
    allowed_origins: Optional[list[str]] = None
    scopes: Optional[list[str]] = None


class HostedLinkBootstrapExchangeRequest(BaseModel):
    """Request body for POST /link/sessions/bootstrap."""

    launch_token: str = Field(..., min_length=1, max_length=4096)
