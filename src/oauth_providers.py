"""OAuth2 social-login provider verification.

Verifies tokens issued by external identity providers (Google, GitHub) by
calling each provider's server-side endpoints, and normalizes the result into
an :class:`OAuthIdentity`. All network access is isolated in this module so it
can be mocked in tests and so the route layer stays provider-agnostic.

Security notes:
- A token is only trusted when it was issued to Plaidify's own OAuth app;
  otherwise any other app the user signed into could replay its token here
  (a "confused deputy"). Google tokens are checked against
  ``OAUTH_GOOGLE_CLIENT_ID`` through the ``tokeninfo`` endpoint's ``aud``
  claim. GitHub tokens are checked with GitHub's "check a token" API
  (``POST /applications/{client_id}/token``, authenticated with the app's
  client id and secret), which only answers for tokens of that app. Missing
  client credentials fail closed; the app refuses to start without them when
  OAuth is enabled (see ``missing_oauth_configuration``).
- A verified email is required by the caller before an account is created,
  and an identity is linked into an existing account only when that
  account's own email is verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from src.logging_config import get_logger

logger = get_logger("auth.oauth")

_HTTP_TIMEOUT = 5.0

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GITHUB_API_URL = "https://api.github.com"
GITHUB_EMAILS_URL = f"{GITHUB_API_URL}/user/emails"
_GITHUB_ACCEPT = "application/vnd.github+json"


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


def missing_oauth_configuration(settings: Any) -> list[str]:
    """Settings an enabled provider needs but lacks (empty when OAuth is off or complete)."""
    if not getattr(settings, "oauth_enabled", False):
        return []
    providers = {p.strip().lower() for p in (settings.oauth_allowed_providers or "").split(",") if p.strip()}
    missing: list[str] = []
    if "google" in providers and not getattr(settings, "oauth_google_client_id", None):
        missing.append("OAUTH_GOOGLE_CLIENT_ID")
    if "github" in providers:
        if not getattr(settings, "oauth_github_client_id", None):
            missing.append("OAUTH_GITHUB_CLIENT_ID")
        if not getattr(settings, "oauth_github_client_secret", None):
            missing.append("OAUTH_GITHUB_CLIENT_SECRET")
    return missing


def verify_oauth_token(provider: str, token: str, settings: Any) -> OAuthIdentity:
    """Verify an external provider token and return the resolved identity.

    Args:
        provider: Lower-cased provider name ("google" or "github").
        token: The provider-issued access token or ID token from the client.
        settings: Application settings (the app's client credentials).

    Raises:
        OAuthVerificationError: If the token is invalid, was not issued to
            Plaidify's OAuth app, the request fails, or required claims
            (subject) are missing.
    """
    provider = (provider or "").lower()
    if not token:
        raise OAuthVerificationError("Empty OAuth token.")
    if provider == "google":
        return _verify_google(token, settings)
    if provider == "github":
        return _verify_github(token, settings)
    raise OAuthVerificationError(f"Unsupported OAuth provider: {provider!r}")


def _response_json(resp: httpx.Response) -> Any:
    if resp.status_code != 200:
        raise OAuthVerificationError(f"Provider returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OAuthVerificationError("Provider returned a non-JSON response.") from exc


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
    return _response_json(resp)


def _http_post_json(url: str, *, json: dict, auth: tuple[str, str], accept: Optional[str] = None) -> Any:
    headers = {"Accept": accept} if accept else {}
    try:
        resp = httpx.post(url, json=json, auth=auth, headers=headers, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthVerificationError(f"Provider request failed: {exc}") from exc
    return _response_json(resp)


def _verify_google(token: str, settings: Any) -> OAuthIdentity:
    expected_aud = getattr(settings, "oauth_google_client_id", None)
    if not expected_aud:
        raise OAuthVerificationError("OAUTH_GOOGLE_CLIENT_ID is not configured; Google tokens cannot be checked.")

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

    if data.get("aud") != expected_aud:
        raise OAuthVerificationError("Google token audience does not match the configured client id.")

    subject = str(data.get("sub"))
    email = data.get("email")
    # tokeninfo returns email_verified as the strings "true"/"false".
    email_verified = str(data.get("email_verified", "")).lower() == "true"
    username = email.split("@", 1)[0] if email else None
    return OAuthIdentity("google", subject, email, email_verified, username)


def _verify_github(token: str, settings: Any) -> OAuthIdentity:
    client_id = getattr(settings, "oauth_github_client_id", None)
    client_secret = getattr(settings, "oauth_github_client_secret", None)
    if not client_id or not client_secret:
        raise OAuthVerificationError(
            "OAUTH_GITHUB_CLIENT_ID and OAUTH_GITHUB_CLIENT_SECRET are required to check GitHub tokens."
        )

    # "Check a token": 200 only for a live token issued to this OAuth app
    # (404 for any other app's token), and it names the user it belongs to.
    check = _http_post_json(
        f"{GITHUB_API_URL}/applications/{client_id}/token",
        json={"access_token": token},
        auth=(client_id, client_secret),
        accept=_GITHUB_ACCEPT,
    )
    app = check.get("app") if isinstance(check, dict) else None
    owner = check.get("user") if isinstance(check, dict) else None
    if not isinstance(app, dict) or str(app.get("client_id")) != str(client_id):
        raise OAuthVerificationError("GitHub token was not issued to the configured OAuth app.")
    if not isinstance(owner, dict) or not owner.get("id"):
        raise OAuthVerificationError("GitHub token check did not name a user.")

    subject = str(owner["id"])
    username = owner.get("login")
    email = None
    email_verified = False

    # The primary verified address lives behind /user/emails (user:email scope).
    try:
        emails = _http_get_json(GITHUB_EMAILS_URL, bearer=token, accept=_GITHUB_ACCEPT)
        if isinstance(emails, list):
            primary = next(
                (e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")),
                None,
            )
            if primary:
                email = primary.get("email")
                email_verified = True
    except OAuthVerificationError:
        # Missing user:email scope: no verified address, and the caller refuses the sign-in.
        logger.info("GitHub /user/emails unavailable; no verified email for this token.")

    return OAuthIdentity("github", subject, email, email_verified, username)
