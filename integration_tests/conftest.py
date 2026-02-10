"""Shared fixtures for integration tests.

Provides:
- EggStack dataclass with gateway URL, IPs, launcher secret, and helpers
- egg_stack (session-scoped): starts/stops the gateway via docker compose
- gateway_session (function-scoped): creates/destroys a gateway session per test
- test_container: starts/stops an alpine container on a given network
- run_claude_structured(): runs Claude Code with JSON schema output parsing
- AgentVerdict / assert_agent_verdict(): structured verdict helpers

All Docker-dependent fixtures skip gracefully when Docker is unavailable.
"""

import json
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
import requests
from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT
from egg_container import (
    ContainerNetworkConfig,
    build_sandbox_docker_cmd,
)

from tests.utils.gateway_client import (
    GatewayClientMixin,
    docker_available,
    wait_for_healthy,
)

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

# Counter for allocating unique container IPs within the test subnet.
# Starts at 100 to leave room for gateway (.2) and other infrastructure.
_next_container_ip_suffix = 100


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
    duplication with tests/functional/conftest.py:MinimalGateway.
    """

    gateway_url: str
    gateway_isolated_ip: str
    gateway_external_ip: str
    gateway_port: int
    proxy_port: int
    launcher_secret: str
    compose_project: str
    config_dir: str
    isolated_network: str
    external_network: str
    certs_volume: str = ""  # Docker volume name for gateway CA certs
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


@pytest.fixture(scope="session")
def egg_stack() -> Generator[EggStack, None, None]:
    """Session-scoped fixture: start the gateway stack via docker compose.

    Builds the gateway image, starts it on test networks, waits for health,
    and tears everything down after the test session.
    """
    if not docker_available():
        pytest.skip("Docker is not available")

    compose_file = PROJECT_ROOT / "integration_tests" / "docker-compose.yml"
    if not compose_file.exists():
        pytest.skip("docker-compose.yml not found")

    # Generate unique project name to avoid collisions
    project_name = f"egg-test-{os.getpid()}"

    # Generate launcher secret
    launcher_secret = secrets.token_urlsafe(32)

    # Create temp config directory
    config_dir = tempfile.mkdtemp(prefix="egg-test-config-")
    _write_test_config(config_dir, launcher_secret)

    # Environment for docker compose
    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "EGG_LAUNCHER_SECRET": launcher_secret,
        "EGG_CONFIG_DIR": config_dir,
        "HOST_UID": str(os.getuid()),
        "HOST_GID": str(os.getgid()),
        "GATEWAY_PORT": "0",  # Random host port
        "PROXY_PORT": "0",
    }

    compose_cmd = ["docker", "compose", "-f", str(compose_file), "-p", project_name]

    try:
        # Build and start
        subprocess.run(
            [*compose_cmd, "up", "-d", "--build"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )

        # Get the mapped gateway port
        result = subprocess.run(
            [*compose_cmd, "port", "gateway", str(GATEWAY_PORT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        # Output is like "0.0.0.0:32768"
        host_port = result.stdout.strip().split(":")[-1]
        gateway_url = f"http://localhost:{host_port}"

        # Wait for gateway to become healthy
        if not wait_for_healthy(gateway_url, timeout=120):
            # Dump logs for debugging
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

        stack = EggStack(
            gateway_url=gateway_url,
            gateway_isolated_ip=GATEWAY_ISOLATED_IP,
            gateway_external_ip=GATEWAY_EXTERNAL_IP,
            gateway_port=int(host_port),
            proxy_port=PROXY_PORT,
            launcher_secret=launcher_secret,
            compose_project=project_name,
            config_dir=config_dir,
            isolated_network=f"{project_name}-isolated",
            external_network=f"{project_name}-external",
            certs_volume=f"{project_name}_certs",
        )

        # Detect what source IP the gateway sees for our requests
        # so sessions can be bound to the correct IP.
        stack.detect_source_ip()

        yield stack

    finally:
        # Tear down compose stack
        subprocess.run(
            [*compose_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )

        # Clean up config directory
        shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture
def gateway_session(egg_stack: EggStack) -> Generator[dict[str, Any], None, None]:
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


@pytest.fixture
def isolated_container(
    egg_stack: EggStack,
) -> Generator[ContainerInfo, None, None]:
    """Function-scoped fixture: alpine container on the isolated (private) network."""
    container = _start_container(egg_stack.isolated_network, "isolated")
    if not container:
        pytest.skip("Could not start container on isolated network")
    yield container
    _cleanup_container(container.container_id)


@pytest.fixture
def external_container(
    egg_stack: EggStack,
) -> Generator[ContainerInfo, None, None]:
    """Function-scoped fixture: alpine container on the external (public) network."""
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


# ---------------------------------------------------------------------------
# Structured output helpers for agent-led testing
# ---------------------------------------------------------------------------

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "fail"],
            "description": "Whether the test condition was met.",
        },
        "evidence": {
            "type": "string",
            "description": "Concrete output or observation supporting the verdict.",
        },
        "details": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["check", "passed"],
            },
            "description": "Individual sub-checks performed.",
        },
    },
    "required": ["verdict", "evidence"],
}

TEST_AGENT_SYSTEM_PROMPT = (
    "You are a TEST agent evaluating the egg sandbox. You did NOT build this code. "
    "Report findings as structured verdicts. Be precise and factual."
)


@dataclass
class AgentVerdict:
    """Parsed result from a structured agent run."""

    verdict: str
    evidence: str
    details: list[dict[str, Any]]
    raw_output: str
    cost_usd: float | None
    infrastructure_failure: bool = False  # Set by run_claude_structured, not the agent

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def _allocate_test_container_ip() -> str:
    """Allocate a unique IP address for a test container.

    Production uses ``_allocate_container_ip()`` which inspects the docker
    network to find available IPs. For tests, we use a simple counter to
    avoid the subprocess overhead and race conditions in parallel tests.

    Returns:
        An IP in the 172.40.0.100+ range (test isolated subnet).
    """
    global _next_container_ip_suffix
    ip = f"172.40.0.{_next_container_ip_suffix}"
    _next_container_ip_suffix += 1
    # Wrap around if we somehow allocate >155 containers in one session
    if _next_container_ip_suffix > 254:
        _next_container_ip_suffix = 100
    return ip


def _capture_container_logs(container_name: str) -> str:
    """Capture logs from a container (even if it crashed or is still running).

    Used for post-mortem diagnostics when tests timeout or fail unexpectedly.
    """
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        logs = []
        if result.stdout:
            logs.append(f"=== STDOUT ===\n{result.stdout}")
        if result.stderr:
            logs.append(f"=== STDERR ===\n{result.stderr}")
        return "\n".join(logs) if logs else "(no logs captured)"
    except subprocess.TimeoutExpired:
        return "(log capture timed out)"
    except Exception as e:
        return f"(log capture failed: {e})"


def _preflight_gateway_check(egg_stack: "EggStack", timeout: int = 10) -> tuple[bool, str]:
    """Perform a pre-flight health check on the gateway before spawning containers.

    This catches infrastructure issues early (before the 180s test timeout)
    and provides clear diagnostics when the gateway is not ready.

    Returns:
        (success, message) tuple
    """
    try:
        health = egg_stack.health_check(timeout=timeout)
        status = health.get("status", "unknown")
        if status == "healthy":
            return True, "Gateway healthy"
        return False, f"Gateway status: {status} (expected: healthy)"
    except requests.exceptions.ConnectionError as e:
        return False, f"Gateway unreachable: {e}"
    except requests.exceptions.Timeout:
        return False, f"Gateway health check timed out after {timeout}s"
    except Exception as e:
        return False, f"Gateway health check failed: {type(e).__name__}: {e}"


def run_claude_structured(
    egg_stack: "EggStack",
    session_token: str,
    prompt: str,
    *,
    model: str = "sonnet",
    max_budget_usd: float = 0.50,
    timeout: int = 180,
    extra_system: str = "",
) -> AgentVerdict:
    """Run Claude Code with structured JSON output in a sandbox container.

    Uses ``--output-format json`` and ``--json-schema`` to get a parsed
    verdict back from the agent. The agent identity is established via
    ``--append-system-prompt`` to separate it from the building agent.

    Network configuration coupling:
        This function constructs ``ContainerNetworkConfig`` manually from
        ``EggStack`` values rather than calling ``_get_container_network_config()``
        (which is internal to ``sandbox/egg_lib/runtime.py``). If that function
        gains new fields or changes defaults, this test helper must be updated
        to match. The shared ``build_sandbox_docker_cmd()`` ensures the *docker
        arguments* stay in sync, but the *config dataclass population* is a
        separate coupling point.
    """
    # Pre-flight check: verify gateway is healthy before spawning container
    # This catches infrastructure issues early with clear diagnostics
    preflight_ok, preflight_msg = _preflight_gateway_check(egg_stack)
    if not preflight_ok:
        return AgentVerdict(
            verdict="fail",
            evidence=f"Pre-flight gateway check failed: {preflight_msg}",
            details=[{"check": "gateway_preflight", "passed": False, "note": preflight_msg}],
            raw_output="",
            cost_usd=None,
            infrastructure_failure=True,
        )

    system_prompt = TEST_AGENT_SYSTEM_PROMPT
    if extra_system:
        system_prompt = f"{system_prompt} {extra_system}"

    schema_json = json.dumps(VERDICT_SCHEMA)

    net_config = ContainerNetworkConfig(
        network_name=egg_stack.isolated_network,
        gateway_hostname="egg-gateway",
        gateway_ip=egg_stack.gateway_isolated_ip,
        gateway_port=GATEWAY_PORT,
        repo_mode="private",
        proxy_url=f"http://egg-gateway:{PROXY_PORT}",
    )

    # Allocate a static IP for this container — matches production behavior
    # where sessions are bound to specific container IPs for request verification.
    container_ip = _allocate_test_container_ip()

    # Generate a predictable container name so we can capture logs on timeout
    container_name = f"test-claude-{os.getpid()}-{time.time_ns()}"

    cmd = build_sandbox_docker_cmd(
        container_name=container_name,
        image="egg-sandbox:latest",
        network=net_config,
        container_ip=container_ip,
        session_token=session_token,
        runtime_uid=1000,
        runtime_gid=1000,
        extra_env={
            "ANTHROPIC_OAUTH_TOKEN": os.environ["ANTHROPIC_OAUTH_TOKEN"],
            # Match production: always set auth method so sandbox startup code
            # branches the same way in tests as in production.
            "ANTHROPIC_AUTH_METHOD": "oauth",
            # Reduce gateway health check timeout for faster test feedback
            # (default 60s is too long when debugging test failures)
            "EGG_GATEWAY_TIMEOUT": "30",
            # Enable debug logging for startup phases (logs to stderr)
            # This helps diagnose container hangs by showing which phase stalled
            "EGG_DEBUG": "1",
        },
    )

    # Note: We intentionally do NOT use --rm here so we can capture logs on timeout.
    # Production uses the LIFECYCLE_FLAGS_INDEX pattern in build_sandbox_docker_cmd()
    # to include --rm; we diverge here because tests need post-mortem log access.
    # Cleanup happens in the finally block below.

    # Mount the gateway CA certificate volume so the sandbox can trust the proxy
    # The volume is created by docker-compose and populated by the gateway entrypoint
    if egg_stack.certs_volume:
        cmd[-1:-1] = ["-v", f"{egg_stack.certs_volume}:/shared/certs:ro"]

    # Claude CLI command after image name
    cmd.extend(
        [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--append-system-prompt",
            system_prompt,
            "--no-session-persistence",
            "--max-budget-usd",
            str(max_budget_usd),
            "--model",
            model,
            "--dangerously-skip-permissions",
            prompt,
        ]
    )

    def _cleanup_container() -> None:
        """Remove the test container (best-effort)."""
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # Capture container logs before cleanup for post-mortem analysis
        container_logs = _capture_container_logs(container_name)
        _cleanup_container()

        raw = (e.stdout or "")[:2000] if e.stdout else ""
        stderr = (e.stderr or "")[:500] if e.stderr else ""
        return AgentVerdict(
            verdict="fail",
            evidence=(
                f"Subprocess timed out after {timeout}s.\n"
                f"stderr: {stderr}\n"
                f"Container logs:\n{container_logs[:3000]}"
            ),
            details=[],
            raw_output=raw,
            cost_usd=None,
            infrastructure_failure=True,
        )

    raw = result.stdout.strip()

    if result.returncode != 0:
        # Capture container logs for debugging non-zero exit
        container_logs = _capture_container_logs(container_name)
        _cleanup_container()
        return AgentVerdict(
            verdict="fail",
            evidence=(
                f"Claude Code exited {result.returncode}: {result.stderr[:500]}\n"
                f"Container logs:\n{container_logs[:3000]}"
            ),
            details=[],
            raw_output=raw,
            cost_usd=None,
            infrastructure_failure=True,
        )

    # Success path - cleanup container
    _cleanup_container()

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return AgentVerdict(
            verdict="fail",
            evidence=f"Could not parse JSON output: {raw[:500]}",
            details=[],
            raw_output=raw,
            cost_usd=None,
            infrastructure_failure=True,
        )

    # The envelope from --output-format json wraps the schema result.
    # Extract the verdict payload — it may be at top level or nested
    # under a "result" key depending on Claude Code version.
    payload = envelope.get("result", envelope)

    cost = None
    if "cost_usd" in envelope:
        cost = envelope["cost_usd"]

    return AgentVerdict(
        verdict=payload.get("verdict", "fail"),
        evidence=payload.get("evidence", ""),
        details=payload.get("details", []),
        raw_output=raw,
        cost_usd=cost,
    )


def assert_agent_verdict(
    verdict: AgentVerdict,
    *,
    min_pass_ratio: float = 1.0,
    msg: str = "",
) -> None:
    """Assert that an agent verdict meets expectations.

    Args:
        verdict: The AgentVerdict to check.
        min_pass_ratio: Fraction of detail checks that must pass (0.0-1.0).
            Use < 1.0 for flaky tolerance.
        msg: Optional context message for assertion errors.
    """
    context = f" ({msg})" if msg else ""

    if verdict.details:
        passed = sum(1 for d in verdict.details if d.get("passed"))
        total = len(verdict.details)
        ratio = passed / total if total else 0.0
        assert ratio >= min_pass_ratio, (
            f"Agent detail checks{context}: {passed}/{total} passed "
            f"(need {min_pass_ratio:.0%}).\n"
            f"Evidence: {verdict.evidence}\n"
            f"Details: {json.dumps(verdict.details, indent=2)}"
        )

    assert verdict.passed, (
        f"Agent verdict: FAIL{context}.\n"
        f"Evidence: {verdict.evidence}\n"
        f"Raw output: {verdict.raw_output[:1000]}"
    )
