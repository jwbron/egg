"""
Shared gateway client utilities for functional and integration tests.

This module provides a base mixin class with common gateway API methods
that can be used by both MinimalGateway (functional tests) and EggStack
(integration tests) to reduce code duplication.
"""

import os
import subprocess
import time
from typing import Any

import requests


class GatewayClientMixin:
    """Mixin providing common gateway API client methods.

    Classes using this mixin must have these attributes:
    - gateway_url: str - Base URL of the gateway
    - launcher_secret: str - Secret for launcher authentication
    - source_ip: str - Detected source IP (set by detect_source_ip)
    """

    gateway_url: str
    launcher_secret: str
    source_ip: str

    def health_check(self, timeout: int = 5) -> dict[str, Any]:
        """Query the gateway health endpoint."""
        resp = requests.get(
            f"{self.gateway_url}/api/v1/health",
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def detect_source_ip(self) -> str:
        """Detect the IP the gateway sees for requests from this host.

        Uses the client_ip field from the health endpoint response.
        """
        health = self.health_check()
        ip = health.get("client_ip", "")
        if not ip:
            raise RuntimeError("Gateway health endpoint did not return client_ip")
        self.source_ip = ip
        return ip

    def create_session(
        self,
        container_id: str | None = None,
        container_ip: str | None = None,
        mode: str = "private",
        repos: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a session via the gateway API.

        Returns the full response JSON including session_token.
        """
        if container_id is None:
            container_id = f"test-{os.getpid()}-{time.time_ns()}"
        if container_ip is None:
            container_ip = self.source_ip or "172.40.0.100"

        resp = requests.post(
            f"{self.gateway_url}/api/v1/sessions/create",
            headers={"Authorization": f"Bearer {self.launcher_secret}"},
            json={
                "container_id": container_id,
                "container_ip": container_ip,
                "mode": mode,
                "repos": repos or ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
            timeout=10,
        )
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            return {
                "success": False,
                "message": f"Non-JSON response (HTTP {resp.status_code})",
                "data": {},
            }

    def delete_session(self, session_token: str) -> dict[str, Any]:
        """Delete a session via the gateway API."""
        resp = requests.delete(
            f"{self.gateway_url}/api/v1/sessions/{session_token}",
            headers={"Authorization": f"Bearer {self.launcher_secret}"},
            timeout=10,
        )
        return resp.json()

    def list_sessions(self) -> dict[str, Any]:
        """List active sessions."""
        resp = requests.get(
            f"{self.gateway_url}/api/v1/sessions",
            headers={"Authorization": f"Bearer {self.launcher_secret}"},
            timeout=10,
        )
        return resp.json()

    def heartbeat(self, session_token: str) -> dict[str, Any]:
        """Send a heartbeat for a session."""
        resp = requests.post(
            f"{self.gateway_url}/api/v1/sessions/{session_token}/heartbeat",
            headers={"Authorization": f"Bearer {self.launcher_secret}"},
            timeout=10,
        )
        return resp.json()

    def api_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: int = 10,
    ) -> requests.Response:
        """Make an authenticated API request to the gateway."""
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return requests.request(
            method,
            f"{self.gateway_url}{path}",
            headers=headers,
            json=json_data,
            timeout=timeout,
        )


def docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def wait_for_healthy(url: str, timeout: int = 60) -> bool:
    """Wait for the gateway health endpoint to return 200."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{url}/api/v1/health", timeout=3)
            if resp.status_code == 200:
                return True
        except requests.ConnectionError:
            pass
        time.sleep(1)
    return False
