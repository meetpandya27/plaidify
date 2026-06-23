"""Tests for OpenTelemetry tracing wiring (safe no-op behavior when disabled)."""

from types import SimpleNamespace


class TestTracing:
    def test_init_tracing_disabled_without_endpoint(self):
        from src.tracing import init_tracing

        fake_settings = SimpleNamespace(otel_endpoint=None, app_name="Plaidify", app_version="0.0", env="test")
        assert init_tracing(object(), fake_settings) is False

    def test_span_context_manager_is_safe(self):
        from src.tracing import span

        with span("test.span", **{"plaidify.test": "yes"}) as sp:
            sp.set_attribute("extra", 1)
        # Reaching here without raising is the assertion.

    def test_tracer_start_span_is_safe(self):
        from src.tracing import tracer

        with tracer.start_as_current_span("noop") as sp:
            sp.set_attribute("k", "v")

    def test_llm_and_engine_import_tracer(self):
        # Spans are wired into these hot paths; importing must not fail.
        from src.core import engine, llm_provider  # noqa: F401
        from src.tracing import span, tracer  # noqa: F401
