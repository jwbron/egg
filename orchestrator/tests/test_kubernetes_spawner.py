"""
Tests for KubernetesSpawner.

Mirrors test_container_spawner.py structure but for the Kubernetes backend.
Tests spawning k8s Jobs with gateway session integration and token-only auth.
"""

from unittest.mock import MagicMock, patch

import pytest
from container_backend import (
    KubernetesClientError,
    PodNotFoundError,
)
from models import AgentRole, ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_client():
    """Mock KubernetesClient for spawner tests."""
    mock = MagicMock()
    mock.is_connected.return_value = True

    # Default create_container returns pending info
    mock.create_container.return_value = ContainerInfo(
        container_id="egg-issue-123-coder",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.PENDING,
    )

    # start_container returns running info (auto-start in k8s)
    mock.start_container.return_value = ContainerInfo(
        container_id="egg-issue-123-coder",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.RUNNING,
    )

    # get_container_info raises not found by default (for cleanup check)
    mock.get_container_info.side_effect = PodNotFoundError("Not found")

    return mock


@pytest.fixture
def mock_gateway_client():
    """Mock GatewayClient for spawner tests."""
    mock = MagicMock()
    mock.check_health.return_value = MagicMock(healthy=True)

    # Register session returns session info
    mock.register_session.return_value = MagicMock(
        session_token="test-session-token-abc123",
        container_id="egg-issue-123-coder",
        container_ip="0.0.0.0",
    )

    # Worktree creation
    mock.create_worktrees.return_value = MagicMock(
        success=True,
        worktrees={"egg": "/home/egg/.egg-worktrees/issue-123-coder/egg"},
    )

    return mock


@pytest.fixture
def spawner(mock_k8s_client, mock_gateway_client):
    """Create a KubernetesSpawner with mocked dependencies."""
    from kubernetes_spawner import KubernetesSpawner

    return KubernetesSpawner(
        k8s_client=mock_k8s_client,
        gateway_client=mock_gateway_client,
    )


# ---------------------------------------------------------------------------
# Basic spawner tests
# ---------------------------------------------------------------------------


class TestKubernetesSpawnerBasics:
    """Basic spawner functionality tests."""

    def test_spawner_creation(self, spawner):
        """Test spawner can be created with injected dependencies."""
        assert spawner is not None

    def test_spawner_lazy_client_init(self, mock_gateway_client):
        """Test spawner lazily initializes k8s client."""
        from kubernetes_spawner import KubernetesSpawner

        spawner = KubernetesSpawner(gateway_client=mock_gateway_client)
        assert spawner._k8s is None

    def test_spawner_lazy_gateway_init(self, mock_k8s_client):
        """Test spawner lazily initializes gateway client."""
        from kubernetes_spawner import KubernetesSpawner

        spawner = KubernetesSpawner(k8s_client=mock_k8s_client)
        assert spawner._gateway is None


# ---------------------------------------------------------------------------
# Agent spawning tests
# ---------------------------------------------------------------------------


class TestSpawnAgentJob:
    """Tests for spawning agent Jobs."""

    def test_spawn_agent_basic(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test basic agent spawn creates Job and registers session."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            issue_number=123,
        )

        assert result is not None
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-123"
        mock_gateway_client.register_session.assert_called_once()
        mock_k8s_client.create_container.assert_called_once()

    def test_spawn_agent_with_volumes(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test spawn with repo volumes creates hostPath mounts."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/home/egg/repos/egg"},
            repos=["owner/egg"],
        )

        assert result is not None
        mock_gateway_client.create_worktrees.assert_called_once()

    def test_spawn_sets_k8s_mode_env(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that spawn sets EGG_K8S_MODE=true in environment."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        assert result.environment.get("EGG_K8S_MODE") == "true"

    def test_spawn_sets_orchestrator_url(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that spawn sets EGG_ORCHESTRATOR_URL for k8s service DNS."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        assert "EGG_ORCHESTRATOR_URL" in result.environment
        assert "svc.cluster.local" in result.environment["EGG_ORCHESTRATOR_URL"]

    def test_spawn_sets_gateway_url(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that spawn sets GATEWAY_URL for k8s service DNS."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        assert "GATEWAY_URL" in result.environment
        assert "svc.cluster.local" in result.environment["GATEWAY_URL"]

    def test_spawn_includes_session_token_in_env(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test session token is injected into environment."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        assert result.environment.get("EGG_SESSION_TOKEN") == "test-session-token-abc123"
        assert result.session_info.session_token == "test-session-token-abc123"

    def test_spawn_sets_pipeline_env(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test spawn sets EGG_PIPELINE_ID and EGG_AGENT_ROLE."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.TESTER,
            phase="implement",
        )

        assert result.environment["EGG_PIPELINE_ID"] == "issue-123"
        assert result.environment["EGG_AGENT_ROLE"] == "tester"
        assert result.environment["EGG_PHASE"] == "implement"

    def test_spawn_extra_env_overrides(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test that extra_env overrides default env vars."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        assert result.environment["CUSTOM_VAR"] == "custom_value"


# ---------------------------------------------------------------------------
# Token-only auth tests (new for k8s)
# ---------------------------------------------------------------------------


class TestTokenOnlyAuth:
    """Tests for token-only gateway auth (no IP binding)."""

    def test_session_registers_with_zero_ip(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test gateway session uses 0.0.0.0 (no IP binding)."""
        spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        call_kwargs = mock_gateway_client.register_session.call_args.kwargs
        assert call_kwargs["container_ip"] == "0.0.0.0"


# ---------------------------------------------------------------------------
# Cleanup on failure tests
# ---------------------------------------------------------------------------


class TestSpawnFailureCleanup:
    """Tests for cleanup when spawning fails."""

    def test_cleans_session_on_k8s_error(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test gateway session is cleaned up when k8s Job creation fails."""
        mock_k8s_client.create_container.side_effect = KubernetesClientError(
            "Job creation failed"
        )

        from kubernetes_spawner import ContainerSpawnError

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_job(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
            )

        mock_gateway_client.delete_session.assert_called_once()

    def test_cleans_worktree_on_k8s_error(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test worktree is cleaned up when k8s Job creation fails."""
        mock_k8s_client.create_container.side_effect = KubernetesClientError(
            "Job creation failed"
        )

        from kubernetes_spawner import ContainerSpawnError

        with pytest.raises(ContainerSpawnError):
            spawner.spawn_agent_job(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
            )

        mock_gateway_client.delete_worktrees.assert_called_once()

    def test_gateway_unhealthy_raises(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test spawn fails when gateway is unhealthy."""
        mock_gateway_client.check_health.return_value = MagicMock(
            healthy=False, status="degraded", error="connection refused"
        )

        from kubernetes_spawner import ContainerSpawnError

        with pytest.raises(ContainerSpawnError, match="not healthy"):
            spawner.spawn_agent_job(
                pipeline_id="issue-123",
                agent_role=AgentRole.CODER,
            )


# ---------------------------------------------------------------------------
# Stop/Remove tests
# ---------------------------------------------------------------------------


class TestStopRemoveJob:
    """Tests for stopping and removing agent Jobs."""

    def test_stop_agent_job(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test stopping an agent Job."""
        mock_k8s_client.stop_container.return_value = ContainerInfo(
            container_id="egg-issue-123-coder",
            container_name="egg-issue-123-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        result = spawner.stop_agent_job("egg-issue-123-coder")

        assert result.status == ContainerStatus.EXITED
        mock_k8s_client.stop_container.assert_called_once()

    def test_stop_agent_cleans_session(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test stopping also cleans up gateway session."""
        mock_k8s_client.stop_container.return_value = ContainerInfo(
            container_id="egg-issue-123-coder",
            container_name="egg-issue-123-coder",
            status=ContainerStatus.EXITED,
        )

        spawner.stop_agent_job("egg-issue-123-coder", cleanup_session=True)

        mock_gateway_client.delete_session_by_container.assert_called_once()

    def test_remove_agent_job(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test removing an agent Job."""
        spawner.remove_agent_job("egg-issue-123-coder")

        mock_k8s_client.remove_container.assert_called_once()
        mock_gateway_client.delete_session_by_container.assert_called_once()

    def test_remove_agent_job_without_session_cleanup(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test removing a Job without session cleanup."""
        spawner.remove_agent_job(
            "egg-issue-123-coder", cleanup_session=False
        )

        mock_k8s_client.remove_container.assert_called_once()
        mock_gateway_client.delete_session_by_container.assert_not_called()


# ---------------------------------------------------------------------------
# Pipeline listing tests
# ---------------------------------------------------------------------------


class TestPipelineListing:
    """Tests for listing pipeline Jobs."""

    def test_list_pipeline_jobs(self, spawner, mock_k8s_client):
        """Test listing all Jobs for a pipeline."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="egg-issue-123-coder",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.RUNNING,
            )
        ]

        jobs = spawner.list_pipeline_jobs("issue-123")

        assert len(jobs) == 1
        mock_k8s_client.list_containers.assert_called_once_with(
            labels={"egg.pipeline.id": "issue-123"},
        )


# ---------------------------------------------------------------------------
# Pipeline cleanup tests
# ---------------------------------------------------------------------------


class TestPipelineCleanup:
    """Tests for pipeline cleanup."""

    def test_cleanup_pipeline(self, spawner, mock_k8s_client, mock_gateway_client):
        """Test cleaning up all Jobs for a pipeline."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="egg-issue-123-coder",
                container_name="egg-issue-123-coder",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="egg-issue-123-tester",
                container_name="egg-issue-123-tester",
                status=ContainerStatus.RUNNING,
            ),
        ]

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR") as mock_wt:
            mock_wt.exists.return_value = False
            removed = spawner.cleanup_pipeline("issue-123")

        assert removed == 2

    def test_cleanup_continues_on_error(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test cleanup continues when individual Job removal fails."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="job1",
                container_name="job1",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="job2",
                container_name="job2",
                status=ContainerStatus.RUNNING,
            ),
        ]

        # First removal fails, second succeeds
        mock_k8s_client.remove_container.side_effect = [
            PodNotFoundError("not found"),
            None,
        ]

        with patch("kubernetes_spawner.WORKTREE_BASE_DIR") as mock_wt:
            mock_wt.exists.return_value = False
            removed = spawner.cleanup_pipeline("issue-123")

        # Only the successful one counts
        assert removed == 1


# ---------------------------------------------------------------------------
# Branch propagation tests
# ---------------------------------------------------------------------------


class TestBranchPropagation:
    """Tests for branch propagation to environment."""

    def test_branch_in_environment(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test explicit branch is set in environment."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            branch="egg/issue-123/work",
        )

        assert result.environment["EGG_BRANCH"] == "egg/issue-123/work"

    def test_default_branch_from_pipeline_id(
        self, spawner, mock_k8s_client, mock_gateway_client
    ):
        """Test default branch is derived from pipeline ID."""
        result = spawner.spawn_agent_job(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
        )

        assert result.environment["EGG_BRANCH"] == "egg/issue-123/work"
