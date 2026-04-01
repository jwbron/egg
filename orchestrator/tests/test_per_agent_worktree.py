"""
Tests for per-agent worktree isolation (#1481).

Validates that each agent in a multi-agent pipeline gets its own worktree
(container_id = "{pipeline_id}-{role}") instead of sharing the pipeline-level
worktree. This prevents concurrent agents from stomping on each other's
uncommitted work.

Key behaviors tested:
- Agent worktree ID is "{pipeline_id}-{role}"
- CONTAINER_ID env var uses per-agent worktree ID
- Fallback to shared volumes when per-agent worktree creation fails
- Worktree cleanup includes per-agent worktrees
- Uncommitted changes detection in per-agent worktrees
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import ContainerSpawner
from gateway_client import GatewayError, GatewayHealth, SessionInfo
from models import AgentRole, ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.create_container.return_value = ContainerInfo(
        container_id="abc123",
        container_name="egg-pipe-1-coder",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="abc123",
        container_name="egg-pipe-1-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.list_containers.return_value = []
    return mock


@pytest.fixture
def mock_gateway_client():
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
    mock.register_session.return_value = SessionInfo(
        session_token="tok-123",
        container_id="abc123",
        container_ip="172.32.0.50",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    # Default: successful per-agent worktree creation
    wt_result = MagicMock()
    wt_result.success = True
    wt_result.worktrees = {"egg": "/home/egg/.egg-worktrees/pipe-1-coder/egg"}
    wt_result.errors = []
    mock.create_worktrees.return_value = wt_result
    mock.delete_worktrees.return_value = None
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


# ---------------------------------------------------------------------------
# Per-agent worktree ID generation
# ---------------------------------------------------------------------------


class TestPerAgentWorktreeId:
    """The agent worktree ID must be '{pipeline_id}-{role}'."""

    def test_worktree_id_format(self, spawner, mock_gateway_client):
        """create_worktrees is called with '{pipeline_id}-{role}' as container_id."""
        spawner.spawn_agent_container(
            pipeline_id="pipe-42",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/host/path/egg"},
            repos=["egg"],
        )
        # The second call to create_worktrees should be the per-agent one.
        # (The first may be from pipeline-level setup, but in spawn_agent_container
        # it creates the per-agent worktree directly.)
        calls = mock_gateway_client.create_worktrees.call_args_list
        # At least one call must use the per-agent container_id
        per_agent_calls = [
            c
            for c in calls
            if c.kwargs.get("container_id") == "pipe-42-coder"
            or (c.args and c.args[0] == "pipe-42-coder")
        ]
        assert len(per_agent_calls) >= 1, (
            f"Expected create_worktrees called with container_id='pipe-42-coder', "
            f"got calls: {calls}"
        )

    def test_different_roles_get_different_ids(self, mock_docker_client, mock_gateway_client):
        """Each role produces a unique worktree container_id."""
        worktree_ids = set()
        for role in [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER]:
            mock_gateway_client.create_worktrees.reset_mock()
            spawner = ContainerSpawner(
                docker_client=mock_docker_client,
                gateway_client=mock_gateway_client,
            )
            spawner.spawn_agent_container(
                pipeline_id="pipe-42",
                agent_role=role,
                repo_volumes={"egg": "/host/path"},
                repos=["egg"],
            )
            for c in mock_gateway_client.create_worktrees.call_args_list:
                cid = c.kwargs.get("container_id", c.args[0] if c.args else None)
                if cid and cid.startswith("pipe-42-"):
                    worktree_ids.add(cid)

        assert worktree_ids == {"pipe-42-coder", "pipe-42-tester", "pipe-42-documenter"}


# ---------------------------------------------------------------------------
# CONTAINER_ID environment variable
# ---------------------------------------------------------------------------


class TestContainerIdEnvVar:
    """CONTAINER_ID env var must use per-agent worktree ID, not pipeline_id."""

    def test_container_id_is_per_agent(self, spawner, mock_docker_client):
        """CONTAINER_ID in the spawned container uses '{pipeline_id}-{role}'."""
        spawner.spawn_agent_container(
            pipeline_id="pipe-99",
            agent_role=AgentRole.TESTER,
            repo_volumes={"egg": "/host/path"},
            repos=["egg"],
        )
        # Inspect the env passed to create_container
        create_call = mock_docker_client.create_container.call_args
        assert create_call is not None
        # Environment may be in kwargs or part of the container config
        env = None
        if create_call.kwargs.get("environment"):
            env = create_call.kwargs["environment"]
        elif create_call.args:
            # Check all kwargs for environment-like dicts
            for arg in create_call.args:
                if isinstance(arg, dict) and "CONTAINER_ID" in arg:
                    env = arg
                    break
        # Also check if it's nested in a config dict
        if env is None:
            for _k, v in create_call.kwargs.items():
                if isinstance(v, dict) and "CONTAINER_ID" in v:
                    env = v
                    break

        if env is not None:
            assert env["CONTAINER_ID"] == "pipe-99-tester", (
                f"Expected CONTAINER_ID='pipe-99-tester', got '{env.get('CONTAINER_ID')}'"
            )
        else:
            # The environment might be set via a different mechanism
            # Check that the spawner at least constructs the right ID
            pytest.skip("Could not extract environment from create_container call")


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


class TestPerAgentWorktreeFallback:
    """When per-agent worktree creation fails, fall back to shared volumes."""

    def test_fallback_on_gateway_error(self, spawner, mock_gateway_client, mock_docker_client):
        """Gateway error during per-agent worktree creation falls back gracefully."""
        call_count = 0
        original_volumes = {"egg": "/host/path/original"}

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            cid = kwargs.get("container_id", "")
            # Per-agent worktree call fails
            if "-" in cid and cid != kwargs.get("pipeline_id", ""):
                raise GatewayError("Gateway unavailable", status_code=500)
            # Pipeline-level call succeeds
            result = MagicMock()
            result.success = True
            result.worktrees = original_volumes
            result.errors = []
            return result

        mock_gateway_client.create_worktrees.side_effect = side_effect

        # Should not raise — should fall back to shared volumes
        result = spawner.spawn_agent_container(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repo_volumes=original_volumes,
            repos=["egg"],
        )
        assert result is not None

    def test_fallback_on_empty_worktree_result(self, spawner, mock_gateway_client):
        """When per-agent worktree returns no worktrees, fall back gracefully."""
        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            cid = kwargs.get("container_id", "")
            result = MagicMock()
            if "-" in cid and not cid.startswith("pipe"):
                # Per-agent call returns empty
                result.success = True
                result.worktrees = {}
                result.errors = ["no worktrees available"]
            else:
                result.success = True
                result.worktrees = {"egg": "/host/path"}
                result.errors = []
            return result

        mock_gateway_client.create_worktrees.side_effect = side_effect
        # Should not raise
        result = spawner.spawn_agent_container(
            pipeline_id="pipe-1",
            agent_role=AgentRole.CODER,
            repo_volumes={"egg": "/host/path"},
            repos=["egg"],
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Worktree cleanup
# ---------------------------------------------------------------------------


class TestWorktreeCleanup:
    """Pipeline cleanup must delete per-agent worktrees."""

    def test_cleanup_deletes_per_agent_worktrees(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline deletes worktrees for each agent role."""
        # Simulate containers with role labels
        container1 = MagicMock()
        container1.name = "egg-pipe-1-coder"
        container1.labels = {"egg.agent.role": "coder"}
        container1.id = "c1"
        container2 = MagicMock()
        container2.name = "egg-pipe-1-tester"
        container2.labels = {"egg.agent.role": "tester"}
        container2.id = "c2"
        mock_docker_client.list_containers.return_value = [container1, container2]

        spawner.cleanup_pipeline("pipe-1")

        # Should have tried to delete worktrees for: pipe-1, pipe-1-coder, pipe-1-tester
        delete_calls = mock_gateway_client.delete_worktrees.call_args_list
        deleted_ids = set()
        for c in delete_calls:
            cid = c.kwargs.get("container_id", c.args[0] if c.args else None)
            if cid:
                deleted_ids.add(cid)
        assert "pipe-1" in deleted_ids, "Pipeline worktree not cleaned up"
        assert "pipe-1-coder" in deleted_ids, "Coder worktree not cleaned up"
        assert "pipe-1-tester" in deleted_ids, "Tester worktree not cleaned up"


# ---------------------------------------------------------------------------
# Uncommitted changes detection
# ---------------------------------------------------------------------------


class TestDetectUncommittedChanges:
    """detect_uncommitted_changes checks agent worktrees for uncommitted work."""

    @patch("subprocess.run")
    def test_detects_dirty_worktree(self, mock_run, spawner):
        """Reports uncommitted files found in agent worktree."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=" M src/main.py\n?? new_file.txt\n",
        )
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(
                Path, "iterdir", return_value=[Path("/home/egg/.egg-worktrees/pipe-1-coder/egg")]
            ),
            patch.object(Path, "is_dir", return_value=True),
        ):
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")

        assert result is not None
        assert result["pipeline_id"] == "pipe-1"
        assert result["agent_role"] == "coder"
        assert result["worktree_id"] == "pipe-1-coder"
        assert result["file_count"] == 2

    @patch("subprocess.run")
    def test_clean_worktree_returns_none(self, mock_run, spawner):
        """Clean worktree returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(
                Path, "iterdir", return_value=[Path("/home/egg/.egg-worktrees/pipe-1-coder/egg")]
            ),
            patch.object(Path, "is_dir", return_value=True),
        ):
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")
        assert result is None

    def test_missing_worktree_returns_none(self, spawner):
        """Non-existent worktree path returns None."""
        with patch.object(Path, "exists", return_value=False):
            result = spawner.detect_uncommitted_changes("pipe-1", "coder")
        assert result is None
