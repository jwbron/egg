"""Fixtures for local SDLC pipeline integration tests.

Provides:
- LocalPipelineStack dataclass with gateway/orchestrator URLs
- local_pipeline_stack (session-scoped): runs against a Kubernetes (k3s)
  cluster.  Expects gateway and orchestrator to already be deployed in
  the egg-system namespace.
- orchestrator_url / gateway_url / launcher_secret (session-scoped):
  shortcuts derived from local_pipeline_stack
- wait_for_pipeline_terminal(): polls pipeline status until
  complete/failed/cancelled (re-exported from .helpers for backwards
  compatibility with tests that imported it from conftest).

Issue #2474 retired the docker-compose runtime; the only supported
backend is k3s.  Tests skip with a clear message when ``kubectl`` is
unavailable.
"""

import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.gateway_client import wait_for_healthy

# Project root (two levels up from integration_tests/local_pipeline/)
PROJECT_ROOT = Path(__file__).parent.parent.parent


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
    checks:
      - name: lint
        command: "echo 'lint ok'"
      - name: test
        command: "echo 'test ok'"

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


def _kubectl_available() -> bool:
    """Check if kubectl is available and can connect to a cluster."""
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False


def _k8s_local_pipeline_stack() -> Generator[LocalPipelineStack]:
    """Create a LocalPipelineStack backed by a Kubernetes deployment.

    Expects gateway and orchestrator to already be deployed in the egg-system
    namespace. Creates a test namespace for agent pods and cleans it up.
    """
    test_namespace = f"egg-lp-test-{os.getpid()}"

    subprocess.run(
        ["kubectl", "create", "namespace", test_namespace],
        capture_output=True,
        timeout=30,
        check=True,
    )

    # Read launcher-secret from the deployed gateway-secrets Secret so
    # the test's bearer matches what the live gateway pod was started
    # with. The previous code used a random/env-only token, which
    # produced cluster-wide "Invalid launcher authorization token" 401s
    # against every endpoint that the test then tried to hit.
    secret_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "egg-system",
            "get",
            "secret",
            "gateway-secrets",
            "-o",
            "jsonpath={.data.launcher-secret}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if secret_result.returncode == 0 and secret_result.stdout:
        import base64

        launcher_secret = base64.b64decode(secret_result.stdout).decode()
    else:
        launcher_secret = os.environ.get("EGG_LAUNCHER_SECRET", secrets.token_urlsafe(32))
    config_dir = tempfile.mkdtemp(prefix="egg-lp-test-config-")
    repos_dir = tempfile.mkdtemp(prefix="egg-lp-test-repos-")
    _write_test_config(config_dir, launcher_secret)

    # Initialize test repo
    subprocess.run(["git", "init", repos_dir], capture_output=True, check=True, timeout=10)
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
    Path(repos_dir, ".gitkeep").touch()
    subprocess.run(
        ["git", "-C", repos_dir, "add", "."], capture_output=True, check=True, timeout=10
    )
    subprocess.run(
        ["git", "-C", repos_dir, "commit", "-m", "init", "--no-verify"],
        capture_output=True,
        check=True,
        timeout=10,
    )

    # Discover gateway and orchestrator URLs from k8s services
    gw_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "egg-system",
            "get",
            "svc",
            "gateway",
            "-o",
            "jsonpath={.spec.clusterIP}:{.spec.ports[0].port}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    gw_addr = gw_result.stdout.strip()
    gateway_url = f"http://{gw_addr}"

    orch_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "egg-system",
            "get",
            "svc",
            "orchestrator",
            "-o",
            "jsonpath={.spec.clusterIP}:{.spec.ports[0].port}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    orch_addr = orch_result.stdout.strip()
    orchestrator_url = f"http://{orch_addr}"

    if not wait_for_healthy(gateway_url, timeout=120):
        pytest.fail("Gateway in k8s did not become healthy within 120s")
    if not wait_for_healthy(orchestrator_url, timeout=120):
        pytest.fail("Orchestrator in k8s did not become healthy within 120s")

    stack = LocalPipelineStack(
        gateway_url=gateway_url,
        orchestrator_url=orchestrator_url,
        launcher_secret=launcher_secret,
        compose_project=f"k8s-{test_namespace}",
        config_dir=config_dir,
        repos_dir=repos_dir,
    )

    try:
        yield stack
    finally:
        subprocess.run(
            ["kubectl", "delete", "namespace", test_namespace, "--ignore-not-found=true"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        shutil.rmtree(config_dir, ignore_errors=True)
        shutil.rmtree(repos_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def local_pipeline_stack() -> Generator[LocalPipelineStack]:
    """Session-scoped fixture: gateway+orchestrator running in Kubernetes (k3s).

    Skips with a clear message if ``kubectl`` is not available — see
    ``docs/guides/testing.md`` for the k3s-on-host setup recipe.
    """
    if not _kubectl_available():
        pytest.skip(
            "kubectl is not available or not connected to a cluster — "
            "local pipeline integration tests require k3s "
            "(see docs/guides/testing.md)"
        )

    yield from _k8s_local_pipeline_stack()


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
