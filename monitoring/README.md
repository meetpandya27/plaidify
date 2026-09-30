# Plaidify Observability & Ops Pack

Deployable monitoring, alerting, and backup automation for a self-hosted
Plaidify deployment. These are operator-facing configs — adjust targets,
thresholds, and credentials for your environment.

## Contents

| File | Purpose |
| ---- | ------- |
| [prometheus.yml](prometheus.yml) | Scrapes the API (`plaidify:8000/metrics`) and the access executor (`access-executor:9101/metrics`); sends alerts to Alertmanager |
| [alert_rules.yml](alert_rules.yml) | Alerting rules (availability, executor liveness, error rate, latency, extraction failures, browser-pool saturation) |
| [tests/alert_rules_test.yml](tests/alert_rules_test.yml) | `promtool` unit tests for every rule (CI runs them) |
| [alertmanager.yml](alertmanager.yml) | Routing, grouping, inhibition, and the receiver placeholder |
| [grafana/dashboards/plaidify-overview.json](grafana/dashboards/plaidify-overview.json) | Grafana dashboard (traffic, latency, errors, browser pool, executor, domain metrics) |
| [grafana/provisioning/](grafana/provisioning/) | Auto-provisions the Prometheus datasource + dashboard |
| [../docker-compose.monitoring.yml](../docker-compose.monitoring.yml) | Runs Prometheus + Alertmanager + Grafana, pre-wired |
| [../deploy/backup-cronjob.yaml](../deploy/backup-cronjob.yaml) | Kubernetes CronJob for scheduled, encrypted DB backups |

## Quick start (Docker Compose)

```bash
# 1. App stack (the API serves /metrics on 8000; the executor on 9101)
docker compose -f docker-compose.production.yml up -d

# 2. Secrets for the monitoring stack (git-ignored)
openssl rand -base64 24 > .secrets/grafana_admin_password
printf '%s' 'https://hooks.example.com/your-endpoint' > .secrets/alertmanager_webhook_url
grep '^METRICS_TOKEN=' .env.production | cut -d= -f2- > .secrets/metrics_token   # the API's METRICS_TOKEN
chmod 644 .secrets/grafana_admin_password .secrets/alertmanager_webhook_url .secrets/metrics_token

# 3. Monitoring stack — attach to the app network (find it with `docker network ls`)
PLAIDIFY_NETWORK=plaidify_default docker compose -f docker-compose.monitoring.yml up -d
```

Every UI listens on `127.0.0.1` only. From another machine use an SSH tunnel
(`ssh -L 3000:127.0.0.1:3000 host`) or an authenticating reverse proxy; never
publish the ports.

- Prometheus: <http://127.0.0.1:9090> (**Status → Targets** shows `plaidify` and `access-executor` UP)
- Alertmanager: <http://127.0.0.1:9093>
- Grafana: <http://127.0.0.1:3000> — user `admin`, password from `.secrets/grafana_admin_password`. The **Plaidify → Overview** dashboard is pre-loaded. There is no default password: Grafana doesn't start without the file.

## Metrics

The API exposes (via `prometheus-fastapi-instrumentator` 8.x defaults + `src/metrics.py`):

- `http_requests_total{method,status,handler}` — request count; `status` is grouped: `2xx`, `3xx`, `4xx`, `5xx`
- `http_request_duration_highr_seconds_bucket{le}` — latency, no labels, buckets 10 ms to 60 s (use for percentiles)
- `http_request_duration_seconds_bucket{method,handler,le}` — latency per handler, buckets 0.1, 0.5, 1 s (use for "slower than 1 s")
- `http_request_size_bytes` / `http_response_size_bytes{handler}` — body sizes
- `plaidify_blueprint_extractions_total{site,status}` — extraction outcomes
- `plaidify_browser_pool_active_contexts` / `plaidify_browser_pool_capacity_contexts` — busy and configured browser contexts, summed over the server's processes
- `plaidify_mfa_challenges_total{mfa_type}` — MFA challenges encountered

The access executor serves its own `/metrics` on `ACCESS_WORKER_METRICS_PORT`
(9101): the extraction, browser-pool and MFA metrics above (in production that
is where the browser work happens), plus
`plaidify_worker_heartbeat_timestamp_seconds`, which its event loop updates
every few seconds. Its `/health` on the same port answers 503 when that loop
has stopped; the compose health check and the Azure probes use it.

Under gunicorn every worker is a separate process. `gunicorn.conf.py` enables
prometheus_client's multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`), so one
scrape of `/metrics` returns the whole container's numbers instead of one
random worker's. Scrape the API on the internal network: nginx doesn't publish
`/metrics`.

When the API runs with `METRICS_TOKEN`, `/metrics` answers 401 without
`Authorization: Bearer <METRICS_TOKEN>`. Uncomment the `authorization` block in
[prometheus.yml](prometheus.yml) and mount the token file into the Prometheus
container (`docker-compose.monitoring.yml` does not mount one yet); otherwise
the `plaidify` target shows as down. The executor's endpoint takes no token.

## Alerts

Defined in [alert_rules.yml](alert_rules.yml), tested by
[tests/alert_rules_test.yml](tests/alert_rules_test.yml):

```bash
promtool check rules monitoring/alert_rules.yml
promtool test rules monitoring/tests/alert_rules_test.yml
```

| Alert | Severity | Fires when |
| ----- | -------- | ---------- |
| `PlaidifyInstanceDown` | critical | an API or executor target can't be scraped for 2m |
| `PlaidifyTargetMissing` / `PlaidifyExecutorMissing` | critical | no API / no executor target at all for 5m |
| `PlaidifyExecutorStalled` | critical | the executor's event loop hasn't run for > 2m |
| `PlaidifyHighErrorRate` | critical | > 5% of responses are 5xx over 5m (health and metrics requests excluded) |
| `PlaidifySlowRequests` | warning | > 5% of fast-path requests take over 1 s for 10m |
| `PlaidifyHighLatencyP95` | warning | p95 over all requests, browser-driven ones included, > 10 s for 15m |
| `PlaidifyExtractionFailureRate` | warning | > 20% extraction failures over 15m |
| `PlaidifyBrowserPoolSaturated` | warning | ≥ 80% of browser contexts busy for 10m |

### Delivering alerts

Prometheus sends alerts to the bundled Alertmanager
([alertmanager.yml](alertmanager.yml)). Its single receiver is a placeholder
webhook whose URL is read from `.secrets/alertmanager_webhook_url`, so it stays
out of git. Point it at your incident tool's generic webhook, or replace the
`webhook_configs` block with `slack_configs`, `pagerduty_configs` or
`email_configs` (keeping secrets in their `*_file` fields). Critical alerts
repeat hourly, warnings every 4 hours, and a target that is down or missing
suppresses the warnings it explains.

Check a change before reloading: `amtool check-config monitoring/alertmanager.yml`.

## Backups

The production compose stack has an opt-in `backup` service and
[../deploy/backup-cronjob.yaml](../deploy/backup-cronjob.yaml) runs the same
image on Kubernetes: hourly `pg_dump` archives encrypted with age, verified
and pruned ([../scripts/backup_db.sh](../scripts/backup_db.sh)). For other
hosts, schedule the script via cron/systemd — see
[../docs/DISASTER_RECOVERY.md](../docs/DISASTER_RECOVERY.md). Always replicate
archives off-host to object storage, and run a restore drill.
