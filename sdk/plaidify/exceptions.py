"""
Plaidify SDK exceptions.

All exceptions inherit from PlaidifyError for easy catching:

    try:
        result = await pfy.connect(...)
    except PlaidifyError as e:
        print(f"Plaidify error: {e}")
"""


class PlaidifyError(Exception):
    """Base exception for all Plaidify SDK errors."""

    def __init__(self, message: str, status_code: int | None = None, detail: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, status_code={self.status_code})"


class ConnectionError(PlaidifyError):
    """Server is unreachable or returned a network-level error."""

    def __init__(self, message: str = "Could not connect to Plaidify server.", **kwargs):
        super().__init__(message, **kwargs)


class AuthenticationError(PlaidifyError):
    """Login credentials were rejected by the target site."""

    def __init__(self, site: str, message: str | None = None, **kwargs):
        self.site = site
        msg = message or f"Authentication failed for site: {site}"
        super().__init__(msg, status_code=401, **kwargs)


class MFARequiredError(PlaidifyError):
    """The target site requires multi-factor authentication."""

    def __init__(
        self,
        site: str,
        session_id: str,
        mfa_type: str = "unknown",
        metadata: dict | None = None,
        **kwargs,
    ):
        self.site = site
        self.session_id = session_id
        self.mfa_type = mfa_type
        self.metadata = metadata or {}
        super().__init__(
            f"MFA required for site: {site} (type: {mfa_type})",
            status_code=403,
            **kwargs,
        )


class BlueprintNotFoundError(PlaidifyError):
    """No blueprint exists for the requested site."""

    def __init__(self, site: str, **kwargs):
        self.site = site
        super().__init__(f"No blueprint found for site: {site}", status_code=404, **kwargs)


class BlueprintValidationError(PlaidifyError):
    """A blueprint file is malformed or fails schema validation."""

    def __init__(self, message: str = "Blueprint validation failed.", **kwargs):
        super().__init__(message, status_code=422, **kwargs)


class ServerError(PlaidifyError):
    """The Plaidify server returned an unexpected error."""

    def __init__(self, message: str = "Plaidify server error.", **kwargs):
        super().__init__(message, status_code=500, **kwargs)


class RateLimitedError(PlaidifyError):
    """Request was rate-limited. Retry after the specified delay."""

    def __init__(self, retry_after: int = 60, **kwargs):
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited. Retry after {retry_after} seconds.",
            status_code=429,
            **kwargs,
        )


class InvalidTokenError(PlaidifyError):
    """The JWT or access token is invalid or expired."""

    def __init__(self, message: str = "Invalid or expired token.", **kwargs):
        super().__init__(message, status_code=401, **kwargs)
