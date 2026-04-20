# Plaidify

Plaidify is a production-oriented authenticated web access service. It gives you API, hosted-link, and agent-facing interfaces for connecting to user-authorized sites, navigating MFA, and returning structured results under a strict read-only posture.

## What Plaidify Provides

| Surface | Use it when | Primary endpoints |
| --- | --- | --- |
| Direct API | Your backend owns the connect flow and polling lifecycle | `POST /connect`, `POST /mfa/submit`, `GET /access_jobs` |
| Hosted Link | You need a Plaid-style launch flow in web or mobile clients | `POST /link/sessions`, `POST /link/bootstrap`, `POST /link/sessions/bootstrap` |
| Agent and MCP access | An internal tool or AI agent needs constrained access to a site workflow | `GET /blueprints`, agent-facing routes, MCP server |

## Core Capabilities

- Hosted link flows for browser, iframe, and native mobile webview clients
- Signed bootstrap tokens for production-safe hosted-link launches
- Detached access jobs with polling, persisted results, and MFA continuation
- Scoped access control, audit-aware workflows, and read-only execution
- Credential encryption in transit and at rest
- Python and TypeScript SDKs for server and client integrations

## How It Works

1. Create a hosted-link session or call `POST /connect` directly.
2. Plaidify executes the connector or blueprint flow and captures any MFA state.
3. Clients continue MFA through the hosted link or `POST /mfa/submit`.
4. Final results are returned immediately for fast completions or retrieved later through `GET /access_jobs`.

## Architecture At A Glance

```text
Client or agent
	-> Plaidify API or hosted /link flow
	-> access-job orchestration and MFA state
	-> connector runtime or blueprint execution
	-> structured result payload
```

Operationally, Plaidify is a FastAPI service with modular routers, Redis-backed shared state for multi-worker coordination, a detached access-job execution path for production deployments, and hosted-link assets that can be embedded in parent apps or native webviews.

## Quick Start

### Docker Compose

```bash
cp .env.example .env
# Set ENCRYPTION_KEY and JWT_SECRET_KEY before starting

docker compose up --build
curl http://localhost:8000/health
```

### Local Development

```bash
cp .env.example .env
# Set ENCRYPTION_KEY and JWT_SECRET_KEY before starting

alembic upgrade head
uvicorn src.main:app --reload
```

## Production Notes

- Use PostgreSQL and Redis in production.
- Prefer `POST /link/bootstrap` plus `POST /link/sessions/bootstrap` for hosted client launches.
- Keep `CORS_ORIGINS` explicit and enable HTTPS enforcement.
- Run detached access jobs with the Redis-worker execution mode for restart-tolerant production behavior.
- Treat fixture connectors as local test assets, not public production integrations.

## SDKs

- [sdk/README.md](sdk/README.md) for the Python SDK
- [sdk-js/README.md](sdk-js/README.md) for the TypeScript and browser SDK

## Key Docs

- [docs/README.md](docs/README.md) for the technical architecture and configuration reference
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for production deployment and operations guidance
- [docs/MOBILE_LINK_INTEGRATION.md](docs/MOBILE_LINK_INTEGRATION.md) for native mobile hosted-link integration
- [docs/AGENTS.md](docs/AGENTS.md) for agent-facing usage
- [docs/RUNBOOK.md](docs/RUNBOOK.md) for operational procedures
- [docs/PRODUCT_PLAN.md](docs/PRODUCT_PLAN.md) for roadmap context

## Validation

```bash
python -m pytest tests/test_agent_integration.py -q
cd sdk-js && npm run typecheck && npm test
```

## License

MIT. See [LICENSE](LICENSE).
