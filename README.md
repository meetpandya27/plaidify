# Plaidify

Plaidify is a production-oriented service for authenticated web access. It exposes API, hosted link, and agent-facing interfaces for connecting to sites, collecting user-authorized data, and returning structured results under a strict read-only posture.

## Capabilities

- Direct API-based connect flows for supported connectors
- Hosted link flows for browser and native clients
- Signed hosted link bootstrap flow for production-safe client launches
- Detached access jobs with polling and status endpoints
- MFA continuation, scoped access control, and audit-aware workflows
- Credential encryption in transit and at rest

## Start

### Docker

```bash
cp .env.example .env
# Set ENCRYPTION_KEY and JWT_SECRET_KEY before starting

docker compose up --build
```

### Local

```bash
cp .env.example .env
# Set ENCRYPTION_KEY and JWT_SECRET_KEY before starting

alembic upgrade head
uvicorn src.main:app --reload
```

## Production Guidance

- Use PostgreSQL and Redis in production.
- Prefer `POST /link/bootstrap` plus `POST /link/sessions/bootstrap` for hosted client launches.
- Keep `CORS_ORIGINS` explicit and enable HTTPS enforcement.
- Treat internal fixture connectors as test-only and non-public.

## Key Endpoints

- `POST /connect`
- `POST /link/sessions`
- `POST /link/bootstrap`
- `POST /link/sessions/bootstrap`
- `POST /mfa/submit`
- `GET /access_jobs`
- `GET /blueprints`

## Documentation

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/MOBILE_LINK_INTEGRATION.md](docs/MOBILE_LINK_INTEGRATION.md)
- [docs/AGENTS.md](docs/AGENTS.md)
- [docs/RUNBOOK.md](docs/RUNBOOK.md)
- [docs/PRODUCT_PLAN.md](docs/PRODUCT_PLAN.md)

## Validation

```bash
python -m pytest tests/test_agent_integration.py -q
cd sdk-js && npm run typecheck && npm test
```

## License

MIT. See [LICENSE](LICENSE).
