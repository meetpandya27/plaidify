"""
Shared error taxonomy for the hosted Link flow (issue #55).

This module is the single source of truth for structured error codes
surfaced to the hosted Link UI, SDKs, and downstream consumers (EXIT /
ERROR bridge events). Each code carries remediation copy and CTA
targets so the frontend can render consistent recovery screens without
hardcoded strings spread across the codebase.

The companion TypeScript mirror lives in
`frontend-next/src/errorTaxonomy.ts` — keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class LinkErrorCode(str, Enum):
    """Canonical hosted-link error codes."""

    INVALID_CREDENTIALS = "invalid_credentials"
    MFA_TIMEOUT = "mfa_timeout"
    INSTITUTION_DOWN = "institution_down"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_SITE = "unsupported_site"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Remediation:
    """Remediation copy + CTAs for a given error code."""

    title: str
    description: str
    primary_cta: str
    primary_action: str  # "retry" | "back_to_picker" | "exit" | "contact_support"
    secondary_cta: str | None
    secondary_action: str | None
    retryable: bool


REMEDIATIONS: dict[LinkErrorCode, Remediation] = {
    LinkErrorCode.INVALID_CREDENTIALS: Remediation(
        title="We couldn't sign you in",
        description=(
            "The username or password doesn't match what your provider has "
            "on file. Double-check your credentials and try again, or reset "
            "them with your provider."
        ),
        primary_cta="Try again",
        primary_action="retry",
        secondary_cta="Choose a different provider",
        secondary_action="back_to_picker",
        retryable=True,
    ),
    LinkErrorCode.MFA_TIMEOUT: Remediation(
        title="Verification timed out",
        description=("We didn't receive your verification code in time. You can request a new code and try again."),
        primary_cta="Start over",
        primary_action="retry",
        secondary_cta="Choose a different provider",
        secondary_action="back_to_picker",
        retryable=True,
    ),
    LinkErrorCode.INSTITUTION_DOWN: Remediation(
        title="Your provider is temporarily unavailable",
        description=(
            "Their systems aren't responding right now. This usually "
            "resolves within a few minutes — try again shortly, or pick a "
            "different provider."
        ),
        primary_cta="Try again",
        primary_action="retry",
        secondary_cta="Choose a different provider",
        secondary_action="back_to_picker",
        retryable=True,
    ),
    LinkErrorCode.RATE_LIMITED: Remediation(
        title="Too many attempts",
        description=("We're pausing briefly to protect your account. Please wait a moment before trying again."),
        primary_cta="Try again in a moment",
        primary_action="retry",
        secondary_cta="Exit",
        secondary_action="exit",
        retryable=True,
    ),
    LinkErrorCode.UNSUPPORTED_SITE: Remediation(
        title="Provider not supported yet",
        description=(
            "We don't have an integration for this provider. Pick a "
            "different one, or let us know what you'd like us to add."
        ),
        primary_cta="Choose a different provider",
        primary_action="back_to_picker",
        secondary_cta="Contact support",
        secondary_action="contact_support",
        retryable=False,
    ),
    LinkErrorCode.NETWORK_ERROR: Remediation(
        title="Connection interrupted",
        description=("We lost the connection to your provider. Check your internet connection and try again."),
        primary_cta="Try again",
        primary_action="retry",
        secondary_cta="Choose a different provider",
        secondary_action="back_to_picker",
        retryable=True,
    ),
    LinkErrorCode.INTERNAL_ERROR: Remediation(
        title="Something went wrong on our end",
        description=(
            "An unexpected error occurred while setting up your secure "
            "connection. Please try again, and contact support if this "
            "keeps happening."
        ),
        primary_cta="Try again",
        primary_action="retry",
        secondary_cta="Contact support",
        secondary_action="contact_support",
        retryable=True,
    ),
}


def remediation_for(code: LinkErrorCode | str) -> Remediation:
    """Resolve a remediation entry by enum member or raw string."""
    if isinstance(code, LinkErrorCode):
        return REMEDIATIONS[code]
    try:
        return REMEDIATIONS[LinkErrorCode(code)]
    except ValueError:
        return REMEDIATIONS[LinkErrorCode.INTERNAL_ERROR]


def serialize_taxonomy() -> dict[str, Any]:
    """Wire-format suitable for /link/error-taxonomy and SDKs."""
    return {
        "version": 1,
        "codes": [
            {
                "code": code.value,
                "title": remediation.title,
                "description": remediation.description,
                "primary_cta": remediation.primary_cta,
                "primary_action": remediation.primary_action,
                "secondary_cta": remediation.secondary_cta,
                "secondary_action": remediation.secondary_action,
                "retryable": remediation.retryable,
            }
            for code, remediation in REMEDIATIONS.items()
        ],
    }


def classify_exception(exc: BaseException) -> LinkErrorCode:
    """Map a known exception type onto a structured error code.

    Callers should prefer the exception's own `error_code` attribute (on
    `PlaidifyError`); this helper is for untyped exceptions bubbled up
    from third-party code.
    """
    # Avoid a hard import cycle — resolve lazily.
    from src.exceptions import PlaidifyError

    if isinstance(exc, PlaidifyError):
        code = getattr(exc, "error_code", None)
        if isinstance(code, LinkErrorCode):
            return code
        if isinstance(code, str):
            try:
                return LinkErrorCode(code)
            except ValueError:
                pass

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return LinkErrorCode.NETWORK_ERROR
    if "connection" in name or "network" in name:
        return LinkErrorCode.NETWORK_ERROR
    return LinkErrorCode.INTERNAL_ERROR
