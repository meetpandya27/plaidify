# Plaidify Observability & Ops Pack

Deployable monitoring, alerting, and backup automation for a self-hosted
Plaidify deployment. These are operator-facing configs — adjust targets,
thresholds, and credentials for your environment.

## Contents

| File | Purpose |
| ---- | ------- |
| [prometheus.yml](prometheus.yml) | Prometheus scrape + rule config (scrapes `plaidify:8000/metrics`) |
| [alert_rules.yml](alert_rules.yml) | Alerting rules (availability, error rate, latency, extraction failures, browser-pool saturation) |
| [grafana/dashboards/plaidify-overview.json](grafana/dashboards/plaidify-overview.json) | Grafana dashboard (traffic, latency, errors, domain metrics) |
| [grafana/provisioning/](grafana/provisioning/) | Auto-provisions the Prometheus datasource + dashboard |
| [../docker-compose.monitoring.yml](../docker-compose.monitoring.yml) | Runs Prometheus + Grafana, pre-wired |
| [../deploy/backup-cronjob.yaml](../deploy/backup-cronjob.yaml) | Kubernetes CronJob for scheduled DB backups |

## Quick start (Docker Compose)

```bash
# 1. App stack (exposes /metrics on the plaidify service)
docker compose -f docker-compose.production.yml up -d

# 2. Monitoring stack — attach to the app network (find it with `docker network ls`)
PLAIDIFY_NETWORK=plaidify_default GRAFANA_ADMIN_PASSWORD='choose-a-strong-one' \
  docker compose -f docker-compose.monitoring.yml up -d
```

- Prometheus: <http://localhost:9090> (check **Status → Targets** shows `plaidify` UP)
- Grafana: <http://localhost:3000> — the **Plaidify → Overview** dashboard is pre-loaded.

## Metrics

The app exposes (via `prometheus-fastapi-instrumentator` + `src/metrics.py`):

- `http_request_duration_seconds{handler,method,status}` — request latency/throughput (auto-instrumented)
- `plaidify_blueprint_extractions_total{site,status}` — extraction outcomes
- `plaidify_browser_pool_active_contexts` — live browser-context gauge
- `plaidify_mfa_challenges_total{mfa_type}` — MFA challenges encountered

## Alerts

Defined in [alert_rules.yml](alert_rules.yml). Tune `PlaidifyBrowserPoolSaturated`
to your `BROWSER_POOL_SIZE`. To deliver alerts, run Alertmanager and uncomment
the `alerting:` block in [prometheus.yml](prometheus.yml).

| Alert | Severity | Fires when |
| ----- | -------- | ---------- |
| `PlaidifyInstanceDown` / `PlaidifyTargetMissing` | critical | target unreachable / absent |
| `PlaidifyHighErrorRate` | critical | >5% 5xx over 5m |
| `PlaidifyHighLatencyP95` | warning | p95 latency >2s over 10m |
| `PlaidifyExtractionFailureRate` | warning | >20% extraction failures over 15m |
| `PlaidifyBrowserPoolSaturated` | warning | active contexts at threshold for 10m |

## Backups

[../deploy/backup-cronjob.yaml](../deploy/backup-cronjob.yaml) runs hourly
`pg_dump` archives to a PVC (mirrors [../scripts/backup_db.sh](../scripts/backup_db.sh)).
For non-Kubernetes hosts, schedule the script via cron/systemd — see
[../docs/DISASTER_RECOVERY.md](../docs/DISASTER_RECOVERY.md). Always replicate
archives off-host to object storage.
