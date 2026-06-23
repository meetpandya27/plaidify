"""OpenTelemetry tracing setup and a shared tracer.

When ``OTEL_ENDPOINT`` is configured, :func:`init_tracing` wires a
``TracerProvider`` with an OTLP/gRPC exporter and instruments the FastAPI app
(automatic spans per HTTP request). When it isn't configured — or the OTel
packages aren't installed — ``tracer`` is a no-op, so the spans sprinkled
through the engine and LLM paths cost nothing.
"""

from __future__ import annotations

from contextlib import contextmanager

from src.logging_config import get_logger

logger = get_logger("tracing")

_initialized = False

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


def init_tracing(app, settings) -> bool:
    """Initialize OTLP tracing + FastAPI instrumentation. Returns True if enabled."""
    global _initialized
    if _initialized or not _OTEL_AVAILABLE:
        return _initialized
    if not settings.otel_endpoint:
        return False

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry SDK/exporter not installed; tracing disabled")
        return False

    resource = Resource.create(
        {
            "service.name": settings.app_name.lower(),
            "service.version": settings.app_version,
            "deployment.environment": settings.env,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)))
    _otel_trace.set_tracer_provider(provider)

    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover - instrumentation is best-effort
        logger.warning("FastAPI OTel instrumentation failed: %s", exc)

    _initialized = True
    logger.info("OpenTelemetry tracing enabled (endpoint=%s)", settings.otel_endpoint)
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
