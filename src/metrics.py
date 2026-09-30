"""Prometheus metrics — defined centrally.

These metrics live here (not in ``src.app``) so any module can record values
without importing the FastAPI application, which would create circular imports
(``app`` imports the routers, which import the engine, which records metrics).

All recorders are best-effort: if ``prometheus_client`` is not installed, or a
label/registry error occurs, recording is a no-op and never breaks a request
flow. The HTTP auto-instrumentation (request counts/latencies) is wired
separately in ``src.app`` via ``prometheus_fastapi_instrumentator``.

Under gunicorn every worker is its own process; ``gunicorn.conf.py`` turns on
prometheus_client's multiprocess mode (``PROMETHEUS_MULTIPROC_DIR``) and
:func:`metrics_registry` merges the workers' values for exposition. Processes
without the web app, like the access-job executor, expose their metrics with
:func:`start_worker_metrics_server`.
"""

from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from src.logging_config import get_logger

logger = get_logger("metrics")

try:
    from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge

    PROMETHEUS_AVAILABLE = True

    browser_pool_active = Gauge(
        "plaidify_browser_pool_active_contexts",
        "Number of active browser contexts in the pool",
        # Summed over the live processes of one server, not one series per pid.
        multiprocess_mode="livesum",
    )
    browser_pool_capacity = Gauge(
        "plaidify_browser_pool_capacity_contexts",
        "Maximum browser contexts of the started pools (BROWSER_POOL_SIZE per process)",
        multiprocess_mode="livesum",
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
    REGISTRY = None
    browser_pool_active = None
    browser_pool_capacity = None
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


def set_browser_pool_capacity(size: int) -> None:
    """Report the pool's configured size when it starts (0 when it stops)."""
    if browser_pool_capacity is None:
        return
    try:
        browser_pool_capacity.set(size)
    except Exception:  # pragma: no cover
        pass


def metrics_registry():
    """The registry to expose: every process's values in multiprocess mode.

    A fresh registry per call, as prometheus_client recommends, because the
    multiprocess collector reads the value files at collection time.
    """
    if not PROMETHEUS_AVAILABLE:
        return None
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


# ── Worker endpoint (access-job executor) ────────────────────────────────────

_loop_heartbeat: Optional[float] = None  # time.monotonic() of the last loop tick
_worker_heartbeat_gauge = None
_worker_server: Optional[ThreadingHTTPServer] = None
_tick_handle = None


def _tick(loop, interval: float) -> None:
    global _loop_heartbeat, _tick_handle
    _loop_heartbeat = time.monotonic()
    if _worker_heartbeat_gauge is not None:
        try:
            _worker_heartbeat_gauge.set_to_current_time()
        except Exception:  # pragma: no cover
            pass
    _tick_handle = loop.call_later(interval, _tick, loop, interval)


def loop_stalled_for() -> Optional[float]:
    """Seconds since the worker's event loop last ran a callback (None before the first tick)."""
    if _loop_heartbeat is None:
        return None
    return time.monotonic() - _loop_heartbeat


class _WorkerEndpointHandler(BaseHTTPRequestHandler):
    stall_seconds = 60.0

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

            self._reply(200, generate_latest(metrics_registry()), CONTENT_TYPE_LATEST)
        elif path == "/health":
            stalled = loop_stalled_for()
            if stalled is not None and stalled <= self.stall_seconds:
                self._reply(200, b'{"status":"healthy"}', "application/json")
            else:
                self._reply(503, b'{"status":"stalled"}', "application/json")
        else:
            self._reply(404, b"not found\n", "text/plain")

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        # Probes arrive every few seconds; keep them out of the log.
        return


def start_worker_metrics_server(
    port: int,
    addr: str = "0.0.0.0",
    *,
    stall_seconds: float = 60.0,
    tick_interval: float = 5.0,
) -> bool:
    """Serve ``/metrics`` and ``/health`` for a process without the web app.

    Call it from inside the process's running asyncio event loop. ``/health``
    answers 200 while that loop keeps running callbacks and 503 once it has
    not for ``stall_seconds`` — a deadlocked or blocked worker that a liveness
    probe should restart. The server runs in a daemon thread, so it stays up
    exactly as long as the worker. Returns False when disabled (port 0),
    prometheus_client is missing, or the port can't be bound.
    """
    global _worker_server, _worker_heartbeat_gauge, _loop_heartbeat, _tick_handle
    import asyncio

    if not port or not PROMETHEUS_AVAILABLE:
        return False
    if _worker_server is not None:
        return True

    loop = asyncio.get_running_loop()
    if _worker_heartbeat_gauge is None:
        _worker_heartbeat_gauge = Gauge(
            "plaidify_worker_heartbeat_timestamp_seconds",
            "Unix time the worker's event loop last ran its heartbeat callback",
            multiprocess_mode="max",
        )

    handler = type("WorkerEndpointHandler", (_WorkerEndpointHandler,), {"stall_seconds": stall_seconds})
    try:
        server = ThreadingHTTPServer((addr, port), handler)
    except OSError as exc:
        logger.error("Worker metrics endpoint could not bind %s:%s: %s", addr, port, exc)
        return False
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="worker-metrics", daemon=True).start()
    _worker_server = server

    _loop_heartbeat = time.monotonic()
    _worker_heartbeat_gauge.set_to_current_time()
    _tick_handle = loop.call_later(tick_interval, _tick, loop, tick_interval)
    logger.info("Worker metrics and health endpoint listening on %s:%s", addr, server.server_address[1])
    return True


def stop_worker_metrics_server() -> None:
    """Shut the worker endpoint down (tests; the process exit does it otherwise)."""
    global _worker_server, _loop_heartbeat, _tick_handle
    if _tick_handle is not None:
        _tick_handle.cancel()
        _tick_handle = None
    if _worker_server is not None:
        _worker_server.shutdown()
        _worker_server.server_close()
        _worker_server = None
    _loop_heartbeat = None
