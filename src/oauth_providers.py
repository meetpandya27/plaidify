"""OAuth2 social-login provider verification.

Verifies tokens issued by external identity providers (Google, GitHub) by
calling each provider's server-side endpoints, and normalizes the result into
an :class:`OAuthIdentity`. All network access is isolated in this module so it
can be mocked in tests and so the route layer stays provider-agnostic.

Security notes:
- Google tokens are checked against the configured client id via the
  ``tokeninfo`` endpoint, which returns the ``aud`` claim. This prevents
  token-substitution ("confused deputy") attacks where a token minted for a
  different application is replayed against Plaidify.
- A verified email is required by the caller before an account is created or
  linked, so email-based account linking is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from src.logging_config import get_logger

logger = get_logger("auth.oauth")

_HTTP_TIMEOUT = 5.0

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


@dataclass
class OAuthIdentity:
    """A normalized identity resolved from an external provider token."""

    provider: str
    subject: str  # stable, provider-assigned user id
    email: Optional[str]
    email_verified: bool
    username: Optional[str]


class OAuthVerificationError(Exception):
    """Raised when a provider token cannot be verified or lacks required claims."""


def verify_oauth_token(provider: str, token: str, settings: Any) -> OAuthIdentity:
    """Verify an external provider token and return the resolved identity.

    Args:
        provider: Lower-cased provider name ("google" or "github").
        token: The provider-issued access token or ID token from the client.
        settings: Application settings (used for audience validation).

    Raises:
        OAuthVerificationError: If the token is invalid, the request fails, or
            required claims (subject) are missing.
    """
    provider = (provider or "").lower()
    if not token:
        raise OAuthVerificationError("Empty OAuth token.")
    if provider == "google":
        return _verify_google(token, settings)
    if provider == "github":
        return _verify_github(token)
    raise OAuthVerificationError(f"Unsupported OAuth provider: {provider!r}")


def _http_get_json(
    url: str,
    *,
    params: Optional[dict] = None,
    bearer: Optional[str] = None,
    accept: Optional[str] = None,
) -> Any:
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if accept:
        headers["Accept"] = accept
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthVerificationError(f"Provider request failed: {exc}") from exc
    if resp.status_code != 200:
        raise OAuthVerificationError(f"Provider returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OAuthVerificationError("Provider returned a non-JSON response.") from exc


def _verify_google(token: str, settings: Any) -> OAuthIdentity:
    # tokeninfo accepts either an id_token or an access_token and returns the
    # audience (`aud`) the token was minted for, enabling an audience check.
    data: Optional[dict] = None
    last_error: Optional[Exception] = None
    for param in ("id_token", "access_token"):
        try:
            result = _http_get_json(GOOGLE_TOKENINFO_URL, params={param: token})
            if isinstance(result, dict) and result.get("sub"):
                data = result
                break
        except OAuthVerificationError as exc:
            last_error = exc
    if not data:
        raise OAuthVerificationError(
            f"Google token verification failed: {last_error}" if last_error else "Google token verification failed."
        )

    expected_aud = getattr(settings, "oauth_google_client_id", None)
    if expected_aud and data.get("aud") != expected_aud:
        raise OAuthVerificationError("Google token audience does not match the configured client id.")

    subject = str(data.get("sub"))
    email = data.get("email")
    # tokeninfo returns email_verified as the strings "true"/"false".
    email_verified = str(data.get("email_verified", "")).lower() == "true"
    username = email.split("@", 1)[0] if email else None
    return OAuthIdentity("google", subject, email, email_verified, username)


def _verify_github(token: str) -> OAuthIdentity:
    accept = "application/vnd.github+json"
    profile = _http_get_json(GITHUB_USER_URL, bearer=token, accept=accept)
    if not isinstance(profile, dict) or not profile.get("id"):
        raise OAuthVerificationError("GitHub profile missing 'id'.")

    subject = str(profile["id"])
    username = profile.get("login")
    email = profile.get("email")
    email_verified = False

    # GitHub's profile email is often null; the primary verified address lives
    # behind /user/emails (requires the user:email scope).
    try:
        emails = _http_get_json(GITHUB_EMAILS_URL, bearer=token, accept=accept)
        if isinstance(emails, list):
            primary = next(
                (e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")),
                None,
            )
            if primary:
                email = primary.get("email")
                email_verified = True
    except OAuthVerificationError:
        # Missing user:email scope — fall back to the (possibly null) profile email.
        logger.info("GitHub /user/emails unavailable; using profile email if present.")

    return OAuthIdentity("github", subject, email, email_verified, username)
