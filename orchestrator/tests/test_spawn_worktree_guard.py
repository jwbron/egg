"""Tests for the worktree creation guard in spawn_agent_container.

Issue #1597: restart_phase and restart_agent call spawn_agent_container
without repo_volumes, causing per-agent worktree creation to be skipped.
The fix changes the guard from ``if repo_volumes and repos:`` to
``if repos:`` so that worktrees are always created when repos are
specified — even when repo_volumes is None (the restart path).

These tests verify:
- Worktree creation triggers when repos is provided but repo_volumes is None
- Worktree creation still works when both repo_volumes and repos are provided
- No worktree creation when repos is None/empty
- Gateway worktree errors propagate correctly
- The restart_agent_container path inherits the fix
- Mounts are correctly populated from gateway worktree results
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
)
from docker_client import ContainerNotFoundError
from gateway_client import GatewayError, GatewayHealth, SessionInfo, WorktreeResult
from models import AgentRole, ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123def456",
        container_name="egg-sandbox-egg-agent-issue-200-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.list_containers.return_value = []

    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client with worktree support."""
    mock = MagicMock()

    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="healthy",
        version="0.1.0",
    )
    mock.register_session.return_value = SessionInfo(
        session_token="test-token-12345",
        container_id="abc123def456",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    # Default: successful worktree creation
    mock.create_worktrees.return_value = WorktreeResult(
        success=True,
        worktrees={"my-repo": "/host/worktrees/issue-200-coder/my-repo"},
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


# ---------------------------------------------------------------------------
# Core fix tests: repos without repo_volumes triggers worktree creation
# ---------------------------------------------------------------------------


class TestWorktreeCreationGuard:
    """Tests for the worktree creation guard condition.

    The fix changes ``if repo_volumes and repos:`` to ``if repos:`` so
    that the restart path (which passes repos but not repo_volumes)
    correctly creates/reuses worktrees.
    """

    def test_spawn_with_repos_but_no_repo_volumes_creates_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Spawn with repos but no repo_volumes should call create_worktrees.

        This is the primary scenario fixed by issue #1597: restart_phase
        and restart_agent pass repos but not repo_volumes.
        """
        result = spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,  # Explicitly None — the restart path
        )

        # Verify gateway.create_worktrees was called
        mock_gateway_client.create_worktrees.assert_called_once()
        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs["repos"] == ["owner/my-repo"]
        assert call_kwargs["container_id"] == "issue-200-coder"

        # Verify the result has the correct worktree environment
        assert isinstance(result, SpawnedContainer)
        # In K8s, volumes are handled by pod templates, not Docker-style mounts.
        # Verify the container was created with the correct environment instead.
        create_call = mock_docker_client.create_container.call_args
        env = create_call.kwargs.get("environment", {})
        assert env.get("CONTAINER_ID") == "issue-200-coder", (
            "CONTAINER_ID must use per-agent worktree ID when worktree is created from repos-only path"
        )

    def test_spawn_with_both_repos_and_repo_volumes_creates_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Spawn with both repos and repo_volumes should still create worktrees.

        This is the existing behavior (initial spawn path via
        create_concurrent_spawn_fn) and must not regress.
        """
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes={"my-repo": "/host/path/to/repo"},
        )

        # create_worktrees should be called (the original repo_volumes is overwritten)
        mock_gateway_client.create_worktrees.assert_called_once()

    def test_spawn_without_repos_does_not_create_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Spawn without repos should not call create_worktrees.

        When repos is None or empty, there is nothing to create.
        """
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=None,
            repo_volumes=None,
        )

        mock_gateway_client.create_worktrees.assert_not_called()

    def test_spawn_with_empty_repos_does_not_create_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Spawn with empty repos list should not call create_worktrees."""
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=[],
            repo_volumes=None,
        )

        mock_gateway_client.create_worktrees.assert_not_called()

    def test_spawn_repos_only_overwrites_repo_volumes(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """When repos is provided, gateway result should populate repo_volumes.

        The gateway's create_worktrees returns host paths; these should
        be used as the worktree source regardless of the input repo_volumes.
        In K8s, volume mounting is handled by pod templates, but the worktree
        creation via gateway should still be called correctly.
        """
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees={"my-repo": "/host/worktrees/issue-200-coder/my-repo"},
            errors=[],
        )

        result = spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,
        )

        # Verify create_worktrees was called and the result is a valid SpawnedContainer
        mock_gateway_client.create_worktrees.assert_called_once()
        assert isinstance(result, SpawnedContainer)
        # Verify CONTAINER_ID uses per-agent worktree ID
        assert result.environment.get("CONTAINER_ID") == "issue-200-coder"

    def test_spawn_repos_only_sets_correct_container_id(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """CONTAINER_ID env var should use per-agent worktree ID for repos-only path.

        In K8s, .git shadow mounts are not used (git isolation is handled by
        NetworkPolicy and gateway). Instead, verify CONTAINER_ID is correct.
        """
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees={"my-repo": "/host/worktrees/issue-200-coder/my-repo"},
            errors=[],
        )

        result = spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,
        )

        assert result.environment.get("CONTAINER_ID") == "issue-200-coder"
        assert result.environment.get("EGG_AGENT_ROLE") == "coder"

    def test_spawn_without_base_branch_passes_none_to_create_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Without explicit base_branch, create_worktrees gets None (gateway default)."""
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,
            branch="egg/issue-200",
        )

        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs.get("base_branch") is None

    def test_spawn_with_base_branch_passes_it_to_create_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Explicit base_branch (restart path) is forwarded to create_worktrees."""
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,
            branch="egg/issue-200",
            base_branch="egg/issue-200",
        )

        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs.get("base_branch") == "egg/issue-200"

    def test_spawn_passes_branch_as_assigned_branch_to_create_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """branch is forwarded as assigned_branch so the per-agent worktree
        is configured to push to the pipeline's shared branch (#1809).

        Without this, the sandbox's push client builds a refspec targeting
        the per-agent local branch name, which the gateway rejects as
        push_denied_wrong_branch.
        """
        spawner.spawn_agent_container(
            pipeline_id="issue-1759-v3",
            agent_role=AgentRole.CODER,
            issue_number=1759,
            repos=["owner/my-repo"],
            repo_volumes=None,
            branch="egg/issue-1759-v3",
            base_branch="main",
        )

        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs.get("assigned_branch") == "egg/issue-1759-v3"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestWorktreeCreationErrors:
    """Tests for error handling during worktree creation."""

    def test_spawn_raises_on_gateway_worktree_error(self, spawner, mock_gateway_client):
        """GatewayError during create_worktrees should propagate as ContainerSpawnError."""
        mock_gateway_client.create_worktrees.side_effect = GatewayError(
            "Worktree creation failed",
            status_code=500,
        )

        with pytest.raises(ContainerSpawnError, match="worktree creation failed"):
            spawner.spawn_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                repos=["owner/my-repo"],
                repo_volumes=None,
            )

    def test_spawn_raises_when_worktree_result_has_no_worktrees(self, spawner, mock_gateway_client):
        """Empty worktree result should raise ContainerSpawnError."""
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees={},
            errors=[],
        )

        with pytest.raises(ContainerSpawnError, match="no worktrees"):
            spawner.spawn_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                repos=["owner/my-repo"],
                repo_volumes=None,
            )

    def test_spawn_raises_when_worktree_result_is_failure(self, spawner, mock_gateway_client):
        """Failed worktree result should raise ContainerSpawnError."""
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=False,
            worktrees={},
            errors=["Repo not found"],
        )

        with pytest.raises(ContainerSpawnError, match="no worktrees"):
            spawner.spawn_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                repos=["owner/my-repo"],
                repo_volumes=None,
            )

    def test_spawn_raises_on_unexpected_exception_during_worktree_creation(
        self, spawner, mock_gateway_client
    ):
        """Unexpected exceptions during create_worktrees should raise ContainerSpawnError."""
        mock_gateway_client.create_worktrees.side_effect = RuntimeError("Unexpected error")

        with pytest.raises(ContainerSpawnError, match="Unexpected error"):
            spawner.spawn_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                repos=["owner/my-repo"],
                repo_volumes=None,
            )


# ---------------------------------------------------------------------------
# Integration with restart paths
# ---------------------------------------------------------------------------


class TestRestartWorktreeIntegration:
    """Tests verifying restart paths trigger worktree creation.

    These tests verify that restart_agent_container (which passes repos
    but not repo_volumes) correctly triggers worktree creation via the
    fixed guard condition.
    """

    def test_restart_agent_container_creates_worktrees_from_repos(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """restart_agent_container with repos should create worktrees.

        restart_agent_container calls spawn_agent_container internally
        without repo_volumes. After the fix, the spawn should still
        create worktrees because repos is provided.
        """
        # Setup: get_container_info for cleanup phase
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id="old-container-abc",
            container_name="egg-sandbox-egg-issue-200-coder",
            status=ContainerStatus.RUNNING,
        )
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="old-container-abc",
            container_name="egg-issue-200-coder",
            status=ContainerStatus.EXITED,
        )

        result = spawner.restart_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            mode="public",
            repos=["owner/my-repo"],
            # repo_volumes intentionally omitted — this is the restart path
        )

        # Verify gateway.create_worktrees was called
        mock_gateway_client.create_worktrees.assert_called_once()
        assert isinstance(result, SpawnedContainer)

        # Verify CONTAINER_ID uses per-agent worktree ID
        assert result.environment.get("CONTAINER_ID") == "issue-200-coder"

    def test_restart_container_not_found_still_creates_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """restart_agent_container should create worktrees even if old container is gone.

        This covers the case where the container already exited/was removed
        before the restart — worktree creation should still happen.
        """
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError(
            "already removed"
        )

        result = spawner.restart_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            mode="public",
            repos=["owner/my-repo"],
        )

        mock_gateway_client.create_worktrees.assert_called_once()
        assert isinstance(result, SpawnedContainer)

    def test_restart_passes_preserve_worktree_on_failure(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """restart_agent_container should pass preserve_worktree_on_failure=True.

        This ensures a transient Docker failure doesn't delete the
        pre-existing worktree containing committed work.
        """
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError(
            "already removed"
        )

        with patch.object(spawner, "spawn_agent_job", wraps=spawner.spawn_agent_job) as mock_spawn:
            spawner.restart_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                mode="public",
                repos=["owner/my-repo"],
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs.get("preserve_worktree_on_failure") is True

    def test_restart_agent_container_forwards_base_branch(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """restart_agent_container should forward base_branch to spawn_agent_container.

        Whatever ``base_branch`` the route chooses (``pipeline.base_branch``
        for pipeline-level / root-slice restarts, the parent slice's
        integration branch for child-slice restarts; see #2439) must
        propagate through to the gateway worktree-create call, otherwise
        the worktree-absent restart path forks from the wrong ref.
        """
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError(
            "already removed"
        )

        with patch.object(spawner, "spawn_agent_job", wraps=spawner.spawn_agent_job) as mock_spawn:
            spawner.restart_agent_container(
                pipeline_id="issue-200",
                agent_role=AgentRole.CODER,
                issue_number=200,
                mode="public",
                repos=["owner/my-repo"],
                branch="egg/issue-200",
                base_branch="egg/issue-200",
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs.get("base_branch") == "egg/issue-200"

        # Also verify it reached the gateway
        gw_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert gw_kwargs.get("base_branch") == "egg/issue-200"


# ---------------------------------------------------------------------------
# Concurrent spawn factory
# ---------------------------------------------------------------------------


class TestConcurrentSpawnFnBaseBranch:
    """Tests that create_concurrent_spawn_fn captures and forwards base_branch.

    The initial creation path (_run_concurrent_phase) uses this factory to
    spawn agents. base_branch must be captured in the closure and forwarded
    to spawn_agent_container so the gateway bases worktrees on the correct
    branch (pipeline.base_branch for initial creation).
    """

    def test_concurrent_spawn_fn_forwards_base_branch(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """The closure returned by create_concurrent_spawn_fn should forward base_branch."""
        spawn_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="issue-200",
            issue_number=200,
            repo_volumes=None,
            mode="public",
            repos=["owner/my-repo"],
            phase=None,
            base_branch="main",
        )

        spawn_fn(role=AgentRole.CODER, branch="egg/issue-200")

        gw_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert gw_kwargs.get("base_branch") == "main"

    def test_concurrent_spawn_fn_without_base_branch_passes_none(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Without base_branch, the closure should pass None to spawn_agent_container."""
        spawn_fn = spawner.create_concurrent_spawn_fn(
            pipeline_id="issue-200",
            issue_number=200,
            repo_volumes=None,
            mode="public",
            repos=["owner/my-repo"],
            phase=None,
            # base_branch omitted — defaults to None
        )

        spawn_fn(role=AgentRole.CODER, branch="egg/issue-200")

        gw_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert gw_kwargs.get("base_branch") is None


# ---------------------------------------------------------------------------
# Multiple repos
# ---------------------------------------------------------------------------


class TestWorktreeMultipleRepos:
    """Tests for worktree creation with multiple repositories."""

    def test_spawn_with_multiple_repos_creates_all_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Multiple repos should all get worktrees via gateway."""
        mock_gateway_client.create_worktrees.return_value = WorktreeResult(
            success=True,
            worktrees={
                "repo-a": "/host/worktrees/issue-200-coder/repo-a",
                "repo-b": "/host/worktrees/issue-200-coder/repo-b",
            },
            errors=[],
        )

        result = spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=AgentRole.CODER,
            issue_number=200,
            repos=["owner/repo-a", "owner/repo-b"],
            repo_volumes=None,
        )

        # In K8s, volume mounting is handled by pod templates.
        # Verify worktree creation was called and spawn succeeded.
        mock_gateway_client.create_worktrees.assert_called_once()
        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs["repos"] == ["owner/repo-a", "owner/repo-b"]
        assert isinstance(result, SpawnedContainer)


# ---------------------------------------------------------------------------
# Agent role in worktree ID
# ---------------------------------------------------------------------------


class TestWorktreeAgentRoleId:
    """Tests that the per-agent worktree ID is correctly formed."""

    @pytest.mark.parametrize(
        "role,expected_wt_id",
        [
            (AgentRole.CODER, "issue-200-coder"),
            (AgentRole.TESTER, "issue-200-tester"),
            (AgentRole.DOCUMENTER, "issue-200-documenter"),
        ],
    )
    def test_worktree_id_includes_pipeline_and_role(
        self, spawner, mock_gateway_client, role, expected_wt_id
    ):
        """Worktree container_id should be {pipeline_id}-{role}."""
        spawner.spawn_agent_container(
            pipeline_id="issue-200",
            agent_role=role,
            issue_number=200,
            repos=["owner/my-repo"],
            repo_volumes=None,
        )

        call_kwargs = mock_gateway_client.create_worktrees.call_args.kwargs
        assert call_kwargs["container_id"] == expected_wt_id
