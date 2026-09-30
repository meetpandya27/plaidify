# ═══════════════════════════════════════════════════════════════════════════════
# Plaidify — multi-stage image
#
# One image, three roles, chosen by the command:
#   API (default)          gunicorn src.main:app -c gunicorn.conf.py
#   access-job executor    python -m src.access_job_worker
#   migrations             alembic upgrade head
# plus a `demo` target (docker build --target demo) for the sandbox.
#
# Build arguments:
#   INSTALL_KMS=true   also install the cloud KMS SDKs (requirements-kms.lock)
#                      for KMS_PROVIDER=aws|azure|vault
# ═══════════════════════════════════════════════════════════════════════════════

# Debian release pinned so the browser library names below stay valid.
ARG PYTHON_IMAGE=python:3.11-slim-trixie
ARG NODE_IMAGE=node:24-slim

# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.lock requirements-kms.lock ./

ARG INSTALL_KMS=false
# --require-hashes: every package, transitive ones included, must be pinned
# with a matching hash in the lock, or the build stops.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes -r requirements.lock \
    && if [ "$INSTALL_KMS" = "true" ]; then \
         /opt/venv/bin/pip install --require-hashes -r requirements-kms.lock; \
       fi

# ── Stage 2: hosted-link frontend ────────────────────────────────────────────
FROM ${NODE_IMAGE} AS frontend-builder

WORKDIR /frontend
COPY frontend-next/package.json frontend-next/package-lock.json ./
# npm ci installs exactly the lockfile, the same tree CI tests.
RUN npm ci --no-audit --no-fund
COPY frontend-next/ ./
RUN npm run build

# ── Stage 3: runtime ─────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# System libraries for Chromium's headless shell — Playwright's own list for
# Debian 13 (`playwright install-deps --dry-run chromium`), without the X
# server and font collection `--with-deps` adds — plus tini, which runs as
# PID 1 to forward signals and reap the browser's exited child processes
# (Azure Container Apps has no init option of its own).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        fonts-liberation \
        libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
        libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
        libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Security: run as an unprivileged user (also required for Chromium's sandbox).
RUN groupadd --system plaidify \
    && useradd --system --gid plaidify --create-home --home-dir /home/plaidify plaidify

COPY --from=builder /opt/venv /opt/venv

# Only the headless shell: no full Chrome build, no xvfb. Installed as root
# into a path the app user can read but not modify.
RUN playwright install --only-shell chromium

WORKDIR /app

# Application code stays owned by root and read-only for the app user.
COPY --chmod=755 scripts/container-entrypoint.sh /usr/local/bin/plaidify-entrypoint
COPY --chmod=755 scripts/healthcheck.py /usr/local/bin/plaidify-healthcheck
COPY gunicorn.conf.py alembic.ini ./
COPY scripts/provision_app_db_role.py ./scripts/provision_app_db_role.py
COPY alembic/ ./alembic/
COPY connectors/ ./connectors/
COPY src/ ./src/
COPY --from=frontend-builder /frontend/dist ./frontend-next/dist
RUN python -m compileall -q /app/src /app/alembic /app/connectors /app/scripts

USER plaidify

EXPOSE 8000

# Graceful shutdown: tini passes SIGTERM to gunicorn, which drains workers and
# closes the browser pool.
STOPSIGNAL SIGTERM

# The API's probe. The executor and the migration job override it (see the
# compose files); a redirect fails it on purpose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["plaidify-healthcheck"]

ENTRYPOINT ["tini", "--", "plaidify-entrypoint"]

# Worker count, timeouts, proxy trust and metrics live in gunicorn.conf.py.
CMD ["gunicorn", "src.main:app", "-c", "gunicorn.conf.py"]

# ── Target: sandbox demo ─────────────────────────────────────────────────────
# docker compose -f docker-compose.demo.yml up --build
FROM runtime AS demo

COPY scripts/demo.py ./scripts/demo.py
CMD ["python", "scripts/demo.py", "--serve", "--api-host", "0.0.0.0", "--api-port", "8000"]

# The last stage is the default build target.
FROM runtime AS production
