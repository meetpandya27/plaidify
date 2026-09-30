#!/usr/bin/env bash
set -euo pipefail

# Plaidify load test runner
# Usage: ./scripts/run-loadtest.sh [--users 50] [--rate 10] [--time 60s] [--host http://localhost:8000]

USERS="${USERS:-50}"
RATE="${RATE:-10}"
TIME="${TIME:-60s}"
HOST="${HOST:-http://localhost:8000}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --users)  USERS="$2"; shift 2 ;;
        --rate)   RATE="$2"; shift 2 ;;
        --time)   TIME="$2"; shift 2 ;;
        --host)   HOST="$2"; shift 2 ;;
        *)        echo "Unknown flag: $1"; exit 1 ;;
    esac
done

echo "=== Plaidify Load Test ==="
echo "  Host:       $HOST"
echo "  Users:      $USERS"
echo "  Spawn rate: $RATE users/sec"
echo "  Duration:   $TIME"
echo ""

# Check that the server is reachable and answers /health itself (a redirect
# here means every request of the test would be redirected too).
status="$(curl -s -o /dev/null -w '%{http_code}' "$HOST/health" 2>/dev/null || true)"
case "$status" in
    200) ;;
    3??) echo "ERROR: $HOST/health redirects (HTTP $status). Use the https:// URL, or check FORWARDED_ALLOW_IPS behind a proxy."; exit 1 ;;
    *)   echo "ERROR: Server at $HOST is not reachable (HTTP ${status:-none}). Start it first."; exit 1 ;;
esac

exec locust \
    -f tests/load/locustfile.py \
    --host "$HOST" \
    --users "$USERS" \
    --spawn-rate "$RATE" \
    --run-time "$TIME" \
    --headless \
    --only-summary
