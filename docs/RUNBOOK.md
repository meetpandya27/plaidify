# Plaidify Operations Runbook

## Table of Contents

1. [Deployment](#deployment)
2. [Key Rotation](#key-rotation)
3. [Incident Response](#incident-response)
4. [Common Issues](#common-issues)
5. [Monitoring & Alerts](#monitoring--alerts)
6. [Backup & Recovery](#backup--recovery)

---

## Deployment

### Pre-deployment Checklist

- [ ] All tests pass: `python -m pytest tests/ -q`
- [ ] Alembic migrations up to date: `alembic upgrade head`
- [ ] Environment variables configured (see `.env.example`)
- [ ] `ENV=production` is set
- [ ] `CORS_ORIGINS` does NOT contain `*`
- [ ] `DATABASE_URL` points to PostgreSQL (not SQLite)
- [ ] `REDIS_URL` is configured
- [ ] `ENCRYPTION_KEY` and `JWT_SECRET_KEY` are set

### Rolling Deployment

```bash
# 1. Run database migrations BEFORE deploying new code
alembic upgrade head

# 2. Deploy new containers (zero-downtime with load balancer)
docker-compose up -d --build --no-deps plaidify

# 3. Verify health
curl -s http://localhost:8000/health | jq .

# 4. Monitor logs for errors
docker-compose logs -f plaidify --tail=100
```

### Rollback

```bash
# 1. Roll back to previous image
docker-compose up -d --no-build plaidify

# 2. If migration needs rollback
alembic downgrade -1

# 3. Verify
curl -s http://localhost:8000/health | jq .
```

---

## Key Rotation

### Master Key Rotation

```bash
# 1. Generate new key
NEW_KEY=$(python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

# 2. Update environment
export ENCRYPTION_KEY_PREVIOUS=$ENCRYPTION_KEY
export ENCRYPTION_KEY=$NEW_KEY
export ENCRYPTION_KEY_VERSION=$((ENCRYPTION_KEY_VERSION + 1))

# 3. Restart application
docker-compose restart plaidify

# 4. Monitor re-encryption progress in logs
docker-compose logs -f plaidify | grep "Re-encrypted"

# 5. After all tokens migrated, remove previous key
unset ENCRYPTION_KEY_PREVIOUS
```

### JWT Secret Rotation

⚠️ **This will invalidate ALL active sessions.** Schedule during maintenance window.

```bash
# 1. Generate new secret
export JWT_SECRET_KEY=$(openssl rand -hex 32)

# 2. Restart — all users must re-authenticate
docker-compose restart plaidify
```

---

## Incident Response

### 1. Rate Limit Errors (429)

**Symptoms**: Users getting HTTP 429 responses.

```bash
# Check current rate limiter state via Redis
redis-cli keys "plaidify:rate_limit:*" | head -20

# Temporarily increase limits (env var)
export RATE_LIMIT_DEFAULT="120/minute"
docker-compose restart plaidify
```

### 2. Browser Pool Exhaustion

**Symptoms**: `/connect` requests timing out, high latency.

```bash
# Check Prometheus metrics
curl -s http://localhost:8000/metrics | grep plaidify_browser_pool

# Increase pool size
export BROWSER_POOL_SIZE=10
docker-compose restart plaidify
```

### 3. Database Connection Pool Exhaustion

**Symptoms**: 500 errors, "connection pool exhausted" in logs.

```bash
# Check pool metrics
curl -s http://localhost:8000/metrics | grep plaidify_db_pool

# Emergency: increase pool
export DB_POOL_SIZE=40
export DB_MAX_OVERFLOW=20
docker-compose restart plaidify

# Investigate: find long-running queries
docker-compose exec postgres psql -U plaidify -c \
  "SELECT pid, now() - pg_stat_activity.query_start AS duration, query
   FROM pg_stat_activity WHERE state = 'active' ORDER BY duration DESC LIMIT 10;"
```

### 4. Audit Log Corruption

**Symptoms**: `GET /audit/verify` returns errors.

```bash
# Verify chain integrity
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/audit/verify | jq .

# If corrupted: identify break point from response
# The entry with the mismatch indicates where tampering or bug occurred
# Document the finding and investigate the cause before taking action
```

### 5. Redis Connection Lost

**Symptoms**: Rate limiting falls back to memory, sessions may not persist across workers.

```bash
# Check Redis health
redis-cli ping

# Check Plaidify health endpoint
curl -s http://localhost:8000/health | jq .checks.redis

# Restart Redis
docker-compose restart redis
```

### 6. Emergency Password Reset (Admin)

```python
# Connect to the database directly
from src.database import SessionLocal, User
from passlib.context import CryptContext

pwd = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12, bcrypt__ident="2b")
db = SessionLocal()
user = db.query(User).filter_by(username="target_user").first()
user.hashed_password = pwd.hash("new-temporary-password")
db.commit()
# Force user to change password on next login (implement in UI)
```

---

## Common Issues

### "ENCRYPTION_KEY must decode to 32 bytes"
The key is malformed. Generate a new one:
```bash
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

### "SQLite is not supported in production"
Set `DATABASE_URL` to a PostgreSQL connection string:
```bash
export DATABASE_URL="postgresql://user:password@host:5432/plaidify"
```

### "CORS wildcard (*) is not allowed in production"
Set explicit origins:
```bash
export CORS_ORIGINS="https://app.example.com,https://dashboard.example.com"
```

### Extraction Timeouts
- Check `BROWSER_NAVIGATION_TIMEOUT` (default 30s)
- Check `GUNICORN_TIMEOUT` (default 120s)
- Monitor Prometheus metrics for slow queries
- Consider increasing `BROWSER_POOL_SIZE`

### Audit Log Growing Too Large
- Check `AUDIT_RETENTION_DAYS` setting (default 730)
- The background cleanup job runs daily
- For immediate cleanup:
  ```sql
  DELETE FROM audit_logs WHERE timestamp < NOW() - INTERVAL '365 days';
  ```

---

## Monitoring & Alerts

### Prometheus Metrics

| Metric | Type | Alert Threshold |
|--------|------|-----------------|
| `plaidify_db_pool_checked_out` | Gauge | > 80% of pool_size |
| `plaidify_db_pool_overflow` | Gauge | > 0 (sustained) |
| `plaidify_browser_pool_active_contexts` | Gauge | > 80% of BROWSER_POOL_SIZE |
| `plaidify_mfa_challenges_total` | Counter | Spike detection |
| `http_request_duration_seconds` | Histogram | p99 > 5s |
| `http_requests_total{status="5xx"}` | Counter | > 1% of traffic |

### Health Check Endpoints

| Endpoint | Auth Required | Purpose |
|----------|--------------|---------|
| `GET /health` | No | Load balancer probe (returns simple status) |
| `GET /health/detailed` | Yes (JWT) | Full component check (DB, Redis, browser pool) |
| `GET /status` | No | Simple API status |

### Log Monitoring

Watch for these log patterns:
- `Slow query detected` — DB performance degradation
- `Refresh failed for` — Background refresh issues
- `Disabled refresh for` — Job hit max failure threshold
- `Redis connection lost` — State store degradation
- `Re-encrypted N token(s)` — Key rotation progress

---

## Backup & Recovery

### Database Backup

```bash
# Automated daily backup
pg_dump -U plaidify -h localhost -Fc plaidify > backup_$(date +%Y%m%d).dump

# Restore
pg_restore -U plaidify -h localhost -d plaidify backup_20260416.dump
```

### Disaster Recovery

1. **Database**: Restore from latest pg_dump or PostgreSQL point-in-time recovery
2. **Encryption Keys**: Must be stored separately in a secure vault (not in the DB)
3. **Redis**: Ephemeral by design — state rebuilds on restart. Scheduled refresh jobs
   are persisted in PostgreSQL and reloaded automatically.
4. **Application**: Stateless — redeploy from container registry

### Recovery Time Objectives

| Component | RTO | RPO |
|-----------|-----|-----|
| API (stateless) | < 5 min | N/A |
| Database | < 30 min | Last backup |
| Redis cache | < 5 min | N/A (ephemeral) |
| Scheduled jobs | Automatic | Persisted in DB |
