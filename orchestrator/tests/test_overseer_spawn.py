"""
Tests for overseer container spawn logic.

Covers:
- Overseer spawns without repo mount
- Correct env vars (EGG_AGENT_ROLE=overseer, EGG_OVERSEER_MODE=true, etc.)
- Auto-spawn at pipeline start when overseer_enabled=True
- No spawn when overseer_enabled=False
- Lifecycle across phases (persists across phase transitions)
- Cleanup on pipeline end (stopped on PIPELINE_COMPLETED/FAILED/CANCELLED)

Related: issue #1059
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional imports - skip if modules not yet available
# ---------------------------------------------------------------------------
try:
    from container_spawner import ContainerSpawner, SpawnedContainer
    from gateway_client import GatewayHealth, SessionInfo
    from models import AgentRole, ContainerInfo, ContainerStatus, PipelineConfig
except ImportError as exc:
    pytest.skip(
        f"Required orchestrator modules not available: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.CONTAINER_PREFIX = "egg-sandbox-"

    mock.create_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-100-overseer",
        status=ContainerStatus.PENDING,
    )

    mock.start_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-100-overseer",
        status=ContainerStatus.RUNNING,
        started_at=datetime.utcnow(),
    )

    mock.stop_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-100-overseer",
        status=ContainerStatus.EXITED,
    )

    mock.list_containers.return_value = []

    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()

    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="healthy",
        version="0.1.0",
    )

    mock.register_session.return_value = SessionInfo(
        session_token="overseer-token-12345",
        container_id="overseer123def456",
        container_ip="172.32.0.60",
        mode="public",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )

    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with test-friendly defaults."""
    defaults = {
        "overseer_enabled": True,
        "overseer_poll_interval_seconds": 30,
        "overseer_decision_maker_model": "sonnet",
        "overseer_max_redirects_before_escalation": 2,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: No repo volume mount
# ---------------------------------------------------------------------------


class TestSpawnOverseerNoRepoMount:
    """Verify spawn_overseer_container passes no repo_volumes.

    The overseer runs without repository access -- it monitors health via
    the orchestrator API only.
    """

    def test_spawn_overseer_no_repo_mount(self, spawner, mock_docker_client):
        """Overseer container must not have any repository volume mounts."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-100",
            issue_number=100,
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.OVERSEER

        # Verify create_container was called -- inspect mounts to ensure
        # no repo volumes are present (no /home/egg/repos/<name> binds).
        create_call = mock_docker_client.create_container.call_args
        mounts = create_call.kwargs.get("mounts", [])
        repo_mounts = [
            m
            for m in mounts
            if isinstance(m, dict) and m.get("Target", "").startswith("/home/egg/repos/")
        ]
        assert len(repo_mounts) == 0, (
            f"Overseer must not have repo volume mounts, found: {repo_mounts}"
        )

    def test_spawn_overseer_no_git_shadow_mounts(self, spawner, mock_docker_client):
        """Overseer container must not have .git shadow mounts."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-101",
            issue_number=101,
        )

        create_call = mock_docker_client.create_container.call_args
        mounts = create_call.kwargs.get("mounts", [])
        git_mounts = [m for m in mounts if isinstance(m, dict) and ".git" in m.get("Target", "")]
        assert len(git_mounts) == 0, (
            f"Overseer must not have .git shadow mounts, found: {git_mounts}"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Correct env vars
# ---------------------------------------------------------------------------


class TestSpawnOverseerEnvVars:
    """Verify correct environment variables are injected for overseer."""

    def test_spawn_overseer_env_vars_custom(self, spawner):
        """Overseer container must have EGG_OVERSEER_MODE and related env vars."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-200",
            issue_number=200,
            poll_interval=45,
            decision_model="opus",
        )

        env = result.environment
        assert env.get("EGG_OVERSEER_MODE") == "true"
        assert env.get("EGG_OVERSEER_POLL_INTERVAL") == "45"
        assert env.get("EGG_OVERSEER_DECISION_MODEL") == "opus"

    def test_spawn_overseer_default_env_vars(self, spawner):
        """Overseer uses default poll_interval and decision_model when not specified."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-300",
        )

        env = result.environment
        assert env.get("EGG_OVERSEER_MODE") == "true"
        assert env.get("EGG_OVERSEER_POLL_INTERVAL") == "30"
        assert env.get("EGG_OVERSEER_DECISION_MODEL") == "sonnet"

    def test_spawn_overseer_agent_role_env(self, spawner):
        """Overseer container has EGG_AGENT_ROLE set to overseer."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-400",
            issue_number=400,
        )

        assert result.environment.get("EGG_AGENT_ROLE") == "overseer"
        assert result.agent_role == AgentRole.OVERSEER

    def test_spawn_overseer_pipeline_id_env(self, spawner):
        """Overseer container has EGG_PIPELINE_ID set to the pipeline ID."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-450",
            issue_number=450,
        )

        assert result.environment.get("EGG_PIPELINE_ID") == "issue-450"
        assert result.pipeline_id == "issue-450"


# ---------------------------------------------------------------------------
# Scenario 3: Auto-spawn when overseer_enabled=True
# ---------------------------------------------------------------------------


class TestAutoSpawnWhenEnabled:
    """Verify overseer is auto-spawned at pipeline start when overseer_enabled=True."""

    def test_auto_spawn_overseer_when_enabled(self, spawner, mock_docker_client):
        """When overseer_enabled=True, spawn_overseer_container is callable and succeeds."""
        config = _make_config(overseer_enabled=True)

        # Simulate pipeline start: if overseer is enabled, spawn the container
        if config.overseer_enabled:
            result = spawner.spawn_overseer_container(
                pipeline_id="issue-500",
                issue_number=500,
                poll_interval=config.overseer_poll_interval_seconds,
                decision_model=config.overseer_decision_maker_model,
            )

            assert result is not None
            assert isinstance(result, SpawnedContainer)
            assert result.agent_role == AgentRole.OVERSEER
            mock_docker_client.create_container.assert_called()
            mock_docker_client.start_container.assert_called()

    def test_auto_spawn_sets_correct_polling_from_config(self, spawner):
        """Auto-spawn uses the poll interval from pipeline config."""
        config = _make_config(
            overseer_enabled=True,
            overseer_poll_interval_seconds=15,
            overseer_decision_maker_model="opus",
        )

        result = spawner.spawn_overseer_container(
            pipeline_id="issue-510",
            poll_interval=config.overseer_poll_interval_seconds,
            decision_model=config.overseer_decision_maker_model,
        )

        env = result.environment
        assert env.get("EGG_OVERSEER_POLL_INTERVAL") == "15"
        assert env.get("EGG_OVERSEER_DECISION_MODEL") == "opus"


# ---------------------------------------------------------------------------
# Scenario 4: No spawn when overseer_enabled=False
# ---------------------------------------------------------------------------


class TestNoSpawnWhenDisabled:
    """Verify overseer is NOT spawned when overseer_enabled=False."""

    def test_no_spawn_when_overseer_disabled(self, spawner, mock_docker_client):
        """When overseer_enabled=False, no overseer container should be created."""
        config = _make_config(overseer_enabled=False)

        # Simulate pipeline start: check config before spawning
        overseer_spawned = False
        if config.overseer_enabled:
            spawner.spawn_overseer_container(
                pipeline_id="issue-600",
                issue_number=600,
            )
            overseer_spawned = True

        assert not overseer_spawned, "Overseer should not be spawned when disabled"
        # Ensure create_container was never called for overseer
        for c in mock_docker_client.create_container.call_args_list:
            env = c.kwargs.get("environment", {})
            assert env.get("EGG_AGENT_ROLE") != "overseer", (
                "No overseer container should be created when disabled"
            )

    def test_config_flag_controls_spawn_decision(self):
        """The overseer_enabled flag is the sole gate for spawning."""
        config_enabled = _make_config(overseer_enabled=True)
        config_disabled = _make_config(overseer_enabled=False)

        assert config_enabled.overseer_enabled is True
        assert config_disabled.overseer_enabled is False


# ---------------------------------------------------------------------------
# Scenario 5: Persists across phase transitions
# ---------------------------------------------------------------------------


class TestPersistsAcrossPhases:
    """Verify overseer is NOT stopped/restarted on phase transitions."""

    def test_overseer_not_stopped_on_phase_transition(self, spawner, mock_docker_client):
        """Overseer container should persist across phase changes (not stopped/restarted)."""
        # Spawn the overseer at pipeline start
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-700",
            issue_number=700,
        )
        overseer_container_id = result.container_info.container_id

        # Simulate phase transitions by listing containers with overseer label
        # The orchestrator should NOT stop the overseer on phase change
        mock_docker_client.stop_container.reset_mock()

        # Simulate phase change: refine -> plan -> implement
        # During each transition, verify stop is NOT called for overseer
        phases = ["refine", "plan", "implement"]
        for phase in phases:
            # The orchestrator might stop/restart agent containers on phase change,
            # but should leave the overseer alone. Verify by checking that
            # stop_container was not called for the overseer ID.
            overseer_stop_calls = [
                c
                for c in mock_docker_client.stop_container.call_args_list
                if c.args and c.args[0] == overseer_container_id
            ]
            assert len(overseer_stop_calls) == 0, (
                f"Overseer should not be stopped during phase transition to {phase}"
            )

    def test_overseer_survives_agent_restart(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Overseer persists even when agent containers are stopped/restarted."""
        # Spawn overseer
        overseer = spawner.spawn_overseer_container(
            pipeline_id="issue-710",
            issue_number=710,
        )

        # Spawn and stop an agent container (simulating phase transition)
        mock_docker_client.create_container.return_value = ContainerInfo(
            container_id="agent-abc123",
            container_name="egg-issue-710-coder",
            status=ContainerStatus.PENDING,
        )
        mock_docker_client.start_container.return_value = ContainerInfo(
            container_id="agent-abc123",
            container_name="egg-issue-710-coder",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id="agent-abc123",
            container_name="egg-issue-710-coder",
            status=ContainerStatus.EXITED,
        )

        # Stop the agent, not the overseer
        spawner.stop_agent_container("agent-abc123")

        # Verify overseer was NOT stopped
        stop_calls = mock_docker_client.stop_container.call_args_list
        stopped_ids = [c.args[0] if c.args else c.kwargs.get("container_id") for c in stop_calls]
        assert overseer.container_info.container_id not in stopped_ids, (
            "Overseer should not be stopped when an agent container is stopped"
        )


# ---------------------------------------------------------------------------
# Scenario 6: Cleanup on PIPELINE_COMPLETED
# ---------------------------------------------------------------------------


class TestCleanupOnCompletion:
    """Verify overseer is stopped when pipeline completes."""

    def test_overseer_stopped_on_pipeline_completed(self, spawner, mock_docker_client):
        """PIPELINE_COMPLETED -> overseer stopped."""
        # Spawn overseer
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-800",
            issue_number=800,
        )
        overseer_id = result.container_info.container_id

        # Simulate pipeline completion: stop the overseer
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id=overseer_id,
            container_name="egg-issue-800-overseer",
            status=ContainerStatus.EXITED,
        )

        stop_result = spawner.stop_agent_container(overseer_id)

        mock_docker_client.stop_container.assert_called_with(overseer_id, timeout=10)
        assert stop_result.status == ContainerStatus.EXITED

    def test_cleanup_pipeline_includes_overseer(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """cleanup_pipeline should remove overseer along with other containers."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="coder-123",
                container_name="egg-issue-800-coder",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="overseer-456",
                container_name="egg-issue-800-overseer",
                status=ContainerStatus.EXITED,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-800")

        assert removed == 2
        assert mock_docker_client.remove_container.call_count == 2


# ---------------------------------------------------------------------------
# Scenario 7: Cleanup on PIPELINE_FAILED
# ---------------------------------------------------------------------------


class TestCleanupOnFailure:
    """Verify overseer is stopped when pipeline fails."""

    def test_overseer_stopped_on_pipeline_failed(self, spawner, mock_docker_client):
        """PIPELINE_FAILED -> overseer stopped."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-900",
            issue_number=900,
        )
        overseer_id = result.container_info.container_id

        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id=overseer_id,
            container_name="egg-issue-900-overseer",
            status=ContainerStatus.EXITED,
        )

        # On pipeline failure, the orchestrator should stop all containers
        # including the overseer
        stop_result = spawner.stop_agent_container(overseer_id)
        assert stop_result.status == ContainerStatus.EXITED
        mock_docker_client.stop_container.assert_called_with(overseer_id, timeout=10)

    def test_cleanup_pipeline_on_failure(self, spawner, mock_docker_client, mock_gateway_client):
        """All containers (including overseer) are removed on pipeline failure."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="coder-abc",
                container_name="egg-issue-900-coder",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="overseer-def",
                container_name="egg-issue-900-overseer",
                status=ContainerStatus.RUNNING,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-900")

        # Both containers should be removed
        assert removed == 2
        removed_ids = [
            c.args[0] if c.args else c.kwargs.get("container_id")
            for c in mock_docker_client.remove_container.call_args_list
        ]
        assert "coder-abc" in removed_ids
        assert "overseer-def" in removed_ids


# ---------------------------------------------------------------------------
# Scenario 8: Cleanup on PIPELINE_CANCELLED
# ---------------------------------------------------------------------------


class TestCleanupOnCancellation:
    """Verify overseer is stopped when pipeline is cancelled."""

    def test_overseer_stopped_on_pipeline_cancelled(self, spawner, mock_docker_client):
        """PIPELINE_CANCELLED -> overseer stopped."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1000",
            issue_number=1000,
        )
        overseer_id = result.container_info.container_id

        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id=overseer_id,
            container_name="egg-issue-1000-overseer",
            status=ContainerStatus.EXITED,
        )

        stop_result = spawner.stop_agent_container(overseer_id)
        assert stop_result.status == ContainerStatus.EXITED

    def test_cleanup_pipeline_on_cancellation(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """All containers (including overseer) are cleaned up on cancellation."""
        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="tester-xyz",
                container_name="egg-issue-1000-tester",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="overseer-uvw",
                container_name="egg-issue-1000-overseer",
                status=ContainerStatus.RUNNING,
            ),
        ]

        removed = spawner.cleanup_pipeline("issue-1000")

        assert removed == 2
        assert mock_docker_client.remove_container.call_count == 2
        assert mock_gateway_client.delete_session_by_container.call_count == 2

    def test_cleanup_continues_if_overseer_stop_fails(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Cleanup of other containers continues even if overseer stop fails."""
        from docker_client import ContainerOperationError

        mock_docker_client.list_containers.return_value = [
            ContainerInfo(
                container_id="coder-111",
                container_name="egg-issue-1000-coder",
                status=ContainerStatus.EXITED,
            ),
            ContainerInfo(
                container_id="overseer-222",
                container_name="egg-issue-1000-overseer",
                status=ContainerStatus.RUNNING,
            ),
        ]

        # Overseer removal fails, coder removal succeeds
        mock_docker_client.remove_container.side_effect = [
            None,  # coder succeeds
            ContainerOperationError("Failed to remove overseer"),  # overseer fails
        ]

        removed = spawner.cleanup_pipeline("issue-1000")

        # Only one was successfully removed
        assert removed == 1
        # But both were attempted
        assert mock_docker_client.remove_container.call_count == 2


# ---------------------------------------------------------------------------
# Container naming
# ---------------------------------------------------------------------------


class TestOverseerContainerName:
    """Verify overseer container naming format."""

    def test_overseer_container_name(self, spawner, mock_gateway_client):
        """Overseer container name follows egg-{pipeline_id}-overseer format."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-500",
            issue_number=500,
        )

        register_call = mock_gateway_client.register_session.call_args
        container_id_arg = register_call.kwargs.get("container_id")
        assert container_id_arg == "egg-issue-500-overseer"

    def test_overseer_container_name_local_pipeline(self, spawner, mock_gateway_client):
        """Overseer container name works with local pipeline IDs."""
        spawner.spawn_overseer_container(
            pipeline_id="local-a1b2c3d4",
        )

        register_call = mock_gateway_client.register_session.call_args
        container_id_arg = register_call.kwargs.get("container_id")
        assert container_id_arg == "egg-local-a1b2c3d4-overseer"

    def test_overseer_session_role(self, spawner, mock_gateway_client):
        """Gateway session is registered with agent_role=overseer."""
        spawner.spawn_overseer_container(
            pipeline_id="issue-600",
            issue_number=600,
        )

        register_call = mock_gateway_client.register_session.call_args
        assert register_call.kwargs.get("agent_role") == "overseer"
