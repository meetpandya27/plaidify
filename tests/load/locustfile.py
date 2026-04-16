"""
Plaidify load tests using Locust.

Run:
    locust -f tests/load/locustfile.py --host http://localhost:8000
    # Or use the helper script: ./scripts/run-loadtest.sh

Web UI: http://localhost:8089
"""

import json
import random
import string

from locust import HttpUser, between, task


def random_string(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class PlaidifyUser(HttpUser):
    """Simulates a typical Plaidify API consumer."""

    wait_time = between(1, 3)

    def on_start(self):
        """Register and login to get a JWT token."""
        self.username = f"loadtest_{random_string()}@test.com"
        self.password = "LoadTest!Pass123"

        # Register
        resp = self.client.post(
            "/auth/register",
            json={
                "username": self.username,
                "password": self.password,
            },
        )
        if resp.status_code not in (200, 201):
            # May already exist in repeated runs
            pass

        # Login
        resp = self.client.post(
            "/auth/token",
            data={
                "username": self.username,
                "password": self.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code == 200:
            token_data = resp.json()
            self.token = token_data.get("access_token", "")
            self.auth_headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = ""
            self.auth_headers = {}

    @task(5)
    def health_check(self):
        """High-frequency health check — lightweight baseline."""
        self.client.get("/health")

    @task(3)
    def list_connectors(self):
        """List available site connectors."""
        self.client.get("/connectors", headers=self.auth_headers)

    @task(2)
    def get_links(self):
        """List user's linked accounts."""
        self.client.get("/links", headers=self.auth_headers)

    @task(1)
    def get_profile(self):
        """Fetch user profile."""
        self.client.get("/auth/me", headers=self.auth_headers)

    @task(1)
    def connect_mock(self):
        """Attempt a connection to the mock_site connector."""
        self.client.post(
            "/connect",
            json={
                "site": "mock_site",
                "username": "demo_user",
                "password": "demo_pass",
            },
            headers=self.auth_headers,
        )


class HealthOnlyUser(HttpUser):
    """Lightweight user that only hits the health endpoint — for baseline RPS testing."""

    wait_time = between(0.5, 1)

    @task
    def health(self):
        self.client.get("/health")
