"""
Plaidify — Production Gunicorn Configuration

Usage:
    gunicorn src.main:app -c gunicorn.conf.py

This file is the only place worker settings live: the Dockerfile, the compose
files and the Azure template start gunicorn with ``-c gunicorn.conf.py`` and
tune it through the environment variables below, never with CLI flags.
"""

import math
import os
import re
import shutil
import tempfile


def _available_cpus() -> float:
    """CPUs this process may use: the container's CPU quota when there is one.

    ``os.cpu_count()`` reports the host's cores, which inside a container
    limited to half a CPU would still start one worker per host core.
    """
    try:  # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max" and int(period) > 0:
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as q, open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as p:
            quota_us, period_us = int(q.read()), int(p.read())
        if quota_us > 0 and period_us > 0:
            return quota_us / period_us
    except (OSError, ValueError):
        pass
    if hasattr(os, "sched_getaffinity"):
        return float(len(os.sched_getaffinity(0)))
    return float(os.cpu_count() or 1)


def default_workers() -> int:
    """One async worker per available CPU, at least 2 and at most 4.

    Uvicorn workers are asynchronous, so the ``2 × cores + 1`` rule for sync
    workers over-provisions them; and each worker may start its own Chromium
    browser pool, so memory, not CPU, is the real ceiling. Two keeps the API
    answering while one worker restarts. Set GUNICORN_WORKERS to go past four.
    """
    return max(2, min(4, math.ceil(_available_cpus())))


# ── Workers ──────────────────────────────────────────────────────────────────
workers = int(os.getenv("GUNICORN_WORKERS") or os.getenv("WEB_CONCURRENCY") or default_workers())
worker_class = "uvicorn.workers.UvicornWorker"

# Each worker imports the application itself. Preloading it in the master and
# forking would share sockets, threads and gRPC channels (the OTLP exporter,
# Redis and database pools) across processes, which none of them survive.
preload_app = False

# ── Binding ──────────────────────────────────────────────────────────────────
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# ── Proxy trust ──────────────────────────────────────────────────────────────
# Peers allowed to set X-Forwarded-For / X-Forwarded-Proto (uvicorn applies
# them through this setting). Addresses or CIDR networks, comma separated.
# Loopback only by default; a deployment behind a reverse proxy lists the
# proxy's network, otherwise every request looks like plain HTTP from the
# proxy's address: HTTPS enforcement redirects in a loop and every client
# shares one rate-limit bucket.
#   docker-compose.production.yml — nginx on the private compose network
#   infra/main.bicep              — the Container Apps ingress
# Use "*" only when nothing but the proxy can reach the port: with "*" the
# client address comes from the left-most X-Forwarded-For entry, which the
# client itself can write.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1,::1")

# ── Timeouts ─────────────────────────────────────────────────────────────────
# Browser extraction can be slow — allow up to 120s per request
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
# Stay below the orchestrator's stop grace period (compose stop_grace_period,
# Container Apps terminationGracePeriodSeconds) so shutdown can finish.
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# Worker heartbeat files on tmpfs, so a slow container disk can't make the
# master think a healthy worker is stuck.
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

# ── Security ─────────────────────────────────────────────────────────────────
# Limit request sizes
limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190

# ── Process Naming ───────────────────────────────────────────────────────────
proc_name = "plaidify"

# ── Control socket ───────────────────────────────────────────────────────────
# gunicorn 25.1+ opens a management socket (worker count, reload, shutdown) in
# $HOME by default. Nothing here uses gunicornc; the orchestrator's signals
# already do those jobs, and a read-only home directory would break it.
control_socket_disable = True

# ── Metrics (Prometheus multiprocess mode) ───────────────────────────────────
# Every worker keeps its own counters. Without multiprocess mode /metrics
# answers from whichever worker the scrape reaches, so rates jump and gauges
# flap. prometheus_client picks the mode when it is first imported, so the
# directory must be in the environment before a worker imports the app (this
# file is read before any worker starts). /metrics then aggregates all
# workers (prometheus-fastapi-instrumentator reads the same variable).
#
# Set PROMETHEUS_MULTIPROC_DIR to choose the directory; it must belong to this
# one server, and the value files a previous run left there are cleared when
# the server starts. Unset, every start gets a fresh private directory that is
# removed on exit.
_OWNED_DIR_FLAG = "_PLAIDIFY_PROMETHEUS_DIR_OWNED"
_STARTED_FLAG = "_PLAIDIFY_PROMETHEUS_DIR_READY"
# prometheus_client names its files "<type>_<pid>.db"; nothing else is touched.
_VALUE_FILE = re.compile(r"^(counter|gauge_[a-z]+|histogram|summary)_\d+\.db$")

if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="plaidify-prometheus-")
    os.environ[_OWNED_DIR_FLAG] = "1"
prometheus_multiproc_dir = os.environ["PROMETHEUS_MULTIPROC_DIR"]
os.makedirs(prometheus_multiproc_dir, exist_ok=True)

# gunicorn re-reads this file on SIGHUP; only the first read may clear files,
# because workers from the previous generation are still writing to them.
if not os.environ.get(_STARTED_FLAG):
    for _name in os.listdir(prometheus_multiproc_dir):
        if _VALUE_FILE.match(_name):
            os.remove(os.path.join(prometheus_multiproc_dir, _name))
    os.environ[_STARTED_FLAG] = "1"


def child_exit(server, worker):
    """Stop counting a dead worker's live gauges (e.g. active browser contexts)."""
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid, prometheus_multiproc_dir)


def on_exit(server):
    """Remove the metrics directory this configuration created."""
    if os.environ.get(_OWNED_DIR_FLAG) == "1":
        shutil.rmtree(prometheus_multiproc_dir, ignore_errors=True)
