"""OpenTelemetry tracing setup and a shared tracer.

When ``OTEL_ENDPOINT`` is configured, :func:`init_tracing` wires a
``TracerProvider`` with an OTLP/gRPC exporter and instruments the FastAPI app
(automatic spans per HTTP request). When it isn't configured — or the OTel
packages aren't installed — ``tracer`` is a no-op, so the spans sprinkled
through the engine and LLM paths cost nothing.

Transport security follows the standard OTel conventions rather than a
hard-coded choice: an ``https://`` endpoint (or one without a scheme) uses TLS,
verified against the system roots or ``OTEL_EXPORTER_OTLP_CERTIFICATE``
(mTLS via ``OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE`` / ``..._CLIENT_KEY``);
an ``http://`` endpoint is plaintext; ``OTEL_EXPORTER_OTLP_INSECURE`` (or the
``_TRACES_`` variant) overrides the scheme. Collector auth headers come from
``OTEL_EXPORTER_OTLP_HEADERS``.

Call :func:`init_tracing` while the module that creates the app is being
imported, before the server sends the app its first (lifespan) message: the
instrumentation hooks into the middleware stack, which Starlette builds on
that first message. Under gunicorn every worker imports the app itself
(``preload_app`` is off), so each worker gets its own exporter.
Processes without an ASGI app, like the access-job executor, call
:func:`configure_tracer_provider` directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from src.logging_config import get_logger

logger = get_logger("tracing")

_initialized = False
_app_instrumented = False

try:
    from opentelemetry import trace as _otel_trace

    tracer = _otel_trace.get_tracer("plaidify")
    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover - OTel API is normally installed
    _otel_trace = None
    _OTEL_AVAILABLE = False

    class _NoopSpan:
        def set_attribute(self, *_a, **_k):
            return None

        def record_exception(self, *_a, **_k):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _NoopTracer:
        def start_as_current_span(self, *_a, **_k):
            return _NoopSpan()

    tracer = _NoopTracer()


def build_span_exporter(settings):
    """The OTLP/gRPC span exporter for ``settings.otel_endpoint``.

    No ``insecure=`` argument on purpose: the exporter derives TLS from the
    endpoint scheme and the OTEL_EXPORTER_OTLP_* environment (see the module
    docstring). Constructing it does not connect.
    """
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=settings.otel_endpoint)


def configure_tracer_provider(settings, service_name: Optional[str] = None) -> bool:
    """Install the global TracerProvider exporting over OTLP/gRPC.

    Returns True when tracing is (already) enabled. Idempotent per process.
    """
    global _initialized
    if _initialized or not _OTEL_AVAILABLE:
        return _initialized
    if not settings.otel_endpoint:
        return False

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter = build_span_exporter(settings)
    except ImportError:
        logger.warning("OpenTelemetry SDK/exporter not installed; tracing disabled")
        return False

    resource = Resource.create(
        {
            "service.name": service_name or settings.app_name.lower(),
            "service.version": settings.app_version,
            "deployment.environment": settings.env,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _otel_trace.set_tracer_provider(provider)

    _initialized = True
    logger.info(
        "OpenTelemetry tracing enabled (endpoint=%s, tls=%s)",
        settings.otel_endpoint,
        "off" if getattr(exporter, "_insecure", False) else "on",
    )
    return True


def init_tracing(app, settings) -> bool:
    """Initialize OTLP tracing + FastAPI instrumentation. Returns True if enabled.

    Must run before the app handles its first message (see the module
    docstring); a call after that still exports manual spans but cannot trace
    HTTP requests, and says so in the log.
    """
    global _app_instrumented
    if not configure_tracer_provider(settings):
        return False
    if _app_instrumented:
        return True

    if getattr(app, "middleware_stack", None) is not None:
        logger.warning(
            "init_tracing() ran after the app started serving; HTTP requests will not be traced. "
            "Call it at import time, right after the FastAPI app is created."
        )
        return True

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        _app_instrumented = True
    except Exception as exc:  # pragma: no cover - instrumentation is best-effort
        logger.warning("FastAPI OTel instrumentation failed: %s", exc)
    return True


@contextmanager
def span(name: str, **attributes):
    """Convenience context manager: start a span and set string/number attributes."""
    with tracer.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            try:
                sp.set_attribute(key, value)
            except Exception:  # pragma: no cover
                pass
        yield sp
