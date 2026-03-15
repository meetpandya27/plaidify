"""
Pydantic request/response models for the Plaidify API.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Any, Dict, Optional


# ── Connection Models ─────────────────────────────────────────────────────────


class ConnectRequest(BaseModel):
    """Request body for POST /connect.

    Credentials can be sent as plaintext (username/password) or encrypted
    (encrypted_username/encrypted_password + link_token). If encrypted fields
    are present they take precedence.
    """

    site: str = Field(..., description="Site identifier matching a blueprint name.")
    username: Optional[str] = Field(default=None, description="Plaintext username (omit if encrypted).")
    password: Optional[str] = Field(default=None, description="Plaintext password (omit if encrypted).")
    encrypted_username: Optional[str] = Field(default=None, description="Base64-encoded RSA-OAEP encrypted username.")
    encrypted_password: Optional[str] = Field(default=None, description="Base64-encoded RSA-OAEP encrypted password.")
    link_token: Optional[str] = Field(default=None, description="Link token whose ephemeral key encrypts the credentials.")
    extract_fields: Optional[list[str]] = Field(
        default=None,
        description="Specific fields to extract (None = all defined in blueprint).",
    )


class ConnectResponse(BaseModel):
    """Response from POST /connect."""

    status: str = Field(..., description="Connection status (e.g., 'connected', 'mfa_required').")
    data: Optional[Dict[str, Any]] = Field(
        default=None, description="Extracted data from the target site."
    )
    session_id: Optional[str] = Field(
        default=None, description="Session ID for MFA continuation."
    )
    mfa_type: Optional[str] = Field(
        default=None, description="Type of MFA required (if status is 'mfa_required')."
    )
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
    password: str = Field(..., min_length=8)


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

    refresh_token: str


class OAuth2LoginRequest(BaseModel):
    """Request body for POST /auth/oauth2."""

    provider: str = Field(..., description="OAuth2 provider name (e.g., 'google', 'github').")
    oauth_token: str = Field(..., description="OAuth2 token from the provider.")


class UserProfileResponse(BaseModel):
    """Response from GET /auth/me."""

    id: int
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: bool


# ── MFA Models ────────────────────────────────────────────────────────────────


class MFASubmitRequest(BaseModel):
    """Request body for POST /mfa/submit."""

    session_id: str = Field(..., description="The MFA session ID from the connect response.")
    code: str = Field(..., description="The MFA code entered by the user.")


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