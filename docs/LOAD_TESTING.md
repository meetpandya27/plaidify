# Load Testing & Capacity Planning

Plaidify ships a [Locust](https://locust.io) harness so you can validate
throughput, latency, and autoscale behaviour before going live and before
trusting the HA replica counts in [HIGH_AVAILABILITY.md](HIGH_AVAILABILITY.md).

## Running a test

```bash
pip install --require-hashes -r requirements-dev.lock   # includes locust
./scripts/run-loadtest.sh --users 50 --rate 10 --time 60s --host http://localhost:8000
```

The scenario ([tests/load/locustfile.py](../tests/load/locustfile.py)) models a
mix per virtual user: registers + logs in on start, then weights
`GET /health` (5), `GET /blueprints` (3), `GET /links` (2), `GET /auth/me` (1),
and `POST /connect` to the bundled `demo_saas` portal (1). That last task drives
a real headless browser, so the target must run in `DEMO_MODE` with the demo
portals up (`python scripts/demo.py --serve` does both); it never touches a
real site. `/connect` is limited to 10 requests a minute per client, so raise
`RATE_LIMIT_CONNECT` on the test instance for a sustained run. Run load tests
only against a disposable instance. A `HealthOnlyUser` is available for pure
probe load.

Registration is off by default when `ENV=production`, so for a production-mode
test instance set `REGISTRATION_ENABLED=true` and
`REGISTRATION_EMAIL_VERIFICATION=false` on it (never on a real deployment), or
the virtual users can't sign up: with verification on, an account exists only
once its emailed link is followed. Rate limits apply per client
address: raise `RATE_LIMIT_*` on the test instance, or the single load
generator's address is throttled like one very busy user. Behind a proxy, set
`FORWARDED_ALLOW_IPS` first (see [DEPLOYMENT.md](DEPLOYMENT.md)) or every user
shares the proxy's bucket.

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

The Prometheus alerts in
[../monitoring/alert_rules.yml](../monitoring/alert_rules.yml) are deliberately
looser than these targets — they page on breakage, not on SLO drift:
`PlaidifySlowRequests` fires when more than 5% of fast-path requests take over
1 s (p95 above 1 s) for 10 minutes, `PlaidifyHighErrorRate` above 5% 5xx for
5 minutes, and `PlaidifyHighLatencyP95` when the p95 across all requests,
browser-driven ones included, exceeds 10 s for 15 minutes. Track the targets
above on the Grafana dashboard, or add recording rules and alerts of your own.

## Capacity-planning workflow

1. **Baseline** — ramp one replica until p95 breaches the SLO; record the RPS at
   that point as the per-replica ceiling.
2. **Scale-out** — add replicas and confirm throughput scales roughly linearly
   and latency holds (validates statelessness + no shared bottleneck).
3. **Find the real bottleneck** — watch the Grafana dashboard
   ([../monitoring/](../monitoring/)): if `plaidify_browser_pool_active_contexts`
   reaches `plaidify_browser_pool_capacity_contexts` or DB CPU climbs first,
   scale executors / the database, not just the app tier. Check the database
   [connection budget](DEPLOYMENT.md#database-connection-budget) before adding
   replicas or workers.
4. **Set autoscale** — set Container Apps `minReplicas`/`maxReplicas` and the
   scale rule (CPU or concurrent requests) with headroom above the measured
   per-replica ceiling.
5. **Re-test after changes** — load is a regression surface; re-run before major
   releases.

## CI smoke option

For a fast guardrail, run a short low-concurrency profile against a disposable
instance and check the per-endpoint failures in the summary (expect the
`/connect` task to fail — rate-limited, or the site unreachable — and nothing
else):

```bash
USERS=10 RATE=5 TIME=30s ./scripts/run-loadtest.sh --host http://127.0.0.1:8000
```
