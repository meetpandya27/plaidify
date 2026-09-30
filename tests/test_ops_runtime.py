"""Tests for the ops runtime wiring.

Covers: the production rules in ``Settings`` (no SQLite, registration opt-in),
the OTLP exporter's transport security, tracing initialised too late, the
Prometheus multiprocess aggregation that gunicorn workers rely on, the access
executor's /metrics + /health endpoint, and ``gunicorn.conf.py`` itself.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
GUNICORN_CONF = REPO_ROOT / "gunicorn.conf.py"

_BASE_SETTINGS = {
    "encryption_key": "s790nQg9kGoAVQGqXreKUbG8Q0OA-A4HASTbyd-ruuQ=",
    "jwt_secret_key": "test-secret-key-for-testing-only-not-production",
    "_env_file": None,
}


def _settings(monkeypatch, **overrides):
    from src.config import Settings

    for name in ("ENV", "DATABASE_URL", "REGISTRATION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return Settings(**{**_BASE_SETTINGS, **overrides})


# ── Settings: production rules ───────────────────────────────────────────────


class TestProductionSettings:
    def test_production_refuses_the_default_sqlite_url(self, monkeypatch):
        with pytest.raises(ValidationError, match="SQLite is not supported in production"):
            _settings(monkeypatch, env="production")

    def test_production_refuses_sqlite_in_any_case(self, monkeypatch):
        with pytest.raises(ValidationError, match="SQLite is not supported in production"):
            _settings(monkeypatch, env="PRODUCTION", database_url=" SQLite:///data/plaidify.db")

    def test_production_refuses_sqlite_from_the_environment(self, monkeypatch):
        from src.config import Settings

        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///plaidify.db")
        with pytest.raises(ValidationError, match="SQLite is not supported in production"):
            Settings(**_BASE_SETTINGS)

    def test_production_accepts_postgres(self, monkeypatch):
        s = _settings(monkeypatch, env="production", database_url="postgresql://u:p@db:5432/plaidify")
        assert s.env == "production"

    @pytest.mark.parametrize("env", ["development", "staging"])
    def test_sqlite_is_fine_outside_production(self, monkeypatch, env):
        assert _settings(monkeypatch, env=env).database_url.startswith("sqlite")

    def test_registration_defaults_off_in_production(self, monkeypatch):
        s = _settings(monkeypatch, env="production", database_url="postgresql://u:p@db/plaidify")
        assert s.registration_enabled is False

    def test_registration_can_be_enabled_in_production(self, monkeypatch):
        from src.config import Settings

        for name in ("ENV", "DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("REGISTRATION_ENABLED", "true")
        s = Settings(**_BASE_SETTINGS, env="production", database_url="postgresql://u:p@db/plaidify")
        assert s.registration_enabled is True

    def test_registration_stays_on_by_default_in_development(self, monkeypatch):
        assert _settings(monkeypatch, env="development").registration_enabled is True

    def test_worker_metrics_port_default(self, monkeypatch):
        assert _settings(monkeypatch).access_worker_metrics_port == 9101


# ── Tracing ──────────────────────────────────────────────────────────────────


class TestTracingTransport:
    """Which gRPC channel the OTLP exporter opens: TLS unless asked otherwise."""

    @pytest.fixture(autouse=True)
    def channels(self, monkeypatch):
        from opentelemetry.exporter.otlp.proto.grpc import exporter as grpc_exporter

        for name in (
            "OTEL_EXPORTER_OTLP_INSECURE",
            "OTEL_EXPORTER_OTLP_TRACES_INSECURE",
            "OTEL_EXPORTER_OTLP_CERTIFICATE",
            "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        ):
            monkeypatch.delenv(name, raising=False)
        opened = []
        # The exporter builds its gRPC stub on the channel, so hand it a mock.
        monkeypatch.setattr(
            grpc_exporter, "insecure_channel", lambda *a, **k: opened.append("plaintext") or MagicMock()
        )
        monkeypatch.setattr(grpc_exporter, "secure_channel", lambda *a, **k: opened.append("tls") or MagicMock())
        return opened

    def _open(self, endpoint, channels):
        from src.tracing import build_span_exporter

        build_span_exporter(SimpleNamespace(otel_endpoint=endpoint))
        assert len(channels) == 1
        return channels[0]

    @pytest.mark.parametrize(
        ("endpoint", "transport"),
        [
            ("https://collector.example.com:4317", "tls"),
            ("collector.example.com:4317", "tls"),
            ("http://otel-collector:4317", "plaintext"),
        ],
    )
    def test_tls_follows_the_endpoint_scheme(self, channels, endpoint, transport):
        assert self._open(endpoint, channels) == transport

    def test_insecure_env_overrides_a_schemeless_endpoint(self, channels, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "true")
        assert self._open("collector:4317", channels) == "plaintext"

    def test_https_is_never_downgraded(self, channels, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "true")
        assert self._open("https://collector:4317", channels) == "tls"


class TestTracingInitOrder:
    @pytest.fixture
    def tracing(self, monkeypatch):
        import src.tracing as tracing

        monkeypatch.setattr(tracing, "_app_instrumented", False)
        monkeypatch.setattr(tracing, "configure_tracer_provider", lambda settings, service_name=None: True)
        return tracing

    def test_instruments_an_app_that_has_not_started(self, tracing):
        from fastapi import FastAPI
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        app = FastAPI()
        try:
            assert tracing.init_tracing(app, SimpleNamespace()) is True
            assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True
        finally:
            FastAPIInstrumentor.uninstrument_app(app)

    def test_warns_instead_of_silently_losing_http_spans(self, tracing, caplog):
        from fastapi import FastAPI

        app = FastAPI()
        app.middleware_stack = app.build_middleware_stack()  # what the first ASGI message does
        with caplog.at_level("WARNING", logger="plaidify.tracing"):
            assert tracing.init_tracing(app, SimpleNamespace()) is True
        assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
        assert "HTTP requests will not be traced" in caplog.text


# ── Prometheus multiprocess mode ─────────────────────────────────────────────

_WORKER_SNIPPET = textwrap.dedent(
    """
    import sys
    from src import metrics
    metrics.record_extraction("mp_site", "success")
    metrics.set_browser_pool_active(int(sys.argv[1]))
    """
)


def _run_metric_writer(tmp_path, contexts):
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER_SNIPPET, str(contexts)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _sample(registry, name, **labels):
    value = registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


class TestMultiprocessMetrics:
    def test_workers_are_summed_and_dead_workers_leave_the_gauge(self, tmp_path):
        from prometheus_client import CollectorRegistry, multiprocess

        _run_metric_writer(tmp_path, 2)
        _run_metric_writer(tmp_path, 3)

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
        assert _sample(registry, "plaidify_blueprint_extractions_total", site="mp_site", status="success") == 2
        # livesum: one series for the server, not one per pid.
        assert _sample(registry, "plaidify_browser_pool_active_contexts") == 5

        # gunicorn's child_exit hook marks an exited worker dead: its live
        # gauge goes, its counters stay.
        pids = sorted(int(f.stem.rsplit("_", 1)[1]) for f in tmp_path.glob("gauge_livesum_*.db"))
        multiprocess.mark_process_dead(pids[0], str(tmp_path))
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
        assert _sample(registry, "plaidify_browser_pool_active_contexts") in (2, 3)
        assert _sample(registry, "plaidify_blueprint_extractions_total", site="mp_site", status="success") == 2

    def test_pool_capacity_is_reported(self):
        from prometheus_client import REGISTRY

        from src import metrics

        metrics.set_browser_pool_capacity(5)
        assert REGISTRY.get_sample_value("plaidify_browser_pool_capacity_contexts") == 5
        metrics.set_browser_pool_capacity(0)

    def test_metrics_registry_is_multiprocess_aware(self, tmp_path, monkeypatch):
        from prometheus_client import REGISTRY

        from src import metrics

        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert metrics.metrics_registry() is REGISTRY

        _run_metric_writer(tmp_path, 4)
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        registry = metrics.metrics_registry()
        assert registry is not REGISTRY
        assert _sample(registry, "plaidify_browser_pool_active_contexts") == 4


# ── Access executor endpoint ─────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class TestWorkerEndpoint:
    @pytest.fixture(autouse=True)
    def _stop_server(self, monkeypatch):
        from src import metrics

        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        yield
        metrics.stop_worker_metrics_server()

    def test_disabled_with_port_zero(self):
        from src import metrics

        async def main():
            return metrics.start_worker_metrics_server(0)

        assert asyncio.run(main()) is False

    def test_serves_metrics_and_reports_a_stalled_loop(self):
        from src import metrics

        port = _free_port()

        async def main():
            assert metrics.start_worker_metrics_server(port, "127.0.0.1", stall_seconds=30, tick_interval=0.05)
            await asyncio.sleep(0.2)  # let the loop tick

            health = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/health")
            scrape = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/metrics")
            missing = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/nope")

            # A blocked loop stops ticking; the probe must notice.
            metrics._tick_handle.cancel()
            metrics._loop_heartbeat = time.monotonic() - 31
            stalled = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/health")
            return health, scrape, missing, stalled

        health, scrape, missing, stalled = asyncio.run(main())
        assert health == (200, '{"status":"healthy"}')
        assert scrape[0] == 200
        assert "plaidify_worker_heartbeat_timestamp_seconds" in scrape[1]
        assert "plaidify_blueprint_extractions_total" in scrape[1]
        assert missing[0] == 404
        assert stalled == (503, '{"status":"stalled"}')

    def test_bind_failure_is_reported_not_raised(self):
        from src import metrics

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]

            async def main():
                return metrics.start_worker_metrics_server(port, "127.0.0.1")

            assert asyncio.run(main()) is False


# ── gunicorn.conf.py ─────────────────────────────────────────────────────────

_CONF_PROBE = textwrap.dedent(
    """
    import json, os, runpy, sys
    ns = runpy.run_path(sys.argv[1])
    from gunicorn.config import Config
    cfg = Config()
    cfg.set("forwarded_allow_ips", ns["forwarded_allow_ips"])  # gunicorn's own validator
    print(json.dumps({
        "workers": ns["workers"],
        "forwarded_allow_ips": ns["forwarded_allow_ips"],
        "validated": cfg.forwarded_allow_ips,
        "preload_app": ns["preload_app"],
        "control_socket_disable": ns["control_socket_disable"],
        "multiproc_dir": os.environ.get("PROMETHEUS_MULTIPROC_DIR"),
        "owned": os.environ.get("_PLAIDIFY_PROMETHEUS_DIR_OWNED"),
        "has_hooks": callable(ns.get("child_exit")) and callable(ns.get("on_exit")),
    }))
    """
)


def _load_gunicorn_conf(**env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GUNICORN_", "_PLAIDIFY_PROMETHEUS"))}
    for name in ("FORWARDED_ALLOW_IPS", "WEB_CONCURRENCY", "PROMETHEUS_MULTIPROC_DIR"):
        env.pop(name, None)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _CONF_PROBE, str(GUNICORN_CONF)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestGunicornConf:
    def test_defaults(self):
        conf = _load_gunicorn_conf()
        assert 2 <= conf["workers"] <= 4
        assert conf["forwarded_allow_ips"] == "127.0.0.1,::1"
        assert conf["preload_app"] is False
        assert conf["control_socket_disable"] is True
        assert conf["has_hooks"] is True
        # A private metrics directory is created (and owned) when none is set.
        assert conf["owned"] == "1"
        assert os.path.isdir(conf["multiproc_dir"])
        os.rmdir(conf["multiproc_dir"])

    def test_worker_count_comes_from_the_environment(self, tmp_path):
        assert _load_gunicorn_conf(GUNICORN_WORKERS="7", PROMETHEUS_MULTIPROC_DIR=str(tmp_path))["workers"] == 7
        assert _load_gunicorn_conf(WEB_CONCURRENCY="3", PROMETHEUS_MULTIPROC_DIR=str(tmp_path))["workers"] == 3

    def test_proxy_networks_pass_gunicorns_validation(self, tmp_path):
        networks = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,127.0.0.1,::1"
        conf = _load_gunicorn_conf(FORWARDED_ALLOW_IPS=networks, PROMETHEUS_MULTIPROC_DIR=str(tmp_path))
        assert conf["validated"] == networks.split(",")

    def test_a_given_metrics_dir_is_cleared_on_first_start_only(self, tmp_path):
        stale = tmp_path / "counter_4242.db"
        live_gauge = tmp_path / "gauge_livesum_4242.db"
        unrelated = [tmp_path / "keep.txt", tmp_path / "app.db"]

        for path in (stale, live_gauge):
            path.write_bytes(b"x")
        for path in unrelated:
            path.write_text("x")
        conf = _load_gunicorn_conf(PROMETHEUS_MULTIPROC_DIR=str(tmp_path))
        assert conf["multiproc_dir"] == str(tmp_path)
        assert conf["owned"] is None
        assert not stale.exists() and not live_gauge.exists()
        assert all(path.exists() for path in unrelated)

        # A SIGHUP reload re-reads the file in the same master: live workers'
        # files must survive it.
        stale.write_bytes(b"x")
        _load_gunicorn_conf(PROMETHEUS_MULTIPROC_DIR=str(tmp_path), _PLAIDIFY_PROMETHEUS_DIR_READY="1")
        assert stale.exists()
