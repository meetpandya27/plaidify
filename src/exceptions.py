"""
Custom exception hierarchy for Plaidify.

All Plaidify-specific exceptions inherit from PlaidifyError.
This allows callers to catch all Plaidify errors with a single except clause,
or catch specific sub-types for fine-grained handling.
"""


class PlaidifyError(Exception):
    """Base exception for all Plaidify errors."""

    def __init__(self, message: str = "An unexpected error occurred.", status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


# ── Blueprint Errors ─────────────────────────────────────────────────────────


class BlueprintNotFoundError(PlaidifyError):
    """Raised when a connector blueprint cannot be found for the requested site."""

    def __init__(self, site: str):
        super().__init__(
            message=f"No blueprint found for site: {site}",
            status_code=404,
        )
        self.site = site


class BlueprintValidationError(PlaidifyError):
    """Raised when a blueprint file is malformed or fails validation."""

    def __init__(self, site: str, detail: str = ""):
        msg = f"Blueprint validation failed for site: {site}"
        if detail:
            msg += f" — {detail}"
        super().__init__(message=msg, status_code=422)
        self.site = site


# ── Connection Errors ────────────────────────────────────────────────────────


class ConnectionFailedError(PlaidifyError):
    """Raised when the engine fails to establish a connection to the target site."""

    def __init__(self, site: str, detail: str = ""):
        msg = f"Connection failed for site: {site}"
        if detail:
            msg += f" — {detail}"
        super().__init__(message=msg, status_code=502)
        self.site = site


class AuthenticationError(PlaidifyError):
    """Raised when login credentials are rejected by the target site."""

    def __init__(self, site: str):
        super().__init__(
            message=f"Authentication failed for site: {site}. Check your credentials.",
            status_code=401,
        )
        self.site = site


class MFARequiredError(PlaidifyError):
    """Raised when the target site requires multi-factor authentication."""

    def __init__(self, site: str, mfa_type: str = "unknown", session_id: str = ""):
        super().__init__(
            message=f"MFA required for site: {site} (type: {mfa_type})",
            status_code=403,
        )
        self.site = site
        self.mfa_type = mfa_type
        self.session_id = session_id


class SiteUnavailableError(PlaidifyError):
    """Raised when the target site is unreachable or returns an error."""

    def __init__(self, site: str, detail: str = ""):
        msg = f"Site unavailable: {site}"
        if detail:
            msg += f" — {detail}"
        super().__init__(message=msg, status_code=503)
        self.site = site


class RateLimitedError(PlaidifyError):
    """Raised when the target site or Plaidify itself is rate-limiting requests."""

    def __init__(self, retry_after: int = 60):
        super().__init__(
            message=f"Rate limited. Retry after {retry_after} seconds.",
            status_code=429,
        )
        self.retry_after = retry_after


class CaptchaRequiredError(PlaidifyError):
    """Raised when the target site presents a CAPTCHA challenge."""

    def __init__(self, site: str, captcha_type: str = "unknown"):
        super().__init__(
            message=f"CAPTCHA required for site: {site} (type: {captcha_type})",
            status_code=403,
        )
        self.site = site
        self.captcha_type = captcha_type


# ── Data Errors ──────────────────────────────────────────────────────────────


class DataExtractionError(PlaidifyError):
    """Raised when the engine fails to extract expected data from a page."""

    def __init__(self, site: str, detail: str = ""):
        msg = f"Data extraction failed for site: {site}"
        if detail:
            msg += f" — {detail}"
        super().__init__(message=msg, status_code=500)
        self.site = site


# ── Auth / Token Errors (Plaidify's own auth) ───────────────────────────────


class InvalidTokenError(PlaidifyError):
    """Raised when a JWT or access token is invalid or expired."""

    def __init__(self, detail: str = "Invalid or expired token."):
        super().__init__(message=detail, status_code=401)


class UserNotFoundError(PlaidifyError):
    """Raised when a user ID from a token doesn't correspond to an existing user."""

    def __init__(self):
        super().__init__(message="User not found.", status_code=401)


class LinkNotFoundError(PlaidifyError):
    """Raised when a link token is invalid or doesn't belong to the user."""

    def __init__(self):
        super().__init__(message="Invalid link token.", status_code=404)
