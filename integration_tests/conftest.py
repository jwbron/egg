"""Shared fixtures for integration tests.

Provides:
- EggStack dataclass with gateway URL, IPs, launcher secret, and helpers
- egg_stack (session-scoped): starts/stops the gateway via Kubernetes (k3s)
- gateway_session (function-scoped): creates/destroys a gateway session per test
- isolated_container / external_container / test_container: alpine helper
  fixtures for tests that still need ad-hoc docker containers attached to a
  network (legacy — slated for replacement by k3s-native equivalents)

Issue #2474 retired the docker-compose stack runtime; the only supported
test runtime is k3s.

The ``isolated_container`` / ``external_container`` / ``test_container``
fixtures shell out to ``docker run --network <name>`` and only worked
under the old docker-compose stack.  Under k3s ``egg_stack.isolated_network``
is a Kubernetes namespace, not a docker network, so those fixtures
``pytest.skip`` with a clear message — ``test_credential_security.py::
TestCredentialIsolation`` (the main consumer) is currently uncovered in
CI and needs a k3s-native replacement before it runs again.
"""

import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT

from tests.utils.gateway_client import (
    GatewayClientMixin,
    wait_for_healthy,
)

# Public surface re-exported by other test modules.  GATEWAY_PORT is
# re-exported from shared/egg_config/constants.py for tests that
# historically imported it from this module (e.g.
# integration_tests/test_network_security.py).
__all__ = ["EggStack", "ContainerInfo", "GATEWAY_PORT", "exec_in_container"]

# Project root (one level up from integration_tests/)
PROJECT_ROOT = Path(__file__).parent.parent

# Test network configuration -- uses 172.40.x/172.41.x to avoid collision
# with production (172.32/172.33) or other CI runs.
ISOLATED_SUBNET = "172.40.0.0/24"
EXTERNAL_SUBNET = "172.41.0.0/24"
GATEWAY_ISOLATED_IP = "172.40.0.2"
GATEWAY_EXTERNAL_IP = "172.41.0.2"
# Use constants from shared module for port configuration
PROXY_PORT = GATEWAY_PROXY_PORT


@dataclass
class ContainerInfo:
    """Information about a running test container."""

    container_id: str
    network: str
    ip: str


@dataclass
class EggStack(GatewayClientMixin):
    """Running integration test stack state.

    Inherits common API methods from GatewayClientMixin to reduce
    duplication in test helper code.
    """

    gateway_url: str
    orchestrator_url: str
    gateway_isolated_ip: str
    gateway_external_ip: str
    gateway_port: int
    proxy_port: int
    launcher_secret: str
    # Lifecycle bearer for the orchestrator's /api/v1/deployment/* and
    # other ``@require_lifecycle_secret`` routes. Sourced from the same
    # gateway-secrets Secret the orchestrator pod mounts; empty when the
    # cluster has no lifecycle-secret key, so deployment-route tests can
    # skip rather than fail closed.
    lifecycle_secret: str
    # Under k3s this carries the ``k8s-<namespace>`` sentinel — legacy
    # docker-only fixtures key off the prefix to skip cleanly.  Some tests
    # (e.g. test_stack_lifecycle, test_worktree_integration) still consume
    # it directly to build docker container names; those silently fail to
    # find a container under k3s and are tracked for k3s-native rewrites.
    compose_project: str
    config_dir: str
    # Both networks point at the same k8s namespace today; the duplicated
    # field is retained so legacy fixtures keep their isolated/external
    # split until the k3s-native replacements land.
    isolated_network: str
    external_network: str
    source_ip: str = ""  # Auto-detected: IP the gateway sees for our requests
    _containers: list[str] = field(default_factory=list)


def _write_test_config(config_dir: str, launcher_secret: str) -> None:
    """Generate minimal gateway config files for testing.

    Creates:
    - repositories.yaml: minimal repo config
    - secrets.env: dummy Anthropic credentials
    - launcher-secret: shared authentication secret
    """
    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)

    # repositories.yaml -- minimal config for gateway startup
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

    # secrets.env -- gateway needs real Anthropic credentials for E2E tests
    # so it can inject them into proxied API requests from the sandbox.
    # Non-E2E tests use dummy credentials (they don't call the real API).
    anthropic_token = os.environ.get("ANTHROPIC_OAUTH_TOKEN", "dummy-anthropic-token")
    (config_path / "secrets.env").write_text(
        f"CLAUDE_CODE_OAUTH_TOKEN={anthropic_token}\n"
        "GATEWAY_BOT_NAME=james-in-a-box\n"
        "GATEWAY_BOT_BRANCH_PREFIX=james-in-a-box\n"
    )
    os.chmod(config_path / "secrets.env", 0o600)

    # launcher-secret
    (config_path / "launcher-secret").write_text(launcher_secret)
    os.chmod(config_path / "launcher-secret", 0o600)


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


def _k8s_egg_stack() -> Generator[EggStack]:
    """Create an EggStack backed by a Kubernetes deployment.

    Expects the gateway to already be deployed in the egg-system namespace
    (via ``kubectl apply -k k8s/overlays/local/``).  Creates a test-specific
    namespace for agent pods and cleans it up after the session.
    """
    test_namespace = f"egg-test-agents-{os.getpid()}"

    # Create test namespace for agent pods
    subprocess.run(
        ["kubectl", "create", "namespace", test_namespace],
        capture_output=True,
        timeout=30,
        check=True,
    )
    # Label the namespace so NetworkPolicies can select it
    subprocess.run(
        [
            "kubectl",
            "label",
            "namespace",
            test_namespace,
            "app.kubernetes.io/part-of=egg",
            "egg/test-namespace=true",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )

    # Discover gateway URL from the cluster
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
    gateway_addr = gw_result.stdout.strip()
    if ":" not in gateway_addr:
        pytest.fail(f"Could not discover gateway service address: {gateway_addr}")

    gateway_ip, gateway_port_str = gateway_addr.rsplit(":", 1)
    gateway_url = f"http://{gateway_ip}:{gateway_port_str}"

    # Discover orchestrator URL from the same namespace. Consumed by
    # `test_k8s_deployment_tools.py` for the lifecycle-auth regression
    # suite (the only remaining caller).
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
    if ":" not in orch_addr:
        pytest.fail(f"Could not discover orchestrator service address: {orch_addr}")
    orchestrator_url = f"http://{orch_addr}"

    # Read launcher secret from the k8s secret
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
    import base64

    if secret_result.returncode == 0 and secret_result.stdout:
        # `.strip()` because `kubectl create secret --from-file=<dir>`
        # keeps every byte of the source file, including any trailing
        # newline left by the upstream secret-generation step. A `\n`
        # inside `f"Bearer {launcher_secret}"` is rejected by
        # `http.client.putheader` as "Invalid header value".
        launcher_secret = base64.b64decode(secret_result.stdout).decode().strip()
    else:
        launcher_secret = os.environ.get("EGG_LAUNCHER_SECRET", secrets.token_urlsafe(32))

    # Pull the lifecycle bearer from the same Secret so tests targeting
    # ``@require_lifecycle_secret`` routes (e.g. /api/v1/deployment/*)
    # can authenticate. Optional: if the cluster doesn't expose this
    # key the bearer is left empty and callers should skip cleanly.
    lifecycle_result = subprocess.run(
        [
            "kubectl",
            "-n",
            "egg-system",
            "get",
            "secret",
            "gateway-secrets",
            "-o",
            "jsonpath={.data.lifecycle-secret}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if lifecycle_result.returncode == 0 and lifecycle_result.stdout:
        lifecycle_secret = base64.b64decode(lifecycle_result.stdout).decode().strip()
    else:
        lifecycle_secret = ""

    config_dir = tempfile.mkdtemp(prefix="egg-test-config-")
    _write_test_config(config_dir, launcher_secret)

    if not wait_for_healthy(gateway_url, timeout=120):
        pytest.fail("Gateway in k8s did not become healthy within 120s")

    stack = EggStack(
        gateway_url=gateway_url,
        orchestrator_url=orchestrator_url,
        gateway_isolated_ip=gateway_ip,
        gateway_external_ip=gateway_ip,
        gateway_port=int(gateway_port_str),
        proxy_port=PROXY_PORT,
        launcher_secret=launcher_secret,
        lifecycle_secret=lifecycle_secret,
        compose_project=f"k8s-{test_namespace}",
        config_dir=config_dir,
        isolated_network=test_namespace,
        external_network=test_namespace,
    )
    stack.detect_source_ip()

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


@pytest.fixture(scope="session")
def egg_stack() -> Generator[EggStack]:
    """Session-scoped fixture: start the gateway stack.

    Backed exclusively by Kubernetes (k3s).  Skips with a clear message
    if ``kubectl`` is unavailable — see ``docs/guides/testing.md`` for
    the k3s-on-host setup recipe.
    """
    if not _kubectl_available():
        pytest.skip(
            "kubectl is not available or not connected to a cluster — "
            "integration tests require k3s (see docs/guides/testing.md)"
        )

    yield from _k8s_egg_stack()


@pytest.fixture(scope="session")
def orchestrator_url(egg_stack: EggStack) -> str:
    """Orchestrator base URL discovered from the egg-system namespace."""
    return egg_stack.orchestrator_url


@pytest.fixture(scope="session")
def lifecycle_secret(egg_stack: EggStack) -> str:
    """Lifecycle bearer for orchestrator `@require_lifecycle_secret` routes.

    Skips the test when the cluster's ``gateway-secrets`` Secret has no
    ``lifecycle-secret`` key — auth-required routes can't be exercised
    without it, and the auth-reject suite in
    ``test_k8s_deployment_tools.py`` already covers the missing-secret
    failure mode.
    """
    if not egg_stack.lifecycle_secret:
        pytest.skip(
            "no lifecycle-secret key in gateway-secrets — auth-required "
            "deployment-route tests need it"
        )
    return egg_stack.lifecycle_secret


@pytest.fixture(scope="session")
def orchestrator_mcp_url(egg_stack: EggStack) -> str:
    """Streamable-HTTP URL for the orchestrator's MCP server.

    The orchestrator pod runs the MCP sidecar on container port 9850
    (see ``orchestrator/api.py::_start_mcp_server``).  The base Service
    (``k8s/base/orchestrator-service.yaml``) only exposes the API port
    (9849); the MCP port is reached via the ``hostPort: 9850`` mapping
    in ``k8s/overlays/local/patches/orchestrator-volumes.yaml`` (the
    overlay used by ``make deploy`` in CI and locally).  Tests reach
    it via ``http://localhost:9850/mcp``.

    Override at test time with ``EGG_MCP_URL`` if the cluster maps the
    port elsewhere.  The fixture skips if the ``/health`` sidecar
    endpoint is unreachable so a missing hostPort produces a clear skip
    rather than a confusing connection error mid-test.
    """
    import urllib.error
    import urllib.request

    url = os.environ.get("EGG_MCP_URL", "http://localhost:9850/mcp")
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=10) as resp:
            if resp.status != 200:
                pytest.skip(f"Orchestrator MCP /health at {health_url} returned {resp.status}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        pytest.skip(
            f"Orchestrator MCP server not reachable at {health_url}: {exc}. "
            "Integration suite expects the local-overlay hostPort mapping "
            "(k8s/overlays/local/patches/orchestrator-volumes.yaml)."
        )
    return url


@pytest.fixture
def gateway_session(egg_stack: EggStack) -> Generator[dict[str, Any]]:
    """Function-scoped fixture: create a gateway session for isolation.

    Creates a unique session per test and cleans it up afterwards.
    Yields the session creation response including the session_token.
    """
    container_id = f"test-{os.getpid()}-{time.time_ns()}"
    result = egg_stack.create_session(
        container_id=container_id,
        mode="private",
    )

    if not result.get("success"):
        pytest.skip(f"Could not create test session: {result.get('message')}")

    session_data = result.get("data", result)
    session_data["container_id"] = container_id
    yield session_data

    # Cleanup
    token = session_data.get("session_token")
    if token:
        egg_stack.delete_session(token)


def _start_container(
    network: str,
    name_suffix: str,
    *,
    dns: str | None = None,
    env: dict[str, str] | None = None,
) -> ContainerInfo | None:
    """Start an alpine container on the specified network.

    Returns ContainerInfo if successful, None otherwise.
    """
    container_id = f"egg-test-{name_suffix}-{os.getpid()}-{int(time.time())}"

    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_id,
        "--network",
        network,
    ]

    if dns:
        cmd.extend(["--dns", dns])

    if env:
        for k, v in env.items():
            cmd.extend(["-e", f"{k}={v}"])

    cmd.extend(["alpine:latest", "sleep", "300"])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        return None

    # Install curl for network testing
    subprocess.run(
        ["docker", "exec", container_id, "apk", "add", "--no-cache", "curl"],
        capture_output=True,
        timeout=60,
        check=False,
    )

    # Get container IP
    ip_result = subprocess.run(
        [
            "docker",
            "inspect",
            container_id,
            "--format",
            f"{{{{.NetworkSettings.Networks.{network}.IPAddress}}}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    ip = ip_result.stdout.strip() if ip_result.returncode == 0 else None
    if not ip:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=False)
        return None

    return ContainerInfo(container_id=container_id, network=network, ip=ip)


def _cleanup_container(container_id: str) -> None:
    """Stop and remove a test container."""
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        timeout=15,
        check=False,
    )


def exec_in_container(
    container_id: str,
    command: list[str],
    timeout: int = 15,
) -> tuple[int, str, str]:
    """Execute a command in a running container.

    Returns (return_code, stdout, stderr).
    """
    result = subprocess.run(
        ["docker", "exec", container_id, *command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


_LEGACY_DOCKER_FIXTURE_SKIP = (
    "legacy docker-network container fixtures are not supported under k3s "
    "(the only supported runtime after #2474). The properties these tests "
    "check still hold under k3s; the test machinery needs a k3s-native "
    "rewrite. Tracked: https://github.com/jwbron/egg/issues/2603."
)


def _skip_if_k8s_backed(stack: EggStack) -> None:
    """Skip the test if the stack is k8s-backed (legacy docker fixtures only)."""
    if stack.compose_project.startswith("k8s-"):
        pytest.skip(_LEGACY_DOCKER_FIXTURE_SKIP)


@pytest.fixture
def isolated_container(
    egg_stack: EggStack,
) -> Generator[ContainerInfo]:
    """Function-scoped fixture: alpine container on the isolated (private) network."""
    _skip_if_k8s_backed(egg_stack)
    container = _start_container(egg_stack.isolated_network, "isolated")
    if not container:
        pytest.skip("Could not start container on isolated network")
    yield container
    _cleanup_container(container.container_id)


@pytest.fixture
def external_container(
    egg_stack: EggStack,
) -> Generator[ContainerInfo]:
    """Function-scoped fixture: alpine container on the external (public) network."""
    _skip_if_k8s_backed(egg_stack)
    container = _start_container(egg_stack.external_network, "external")
    if not container:
        pytest.skip("Could not start container on external network")
    yield container
    _cleanup_container(container.container_id)


@pytest.fixture
def test_container(egg_stack: EggStack):
    """Factory fixture for creating test containers with custom options.

    Usage:
        def test_something(test_container):
            container = test_container(network="egg-test-isolated", dns="0.0.0.0")
            ...
    """
    _skip_if_k8s_backed(egg_stack)
    containers: list[str] = []

    def _factory(
        network: str | None = None,
        name_suffix: str = "custom",
        *,
        dns: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ContainerInfo:
        net = network or egg_stack.isolated_network
        container = _start_container(net, name_suffix, dns=dns, env=env)
        if not container:
            pytest.skip(f"Could not start container on {net}")
        containers.append(container.container_id)
        return container

    yield _factory

    for cid in containers:
        _cleanup_container(cid)
