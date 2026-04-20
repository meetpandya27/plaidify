"""Tests for Plaidify SDK exceptions."""

from plaidify.exceptions import (
    PlaidifyError,
    ConnectionError,
    AuthenticationError,
    MFARequiredError,
    BlueprintNotFoundError,
    BlueprintValidationError,
    ServerError,
    RateLimitedError,
    InvalidTokenError,
)


class TestPlaidifyError:
    def test_base_error(self):
        e = PlaidifyError("Something went wrong", status_code=500)
        assert str(e) == "Something went wrong"
        assert e.status_code == 500
        assert e.detail == {}

    def test_base_with_detail(self):
        e = PlaidifyError("err", detail={"key": "value"})
        assert e.detail == {"key": "value"}

    def test_repr(self):
        e = PlaidifyError("test", status_code=400)
        assert "PlaidifyError" in repr(e)
        assert "400" in repr(e)

    def test_is_exception(self):
        e = PlaidifyError("test")
        assert isinstance(e, Exception)


class TestConnectionError:
    def test_default_message(self):
        e = ConnectionError()
        assert "connect" in e.message.lower()

    def test_custom_message(self):
        e = ConnectionError("Server offline")
        assert e.message == "Server offline"


class TestAuthenticationError:
    def test_auth_error(self):
        e = AuthenticationError(site="internal_bank")
        assert "internal_bank" in str(e)
        assert e.site == "internal_bank"
        assert e.status_code == 401


class TestMFARequiredError:
    def test_mfa_error(self):
        e = MFARequiredError(
            site="bank",
            session_id="sess-123",
            mfa_type="otp",
            metadata={"prompt": "Enter code"},
        )
        assert e.site == "bank"
        assert e.session_id == "sess-123"
        assert e.mfa_type == "otp"
        assert e.metadata == {"prompt": "Enter code"}
        assert e.status_code == 403


class TestBlueprintNotFoundError:
    def test_not_found(self):
        e = BlueprintNotFoundError(site="nonexistent")
        assert "nonexistent" in str(e)
        assert e.status_code == 404


class TestBlueprintValidationError:
    def test_validation_error(self):
        e = BlueprintValidationError("Missing auth section")
        assert e.status_code == 422


class TestServerError:
    def test_server_error(self):
        e = ServerError()
        assert e.status_code == 500


class TestRateLimitedError:
    def test_rate_limited(self):
        e = RateLimitedError(retry_after=30)
        assert e.retry_after == 30
        assert e.status_code == 429
        assert "30" in str(e)


class TestInvalidTokenError:
    def test_invalid_token(self):
        e = InvalidTokenError()
        assert e.status_code == 401


class TestExceptionHierarchy:
    """All exceptions should inherit from PlaidifyError."""

    def test_hierarchy(self):
        classes = [
            ConnectionError,
            AuthenticationError,
            MFARequiredError,
            BlueprintNotFoundError,
            BlueprintValidationError,
            ServerError,
            RateLimitedError,
            InvalidTokenError,
        ]
        for cls in classes:
            assert issubclass(cls, PlaidifyError), f"{cls.__name__} should inherit from PlaidifyError"

    def test_catch_all(self):
        """A single `except PlaidifyError` should catch all subtypes."""
        try:
            raise BlueprintNotFoundError(site="x")
        except PlaidifyError:
            pass  # Expected
