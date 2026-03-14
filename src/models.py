"""
Pydantic request/response models for the Plaidify API.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Any, Dict, Optional


# ── Connection Models ─────────────────────────────────────────────────────────


class ConnectRequest(BaseModel):
    """Request body for POST /connect."""

    site: str = Field(..., description="Site identifier matching a blueprint name.")
    username: str = Field(..., description="Username for the target site.")
    password: str = Field(..., description="Password for the target site.")


class ConnectResponse(BaseModel):
    """Response from POST /connect."""

    status: str = Field(..., description="Connection status (e.g., 'connected').")
    data: Optional[Dict[str, Any]] = Field(
        default=None, description="Extracted data from the target site."
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
    token_type: str = "bearer"


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