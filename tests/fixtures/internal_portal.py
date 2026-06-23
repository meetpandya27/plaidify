"""Internal browser test fixture portal for Playwright integration tests.

The portal implementation now lives in :mod:`src.demo.portal` so the shipped
sandbox target and the test fixture stay byte-for-byte identical. This module
re-exports it for backwards compatibility with tests that import
``tests.fixtures.internal_portal``.
"""

from src.demo.portal import (  # noqa: F401
    MFA_CODE,
    VALID_USERS,
    app,
    create_app,
)

__all__ = ["MFA_CODE", "VALID_USERS", "app", "create_app"]
