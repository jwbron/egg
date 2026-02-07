"""
Shared fixtures for functional tests.

Functional tests sit between unit tests and full integration tests:
- They use Docker containers for realistic testing
- But use lighter-weight fixtures than integration_tests/
- Focus on component pairs rather than full system
- Target ~5-10s startup vs ~30s for full stack

These fixtures reuse patterns from integration_tests/conftest.py
but with module-scoped lifecycle for faster iteration.
"""

import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from tests.utils.gateway_client import (
    GatewayClientMixin,
    docker_available,
    wait_for_healthy,
)

# Project root (two levels up from tests/functional/)
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Network configuration for functional tests
# Uses 172.42.x to avoid collision with integration tests (172.40/41)
FUNCTIONAL_SUBNET = "172.42.0.0/24"
GATEWAY_IP = "172.42.0.2"
GATEWAY_PORT = 9848

# Counter for allocating unique container IPs
_next_container_ip_suffix = 100


def _write_minimal_config(config_dir: str, launcher_secret: str) -> None:
    """Generate minimal gateway config for functional tests.

    Creates lightweight config without squid proxy for faster startup.
    """
    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)

    # repositories.yaml -- minimal config
    (config_path / "repositories.yaml").write_text(
        """\
github_username: test-user
bot_username: egg

writable_repos:
  - test-owner/test-repo

repo_settings:
  test-owner/test-repo:
    auth_mode: bot

user_mode:
  github_user: test-user
  git_name: Test User
  git_email: test@example.com

local_repos:
  paths:
    - /home/egg/repos/test-repo
"""
    )

    # secrets.env -- minimal for functional tests (no real API calls)
    (config_path / "secrets.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=dummy-anthropic-token\n"
        "GATEWAY_BOT_NAME=egg\n"
        "GATEWAY_BOT_BRANCH_PREFIX=egg\n"
    )
    os.chmod(config_path / "secrets.env", 0o600)

    # launcher-secret
    (config_path / "launcher-secret").write_text(launcher_secret)
    os.chmod(config_path / "launcher-secret", 0o600)


@dataclass
class MinimalGateway(GatewayClientMixin):
    """Lightweight gateway instance for functional tests.

    Unlike EggStack (session-scoped, full Docker Compose), this is:
    - Module-scoped for faster test isolation
    - Single container (no proxy, no squid)
    - Faster startup (~5-10s vs ~30s)

    Inherits common API methods from GatewayClientMixin to reduce
    duplication with integration_tests/conftest.py:EggStack.
    """

    gateway_url: str
    gateway_ip: str
    gateway_port: int
    launcher_secret: str
    container_id: str
    network_name: str
    config_dir: str
    source_ip: str = ""


@pytest.fixture(scope="module")
def minimal_gateway() -> Generator[MinimalGateway, None, None]:
    """Module-scoped fixture: start a lightweight gateway container.

    Unlike the full egg_stack, this:
    - Starts a single gateway container directly (no docker-compose)
    - Skips squid proxy for faster startup
    - Uses a separate network (172.42.x) to avoid conflicts
    """
    if not docker_available():
        pytest.skip("Docker is not available")

    # Generate unique identifiers
    project_id = f"func-{os.getpid()}-{int(time.time())}"
    network_name = f"egg-func-{project_id}"
    container_id = f"egg-gateway-{project_id}"
    launcher_secret = secrets.token_urlsafe(32)

    # Create temp config directory
    config_dir = tempfile.mkdtemp(prefix="egg-func-config-")
    _write_minimal_config(config_dir, launcher_secret)

    try:
        # Create network
        subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--subnet",
                FUNCTIONAL_SUBNET,
                network_name,
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )

        # Build gateway image if needed
        dockerfile = PROJECT_ROOT / "gateway" / "Dockerfile"
        if dockerfile.exists():
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    "egg-gateway:func-test",
                    "-f",
                    str(dockerfile),
                    str(PROJECT_ROOT),
                ],
                capture_output=True,
                timeout=300,
                check=True,
            )

        # Start gateway container
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_id,
                "--network",
                network_name,
                "--ip",
                GATEWAY_IP,
                "-p",
                f"0:{GATEWAY_PORT}",
                "-e",
                f"EGG_LAUNCHER_SECRET={launcher_secret}",
                "-e",
                "EGG_REPO_CONFIG=/config/repositories.yaml",
                "-e",
                "GITHUB_USER_TOKEN=dummy-github-token",
                "-e",
                "HOST_UID=1000",
                "-e",
                "HOST_GID=1000",
                "-e",
                "EGG_USER_GIT_NAME=test-user",
                "-e",
                "EGG_USER_GIT_EMAIL=test@example.com",
                "-v",
                f"{config_dir}/repositories.yaml:/config/repositories.yaml:ro",
                "-v",
                f"{config_dir}/secrets.env:/secrets/secrets.env:ro",
                "-v",
                f"{config_dir}/launcher-secret:/secrets/launcher-secret:ro",
                "egg-gateway:func-test",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

        # Get mapped port
        port_result = subprocess.run(
            [
                "docker",
                "port",
                container_id,
                str(GATEWAY_PORT),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        host_port = port_result.stdout.strip().split(":")[-1]
        gateway_url = f"http://localhost:{host_port}"

        # Wait for gateway to become healthy
        if not wait_for_healthy(gateway_url, timeout=60):
            # Dump logs for debugging
            logs = subprocess.run(
                ["docker", "logs", container_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pytest.fail(
                f"Gateway did not become healthy within 60s.\n"
                f"Logs:\n{logs.stdout}\n{logs.stderr}"
            )

        gateway = MinimalGateway(
            gateway_url=gateway_url,
            gateway_ip=GATEWAY_IP,
            gateway_port=int(host_port),
            launcher_secret=launcher_secret,
            container_id=container_id,
            network_name=network_name,
            config_dir=config_dir,
        )

        # detect_source_ip() is inside try block to ensure cleanup on failure
        gateway.detect_source_ip()

        yield gateway

    finally:
        # Cleanup
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            timeout=15,
            check=False,
        )
        subprocess.run(
            ["docker", "network", "rm", network_name],
            capture_output=True,
            timeout=15,
            check=False,
        )
        shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture
def functional_session(
    minimal_gateway: MinimalGateway,
) -> Generator[dict[str, Any], None, None]:
    """Function-scoped fixture: create a gateway session for test isolation.

    Creates a unique session per test and cleans it up afterwards.
    """
    container_id = f"func-test-{os.getpid()}-{time.time_ns()}"
    result = minimal_gateway.create_session(
        container_id=container_id,
        mode="private",
    )

    if not result.get("success"):
        pytest.skip(f"Could not create test session: {result.get('message')}")

    session_data = result.get("data", result)

    # Validate session_token is present before proceeding
    token = session_data.get("session_token")
    if not token:
        pytest.fail(
            f"Session created successfully but missing session_token. "
            f"Response data: {session_data}"
        )

    session_data["container_id"] = container_id
    yield session_data

    # Cleanup - token is guaranteed to exist from validation above
    minimal_gateway.delete_session(token)


@dataclass
class GitCommandResult:
    """Result of a git command execution via the gateway."""

    success: bool
    output: str
    error: str
    status_code: int
    raw_response: dict[str, Any]


@pytest.fixture
def git_command_tester(
    minimal_gateway: MinimalGateway,
    functional_session: dict[str, Any],
) -> Callable[..., GitCommandResult]:
    """Factory fixture for testing git command handling.

    Usage:
        def test_git_status(git_command_tester):
            result = git_command_tester("status", args=["--porcelain"])
            assert result.success or result.status_code == 400  # repo may not exist
    """

    def _test(
        operation: str,
        *,
        repo_path: str = "/home/egg/repos/test-repo",
        args: list[str] | None = None,
    ) -> GitCommandResult:
        token = functional_session.get("session_token")
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": repo_path,
                "operation": operation,
                "args": args or [],
                "container_id": functional_session.get("container_id"),
            },
        )

        try:
            body = resp.json()
        except requests.exceptions.JSONDecodeError:
            body = {"success": False, "message": resp.text}

        data = body.get("data", body)
        return GitCommandResult(
            success=body.get("success", False),
            output=data.get("output", data.get("stdout", "")),
            error=data.get("stderr", body.get("message", "")),
            status_code=resp.status_code,
            raw_response=body,
        )

    return _test


@dataclass
class GhCommandResult:
    """Result of a gh command execution via the gateway."""

    success: bool
    output: str
    error: str
    status_code: int
    raw_response: dict[str, Any]


@pytest.fixture
def gh_command_tester(
    minimal_gateway: MinimalGateway,
    functional_session: dict[str, Any],
) -> Callable[..., GhCommandResult]:
    """Factory fixture for testing gh command handling.

    Usage:
        def test_gh_version(gh_command_tester):
            result = gh_command_tester(["--version"])
            assert result.success
            assert "gh version" in result.output
    """

    def _test(
        args: list[str],
        *,
        repo: str | None = None,
    ) -> GhCommandResult:
        token = functional_session.get("session_token")
        json_data: dict[str, Any] = {"args": args}
        if repo:
            json_data["repo"] = repo

        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data=json_data,
        )

        try:
            body = resp.json()
        except requests.exceptions.JSONDecodeError:
            body = {"success": False, "message": resp.text}

        data = body.get("data", body)
        return GhCommandResult(
            success=body.get("success", False),
            output=data.get("output", data.get("stdout", "")),
            error=data.get("stderr", body.get("message", "")),
            status_code=resp.status_code,
            raw_response=body,
        )

    return _test


@pytest.fixture
def session_lifecycle_tester(
    minimal_gateway: MinimalGateway,
) -> Generator[Callable[..., dict[str, Any]], None, None]:
    """Factory fixture for testing session lifecycle operations.

    Usage:
        def test_session_create_delete(session_lifecycle_tester):
            result = session_lifecycle_tester("create")
            assert result["success"]
            token = result["data"]["session_token"]
            result = session_lifecycle_tester("delete", token=token)
            assert result["success"]
    """
    created_tokens: list[str] = []

    def _test(
        operation: str,
        *,
        token: str | None = None,
        container_id: str | None = None,
        mode: str = "private",
    ) -> dict[str, Any]:
        if operation == "create":
            if container_id is None:
                container_id = f"lifecycle-{time.time_ns()}"
            result = minimal_gateway.create_session(
                container_id=container_id,
                mode=mode,
            )
            if result.get("success"):
                t = result.get("data", result).get("session_token")
                if t:
                    created_tokens.append(t)
            return result

        elif operation == "delete":
            if not token:
                return {"success": False, "message": "Token required for delete"}
            result = minimal_gateway.delete_session(token)
            if result.get("success") and token in created_tokens:
                created_tokens.remove(token)
            return result

        elif operation == "heartbeat":
            if not token:
                return {"success": False, "message": "Token required for heartbeat"}
            return minimal_gateway.heartbeat(token)

        elif operation == "list":
            return minimal_gateway.list_sessions()

        else:
            return {"success": False, "message": f"Unknown operation: {operation}"}

    try:
        yield _test
    finally:
        # Cleanup any sessions created during test, even if test raises exception
        for t in created_tokens:
            minimal_gateway.delete_session(t)
