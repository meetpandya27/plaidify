# Plaidify — Production Deployment Guide

## Prerequisites

| Component   | Minimum Version | Purpose                            |
|-------------|----------------|------------------------------------|
| Python      | 3.11+          | Runtime                            |
| PostgreSQL  | 16+            | Primary database                   |
| Redis       | 7+             | Shared state, rate limiting, RSA keys |
| Playwright  | Latest         | Browser automation engine          |
| Docker      | 24+            | Container deployment (recommended) |

---

## Quick Start (Docker Compose)

```bash
# 1. Clone & enter
git clone <repo-url> && cd plaidify

# 2. Create .env from template
cp .env.example .env
# Edit .env — set ENCRYPTION_KEY, JWT_SECRET_KEY, etc.

# 3. Launch stack
docker compose up -d

# 4. Run database migrations
docker compose exec plaidify alembic upgrade head

# 5. Verify
curl http://localhost:8000/health
```

---

## Environment Variables Reference

### Required (no defaults — app will fail without these)

| Variable          | Description                                         | Generate with                                                                 |
|-------------------|-----------------------------------------------------|-------------------------------------------------------------------------------|
| `ENCRYPTION_KEY`  | Base64url-encoded 256-bit key for AES-256-GCM       | `python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `JWT_SECRET_KEY`  | Secret for signing JWT tokens                       | `openssl rand -hex 32`                                                        |

### Database

| Variable            | Default                   | Description                                           |
|---------------------|---------------------------|-------------------------------------------------------|
| `DATABASE_URL`      | `sqlite:///plaidify.db`   | SQLAlchemy URL. **Use PostgreSQL in production.**      |
| `DB_POOL_SIZE`      | `20`                      | Connection pool size (ignored for SQLite)              |
| `DB_MAX_OVERFLOW`   | `10`                      | Extra connections beyond pool_size                     |
| `DB_POOL_RECYCLE`   | `3600`                    | Seconds before a connection is recycled                |

### Redis

| Variable    | Default | Description                                              |
|-------------|---------|----------------------------------------------------------|
| `REDIS_URL` | `None`  | Redis URL for shared RSA keys & rate limiting. Example: `redis://localhost:6379/0` |

### Auth & Tokens

| Variable                          | Default   | Description                       |
|-----------------------------------|-----------|-----------------------------------|
| `JWT_ALGORITHM`                   | `HS256`   | JWT signing algorithm             |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15`      | Access token TTL (minutes)        |
| `JWT_REFRESH_TOKEN_EXPIRE_MINUTES`| `10080`   | Refresh token TTL (7 days)        |

### Server

| Variable         | Default        | Description                                           |
|------------------|----------------|-------------------------------------------------------|
| `APP_NAME`       | `Plaidify`     | Application name                                      |
| `APP_VERSION`    | `0.3.0a1`      | Reported version                                      |
| `ENV`            | `development`  | `development`, `staging`, or `production`              |
| `DEBUG`          | `false`        | Enable debug mode                                     |
| `LOG_LEVEL`      | `INFO`         | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`       |
| `LOG_FORMAT`     | `json`         | `json` (structured) or `text`                         |
| `CORS_ORIGINS`   | `localhost:*`  | Comma-separated allowed origins. Be explicit in prod. |
| `ENFORCE_HTTPS`  | `false`        | Redirect HTTP→HTTPS + HSTS. Auto-enabled in prod.     |

### Rate Limiting

| Variable              | Default      | Description                     |
|-----------------------|--------------|---------------------------------|
| `RATE_LIMIT_ENABLED`  | `true`       | Enable/disable rate limiting    |
| `RATE_LIMIT_AUTH`     | `5/minute`   | Auth endpoints limit            |
| `RATE_LIMIT_CONNECT`  | `10/minute`  | `/connect` endpoint limit       |
| `RATE_LIMIT_DEFAULT`  | `60/minute`  | Default limit for all endpoints |

### Browser Engine

| Variable                    | Default | Description                                     |
|-----------------------------|---------|-------------------------------------------------|
| `BROWSER_HEADLESS`          | `true`  | Run Playwright in headless mode                  |
| `BROWSER_POOL_SIZE`         | `5`     | Max concurrent browser contexts                  |
| `BROWSER_IDLE_TIMEOUT`      | `300`   | Seconds before idle context is closed            |
| `BROWSER_NAVIGATION_TIMEOUT`| `30000` | Navigation timeout (ms)                          |
| `BROWSER_ACTION_TIMEOUT`    | `10000` | Action timeout (click, fill) (ms)                |
| `BROWSER_BLOCK_RESOURCES`   | `true`  | Block images/fonts/analytics for speed           |
| `BROWSER_STEALTH`           | `true`  | Enable anti-detection measures                   |

### LLM Extraction

| Variable            | Default   | Description                                         |
|---------------------|-----------|-----------------------------------------------------|
| `LLM_PROVIDER`      | `openai`  | `openai` or `anthropic`                              |
| `LLM_API_KEY`       | `None`    | API key for the LLM provider                         |
| `LLM_MODEL`         | `None`    | Model override (e.g. `gpt-4o`)                       |
| `LLM_BASE_URL`      | `None`    | Custom base URL (Azure OpenAI, local servers)        |
| `LLM_MAX_TOKENS`    | `4096`    | Max completion tokens                                |
| `LLM_TEMPERATURE`   | `0.0`     | Temperature (0.0 = deterministic)                    |
| `LLM_TIMEOUT`       | `60.0`    | HTTP timeout for LLM calls (seconds)                 |
| `LLM_TOKEN_BUDGET`  | `30000`   | Max input tokens sent to LLM                         |
| `LLM_FALLBACK_MODEL`| `None`    | Fallback model if primary fails                      |

### Encryption Key Rotation

| Variable                   | Default | Description                                   |
|----------------------------|---------|-----------------------------------------------|
| `ENCRYPTION_KEY_VERSION`   | `1`     | Current key version. Increment on rotation.   |
| `ENCRYPTION_KEY_PREVIOUS`  | `None`  | Previous key (base64url) for decrypting old data during rotation. |

---

## Production Checklist

### Security

- [ ] Set strong `ENCRYPTION_KEY` and `JWT_SECRET_KEY` (never reuse dev values)
- [ ] Set `ENV=production` and `ENFORCE_HTTPS=true`
- [ ] Configure `CORS_ORIGINS` to your exact frontend domain(s)
- [ ] Place behind a reverse proxy (nginx, Caddy, ALB) that terminates TLS
- [ ] Restrict database and Redis access to the application network only
- [ ] Set `DEBUG=false`

### Database

- [ ] Use PostgreSQL (not SQLite) with the `DATABASE_URL` env var
- [ ] Run all migrations: `alembic upgrade head`
- [ ] Schedule regular backups (pg_dump or managed service snapshots)
- [ ] Tune `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` based on expected concurrency

### Redis

- [ ] Set `REDIS_URL` for shared state across workers
- [ ] Enable Redis persistence (RDB or AOF) if you need durability
- [ ] Set a Redis password in production (`redis://:password@host:6379/0`)

### Process Management

The included `gunicorn.conf.py` configures:

- **Workers**: `(2 × CPU cores) + 1` uvicorn workers
- **Timeouts**: 120s request, 30s graceful shutdown
- **Preload**: Enabled for shared memory and faster restarts

```bash
# Run directly
gunicorn src.main:app -c gunicorn.conf.py

# Or via Docker Compose
docker compose up -d
```

### Monitoring

- **Prometheus metrics** are exposed at `GET /metrics`
- **Health check** at `GET /health` returns:
  - `200` — all systems healthy
  - `503` — one or more checks failed (database, browser pool, or Redis)
- **Structured logs** in JSON format (default) for log aggregation (ELK, Datadog, etc.)

### Scaling

- Increase `BROWSER_POOL_SIZE` for higher concurrent scraping throughput
- Scale horizontally by running multiple Plaidify containers behind a load balancer
- Redis is **required** for multi-worker/multi-container deployments (shared RSA keys + rate limits)

---

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Check current migration state
alembic current

# Create a new migration after model changes
alembic revision --autogenerate -m "description"
```

---

## TLS Configuration

Plaidify does not terminate TLS itself. Use a reverse proxy:

**Caddy (auto-TLS):**
```
plaidify.example.com {
    reverse_proxy localhost:8000
}
```

**nginx:**
```nginx
server {
    listen 443 ssl;
    server_name plaidify.example.com;
    ssl_certificate /etc/ssl/cert.pem;
    ssl_certificate_key /etc/ssl/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With TLS termination in place, set `ENFORCE_HTTPS=true` so Plaidify adds HSTS headers and redirects HTTP → HTTPS.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `500` on startup | Missing `ENCRYPTION_KEY` or `JWT_SECRET_KEY` | Set required env vars |
| `/health` returns `503` | Database or Redis unreachable | Check connection URLs and network |
| `429 Too Many Requests` | Rate limit exceeded | Adjust `RATE_LIMIT_*` env vars or wait |
| Browser timeouts | Slow target sites | Increase `BROWSER_NAVIGATION_TIMEOUT` |
| High memory usage | Too many browser contexts | Lower `BROWSER_POOL_SIZE` |
