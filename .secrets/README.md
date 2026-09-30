# `.secrets/`

Local secret files read by the compose stacks as Docker secrets. Everything in
this directory except this README is git-ignored and excluded from the Docker
build context, so none of it can be committed or baked into an image.

| File | Used by | Create with |
| ---- | ------- | ----------- |
| `postgres_password` | `docker-compose.yml`, `docker-compose.production.yml` (Postgres and the app) | `openssl rand -base64 32 > .secrets/postgres_password` |
| `redis_password` | both stacks (Redis `--requirepass` and the app) | `openssl rand -base64 32 > .secrets/redis_password` |
| `grafana_admin_password` | `docker-compose.monitoring.yml` | `openssl rand -base64 24 > .secrets/grafana_admin_password` |
| `metrics_token` | `docker-compose.monitoring.yml` (Prometheus' bearer token for the API's `/metrics`) | the `METRICS_TOKEN` value from `.env.production`: `grep '^METRICS_TOKEN=' .env.production \| cut -d= -f2- > .secrets/metrics_token` (generate one for both with `openssl rand -hex 32` if it isn't set) |
| `alertmanager_webhook_url` | `docker-compose.monitoring.yml` (see `monitoring/alertmanager.yml`) | `printf '%s' 'https://hooks.example.com/…' > .secrets/alertmanager_webhook_url` |
| `backup_age_recipients` | `docker-compose.production.yml` (the opt-in `backup` service) | the age public key(s), one per line: `echo 'age1…' > .secrets/backup_age_recipients` ([DISASTER_RECOVERY.md](../docs/DISASTER_RECOVERY.md)) |

Keep the directory private, and the files readable:

```bash
chmod 700 .secrets
chmod 644 .secrets/*
```

The directory is what keeps other users of the host out. The files have to be
world-readable because Compose bind-mounts each one into the containers as it
is — outside Swarm it ignores a secret's `uid`, `gid` and `mode` — and the
API, the executor, the backup job, Prometheus, Alertmanager and Grafana read
them as non-root users. With `600`, a Linux host refuses them those reads and
the containers fail to start (Docker Desktop's file sharing hides this).

The compose files never pass these values as environment variables (so they
don't show in `docker inspect` or `docker compose config`): inside each app
container, `scripts/container-entrypoint.sh` builds `DATABASE_URL` and
`REDIS_URL` from the mounted files when it starts. Rotating a password means
changing it in the service (for Postgres, `ALTER ROLE ... PASSWORD ...`),
updating the file, and recreating the containers that read it.
