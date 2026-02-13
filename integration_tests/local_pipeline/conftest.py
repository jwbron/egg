"""Fixtures for local SDLC pipeline integration tests.

Provides:
- LocalPipelineStack dataclass with gateway/orchestrator URLs
- local_pipeline_stack (session-scoped): builds mock sandbox, starts compose,
  waits for health, tears down
- orchestrator_url / gateway_url (session-scoped): extracted from compose ports
- wait_for_pipeline_terminal(): polls pipeline status until complete/failed/cancelled
"""

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from egg_config import GATEWAY_PORT
from egg_config.constants import ORCHESTRATOR_PORT

from tests.utils.gateway_client import docker_available, wait_for_healthy

# Project root (two levels up from integration_tests/local_pipeline/)
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Compose file for this test suite
COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"

# Mock sandbox build context
MOCK_SANDBOX_DIR = Path(__file__).parent / "mock-sandbox"


def _write_test_config(config_dir: str, launcher_secret: str) -> None:
    """Generate minimal gateway config files for testing."""
    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)

    (config_path / "repositories.yaml").write_text(
        """\
github_username: test-user
bot_username: james-in-a-box

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

    anthropic_token = os.environ.get("ANTHROPIC_OAUTH_TOKEN", "dummy-anthropic-token")
    (config_path / "secrets.env").write_text(
        f"CLAUDE_CODE_OAUTH_TOKEN={anthropic_token}\n"
        "GATEWAY_BOT_NAME=james-in-a-box\n"
        "GATEWAY_BOT_BRANCH_PREFIX=james-in-a-box\n"
    )
    os.chmod(config_path / "secrets.env", 0o600)

    (config_path / "launcher-secret").write_text(launcher_secret)
    os.chmod(config_path / "launcher-secret", 0o600)


@dataclass
class LocalPipelineStack:
    """Running local pipeline test stack state."""

    gateway_url: str
    orchestrator_url: str
    launcher_secret: str
    compose_project: str
    config_dir: str
    repos_dir: str


# Re-export wait_for_pipeline_terminal from helpers for backwards compatibility
# with any tests that import it from conftest
from .helpers import wait_for_pipeline_terminal  # noqa: E402, F401


def _cleanup_orphaned_containers() -> None:
    """Remove any leftover egg-sandbox containers from previous test runs."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=egg-sandbox-egg-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    for name in result.stdout.strip().splitlines():
        if name:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=10,
                check=False,
            )


@pytest.fixture(scope="session")
def local_pipeline_stack() -> Generator[LocalPipelineStack, None, None]:
    """Session-scoped fixture: build mock sandbox, start gateway+orchestrator.

    Builds the mock-sandbox image, starts the compose stack, waits for both
    gateway and orchestrator to become healthy, yields the stack info,
    then tears everything down.
    """
    if not docker_available():
        pytest.skip("Docker is not available")

    if not COMPOSE_FILE.exists():
        pytest.skip("local_pipeline docker-compose.yml not found")

    project_name = f"egg-lp-test-{os.getpid()}"
    launcher_secret = secrets.token_urlsafe(32)

    config_dir = tempfile.mkdtemp(prefix="egg-lp-test-config-")
    repos_dir = tempfile.mkdtemp(prefix="egg-lp-test-repos-")
    _write_test_config(config_dir, launcher_secret)

    # Initialize a bare git repo so the orchestrator's state store works
    subprocess.run(
        ["git", "init", repos_dir],
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "-C", repos_dir, "config", "user.name", "test"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "-C", repos_dir, "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    # Add a fake origin remote so gateway push endpoint can resolve repo names
    subprocess.run(
        [
            "git",
            "-C",
            repos_dir,
            "remote",
            "add",
            "origin",
            "https://github.com/test-owner/test-repo.git",
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    # Create initial commit so git operations work
    Path(repos_dir, ".gitkeep").touch()
    subprocess.run(
        ["git", "-C", repos_dir, "add", "."],
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "-C", repos_dir, "commit", "-m", "init", "--no-verify"],
        capture_output=True,
        check=True,
        timeout=10,
    )

    # Generate docker-compose.override.yml with per-repo volume mounts
    repo_name = "test-repo"
    override_file = Path(config_dir) / "docker-compose.override.yml"
    override_file.write_text(
        f"# Auto-generated for testing\n"
        f"services:\n"
        f"  gateway:\n"
        f"    volumes:\n"
        f"      - {repos_dir}:/home/egg/repos/{repo_name}\n"
        f"  orchestrator:\n"
        f"    volumes:\n"
        f"      - {repos_dir}:/home/egg/repos/{repo_name}\n"
    )

    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "EGG_LAUNCHER_SECRET": launcher_secret,
        "EGG_CONFIG_DIR": config_dir,
        "EGG_HOST_REPO_MAP": json.dumps({repo_name: repos_dir}),
        "HOST_UID": str(os.getuid()),
        "HOST_GID": str(os.getgid()),
        "GATEWAY_PORT": "0",
        "PROXY_PORT": "0",
        "ORCHESTRATOR_PORT": "0",
    }

    compose_cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(override_file),
        "-p",
        project_name,
    ]

    try:
        # Clean up any orphaned sandbox containers from previous test runs
        # that might cause name conflicts
        _cleanup_orphaned_containers()

        # Build mock-sandbox image first
        subprocess.run(
            [
                "docker",
                "build",
                "-t",
                "mock-sandbox:latest",
                str(MOCK_SANDBOX_DIR),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )

        # Build and start the compose stack
        result = subprocess.run(
            [*compose_cmd, "up", "-d", "--build"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                f"docker compose up failed (exit {result.returncode}).\n"
                f"STDOUT:\n{result.stdout[-3000:]}\n"
                f"STDERR:\n{result.stderr[-3000:]}"
            )

        # Get mapped gateway port
        port_result = subprocess.run(
            [*compose_cmd, "port", "gateway", str(GATEWAY_PORT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        gateway_host_port = port_result.stdout.strip().split(":")[-1]
        gateway_url = f"http://localhost:{gateway_host_port}"

        # Get mapped orchestrator port
        port_result = subprocess.run(
            [*compose_cmd, "port", "orchestrator", str(ORCHESTRATOR_PORT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        orchestrator_host_port = port_result.stdout.strip().split(":")[-1]
        orchestrator_url = f"http://localhost:{orchestrator_host_port}"

        # Wait for gateway to become healthy
        if not wait_for_healthy(gateway_url, timeout=120):
            logs = subprocess.run(
                [*compose_cmd, "logs", "gateway"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pytest.fail(
                f"Gateway did not become healthy within 120s.\nLogs:\n{logs.stdout}\n{logs.stderr}"
            )

        # Wait for orchestrator to become healthy
        if not wait_for_healthy(orchestrator_url, timeout=120):
            logs = subprocess.run(
                [*compose_cmd, "logs", "orchestrator"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pytest.fail(
                f"Orchestrator did not become healthy within 120s.\n"
                f"Logs:\n{logs.stdout}\n{logs.stderr}"
            )

        stack = LocalPipelineStack(
            gateway_url=gateway_url,
            orchestrator_url=orchestrator_url,
            launcher_secret=launcher_secret,
            compose_project=project_name,
            config_dir=config_dir,
            repos_dir=repos_dir,
        )

        yield stack

    finally:
        # Dump logs for debugging if tests fail
        subprocess.run(
            [*compose_cmd, "logs", "--tail", "100"],
            env=env,
            capture_output=False,
            timeout=30,
            check=False,
        )

        # Tear down
        subprocess.run(
            [*compose_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )

        shutil.rmtree(config_dir, ignore_errors=True)
        shutil.rmtree(repos_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def orchestrator_url(local_pipeline_stack: LocalPipelineStack) -> str:
    """Orchestrator base URL."""
    return local_pipeline_stack.orchestrator_url


@pytest.fixture(scope="session")
def gateway_url(local_pipeline_stack: LocalPipelineStack) -> str:
    """Gateway base URL."""
    return local_pipeline_stack.gateway_url


@pytest.fixture(scope="session")
def launcher_secret(local_pipeline_stack: LocalPipelineStack) -> str:
    """Launcher secret for gateway auth."""
    return local_pipeline_stack.launcher_secret
