"""
Tests for container spawner (KubernetesSpawner) with gateway integration.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
    _host_to_local_volumes,
    get_container_spawner,
)
from docker_client import ContainerOperationError
from gateway_client import GatewayError, GatewayHealth, SessionInfo
from models import AgentRole, ContainerInfo, ContainerStatus


@pytest.fixture
def mock_k8s_client():
    """Create a mock Kubernetes client."""
    mock = MagicMock()
    mock.is_connected.return_value = True

    # Default create_container behavior (used by spawn_agent_job)
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-sandbox-egg-agent-issue-123-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )

    # Default list_containers behavior
    mock.list_containers.return_value = []

    # Default stop_container behavior
    mock.stop_container.return_value = ContainerInfo(
        container_id="abc123",
        container_name="test",
        status=ContainerStatus.EXITED,
    )

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
        container_id="egg-agent-issue-123-coder",
        container_ip=None,
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    return mock


@pytest.fixture
def spawner(mock_k8s_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_k8s_client,
        gateway_client=mock_gateway_client,
    )


class TestContainerSpawnerBasics:
    """Basic spawner tests."""

    def test_lazy_client_initialization(self):
        """Test that clients are lazily initialized."""
        spawner = ContainerSpawner()
        # Clients should not be initialized yet
        assert spawner._k8s is None
        assert spawner._gateway is None

    def test_explicit_client_initialization(self, mock_k8s_client, mock_gateway_client):
        """Test explicit client initialization."""
        spawner = ContainerSpawner(
            docker_client=mock_k8s_client,
            gateway_client=mock_gateway_client,
        )

        assert spawner.k8s is mock_k8s_client
        assert spawner.gateway is mock_gateway_client


class TestSpawnAgentContainer:
    """Tests for spawning agent containers."""

    def test_spawn_coder_container(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test spawning a coder container."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-123"
        assert result.session_info is not None
        assert result.session_info.session_token == "test-token-12345"

        # Verify K8s client creates container (job)
        assert mock_k8s_client.create_container.called

        # Verify gateway registration (pre-registered, no update)
        mock_gateway_client.register_session.assert_called()

    def test_spawn_with_custom_image(self, spawner, mock_k8s_client):
        """Test spawning with custom image."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            issue_number=123,
            image="custom-sandbox:v2",
        )

        # Check that custom image was used
        create_call = mock_k8s_client.create_container.call_args
        assert create_call.kwargs.get("image") == "custom-sandbox:v2"

    def test_spawn_with_extra_env(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test spawning with extra environment variables."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
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
            wait_for_gateway=False,
        )

        assert result is not None

    def test_spawn_raises_on_session_failure(
        self, spawner, mock_gateway_client, mock_k8s_client
    ):
        """Test that spawn raises ContainerSpawnError if session registration fails."""
        mock_gateway_client.check_health.return_value = GatewayHealth(
            healthy=True,
            status="healthy",
        )
        mock_gateway_client.register_session.side_effect = GatewayError("Registration failed")

        with pytest.raises(ContainerSpawnError) as exc_info:
            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
                issue_number=123,
            )

        assert "session" in str(exc_info.value).lower()

    def test_spawn_sets_labels(self, spawner, mock_k8s_client):
        """Test that proper labels are set on container."""
        spawner.spawn_agent_container(
            pipeline_id="issue-456",
            agent_role=AgentRole.DOCUMENTER,
            issue_number=456,
        )

        create_call = mock_k8s_client.create_container.call_args
        labels = create_call.kwargs.get("labels", {})

        assert labels.get("egg.pipeline.id") == "issue-456"
        assert labels.get("egg.agent.role") == "documenter"
        assert labels.get("egg.issue.number") == "456"


class TestSpawnBranchPropagation:
    """Tests for branch parameter propagation to gateway registration."""

    def test_branch_passed_to_register_session(self, spawner, mock_gateway_client):
        """Branch param is threaded to gateway register_session."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            branch="egg/fix-auth-bug",
        )

        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("branch") == "egg/fix-auth-bug"

    def test_branch_none_by_default(self, spawner, mock_gateway_client):
        """Branch is None when not provided."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        mock_gateway_client.register_session.assert_called()
        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs.get("branch") is None

    def test_egg_branch_env_set_when_branch_provided(self, spawner):
        """EGG_BRANCH env var is set to the explicit branch value."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            branch="egg/issue-123/work",
        )

        assert result.environment.get("EGG_BRANCH") == "egg/issue-123/work"

    def test_egg_branch_env_falls_back_to_canonical(self, spawner):
        """EGG_BRANCH falls back to egg/{pipeline_id}/work when no branch provided."""
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        assert result.environment.get("EGG_BRANCH") == "egg/issue-123/work"


class TestStopAgentContainer:
    """Tests for stopping agent containers."""

    def test_stop_container(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test stopping a container."""
        result = spawner.stop_agent_container("abc123")

        assert result.status == ContainerStatus.EXITED
        mock_k8s_client.stop_container.assert_called_with("abc123", timeout=10)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_stop_container_without_session_cleanup(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test stopping without session cleanup."""
        spawner.stop_agent_container("abc123", cleanup_session=False)

        mock_gateway_client.delete_session_by_container.assert_not_called()

    def test_stop_container_session_cleanup_error(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that session cleanup errors are logged but not raised."""
        mock_gateway_client.delete_session_by_container.side_effect = GatewayError("Error")

        # Should not raise
        result = spawner.stop_agent_container("abc123")
        assert result is not None


class TestRemoveAgentContainer:
    """Tests for removing agent containers."""

    def test_remove_container(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test removing a container."""
        spawner.remove_agent_container("abc123")

        mock_k8s_client.remove_container.assert_called_with("abc123", force=False)
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")

    def test_remove_container_force(self, spawner, mock_k8s_client):
        """Test force removing a container."""
        spawner.remove_agent_container("abc123", force=True)

        mock_k8s_client.remove_container.assert_called_with("abc123", force=True)

    def test_remove_cleans_up_session_on_error(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that session is cleaned up even if removal fails."""
        mock_k8s_client.remove_container.side_effect = ContainerOperationError("Failed")

        with pytest.raises(ContainerOperationError):
            spawner.remove_agent_container("abc123")

        # Session should still be cleaned up
        mock_gateway_client.delete_session_by_container.assert_called_with("abc123")


class TestListPipelineContainers:
    """Tests for listing pipeline containers."""

    def test_list_pipeline_containers(self, spawner, mock_k8s_client):
        """Test listing containers for a pipeline."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-agent-issue-123-coder",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-agent-issue-123-tester",
                status=ContainerStatus.RUNNING,
            ),
        ]

        result = spawner.list_pipeline_containers("issue-123")

        assert len(result) == 2
        mock_k8s_client.list_containers.assert_called_with(
            labels={"egg.pipeline.id": "issue-123"}
        )


class TestCleanupPipeline:
    """Tests for pipeline cleanup."""

    def test_cleanup_pipeline(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test cleaning up all pipeline containers."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="egg-agent-issue-123-coder",
                status=ContainerStatus.EXITED,
                job_name="egg-sandbox-egg-agent-issue-123-coder",
            ),
            ContainerInfo(
                container_id="def456",
                container_name="egg-agent-issue-123-tester",
                status=ContainerStatus.EXITED,
                job_name="egg-sandbox-egg-agent-issue-123-tester",
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 2
        assert mock_k8s_client.remove_container.call_count == 2
        assert mock_gateway_client.delete_session_by_container.call_count == 2

    def test_cleanup_continues_on_error(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test that cleanup continues even if some containers fail."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="abc123",
                container_name="test1",
                status=ContainerStatus.EXITED,
                job_name="egg-sandbox-test1",
            ),
            ContainerInfo(
                container_id="def456",
                container_name="test2",
                status=ContainerStatus.EXITED,
                job_name="egg-sandbox-test2",
            ),
        ]

        # First removal fails, second succeeds
        mock_k8s_client.remove_container.side_effect = [
            ContainerOperationError("Failed"),
            None,
        ]

        removed = spawner.cleanup_pipeline("issue-123")

        # Only the second container was successfully removed
        assert removed == 1


class TestContainerEnvironmentAtCreation:
    """Tests verifying gateway environment is included at container creation time."""

    def test_spawn_includes_session_token_in_container_env(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that session token is passed to create_container, not added after."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        # Verify the environment passed to create_container includes the session token
        create_call = mock_k8s_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "EGG_SESSION_TOKEN" in container_env, (
            "Session token must be included in container environment at creation time"
        )
        assert container_env["EGG_SESSION_TOKEN"] == "test-token-12345"

    def test_spawn_includes_gateway_url_in_container_env(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that GATEWAY_URL is set to K8s service DNS."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        create_call = mock_k8s_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        assert "GATEWAY_URL" in container_env, (
            "Gateway URL must be included in container environment at creation time"
        )
        # K8s uses service DNS names, not Docker hostnames
        assert "gateway" in container_env["GATEWAY_URL"].lower()

    def test_spawn_includes_proxy_config(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that proxy configuration is always set for K8s containers."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            mode="public",
        )

        create_call = mock_k8s_client.create_container.call_args
        container_env = create_call.kwargs.get("environment", {})

        # In K8s mode, proxy is always set (NetworkPolicy enforces isolation)
        assert "HTTP_PROXY" in container_env
        assert "HTTPS_PROXY" in container_env

    def test_spawn_passes_repos_and_phase_to_register_session(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that repos and phase are passed through to session registration."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
            repos=["test-owner/test-repo"],
            phase="refine",
        )

        # Verify register_session was called with repos and phase
        mock_gateway_client.register_session.assert_called_once()
        register_call = mock_gateway_client.register_session.call_args
        assert register_call.kwargs.get("repos") == ["test-owner/test-repo"]
        assert register_call.kwargs.get("phase") == "refine"


class TestHostToLocalVolumes:
    """Tests for _host_to_local_volumes()."""

    def test_translates_host_home_to_container_home(self):
        """HOST_HOME prefix is replaced with /home/egg."""
        repo_volumes = {"repo": "/home/jwies/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/.egg-worktrees/repo"}

    def test_passthrough_when_host_home_matches_container(self):
        """No translation when HOST_HOME equals container home."""
        repo_volumes = {"repo": "/home/egg/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/egg"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result is repo_volumes

    def test_passthrough_when_host_home_empty(self):
        """No translation when HOST_HOME is not set."""
        repo_volumes = {"repo": "/some/path/repo"}
        with patch.dict("os.environ", {"HOST_HOME": ""}):
            result = _host_to_local_volumes(repo_volumes)
        assert result is repo_volumes

    def test_only_replaces_prefix(self):
        """Only the first occurrence of HOST_HOME at the start is replaced."""
        repo_volumes = {"repo": "/home/jwies/repos/home/jwies/nested"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/repos/home/jwies/nested"}

    def test_multiple_repos(self):
        """All repos in the mapping are translated."""
        repo_volumes = {
            "a": "/home/jwies/.egg-worktrees/a",
            "b": "/home/jwies/.egg-worktrees/b",
        }
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {
            "a": "/home/egg/.egg-worktrees/a",
            "b": "/home/egg/.egg-worktrees/b",
        }

    def test_non_matching_paths_unchanged(self):
        """Paths not starting with HOST_HOME are left unchanged."""
        repo_volumes = {
            "a": "/home/jwies/.egg-worktrees/a",
            "b": "/other/path/b",
        }
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {
            "a": "/home/egg/.egg-worktrees/a",
            "b": "/other/path/b",
        }

    def test_trailing_slash_on_host_home(self):
        """HOST_HOME with trailing slash does not produce double slashes."""
        repo_volumes = {"repo": "/home/jwies/.egg-worktrees/repo"}
        with patch.dict("os.environ", {"HOST_HOME": "/home/jwies/"}):
            result = _host_to_local_volumes(repo_volumes)
        assert result == {"repo": "/home/egg/.egg-worktrees/repo"}


class TestSingletonSpawner:
    """Tests for singleton spawner."""

    def test_get_container_spawner_returns_singleton(self):
        """Test that get_container_spawner returns the same instance."""
        import kubernetes_spawner

        kubernetes_spawner._spawner = None

        spawner1 = get_container_spawner()
        spawner2 = get_container_spawner()

        assert spawner1 is spawner2

        # Reset for other tests
        kubernetes_spawner._spawner = None
