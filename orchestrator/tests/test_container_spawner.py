"""
Tests for container spawner with gateway integration.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
    get_container_spawner,
)
from docker_client import ContainerNotFoundError, ContainerOperationError
from gateway_client import GatewayError, GatewayHealth, SessionInfo
from models import AgentRole, ContainerInfo, ContainerStatus


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True

    # Default create_container behavior
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.PENDING,
    )

    # Default start_container behavior
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.utcnow(),
    )

    # Default list_containers behavior
    mock.list_containers.return_value = []

    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()

    # Default health check
    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="healthy",
        version="0.1.0",
    )

    # Default session registration
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-12345",
        container_id="abc123def456",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )

    # Default environment
    mock.get_container_env.return_value = {
        "EGG_SESSION_TOKEN": "test-token-12345",
        "GATEWAY_URL": "http://egg-gateway:9848",
        "EGG_ISSUE_NUMBER": "123",
        "EGG_REPO_PATH": "/workspace/repo",
        "EGG_AGENT_ROLE": "coder",
        "HTTP_PROXY": "http://172.32.0.2:3129",
        "HTTPS_PROXY": "http://172.32.0.2:3129",
    }

    # Default session update behavior
    mock.update_session.return_value = True

    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


class TestContainerSpawnerBasics:
    """Basic spawner tests."""

    def test_lazy_client_initialization(self):
        """Test that clients are lazily initialized."""
        spawner = ContainerSpawner()
        # Clients should not be initialized yet
        assert spawner._docker is None
        assert spawner._gateway is None

    def test_explicit_client_initialization(self, mock_docker_client, mock_gateway_client):
        """Test explicit client initialization."""
        spawner = ContainerSpawner(
            docker_client=mock_docker_client,
            gateway_client=mock_gateway_client,
        )

        assert spawner.docker is mock_docker_client
        assert spawner.gateway is mock_gateway_client


class TestSpawnAgentContainer:
    """Tests for spawning agent containers."""

    def test_spawn_coder_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test spawning a coder container."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            mode="public",
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-123"
        assert result.session_info is not None
        assert result.session_info.session_token == "test-token-12345"

        # Verify Docker client calls
        assert mock_docker_client.create_container.called
        assert mock_docker_client.start_container.called

        # Verify gateway registration
        mock_gateway_client.register_session.assert_called()

    def test_spawn_with_custom_image(self, spawner, mock_docker_client):
        """Test spawning with custom image."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            repo_path="/workspace/repo",
            image="custom-sandbox:v2",
        )

        # Check that custom image was used
        calls = mock_docker_client.create_container.call_args_list
        # Get the last call (after recreate with env)
        last_call = calls[-1]
        assert last_call.kwargs.get("image") == "custom-sandbox:v2"

    def test_spawn_with_repo_mount(self, spawner, mock_docker_client):
        """Test spawning with repository mount."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            repo_mount="/host/path/to/repo",
        )

        # Check that volume was configured
        calls = mock_docker_client.create_container.call_args_list
        last_call = calls[-1]
        volumes = last_call.kwargs.get("volumes", {})
        assert "/host/path/to/repo" in volumes

    def test_spawn_with_extra_env(self, spawner, mock_docker_client, mock_gateway_client):
        """Test spawning with extra environment variables."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        assert "CUSTOM_VAR" in result.environment
        assert result.environment["CUSTOM_VAR"] == "custom_value"

    def test_spawn_private_mode(self, spawner, mock_gateway_client):
        """Test spawning in private mode."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            mode="private",
        )

        # Verify private mode was passed to gateway
        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("mode") == "private"

    def test_spawn_unhealthy_gateway_fails(self, spawner, mock_gateway_client):
        """Test that spawn fails when gateway is unhealthy."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=False,
            status="unhealthy",
            error="Connection refused",
        )

        with pytest.raises(ContainerSpawnError) as exc_info:
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
                issue_number=123,
                repo_path="/workspace/repo",
            )

        assert "not healthy" in str(exc_info.value).lower()

    def test_spawn_skip_gateway_health_check(self, spawner, mock_gateway_client):
        """Test spawning without gateway health check."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=False,
            status="unhealthy",
        )

        # Should not raise when wait_for_gateway=False
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            wait_for_gateway=False,
        )

        assert result is not None

    def test_spawn_continues_without_session(self, spawner, mock_gateway_client, mock_docker_client):
        """Test that spawn continues even if session registration fails."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=True,
            status="healthy",
        )
        mock_gateway_client.register_session.side_effect = GatewayError("Registration failed")

        # Should not raise - just spawn without session
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
        )

        assert result.session_info is None
        assert result.container_info is not None

    def test_spawn_sets_labels(self, spawner, mock_docker_client):
        """Test that proper labels are set on container."""
        spawner.spawn_agent_container(
            pipeline_id="issue-456",
            agent_role=AgentRole.DOCUMENTER,
            issue_number=456,
            repo_path="/workspace/repo",
        )

        calls = mock_docker_client.create_container.call_args_list
        last_call = calls[-1]
        labels = last_call.kwargs.get("labels", {})

        assert labels.get("egg.pipeline.id") == "issue-456"
        assert labels.get("egg.agent.role") == "documenter"
        assert labels.get("egg.issue.number") == "456"


class TestStopAgentContainer:
    """Tests for stopping agent containers."""

    def test_stop_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test stopping a container."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )

        result = spawner.stop_agent_container("abc123")

        assert result.status == ContainerStatus.EXITED
        mock_docker_client.stop_container.assert_called_with("abc123", timeout=10)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_stop_container_without_session_cleanup(self, spawner, mock_docker_client, mock_gateway_client):
        """Test stopping without session cleanup."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )

        spawner.stop_agent_container("abc123", cleanup_session=False)

        mock_gateway_client.delete_session_by_container.assert_not_called()

    def test_stop_container_session_cleanup_error(self, spawner, mock_docker_client, mock_gateway_client):
        """Test that session cleanup errors are logged but not raised."""
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="abc123",
            container_name="test",
            status=ContainerStatus.EXITED,
        )
        mock_gateway_client.delete_session_by_container.side_effect = GatewayError("Error")

        # Should not raise
        result = spawner.stop_agent_container("abc123")
        assert result is not None


class TestRemoveAgentContainer:
    """Tests for removing agent containers."""

    def test_remove_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Test removing a container."""
        spawner.remove_agent_container("abc123")

        mock_docker_client.remove_container.assert_called_with("abc123", force=False)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_remove_container_force(self, spawner, mock_docker_client):
        """Test force removing a container."""
        spawner.remove_agent_container("abc123", force=True)

        mock_docker_client.remove_container.assert_called_with("abc123", force=True)

    def test_remove_cleans_up_session_on_error(self, spawner, mock_docker_client, mock_gateway_client):
        """Test that session is cleaned up even if removal fails."""
        mock_docker_client.remove_container.side_effect = ContainerOperationError("Failed")

        with pytest.raises(ContainerOperationError):
            spawner.remove_agent_container("abc123")

        # Session should still be cleaned up
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")


class TestListPipelineContainers:
    """Tests for listing pipeline containers."""

    def test_list_pipeline_containers(self, spawner, mock_docker_client):
        """Test listing containers for a pipeline."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.RUNNING,
            ),
        ]

        result = spawner.list_pipeline_containers("issue-123")

        assert len(result) == 2
        mock_docker_client.list_containers.assert_called_with(
            labels={"egg.pipeline.id": "issue-123"}
        )


class TestCleanupPipeline:
    """Tests for pipeline cleanup."""

    def test_cleanup_pipeline(self, spawner, mock_docker_client, mock_gateway_client):
        """Test cleaning up all pipeline containers."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.EXITED,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 2
        assert mock_docker_client.remove_container.call_count == 2
        assert mock_gateway_client.delete_session_by_container.call_count == 2

    def test_cleanup_continues_on_error(self, spawner, mock_docker_client, mock_gateway_client):
        """Test that cleanup continues even if some containers fail."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="test1",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="test2",
                status=ContainerStatus.EXITED,
            ),
        ]

        # First removal fails, second succeeds
        mock_docker_client.remove_container.side_effect = [
            ContainerOperationError("Failed"),
            None,
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        # Only the second container was successfully removed
        assert removed == 1


class TestGetContainerIp:
    """Tests for container IP resolution."""

    def test_get_ip_from_docker(self, spawner, mock_docker_client):
        """Test getting IP from Docker network info."""
        mock_container = MagicMock()
        mock_container.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "egg-isolated": {
                        "IPAddress": "172.32.0.42",
                    },
                },
            },
        }
        mock_docker_client.client.containers.get.return_value = mock_container

        ip = spawner._get_container_ip("abc123def456")

        assert ip == "172.32.0.42"

    def test_get_ip_fallback(self, spawner, mock_docker_client):
        """Test fallback IP generation."""
        mock_docker_client.client.containers.get.side_effect = Exception("Not found")

        ip = spawner._get_container_ip("abc123def456")

        # Should return a valid IP in the range
        assert ip.startswith("172.32.0.")


class TestContainerEnvironmentAtCreation:
    """Tests verifying gateway environment is included at container creation time."""

    def test_spawn_includes_session_token_in_container_env(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that session token is passed to create_container, not added after."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
            mode="public",
        )

        # Verify the environment passed to create_container includes the session token
        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "EGG_SESSION_TOKEN" in container_env, (
            "Session token must be included in container environment at creation time"
        )
        assert container_env["EGG_SESSION_TOKEN"] == "test-token-12345"

    def test_spawn_includes_gateway_url_in_container_env(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that GATEWAY_URL is passed to create_container."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
        )

        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "GATEWAY_URL" in container_env, (
            "Gateway URL must be included in container environment at creation time"
        )

    def test_spawn_includes_proxy_config_in_container_env(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that proxy configuration is passed to create_container."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
        )

        create_call = mock_docker_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "HTTP_PROXY" in container_env, (
            "HTTP_PROXY must be included in container environment at creation time"
        )
        assert "HTTPS_PROXY" in container_env, (
            "HTTPS_PROXY must be included in container environment at creation time"
        )

    def test_spawn_updates_session_after_container_creation(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Test that session is updated with real container ID after creation."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repo_path="/workspace/repo",
        )

        # Verify update_session was called with the real container ID
        mock_gateway_client.update_session.assert_called_once()
        update_call = mock_gateway_client.update_session.call_args
        assert update_call.kwargs.get("session_token") == "test-token-12345"
        assert update_call.kwargs.get("container_id") == "abc123def456"


class TestSingletonSpawner:
    """Tests for singleton spawner."""

    def test_get_container_spawner_returns_singleton(self):
        """Test that get_container_spawner returns the same instance."""
        import container_spawner
        container_spawner._spawner = None

        spawner1 = get_container_spawner()
        spawner2 = get_container_spawner()

        assert spawner1 is spawner2
