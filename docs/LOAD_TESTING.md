# Load Testing & Capacity Planning

Plaidify ships a [Locust](https://locust.io) harness so you can validate
throughput, latency, and autoscale behaviour before going live and before
trusting the HA replica counts in [HIGH_AVAILABILITY.md](HIGH_AVAILABILITY.md).

## Running a test

```bash
pip install -r requirements-dev.txt   # includes locust
./scripts/run-loadtest.sh --users 50 --rate 10 --time 60s --host http://localhost:8000
```

The scenario ([tests/load/locustfile.py](../tests/load/locustfile.py)) models a
realistic mix per virtual user: registers + logs in on start, then weights
`GET /health` (5), `GET /blueprints` (3), `GET /links` (2), `GET /auth/me` (1),
and a mock connect (1). A `HealthOnlyUser` is available for pure probe load.

For an interactive run with the web UI, drop `--headless`:

```bash
locust -f tests/load/locustfile.py --host http://localhost:8000
# open http://localhost:8089
```

## Suggested SLOs

Treat these as starting targets for the read-heavy API surface; tune to your
hardware and traffic. The browser-driven extraction path is intentionally
excluded — it is bound by the target sites and runs asynchronously.

| Metric | Target |
| ------ | ------ |
| p50 latency (read endpoints) | < 100 ms |
| p95 latency (read endpoints) | < 500 ms |
| p99 latency (read endpoints) | < 1.5 s |
| Error rate (5xx) | < 0.1% |
| Throughput per app replica | establish a baseline, then scale linearly |

These align with the Prometheus alert thresholds in
[../monitoring/alert_rules.yml](../monitoring/alert_rules.yml)
(`PlaidifyHighLatencyP95`, `PlaidifyHighErrorRate`).

## Capacity-planning workflow

1. **Baseline** — ramp one replica until p95 breaches the SLO; record the RPS at
   that point as the per-replica ceiling.
2. **Scale-out** — add replicas and confirm throughput scales roughly linearly
   and latency holds (validates statelessness + no shared bottleneck).
3. **Find the real bottleneck** — watch the Grafana dashboard
   ([../monitoring/](../monitoring/)): if `plaidify_browser_pool_active_contexts`
   saturates or DB CPU climbs first, scale executors / the database, not just
   the app tier.
4. **Set autoscale** — set Container Apps `minReplicas`/`maxReplicas` and the
   scale rule (CPU or concurrent requests) with headroom above the measured
   per-replica ceiling.
5. **Re-test after changes** — load is a regression surface; re-run before major
   releases.

## CI smoke option

For a fast guardrail, run a short low-concurrency profile against a disposable
instance and assert the summary has zero failures:

```bash
USERS=10 RATE=5 TIME=30s ./scripts/run-loadtest.sh --host http://127.0.0.1:8000
```
