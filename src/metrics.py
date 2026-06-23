"""Prometheus metrics — defined centrally.

These metrics live here (not in ``src.app``) so any module can record values
without importing the FastAPI application, which would create circular imports
(``app`` imports the routers, which import the engine, which records metrics).

All recorders are best-effort: if ``prometheus_client`` is not installed, or a
label/registry error occurs, recording is a no-op and never breaks a request
flow. The HTTP auto-instrumentation (request counts/latencies) is wired
separately in ``src.app`` via ``prometheus_fastapi_instrumentator``.
"""

from __future__ import annotations

from src.logging_config import get_logger

logger = get_logger("metrics")

try:
    from prometheus_client import Counter, Gauge

    PROMETHEUS_AVAILABLE = True

    browser_pool_active = Gauge(
        "plaidify_browser_pool_active_contexts",
        "Number of active browser contexts in the pool",
    )
    extraction_total = Counter(
        "plaidify_blueprint_extractions_total",
        "Total blueprint data extractions",
        ["site", "status"],
    )
    mfa_challenges_total = Counter(
        "plaidify_mfa_challenges_total",
        "Total MFA challenges encountered",
        ["mfa_type"],
    )
except ImportError:  # pragma: no cover - prometheus is an optional dependency
    PROMETHEUS_AVAILABLE = False
    browser_pool_active = None
    extraction_total = None
    mfa_challenges_total = None


def record_extraction(site: str, status: str) -> None:
    """Count one blueprint extraction attempt with its outcome (success/error)."""
    if extraction_total is None:
        return
    try:
        extraction_total.labels(site=site, status=status).inc()
    except Exception:  # pragma: no cover - metrics must never break a flow
        pass


def record_mfa_challenge(mfa_type: str) -> None:
    """Count one MFA challenge encountered, labelled by MFA type."""
    if mfa_challenges_total is None:
        return
    try:
        mfa_challenges_total.labels(mfa_type=mfa_type or "unknown").inc()
    except Exception:  # pragma: no cover
        pass


def set_browser_pool_active(count: int) -> None:
    """Set the gauge of currently-active browser contexts."""
    if browser_pool_active is None:
        return
    try:
        browser_pool_active.set(count)
    except Exception:  # pragma: no cover
        pass
