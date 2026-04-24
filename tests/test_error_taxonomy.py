"""Tests for the hosted-link error taxonomy (issue #55)."""

from fastapi.testclient import TestClient

from src.app import app
from src.error_taxonomy import (
    REMEDIATIONS,
    LinkErrorCode,
    classify_exception,
    remediation_for,
    serialize_taxonomy,
)
from src.exceptions import (
    AuthenticationError,
    BlueprintNotFoundError,
    ConnectionFailedError,
    RateLimitedError,
    SiteUnavailableError,
)


def test_every_code_has_a_remediation() -> None:
    for code in LinkErrorCode:
        assert code in REMEDIATIONS
        entry = REMEDIATIONS[code]
        assert entry.title and entry.description and entry.primary_cta
        assert entry.primary_action in {
            "retry",
            "back_to_picker",
            "exit",
            "contact_support",
        }


def test_remediation_for_accepts_strings_and_defaults() -> None:
    assert remediation_for("invalid_credentials") is REMEDIATIONS[LinkErrorCode.INVALID_CREDENTIALS]
    # Unknown codes degrade gracefully to internal_error.
    assert remediation_for("not_a_code").title == REMEDIATIONS[LinkErrorCode.INTERNAL_ERROR].title


def test_classify_exception_maps_known_plaidify_errors() -> None:
    assert classify_exception(AuthenticationError("demo")) == LinkErrorCode.INVALID_CREDENTIALS
    assert classify_exception(SiteUnavailableError("demo")) == LinkErrorCode.INSTITUTION_DOWN
    assert classify_exception(RateLimitedError()) == LinkErrorCode.RATE_LIMITED
    assert classify_exception(BlueprintNotFoundError("demo")) == LinkErrorCode.UNSUPPORTED_SITE
    assert classify_exception(ConnectionFailedError("demo")) == LinkErrorCode.NETWORK_ERROR
    assert classify_exception(RuntimeError("boom")) == LinkErrorCode.INTERNAL_ERROR


def test_link_error_taxonomy_endpoint_exposes_all_codes() -> None:
    client = TestClient(app)
    response = client.get("/link/error-taxonomy")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    codes = {entry["code"] for entry in body["codes"]}
    assert codes == {member.value for member in LinkErrorCode}


def test_plaidify_error_handler_includes_error_code() -> None:
    # We need an endpoint that raises AuthenticationError. Use a handler
    # via FastAPI dependency injection on an ad-hoc route.
    from src.app import app as fastapi_app

    @fastapi_app.get("/_test/auth-fail")
    def _auth_fail() -> None:
        raise AuthenticationError("demo-site")

    client = TestClient(fastapi_app)
    response = client.get("/_test/auth-fail")
    assert response.status_code == 401
    body = response.json()
    assert body["error_code"] == LinkErrorCode.INVALID_CREDENTIALS.value


def test_serialize_taxonomy_is_deterministic() -> None:
    first = serialize_taxonomy()
    second = serialize_taxonomy()
    assert first == second
