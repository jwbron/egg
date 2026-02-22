"""
Tests for DinD sidecar integration in ContainerSpawner.

Covers the DinD provisioning paths in spawn_agent_container() and
cleanup_pipeline() that the coder's changes added. All Docker and
DindManager interactions are mocked.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock docker SDK before importing modules that depend on it
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from container_spawner import (  # noqa: E402
    ContainerSpawner,
    ContainerSpawnError,
)
from docker_client import DockerClientError  # noqa: E402
from gateway_client import GatewayHealth, SessionInfo  # noqa: E402
from models import AgentRole, ContainerInfo, ContainerStatus  # noqa: E402

# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client with default behaviors."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-tester",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-tester",
        status=ContainerStatus.RUNNING,
        started_at=datetime.utcnow(),
    )
    mock.list_containers.return_value = []
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client with default behaviors."""
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-12345",
        container_id="abc123def456",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a ContainerSpawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


def _make_mock_dind_manager(healthy=True, daemon_url="tcp://172.17.0.5:2375"):
    """Create a mock DindManager."""
    from dind_manager import DindStatus, DindStatusValue

    mock = MagicMock()
    status = DindStatus(
        status=DindStatusValue.HEALTHY if healthy else DindStatusValue.UNHEALTHY,
        container_id="dind-abc123",
        daemon_url=daemon_url if healthy else "",
    )
    mock.start.return_value = status
    mock.teardown.return_value = None
    return mock


# ── DinD Provisioning in spawn_agent_container ──────────────────


class TestSpawnWithDindProvisioning:
    """Tests for DinD sidecar provisioning when spawning tester containers."""

    @patch("container_spawner.DindManager")
    def test_dind_provisioned_for_tester_when_enabled(
        self, MockDindManager, spawner, mock_docker_client
    ):
        """DinD sidecar is provisioned when integration_test_enabled=True for TESTER."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        MockDindManager.assert_called_once_with(
            pipeline_id="issue-123",
            docker_client=mock_docker_client.client,
        )
        mock_dind.start.assert_called_once()
        assert "DOCKER_HOST" in result.environment
        assert result.environment["DOCKER_HOST"] == "tcp://172.17.0.5:2375"

    @patch("container_spawner.DindManager")
    def test_dind_not_provisioned_for_non_tester(self, MockDindManager, spawner):
        """DinD sidecar is NOT provisioned for non-TESTER roles even when enabled."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            integration_test_enabled=True,
        )

        MockDindManager.assert_not_called()
        assert "DOCKER_HOST" not in result.environment

    @patch("container_spawner.DindManager")
    def test_dind_not_provisioned_when_disabled(self, MockDindManager, spawner):
        """DinD sidecar is NOT provisioned when integration_test_enabled=False."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=False,
        )

        MockDindManager.assert_not_called()

    @patch("container_spawner.DindManager", None)
    def test_dind_not_provisioned_when_import_failed(self, spawner):
        """DinD sidecar is NOT provisioned when DindManager import failed (None)."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        assert "DOCKER_HOST" not in result.environment

    @patch("container_spawner.DindManager")
    def test_dind_docker_host_injected_into_extra_env(self, MockDindManager, spawner):
        """DOCKER_HOST is added to extra_env when DinD starts successfully."""
        mock_dind = _make_mock_dind_manager(daemon_url="tcp://10.0.0.5:2375")
        MockDindManager.return_value = mock_dind

        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            extra_env={"MY_VAR": "my_value"},
            integration_test_enabled=True,
        )

        assert result.environment.get("DOCKER_HOST") == "tcp://10.0.0.5:2375"
        assert result.environment.get("MY_VAR") == "my_value"

    @patch("container_spawner.DindManager")
    def test_dind_creates_extra_env_dict_when_none(self, MockDindManager, spawner):
        """When extra_env is None, DinD provisioning creates the dict for DOCKER_HOST."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            extra_env=None,
            integration_test_enabled=True,
        )

        assert "DOCKER_HOST" in result.environment

    @patch("container_spawner.DindManager")
    def test_dind_unhealthy_no_docker_host(self, MockDindManager, spawner):
        """When DinD starts but daemon_url is empty, DOCKER_HOST is not set."""
        mock_dind = _make_mock_dind_manager(healthy=False, daemon_url="")
        MockDindManager.return_value = mock_dind

        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        assert "DOCKER_HOST" not in result.environment

    @patch("container_spawner.DindManager")
    def test_dind_startup_failure_is_non_fatal(self, MockDindManager, spawner):
        """DinD startup failure does not prevent container from being spawned."""
        mock_dind = MagicMock()
        mock_dind.start.side_effect = Exception("DinD daemon unavailable")
        mock_dind.teardown.return_value = None
        MockDindManager.return_value = mock_dind

        # Should not raise - DinD failure is non-fatal
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        assert result is not None
        mock_dind.teardown.assert_called_once()
        assert "DOCKER_HOST" not in result.environment

    @patch("container_spawner.DindManager")
    def test_dind_tracked_in_managers_dict(self, MockDindManager, spawner):
        """Successful DinD manager is stored in _dind_managers for later cleanup."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        assert "issue-123" in spawner._dind_managers
        assert spawner._dind_managers["issue-123"] is mock_dind

    @patch("container_spawner.DindManager")
    def test_dind_not_tracked_on_startup_failure(self, MockDindManager, spawner):
        """Failed DinD manager is NOT stored in _dind_managers."""
        mock_dind = MagicMock()
        mock_dind.start.side_effect = Exception("startup failed")
        MockDindManager.return_value = mock_dind

        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            integration_test_enabled=True,
        )

        assert "issue-123" not in spawner._dind_managers

    @patch("container_spawner.DindManager")
    def test_dind_uses_network_from_mode(self, MockDindManager, spawner):
        """DinD start() receives the network name derived from the mode."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            mode="public",
            integration_test_enabled=True,
        )

        # Verify start was called with a network_name argument
        mock_dind.start.assert_called_once()
        call_kwargs = mock_dind.start.call_args
        assert "network_name" in call_kwargs.kwargs or len(call_kwargs.args) > 0


# ── DinD cleanup on Docker errors ──────────────────────────────


class TestSpawnDindCleanupOnError:
    """Tests for DinD cleanup when container spawning fails."""

    @patch("container_spawner.DindManager")
    def test_dind_torn_down_on_docker_error(
        self, MockDindManager, spawner, mock_docker_client, mock_gateway_client
    ):
        """DinD sidecar is torn down when Docker container creation fails."""
        mock_dind = _make_mock_dind_manager()
        MockDindManager.return_value = mock_dind

        mock_docker_client.create_container.side_effect = DockerClientError(
            "Container creation failed"
        )

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.TESTER,
                issue_number=123,
                integration_test_enabled=True,
            )

        mock_dind.teardown.assert_called_once()

    @patch("container_spawner.DindManager")
    def test_dind_not_torn_down_when_not_provisioned(
        self, MockDindManager, spawner, mock_docker_client
    ):
        """When DinD was not requested, no teardown happens on Docker error."""
        mock_docker_client.create_container.side_effect = DockerClientError("fail")

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
                issue_number=123,
                integration_test_enabled=False,
            )

        MockDindManager.assert_not_called()


# ── DinD cleanup in cleanup_pipeline ────────────────────────────


class TestCleanupPipelineDind:
    """Tests for DinD cleanup during pipeline cleanup."""

    def test_cleanup_pipeline_tears_down_dind(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline() tears down tracked DinD sidecar."""
        mock_dind = MagicMock()
        spawner._dind_managers["issue-123"] = mock_dind
        mock_docker_client.list_containers.return_value = []

        spawner.cleanup_pipeline("issue-123")

        mock_dind.teardown.assert_called_once()
        assert "issue-123" not in spawner._dind_managers

    def test_cleanup_pipeline_no_dind(self, spawner, mock_docker_client, mock_gateway_client):
        """cleanup_pipeline() works fine when no DinD sidecar was provisioned."""
        mock_docker_client.list_containers.return_value = []

        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 0
        assert "issue-123" not in spawner._dind_managers

    def test_cleanup_pipeline_dind_teardown_error_non_fatal(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """DinD teardown errors during pipeline cleanup are non-fatal."""
        mock_dind = MagicMock()
        mock_dind.teardown.side_effect = Exception("teardown failed")
        spawner._dind_managers["issue-123"] = mock_dind
        mock_docker_client.list_containers.return_value = []

        # Should not raise
        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 0
        mock_dind.teardown.assert_called_once()
        # Manager should still be removed from dict
        assert "issue-123" not in spawner._dind_managers

    def test_cleanup_pipeline_containers_and_dind(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline() removes containers AND tears down DinD."""
        mock_dind = MagicMock()
        spawner._dind_managers["issue-123"] = mock_dind

        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.EXITED,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 1
        mock_dind.teardown.assert_called_once()


# ── _dind_managers initialization ────────────────────────────────


class TestDindManagersDict:
    """Tests for the _dind_managers dictionary on ContainerSpawner."""

    def test_dind_managers_initialized_empty(self, mock_docker_client, mock_gateway_client):
        """_dind_managers is initialized as an empty dict."""
        spawner = ContainerSpawner(
            docker_client=mock_docker_client,
            gateway_client=mock_gateway_client,
        )
        assert spawner._dind_managers == {}

    def test_dind_managers_not_shared_between_instances(
        self, mock_docker_client, mock_gateway_client
    ):
        """Each ContainerSpawner has its own _dind_managers dict."""
        s1 = ContainerSpawner(docker_client=mock_docker_client, gateway_client=mock_gateway_client)
        s2 = ContainerSpawner(docker_client=mock_docker_client, gateway_client=mock_gateway_client)
        s1._dind_managers["test"] = MagicMock()
        assert "test" not in s2._dind_managers
