"""
Pydantic request/response models for the Plaidify API.
"""

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

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
    session_id: Optional[str] = Field(default=None, description="Session ID for MFA continuation.")
    mfa_type: Optional[str] = Field(default=None, description="Type of MFA required (if status is 'mfa_required').")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional metadata (e.g., MFA question text)."
    )


# ── Status Models ─────────────────────────────────────────────────────────────


class StatusResponse(BaseModel):
    """Response from GET /status."""

    status: str
    message: Optional[str] = None


class DisconnectResponse(BaseModel):
    """Response from POST /disconnect."""

    status: str
    message: Optional[str] = None


# ── Auth Models ───────────────────────────────────────────────────────────────


class UserRegisterRequest(BaseModel):
    """Request body for POST /auth/register."""

    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r"[^A-Za-z0-9]", v):
            raise ValueError("Password must contain at least one special character.")
        return v


class UserLoginRequest(BaseModel):
    """Request body for POST /auth/token."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    """Request body for POST /auth/refresh."""

    refresh_token: str = Field(..., max_length=256)


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
    def validate_password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r"[^A-Za-z0-9]", v):
            raise ValueError("Password must contain at least one special character.")
        return v


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


# ── Blueprint Info Models ─────────────────────────────────────────────────────


class BlueprintInfoResponse(BaseModel):
    """Response from GET /blueprints/{site}."""

    name: str
    domain: str
    tags: list[str]
    has_mfa: bool
    extract_fields: list[str]
    schema_version: str


# ── Hosted Link Bootstrap Models ────────────────────────────────────────────


class HostedLinkBootstrapRequest(BaseModel):
    """Request body for POST /link/bootstrap."""

    site: Optional[str] = Field(default=None, min_length=1, max_length=64)
    allowed_origin: Optional[str] = Field(default=None, max_length=512)
    scopes: Optional[list[str]] = Field(default=None, max_length=100)

    @field_validator("allowed_origin")
    @classmethod
    def normalize_allowed_origin(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip().rstrip("/")
        return normalized or None


class HostedLinkBootstrapResponse(BaseModel):
    """Response from POST /link/bootstrap."""

    launch_token: str
    expires_in: int
    site: Optional[str] = None
    allowed_origin: Optional[str] = None
    scopes: Optional[list[str]] = None


class HostedLinkBootstrapExchangeRequest(BaseModel):
    """Request body for POST /link/sessions/bootstrap."""

    launch_token: str = Field(..., min_length=1, max_length=4096)
