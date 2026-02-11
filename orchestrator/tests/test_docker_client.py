"""
Tests for Docker client.

Note: These tests mock Docker SDK since real Docker operations
are not available in the sandbox.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from docker_client import (
    ContainerNotFoundError,
    ContainerOperationError,
    DockerClient,
    DockerClientError,
    ImageNotFoundError,
    get_docker_client,
)
from models import AgentRole, ContainerStatus


@pytest.fixture
def mock_docker():
    """Mock docker module."""
    with patch("docker_client.docker") as mock:
        mock_client = MagicMock()
        mock.from_env.return_value = mock_client
        mock.DockerClient.return_value = mock_client
        yield mock_client


@pytest.fixture
def docker_client(mock_docker):
    """Create a DockerClient with mocked backend."""
    return DockerClient()


class TestDockerClientConnection:
    """Tests for Docker client connection."""

    def test_is_connected_true(self, docker_client, mock_docker):
        """Test is_connected returns True when ping succeeds."""
        mock_docker.ping.return_value = True
        assert docker_client.is_connected() is True

    def test_is_connected_false(self, docker_client, mock_docker):
        """Test is_connected returns False when ping fails."""
        from docker.errors import DockerException

        mock_docker.ping.side_effect = DockerException("Connection failed")
        assert docker_client.is_connected() is False


class TestContainerCreation:
    """Tests for container creation."""

    def test_create_container(self, docker_client, mock_docker):
        """Test creating a container."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_docker.containers.create.return_value = mock_container
        mock_docker.images.get.return_value = MagicMock()

        info = docker_client.create_container(
            name="test",
            environment={"FOO": "bar"},
        )

        assert info.container_id == "abc123def456"
        assert info.status == ContainerStatus.PENDING
        mock_docker.containers.create.assert_called_once()

    def test_create_container_image_not_found(self, docker_client, mock_docker):
        """Test create fails when image not found."""
        from docker.errors import ImageNotFound

        mock_docker.images.get.return_value = None

        with pytest.raises(ImageNotFoundError):
            docker_client.create_container(name="test", image="nonexistent:latest")

    def test_create_container_with_labels(self, docker_client, mock_docker):
        """Test creating container with custom labels."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_docker.containers.create.return_value = mock_container
        mock_docker.images.get.return_value = MagicMock()

        docker_client.create_container(
            name="test",
            labels={"custom.label": "value"},
        )

        call_kwargs = mock_docker.containers.create.call_args.kwargs
        assert "egg.orchestrator" in call_kwargs["labels"]
        assert call_kwargs["labels"]["custom.label"] == "value"


class TestContainerOperations:
    """Tests for container operations."""

    def test_start_container(self, docker_client, mock_docker):
        """Test starting a container."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.start_container("abc123")

        assert info.status == ContainerStatus.RUNNING
        assert info.started_at is not None
        mock_container.start.assert_called_once()

    def test_start_container_not_found(self, docker_client, mock_docker):
        """Test start fails when container not found."""
        from docker.errors import NotFound

        mock_docker.containers.get.side_effect = NotFound("not found")

        with pytest.raises(ContainerNotFoundError):
            docker_client.start_container("nonexistent")

    def test_stop_container(self, docker_client, mock_docker):
        """Test stopping a container."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.attrs = {"State": {"ExitCode": 0}}
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.stop_container("abc123")

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0
        mock_container.stop.assert_called_once()

    def test_remove_container(self, docker_client, mock_docker):
        """Test removing a container."""
        mock_container = MagicMock()
        mock_docker.containers.get.return_value = mock_container

        docker_client.remove_container("abc123")

        mock_container.remove.assert_called_once_with(force=False, v=True)

    def test_remove_container_force(self, docker_client, mock_docker):
        """Test force removing a container."""
        mock_container = MagicMock()
        mock_docker.containers.get.return_value = mock_container

        docker_client.remove_container("abc123", force=True)

        mock_container.remove.assert_called_once_with(force=True, v=True)


class TestContainerInfo:
    """Tests for getting container info."""

    def test_get_container_info_running(self, docker_client, mock_docker):
        """Test getting info for running container."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.attrs = {
            "State": {
                "Status": "running",
                "StartedAt": "2024-01-15T12:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Config": {"Labels": {}},
        }
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.get_container_info("abc123")

        assert info.status == ContainerStatus.RUNNING
        assert info.started_at is not None

    def test_get_container_info_exited(self, docker_client, mock_docker):
        """Test getting info for exited container."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.attrs = {
            "State": {
                "Status": "exited",
                "ExitCode": 0,
                "StartedAt": "2024-01-15T12:00:00Z",
                "FinishedAt": "2024-01-15T12:30:00Z",
            },
            "Config": {"Labels": {}},
        }
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.get_container_info("abc123")

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0
        assert info.exited_at is not None

    def test_get_container_info_with_agent_role(self, docker_client, mock_docker):
        """Test getting info with agent role label."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-coder"
        mock_container.attrs = {
            "State": {"Status": "running"},
            "Config": {"Labels": {"egg.agent.role": "coder"}},
        }
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.get_container_info("abc123")

        assert info.agent_role == AgentRole.CODER


class TestContainerListing:
    """Tests for listing containers."""

    def test_list_containers(self, docker_client, mock_docker):
        """Test listing containers."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.attrs = {
            "State": {"Status": "running"},
            "Config": {"Labels": {}},
        }
        mock_docker.containers.list.return_value = [mock_container]
        mock_docker.containers.get.return_value = mock_container

        containers = docker_client.list_containers()

        assert len(containers) == 1
        assert containers[0].container_id == "abc123"

    def test_list_containers_with_labels(self, docker_client, mock_docker):
        """Test listing containers with label filter."""
        mock_docker.containers.list.return_value = []

        docker_client.list_containers(labels={"pipeline.id": "issue-123"})

        call_kwargs = mock_docker.containers.list.call_args.kwargs
        assert "pipeline.id=issue-123" in call_kwargs["filters"]["label"]


class TestContainerLogs:
    """Tests for container logs."""

    def test_get_container_logs(self, docker_client, mock_docker):
        """Test getting container logs."""
        mock_container = MagicMock()
        mock_container.logs.return_value = b"2024-01-15T12:00:00Z Log line 1\n"
        mock_docker.containers.get.return_value = mock_container

        logs = docker_client.get_container_logs("abc123")

        assert "Log line 1" in logs


class TestContainerWait:
    """Tests for waiting on containers."""

    def test_wait_for_container(self, docker_client, mock_docker):
        """Test waiting for container to exit."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_docker.containers.get.return_value = mock_container

        info = docker_client.wait_for_container("abc123")

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0


class TestCleanup:
    """Tests for cleanup operations."""

    def test_cleanup_orphaned_containers(self, docker_client, mock_docker):
        """Test cleaning up orphaned containers."""
        mock_container = MagicMock()
        mock_container.id = "abc123"
        mock_container.name = "egg-sandbox-test"
        mock_container.attrs = {
            "State": {
                "Status": "exited",
                "ExitCode": 0,
            },
            "Config": {"Labels": {}},
        }

        # Container exited 48 hours ago
        from datetime import timedelta

        old_time = datetime.utcnow() - timedelta(hours=48)

        mock_docker.containers.list.return_value = [mock_container]
        mock_docker.containers.get.return_value = mock_container

        # Patch get_container_info to return old container
        with patch.object(
            docker_client,
            "get_container_info",
            return_value=MagicMock(
                container_id="abc123",
                status=ContainerStatus.EXITED,
                exited_at=old_time,
            ),
        ):
            removed = docker_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1


class TestGetDockerClient:
    """Tests for singleton getter."""

    def test_get_docker_client_returns_same_instance(self, mock_docker):
        """Test singleton behavior."""
        # Reset singleton
        import docker_client

        docker_client._docker_client = None

        client1 = get_docker_client()
        client2 = get_docker_client()

        # Should be same instance
        assert client1 is client2

        # Reset for other tests
        docker_client._docker_client = None
