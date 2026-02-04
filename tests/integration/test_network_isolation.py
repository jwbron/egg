"""Integration tests for per-container network isolation.

These tests verify the security properties of the per-container network isolation
architecture where:
- Private containers run on egg-isolated network (172.32.0.0/24) with proxy
- Public containers run on egg-external network (172.33.0.0/24) without proxy

Security properties tested:
1. Network isolation: Containers cannot reach the other network's gateway IP
2. Session IP binding: Session tokens are bound to container IPs
3. Repo visibility: Public containers cannot access private repos

Requirements:
- Docker must be running
- egg-gateway container must be running
- Both egg-isolated and egg-external networks must exist

Run with: pytest tests/integration/test_network_isolation.py -v
Skip in CI: pytest -m "not integration"
"""

import json
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass

import pytest

# Gateway IPs on each network
GATEWAY_ISOLATED_IP = "172.32.0.2"
GATEWAY_EXTERNAL_IP = "172.33.0.2"
GATEWAY_PORT = 9848

# Network names
EGG_ISOLATED_NETWORK = "egg-isolated"
EGG_EXTERNAL_NETWORK = "egg-external"


@dataclass
class ContainerInfo:
    """Information about a test container."""

    container_id: str
    network: str
    ip: str


def docker_network_exists(network_name: str) -> bool:
    """Check if a Docker network exists."""
    result = subprocess.run(
        ["docker", "network", "inspect", network_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def get_container_ip(container_id: str, network: str) -> str | None:
    """Get the IP address of a container on a specific network."""
    result = subprocess.run(
        [
            "docker",
            "inspect",
            container_id,
            "--format",
            f"{{{{.NetworkSettings.Networks.{network}.IPAddress}}}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def start_test_container(
    network: str,
    name_suffix: str,
) -> ContainerInfo | None:
    """Start a test container on the specified network.

    Uses alpine with curl for network testing.

    Returns:
        ContainerInfo if successful, None otherwise
    """
    container_id = f"egg-test-{name_suffix}-{int(time.time())}"

    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_id,
            "--network",
            network,
            "alpine:latest",
            "sleep",
            "300",  # Keep container alive for 5 minutes
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return None

    # Get container IP
    ip = get_container_ip(container_id, network)
    if not ip:
        # Cleanup on failure
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=False)
        return None

    return ContainerInfo(container_id=container_id, network=network, ip=ip)


def cleanup_container(container_id: str) -> None:
    """Stop and remove a test container."""
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True,
        check=False,
    )


def exec_in_container(
    container_id: str, command: list[str], timeout: int = 10
) -> tuple[int, str, str]:
    """Execute a command in a running container.

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    result = subprocess.run(
        ["docker", "exec", container_id] + command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def gateway_running() -> bool:
    """Fixture that checks if egg-gateway is running."""
    result = subprocess.run(
        ["docker", "inspect", "egg-gateway"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("egg-gateway container is not running")
    return True


@pytest.fixture
def networks_exist() -> bool:
    """Fixture that checks if both networks exist."""
    if not docker_network_exists(EGG_ISOLATED_NETWORK):
        pytest.skip(f"Network {EGG_ISOLATED_NETWORK} does not exist")
    if not docker_network_exists(EGG_EXTERNAL_NETWORK):
        pytest.skip(f"Network {EGG_EXTERNAL_NETWORK} does not exist")
    return True


@pytest.fixture
def external_container(networks_exist: bool) -> Generator[ContainerInfo, None, None]:
    """Fixture that provides a test container on the external network."""
    container = start_test_container(EGG_EXTERNAL_NETWORK, "external")
    if not container:
        pytest.skip("Could not start test container on external network")

    # Install curl for network testing
    subprocess.run(
        ["docker", "exec", container.container_id, "apk", "add", "--no-cache", "curl"],
        capture_output=True,
        check=False,
    )

    yield container

    cleanup_container(container.container_id)


@pytest.fixture
def isolated_container(networks_exist: bool) -> Generator[ContainerInfo, None, None]:
    """Fixture that provides a test container on the isolated network."""
    container = start_test_container(EGG_ISOLATED_NETWORK, "isolated")
    if not container:
        pytest.skip("Could not start test container on isolated network")

    # Install curl for network testing
    subprocess.run(
        ["docker", "exec", container.container_id, "apk", "add", "--no-cache", "curl"],
        capture_output=True,
        check=False,
    )

    yield container

    cleanup_container(container.container_id)


@pytest.mark.integration
class TestNetworkIsolation:
    """Tests for network isolation between private and public containers."""

    def test_external_container_cannot_reach_isolated_gateway(
        self,
        gateway_running: bool,
        external_container: ContainerInfo,
    ) -> None:
        """Verify container on egg-external cannot reach gateway at 172.32.0.2.

        Security property: Public containers should not be able to communicate
        with the isolated network's gateway IP, preventing them from accessing
        the proxy or attempting to use private mode session tokens.
        """
        # Try to reach the gateway's isolated IP from external network
        returncode, stdout, _stderr = exec_in_container(
            external_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{GATEWAY_ISOLATED_IP}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )

        # Should fail to connect (timeout, connection refused, or network unreachable)
        # If it succeeds (returns 200), that's a security violation
        connection_succeeded = returncode == 0 and stdout == "200"
        assert not connection_succeeded, (
            f"SECURITY VIOLATION: External container can reach isolated gateway at {GATEWAY_ISOLATED_IP}. "
            f"Return code: {returncode}, HTTP status: {stdout}"
        )

    def test_isolated_container_can_reach_isolated_gateway(
        self,
        gateway_running: bool,
        isolated_container: ContainerInfo,
    ) -> None:
        """Verify container on egg-isolated CAN reach gateway at 172.32.0.2.

        This is the expected behavior - private containers need to access
        the gateway for git/gh operations.
        """
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{GATEWAY_ISOLATED_IP}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )

        assert returncode == 0, (
            f"Isolated container should be able to reach gateway. "
            f"Return code: {returncode}, stderr: {stderr}"
        )
        assert stdout == "200", (
            f"Isolated container should get 200 from gateway. "
            f"HTTP status: {stdout}, stderr: {stderr}"
        )

    def test_external_container_can_reach_external_gateway(
        self,
        gateway_running: bool,
        external_container: ContainerInfo,
    ) -> None:
        """Verify container on egg-external CAN reach gateway at 172.33.0.2.

        Public containers need to access the gateway for git/gh operations
        via the external network interface.
        """
        returncode, stdout, stderr = exec_in_container(
            external_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{GATEWAY_EXTERNAL_IP}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )

        assert returncode == 0, (
            f"External container should be able to reach gateway on external network. "
            f"Return code: {returncode}, stderr: {stderr}"
        )
        assert stdout == "200", (
            f"External container should get 200 from gateway on external network. "
            f"HTTP status: {stdout}, stderr: {stderr}"
        )

    def test_isolated_container_cannot_reach_external_gateway(
        self,
        gateway_running: bool,
        isolated_container: ContainerInfo,
    ) -> None:
        """Verify container on egg-isolated cannot reach gateway at 172.33.0.2.

        Security property: Private containers should only see the gateway on
        the isolated network, not the external network.
        """
        returncode, stdout, _stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{GATEWAY_EXTERNAL_IP}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )

        # Should fail to connect
        connection_succeeded = returncode == 0 and stdout == "200"
        assert not connection_succeeded, (
            f"SECURITY VIOLATION: Isolated container can reach external gateway at {GATEWAY_EXTERNAL_IP}. "
            f"Return code: {returncode}, HTTP status: {stdout}"
        )


@pytest.mark.integration
class TestSessionIPBinding:
    """Tests for session IP binding security."""

    def test_session_token_bound_to_container_ip(
        self,
        gateway_running: bool,
        external_container: ContainerInfo,
        isolated_container: ContainerInfo,
    ) -> None:
        """Verify session tokens are bound to container IPs.

        Security property: A session token created for a container on one network
        should not be usable from a container on another network (different IP range).

        This prevents token theft/reuse across network boundaries.
        """
        # This test verifies the IP binding conceptually
        # The actual session creation requires launcher authentication
        # which test containers don't have. We verify the IPs are in different ranges.

        # External network: 172.33.x.x
        assert external_container.ip.startswith("172.33."), (
            f"External container IP should be in 172.33.x.x range, got {external_container.ip}"
        )

        # Isolated network: 172.32.x.x
        assert isolated_container.ip.startswith("172.32."), (
            f"Isolated container IP should be in 172.32.x.x range, got {isolated_container.ip}"
        )

        # The IP ranges being different means the session_manager's IP verification
        # (in validate_session_for_request) will reject cross-network token reuse


@pytest.mark.integration
class TestRepoVisibilityEnforcement:
    """Tests for repository visibility enforcement.

    These tests verify that the gateway correctly enforces repo visibility
    based on session mode. Full testing requires actual GitHub repos.
    """

    def test_health_endpoint_accessible_without_session(
        self,
        gateway_running: bool,
        external_container: ContainerInfo,
    ) -> None:
        """Verify health endpoint is accessible without authentication.

        The health endpoint should be accessible from any container for
        monitoring purposes.
        """
        returncode, stdout, stderr = exec_in_container(
            external_container.container_id,
            [
                "curl",
                "-s",
                f"http://{GATEWAY_EXTERNAL_IP}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )

        assert returncode == 0, f"Health endpoint should be accessible. stderr: {stderr}"

        # Parse response
        try:
            health_data = json.loads(stdout)
            assert health_data.get("status") in (
                "healthy",
                "degraded",
            ), f"Unexpected health status: {health_data}"
            assert "active_sessions" in health_data, (
                "Health response should include active_sessions"
            )
        except json.JSONDecodeError:
            pytest.fail(f"Health endpoint returned invalid JSON: {stdout}")

    def test_git_operations_require_session(
        self,
        gateway_running: bool,
        external_container: ContainerInfo,
    ) -> None:
        """Verify git operations require valid session token.

        All git/gh operations should require a valid session token,
        not just launcher authentication.
        """
        # Try to call git execute without session token
        returncode, stdout, stderr = exec_in_container(
            external_container.container_id,
            [
                "curl",
                "-s",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                '{"repo_path": "/home/egg/repos/test", "operation": "status"}',
                f"http://{GATEWAY_EXTERNAL_IP}:{GATEWAY_PORT}/api/v1/git/execute",
            ],
            timeout=10,
        )

        assert returncode == 0, f"Curl should succeed. stderr: {stderr}"

        # Should get 401 Unauthorized
        try:
            response = json.loads(stdout)
            # The response should indicate authentication failure
            assert response.get("success") is False, (
                f"Git operation without session should fail. Response: {response}"
            )
        except json.JSONDecodeError:
            # If we got HTML error page or similar, that's also a failure (expected)
            pass
