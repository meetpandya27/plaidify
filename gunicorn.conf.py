"""
Plaidify — Production Gunicorn Configuration

Usage:
    gunicorn src.main:app -c gunicorn.conf.py
"""

import multiprocessing
import os

# ── Workers ──────────────────────────────────────────────────────────────────
# Formula: 2 * CPU cores + 1 (standard recommendation for I/O-bound apps)
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"

# ── Binding ──────────────────────────────────────────────────────────────────
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# ── Timeouts ─────────────────────────────────────────────────────────────────
# Browser extraction can be slow — allow up to 120s per request
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = 5
graceful_timeout = 30

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

# ── Preload ──────────────────────────────────────────────────────────────────
# Preload app for faster worker startup and shared memory
preload_app = True
