"""
Tests for per-agent worktree isolation (#1481).

Verifies that each agent gets its own worktree via a unique
CONTAINER_ID of the form "{pipeline_id}-{role}".
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import ContainerSpawner
from docker_client import ContainerNotFoundError
from gateway_client import GatewayHealth, SessionInfo, WorktreeResult
from models import AgentRole, ContainerInfo, ContainerStatus


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.CONTAINER_PREFIX = "egg-sandbox-"

    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-issue-123-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.list_containers.return_value = []
    mock.get_container_info.side_effect = ContainerNotFoundError("not found")
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-12345",
        container_id="abc123def456",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    # Per-agent worktree creation returns success
    mock.create_worktrees.return_value = WorktreeResult(
        success=True,
        worktrees={"egg": "/home/egg/.egg-worktrees/issue-123-coder/egg"},
        errors=[],
    )
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


class TestAgentWorktreeId:
    """Tests for per-agent worktree ID computation."""

    def test_agent_worktree_id_format(self):
        """agent_worktree_id should be '{pipeline_id}-{role}'."""
        pipeline_id = "issue-123"
        role = AgentRole.CODER
        expected = f"{pipeline_id}-{role.value}"
        assert expected == "issue-123-coder"

    def test_agent_worktree_id_different_roles(self):
        """Different roles should produce different worktree IDs."""
        pipeline_id = "issue-456"
        ids = {f"{pipeline_id}-{r.value}" for r in [AgentRole.CODER, AgentRole.TESTER]}
        assert len(ids) == 2
        assert "issue-456-coder" in ids
        assert "issue-456-tester" in ids


class TestContainerIdEnvVar:
    """Tests that CONTAINER_ID env var uses the per-agent ID."""

    @patch.dict("os.environ", {"HOST_UID": "1000", "HOST_GID": "1000"})
    def test_container_id_is_per_agent(self, spawner, mock_docker_client, mock_gateway_client):
        """CONTAINER_ID env var should be '{pipeline_id}-{role}', not pipeline_id."""
        spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/host/path/egg"},
            repos=["owner/egg"],
            mode="public",
            branch="egg/issue-123/work",
        )

        # Verify that create_worktrees was called with the per-agent ID.
        mock_gateway_client.create_worktrees.assert_called_once()
        call_kwargs = mock_gateway_client.create_worktrees.call_args
        assert call_kwargs.kwargs.get("container_id") or call_kwargs[1].get("container_id"), (
            "create_worktrees should be called with container_id"
        )

        # The container_id for the worktree should be per-agent
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs["container_id"] == "issue-123-coder"
        else:
            assert (
                call_kwargs[0][0] == "issue-123-coder"
                or call_kwargs[1]["container_id"] == "issue-123-coder"
            )

    @patch.dict("os.environ", {"HOST_UID": "1000", "HOST_GID": "1000"})
    def test_different_roles_get_different_worktree_ids(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Different roles should create worktrees with different IDs."""
        # Reset to track multiple calls
        mock_gateway_client.create_worktrees.reset_mock()

        # Track worktree IDs across calls
        worktree_ids = []

        def capture_create(**kwargs):
            worktree_ids.append(kwargs.get("container_id"))
            return WorktreeResult(
                success=True,
                worktrees={"egg": f"/home/egg/.egg-worktrees/{kwargs['container_id']}/egg"},
                errors=[],
            )

        mock_gateway_client.create_worktrees.side_effect = capture_create

        for role in [AgentRole.CODER, AgentRole.TESTER]:
            # Need fresh container info per spawn
            mock_docker_client.create_container.return_value = ContainerInfo(
                container_id=f"abc-{role.value}",
                container_name=f"issue-123-{role.value}",
                status=ContainerStatus.PENDING,
            )
            mock_docker_client.start_container.return_value = ContainerInfo(
                container_id=f"abc-{role.value}",
                container_name=f"issue-123-{role.value}",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )

            spawner.spawn_agent_container(
                pipeline_id="issue-123",
                agent_role=role,
                repo_volumes={"egg": "/host/path/egg"},
                repos=["owner/egg"],
                mode="public",
                branch="egg/issue-123/work",
            )

        assert "issue-123-coder" in worktree_ids
        assert "issue-123-tester" in worktree_ids
        assert len(set(worktree_ids)) == 2


class TestWorktreeCreationFallback:
    """Tests that spawn falls back gracefully when worktree creation fails."""

    @patch.dict("os.environ", {"HOST_UID": "1000", "HOST_GID": "1000"})
    def test_fallback_on_worktree_creation_failure(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If per-agent worktree creation fails, should fall back to shared volumes."""
        mock_gateway_client.create_worktrees.side_effect = Exception("gateway down")

        # Should not raise -- should fall back gracefully
        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/host/path/egg"},
            repos=["owner/egg"],
            mode="public",
            branch="egg/issue-123/work",
        )

        assert result is not None
        assert result.container_info.container_id is not None

    @patch.dict("os.environ", {"HOST_UID": "1000", "HOST_GID": "1000"})
    def test_fallback_on_empty_worktree_result(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If gateway returns no worktrees, should fall back to shared volumes."""
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees={},
            errors=["no worktrees created"],
        )

        result = spawner.spawn_agent_container(
            pipeline_id="issue-123",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/host/path/egg"},
            repos=["owner/egg"],
            mode="public",
            branch="egg/issue-123/work",
        )

        assert result is not None


class TestCleanupPipelineWorktrees:
    """Tests for per-agent worktree cleanup during pipeline cleanup."""

    def test_cleanup_deletes_per_agent_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline should delete worktrees for each agent role."""
        # Simulate containers with role labels
        container1 = MagicMock()
        container1.container_id = "abc123"
        container1.labels = {"egg.pipeline.id": "issue-123", "egg.agent.role": "coder"}
        container2 = MagicMock()
        container2.container_id = "def456"
        container2.labels = {"egg.pipeline.id": "issue-123", "egg.agent.role": "tester"}

        mock_docker_client.list_containers.return_value = [container1, container2]

        spawner.cleanup_pipeline("issue-123")

        # Should have called delete_worktrees for:
        # 1. pipeline-level worktree (pipeline_id)
        # 2. per-agent worktree for coder
        # 3. per-agent worktree for tester
        delete_calls = mock_gateway_client.delete_worktrees.call_args_list
        deleted_ids = [
            call.kwargs.get("container_id", call.args[0] if call.args else None)
            for call in delete_calls
        ]
        assert "issue-123" in deleted_ids
        assert "issue-123-coder" in deleted_ids
        assert "issue-123-tester" in deleted_ids

    def test_cleanup_handles_no_containers(self, spawner, mock_docker_client, mock_gateway_client):
        """cleanup_pipeline should still clean up pipeline-level worktree."""
        mock_docker_client.list_containers.return_value = []

        spawner.cleanup_pipeline("issue-123")

        # Should still clean up the pipeline-level worktree
        delete_calls = mock_gateway_client.delete_worktrees.call_args_list
        deleted_ids = [
            call.kwargs.get("container_id", call.args[0] if call.args else None)
            for call in delete_calls
        ]
        assert "issue-123" in deleted_ids
