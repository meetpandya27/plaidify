#!/usr/bin/env python3
"""
Plaidify Demo Runner

Starts both servers with a single command:
  1. GreenGrid Energy portal (localhost:8080)  — the fake utility site
  2. Plaidify API (localhost:8000)              — the extraction engine + demo UI

Usage:
  python run_demo.py

Then open http://localhost:8000/ui/demo.html in your browser.

Press Ctrl+C to stop both servers.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time

# ── Config ────────────────────────────────────────────────────────────────────

PLAIDIFY_PORT = 8000
EXAMPLE_PORT = 8080
ROOT = os.path.dirname(os.path.abspath(__file__))


def banner():
    print()
    print("  ◆ Plaidify Demo")
    print("  ─────────────────────────────────────────────")
    print(f"  Demo UI:       http://localhost:{PLAIDIFY_PORT}/ui/demo.html")
    print(f"  API Docs:      http://localhost:{PLAIDIFY_PORT}/docs")
    print(f"  Utility Portal: http://localhost:{EXAMPLE_PORT}")
    print("  ─────────────────────────────────────────────")
    print("  Press Ctrl+C to stop\n")


def ensure_env():
    """Set required env vars if not already set."""
    os.environ.setdefault("ENCRYPTION_KEY", "demo-key-not-for-production-use!!")
    os.environ.setdefault("JWT_SECRET_KEY", "demo-jwt-secret-not-for-production")
    os.environ.setdefault("DATABASE_URL", "sqlite:///./plaidify_demo.db")
    os.environ.setdefault("DEBUG", "true")
    os.environ.setdefault("LOG_LEVEL", "INFO")
    os.environ.setdefault("CORS_ORIGINS", "*")
    os.environ.setdefault("BROWSER_HEADLESS", "true")


def main():
    ensure_env()

    procs = []

    try:
        # Start GreenGrid Energy portal
        print("  Starting GreenGrid Energy portal...")
        example_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "example_site.server:app",
             "--host", "0.0.0.0", "--port", str(EXAMPLE_PORT),
             "--log-level", "warning"],
            cwd=ROOT,
            env=os.environ.copy(),
        )
        procs.append(example_proc)

        # Give it a moment to start
        time.sleep(1)

        # Start Plaidify API
        print("  Starting Plaidify API...")
        api_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "src.main:app",
             "--host", "0.0.0.0", "--port", str(PLAIDIFY_PORT),
             "--log-level", "info"],
            cwd=ROOT,
            env=os.environ.copy(),
        )
        procs.append(api_proc)

        time.sleep(1)
        banner()

        # Wait for either to exit
        while True:
            for p in procs:
                ret = p.poll()
                if ret is not None:
                    print(f"\n  Process exited with code {ret}. Shutting down...")
                    raise KeyboardInterrupt
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
        print("  Done.\n")


if __name__ == "__main__":
    main()
