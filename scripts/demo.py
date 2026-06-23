#!/usr/bin/env python
"""Plaidify sandbox — one-command end-to-end demo.

This script proves the *whole* Plaidify stack works together. It can either spin
up a fully self-contained sandbox (a demo target portal + the Plaidify API) or
drive an existing Plaidify deployment, then runs the complete journey:

    register → /connect → MFA → poll access job → structured data

Usage::

    # Fully self-contained (starts a demo portal + the API, zero setup):
    python scripts/demo.py

    # No-MFA variant:
    python scripts/demo.py --no-mfa

    # Smoke-test an already-running Plaidify (you supply a reachable demo portal
    # and DEMO_MODE=true on the server):
    python scripts/demo.py --base-url https://plaidify.example.com \
        --username demo_mfa --password demo_pass

The self-contained mode generates throwaway secrets and a temporary SQLite DB,
so it leaves no state behind.
"""

from __future__ import annotations

import argparse
import base64
import multiprocessing
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ensure ``src`` is importable in both this process and any spawned children
# (multiprocessing 'spawn' re-imports this module but does not add the repo
# root to sys.path automatically).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Bundled demo sites. Each maps to a connector blueprint + a target portal that
# ships in src/demo/. They deliberately differ in login flow, MFA type, and DOM
# structure to show one engine handling many site styles.
#   mfa_code = None  → site has no MFA step.
DEMO_SITES: dict[str, dict] = {
    "demo_utility": {
        "label": "GreenGrid Energy (utility · OTP)",
        "app": "src.demo.portal:app",
        "port": 8799,
        "username": "demo_mfa",
        "password": "demo_pass",
        "mfa_code": "123456",
    },
    "demo_bank": {
        "label": "Acme Bank (finance · security question)",
        "app": "src.demo.bank:app",
        "port": 8798,
        "username": "demo@acme.test",
        "password": "demo_pass",
        "mfa_code": "plaidify",
    },
    "demo_saas": {
        "label": "CloudMail (SaaS · no MFA)",
        "app": "src.demo.saas:app",
        "port": 8797,
        "username": "demo_saas",
        "password": "demo_pass",
        "mfa_code": None,
    },
}
DEFAULT_SITE = "demo_utility"


# ── Subprocess targets (must be module-level for spawn start method) ──────────


def _run_portal(app_path: str, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app_path, host=host, port=port, log_level="warning")


def _run_api(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run("src.main:app", host=host, port=port, log_level="warning")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _wait_for_health(url: str, *, label: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.5)
    raise RuntimeError(f"{label} did not become healthy at {url}: {last_err}")


def _log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _step(msg: str) -> None:
    print(f"\n\033[1m▶ {msg}\033[0m", flush=True)


def _spawn(target, *args) -> multiprocessing.Process:
    proc = multiprocessing.Process(target=target, args=args, daemon=True)
    proc.start()
    return proc


# ── Core flow ────────────────────────────────────────────────────────────────


def run_journey(base_url: str, site: str, username: str, password: str, mfa_code: str | None) -> dict:
    """Drive the full connect journey for one demo site against a running Plaidify API."""
    base_url = base_url.rstrip("/")
    client = httpx.Client(base_url=base_url, timeout=60.0)

    _step("Registering a sandbox user")
    suffix = secrets.token_hex(4)
    reg = client.post(
        "/auth/register",
        json={
            "username": f"sandbox_{suffix}",
            "email": f"sandbox_{suffix}@plaidify.dev",
            "password": "SandboxPass123!",
        },
    )
    reg.raise_for_status()
    token = reg.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    _log(f"Authenticated as sandbox_{suffix}")

    _step("Discovering sandbox connectors (GET /blueprints)")
    bps = client.get("/blueprints", headers=auth).json()
    sites = [b["site"] for b in bps.get("blueprints", [])]
    _log(f"Discoverable connectors: {', '.join(sites) or '(none)'}")
    if site not in sites:
        _log(f"⚠ '{site}' is not discoverable — is DEMO_MODE enabled on the server? Attempting the connection anyway.")

    _step(f"Connecting to '{site}' as '{username}' (POST /connect)")
    connect = client.post(
        "/connect",
        headers=auth,
        json={"site": site, "username": username, "password": password},
    )
    connect.raise_for_status()
    payload = connect.json()
    job_id = payload.get("job_id")
    session_id = payload.get("session_id")
    status = payload.get("status")
    _log(f"Status: {status}  job_id={job_id}")

    if status == "mfa_required":
        if not mfa_code:
            _log("⚠ Site issued an MFA challenge but no demo code is configured.")
        _step("Submitting MFA challenge (POST /mfa/submit)")
        mfa = client.post(
            "/mfa/submit",
            headers=auth,
            params={"session_id": session_id, "code": mfa_code or ""},
        )
        mfa.raise_for_status()
        _log(f"MFA: {mfa.json().get('status')}")

    _step("Polling the access job until completion (GET /access_jobs/{job_id})")
    result: dict = {}
    deadline = time.time() + 90
    last_status = None
    while time.time() < deadline:
        job = client.get(f"/access_jobs/{job_id}", headers=auth).json()
        job_status = job.get("status")
        if job_status != last_status:
            _log(f"Job status: {job_status}")
            last_status = job_status
        if job_status in {"completed", "succeeded"}:
            result = job.get("result") or {}
            break
        if job_status in {"failed", "error"}:
            raise RuntimeError(f"Access job failed: {job.get('error_message')}")
        time.sleep(1.5)
    else:
        raise RuntimeError("Timed out waiting for the access job to complete.")

    client.close()
    return result


def _print_result(result: dict) -> None:
    import json

    data = result.get("data") if isinstance(result, dict) else None
    _step("Extracted data")
    if not data:
        print(json.dumps(result, indent=2))
        return

    for key, value in data.items():
        if isinstance(value, list):
            print(f"\n    {key} ({len(value)} rows):")
            for row in value[:3]:
                print(f"      • {row}")
            if len(value) > 3:
                print(f"      … {len(value) - 3} more")
        elif isinstance(value, dict):
            print(f"    {key:18} {json.dumps(value)}")
        else:
            print(f"    {key:18} {value}")


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Plaidify one-command end-to-end sandbox demo.")
    parser.add_argument("--base-url", help="Drive an existing Plaidify API instead of starting one.")
    parser.add_argument(
        "--site",
        choices=sorted(DEMO_SITES),
        default=DEFAULT_SITE,
        help=f"Which bundled demo site to connect (default {DEFAULT_SITE}).",
    )
    parser.add_argument("--all", action="store_true", help="Run the journey for every bundled demo site.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the portals + API and keep them running for manual hosted-Link exploration.",
    )
    parser.add_argument("--username", help="Override the demo username for --site.")
    parser.add_argument("--password", help="Override the demo password for --site.")
    parser.add_argument("--api-port", type=int, default=8000, help="Port for the self-hosted API (default 8000).")
    parser.add_argument(
        "--api-host",
        default="127.0.0.1",
        help="Bind host for the self-hosted API (use 0.0.0.0 in containers).",
    )
    args = parser.parse_args()

    selected_sites = list(DEMO_SITES) if (args.all or args.serve) else [args.site]

    processes: list[multiprocessing.Process] = []
    tmp_db: str | None = None

    try:
        if args.base_url:
            base_url = args.base_url.rstrip("/")
            _step(f"Using existing Plaidify at {base_url}")
            _wait_for_health(f"{base_url}/health", label="Plaidify API")
        else:
            # Spawn a fully self-contained sandbox.
            try:
                multiprocessing.set_start_method("spawn")
            except RuntimeError:
                pass

            os.environ["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
            os.chdir(REPO_ROOT)

            # Throwaway secrets + DB so the demo leaves no state behind.
            os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
            os.environ.setdefault("JWT_SECRET_KEY", secrets.token_hex(32))
            tmp_db = os.path.join(tempfile.mkdtemp(prefix="plaidify-demo-"), "demo.db")
            os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
            os.environ["ENV"] = "development"
            os.environ["DEBUG"] = "true"
            os.environ["DEMO_MODE"] = "true"
            os.environ["CONNECTORS_DIR"] = str(REPO_ROOT / "connectors")
            os.environ["CORS_ORIGINS"] = "*"
            os.environ["RATE_LIMIT_ENABLED"] = "false"
            os.environ.pop("REDIS_URL", None)  # inprocess access jobs

            _step("Starting demo target sites")
            for s in selected_sites:
                cfg = DEMO_SITES[s]
                processes.append(_spawn(_run_portal, cfg["app"], "127.0.0.1", cfg["port"]))
            for s in selected_sites:
                cfg = DEMO_SITES[s]
                _wait_for_health(f"http://127.0.0.1:{cfg['port']}/health", label=f"{s} portal")
                _log(f"{cfg['label']} → http://127.0.0.1:{cfg['port']}")

            _step("Starting the Plaidify API")
            processes.append(_spawn(_run_api, args.api_host, args.api_port))
            base_url = f"http://127.0.0.1:{args.api_port}"
            _wait_for_health(f"{base_url}/health", label="Plaidify API")
            _log(f"Plaidify API ready at {base_url}")

        if args.serve:
            _step("Sandbox is running — explore the hosted Link UI")
            _log(f"Hosted Link UI:  {base_url}/link")
            _log(f"API docs:        {base_url}/docs")
            _log("Discoverable demo sites:")
            for s in selected_sites:
                _log(f"   • {s}: {DEMO_SITES[s]['label']}")
            _log("Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print("\nStopping sandbox…")
            return 0

        for s in selected_sites:
            cfg = DEMO_SITES[s]
            username = args.username if (args.username and s == args.site) else cfg["username"]
            password = args.password if (args.password and s == args.site) else cfg["password"]
            print(f"\n\033[1;36m══ {cfg['label']} ══\033[0m")
            result = run_journey(base_url, s, username, password, cfg["mfa_code"])
            _print_result(result)
            print(f"\033[1;32m✓ {s}: full connect → extract loop complete.\033[0m")

        print(
            "\n\033[1;32m✓ Sandbox journey complete.\033[0m The full stack — auth, connector "
            "execution,\n  headless browser, MFA, and structured extraction — ran together.\n"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n\033[1;31m✗ Demo failed: {exc}\033[0m", file=sys.stderr)
        return 1
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            proc.join(timeout=5)
        if tmp_db and os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
