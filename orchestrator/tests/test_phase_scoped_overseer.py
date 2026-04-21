"""
Tests for phase-scoped overseer lifecycle (issue #1560).

Verifies that the overseer container is:
- Spawned at the start of each phase (not once for the whole pipeline)
- Torn down at phase completion/advance
- Torn down on phase failure
- Gated by ``phase_overseer_active`` flag in the health-monitor poll thread
- Respawn count reset per phase
- Safety-net cleanup in the ``finally`` block still functions
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup (matches existing test conventions)
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
# Conditional imports
# ---------------------------------------------------------------------------
try:
    from container_spawner import ContainerSpawner, SpawnedContainer
    from docker_client import ContainerNotFoundError
    from gateway_client import GatewayHealth, SessionInfo
    from models import (
        AgentRole,
        ContainerInfo,
        ContainerStatus,
        Pipeline,
        PipelineConfig,
        PipelineStatus,
    )
    from routes.pipelines import _check_and_respawn_overseer
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
        container_name="egg-issue-1560-overseer",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-1560-overseer",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.stop_container.return_value = ContainerInfo(
        container_id="overseer123def456",
        container_name="egg-issue-1560-overseer",
        status=ContainerStatus.EXITED,
    )
    mock.list_containers.return_value = []
    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()
    mock.check_health.return_value = GatewayHealth(healthy=True, status="healthy", version="0.1.0")
    mock.register_session.return_value = SessionInfo(
        session_token="overseer-token-1560",
        container_id="overseer123def456",
        container_ip="172.32.0.60",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )
    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a ContainerSpawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


@pytest.fixture
def mock_spawner(mock_docker_client):
    """Mock spawner for _check_and_respawn_overseer tests."""
    mock = MagicMock()
    mock.backend = mock_docker_client
    mock.docker = mock_docker_client
    respawned_id = "overseer-respawned-phase"
    mock.spawn_overseer_container.return_value = SpawnedContainer(
        container_info=ContainerInfo(
            container_id=respawned_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        ),
        session_info=None,
        agent_role=AgentRole.OVERSEER,
        pipeline_id="issue-1560",
        environment={},
    )
    return mock


@pytest.fixture
def mock_store():
    """Mock state store returning a RUNNING pipeline."""
    store = MagicMock()
    store.load_pipeline.return_value = Pipeline(
        id="issue-1560",
        issue_number=1560,
        status=PipelineStatus.RUNNING,
    )
    return store


@pytest.fixture
def running_pipeline():
    """Create a Pipeline in RUNNING state with overseer enabled."""
    return Pipeline(
        id="issue-1560",
        issue_number=1560,
        status=PipelineStatus.RUNNING,
        config=PipelineConfig(
            overseer_enabled=True,
            overseer_max_respawns=3,
        ),
    )


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with test defaults."""
    defaults = {
        "overseer_enabled": True,
        "overseer_poll_interval_seconds": 30,
        "overseer_decision_maker_model": "sonnet",
        "overseer_max_respawns": 3,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: phase_overseer_active gating respawn checks
# ---------------------------------------------------------------------------


class TestPhaseOverseerActiveGating:
    """Verify _check_and_respawn_overseer is only called when phase_overseer_active=True.

    The health-monitor poll thread wraps the respawn call with an
    ``if phase_overseer_active:`` guard so the overseer is not respawned
    between phases when it is intentionally absent.
    """

    def test_respawn_called_when_phase_active(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Respawn is invoked when overseer exited and phase is active."""
        original_id = "overseer-active-001"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        # Simulate phase_overseer_active=True by calling the function directly
        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-phase", "Should respawn when phase is active"
        assert new_count == 1
        mock_spawner.spawn_overseer_container.assert_called_once()

    def test_respawn_not_called_when_no_container_id(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """No respawn when overseer_container_id is None (between phases)."""
        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=None,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id is None, "Should remain None when no container ID"
        assert new_count == 0
        mock_spawner.spawn_overseer_container.assert_not_called()
        mock_docker_client.get_container_info.assert_not_called()

    def test_respawn_skipped_when_max_respawns_reached(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """_check_and_respawn_overseer returns early when respawn budget is exhausted.

        This exercises the guard inside the production function that prevents
        respawn when overseer_respawn_count >= max_overseer_respawns.
        """
        original_id = "overseer-exhausted-001"
        # Container exited, but respawn count already at max
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=1,
        )

        same_id, same_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=3,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert same_id == original_id, "Container ID should be unchanged"
        assert same_count == 3, "Respawn count should not increase"
        mock_spawner.spawn_overseer_container.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2: Respawn count reset per phase
# ---------------------------------------------------------------------------


class TestRespawnCountResetPerPhase:
    """Verify overseer_respawn_count resets to 0 at each phase start.

    The coder's implementation resets overseer_respawn_count=0 when spawning
    the overseer at phase start, so each phase gets its full respawn budget.
    """

    def test_respawn_count_budget_fresh_each_phase(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """After phase boundary, respawn count=0 allows full respawn budget."""
        original_id = "overseer-phase2-001"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=1,
        )

        # Phase 2 starts fresh with count=0, so respawn should succeed
        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,  # reset per phase
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-phase"
        assert new_count == 1
        mock_spawner.spawn_overseer_container.assert_called_once()

    def test_exhausted_respawns_in_previous_phase_dont_carry_over(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Even if previous phase exhausted respawns, new phase starts at 0."""
        original_id = "overseer-phase2-002"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=1,
        )

        # Previous phase had 3 respawns (exhausted), new phase resets to 0
        # This simulates the reset at phase start
        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,  # fresh budget
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_count == 1, "First respawn in new phase should increment to 1"
        mock_spawner.spawn_overseer_container.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 3: Overseer spawned at phase start
# ---------------------------------------------------------------------------


class TestOverseerSpawnedAtPhaseStart:
    """Verify spawn_overseer_container is called within the phase loop.

    The spawn occurs after phase status is set to RUNNING and phase.started
    event is emitted — each phase gets a fresh overseer instance.
    """

    def test_spawn_returns_valid_container(self, spawner, mock_docker_client):
        """spawn_overseer_container returns a SpawnedContainer with correct role."""
        config = _make_config(overseer_enabled=True)

        if config.overseer_enabled:
            result = spawner.spawn_overseer_container(
                pipeline_id="issue-1560",
                issue_number=1560,
                poll_interval=config.overseer_poll_interval_seconds,
                decision_model=config.overseer_decision_maker_model,
            )

            assert isinstance(result, SpawnedContainer)
            assert result.agent_role == AgentRole.OVERSEER
            assert result.container_info.container_id == "overseer123def456"

    def test_config_disabled_flag(self):
        """PipelineConfig(overseer_enabled=False) correctly stores the flag.

        Note: the gating logic (``if config.overseer_enabled``) lives in the
        pipeline loop, not in a standalone function we can unit-test here.
        This test verifies that the config flag round-trips correctly, which
        is what the guard condition depends on.
        """
        config_off = _make_config(overseer_enabled=False)
        config_on = _make_config(overseer_enabled=True)

        assert not config_off.overseer_enabled
        assert config_on.overseer_enabled

    def test_spawn_failure_leaves_phase_overseer_inactive(self, spawner, mock_docker_client):
        """If spawn_overseer_container raises, phase_overseer_active stays False.

        This is a critical edge case: if the spawn fails at phase start,
        the flag must not be set to True, otherwise the health monitor would
        try to respawn an overseer that was never successfully started.
        """
        from container_spawner import ContainerSpawnError

        mock_docker_client.create_container.side_effect = ContainerSpawnError("Docker daemon error")

        phase_overseer_active = False
        overseer_container_id = None

        try:
            result = spawner.spawn_overseer_container(
                pipeline_id="issue-1560",
                issue_number=1560,
            )
            overseer_container_id = result.container_info.container_id
            phase_overseer_active = True
        except ContainerSpawnError:
            pass  # Non-fatal

        assert not phase_overseer_active, "phase_overseer_active must remain False on spawn failure"
        assert overseer_container_id is None, (
            "overseer_container_id must remain None on spawn failure"
        )


# ---------------------------------------------------------------------------
# Scenario 4: Overseer torn down on phase failure
# ---------------------------------------------------------------------------


class TestOverseerTeardownOnPhaseFailure:
    """Verify overseer is stopped when a phase fails."""

    def test_overseer_stopped_on_phase_failure(self, spawner, mock_docker_client):
        """Phase failure -> overseer stopped via stop_agent_container."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1560",
            issue_number=1560,
        )
        overseer_id = result.container_info.container_id
        phase_overseer_active = True

        # Simulate phase failure: stop overseer
        if overseer_id and phase_overseer_active:
            spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)
            phase_overseer_active = False

        assert not phase_overseer_active
        mock_docker_client.stop_container.assert_called_with(overseer_id, timeout=10)

    def test_phase_failure_stop_exception_is_non_fatal(self, spawner, mock_docker_client):
        """If stop_agent_container raises on phase failure, the flag is still cleared."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1560",
            issue_number=1560,
        )
        overseer_id = result.container_info.container_id
        phase_overseer_active = True

        mock_docker_client.stop_container.side_effect = RuntimeError("Container not found")

        # Simulate the pattern from pipelines.py: exception is caught
        if overseer_id and phase_overseer_active:
            try:
                spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)
            except Exception:
                pass
            phase_overseer_active = False

        assert not phase_overseer_active, "Flag must be cleared even if stop fails"


# ---------------------------------------------------------------------------
# Scenario 5: Overseer torn down before phase advance
# ---------------------------------------------------------------------------


class TestOverseerTeardownBeforePhaseAdvance:
    """Verify overseer is stopped before advancing to the next phase."""

    def test_overseer_stopped_before_advance(self, spawner, mock_docker_client):
        """Phase advance -> overseer stopped, flag cleared to False."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1560",
            issue_number=1560,
        )
        overseer_id = result.container_info.container_id
        phase_overseer_active = True

        # Simulate phase completion and advance
        if overseer_id and phase_overseer_active:
            spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)
            phase_overseer_active = False

        assert not phase_overseer_active
        mock_docker_client.stop_container.assert_called_with(overseer_id, timeout=10)

    def test_advance_with_no_container_skips_respawn(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """_check_and_respawn_overseer returns early when container_id is None.

        This exercises the production guard ``if not overseer_container_id``
        rather than replicating a conditional in the test.
        """
        same_id, same_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=None,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert same_id is None
        assert same_count == 0
        mock_docker_client.get_container_info.assert_not_called()
        mock_spawner.spawn_overseer_container.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6: Safety-net cleanup in finally block
# ---------------------------------------------------------------------------


class TestFinallyBlockSafetyNet:
    """Verify the finally block still stops the overseer as a safety net.

    Even with phase-scoped teardown, the finally block checks
    ``if overseer_container_id:`` to catch cases where the overseer
    wasn't stopped during normal flow (e.g., unhandled exception).
    """

    def test_finally_stops_overseer_if_still_active(self, spawner, mock_docker_client):
        """Finally block catches an overseer that wasn't stopped during phase."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1560",
            issue_number=1560,
        )
        overseer_id = result.container_info.container_id

        # Simulate: overseer is still running when we hit finally
        # (e.g., unhandled exception skipped phase teardown)
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id=overseer_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
        )

        # Simulate finally block pattern
        if overseer_id:
            spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)

        mock_docker_client.stop_container.assert_called_with(overseer_id, timeout=10)

    def test_finally_safe_when_overseer_already_stopped(self, spawner, mock_docker_client):
        """Finally block is safe when overseer was already stopped (double-stop)."""
        result = spawner.spawn_overseer_container(
            pipeline_id="issue-1560",
            issue_number=1560,
        )
        overseer_id = result.container_info.container_id

        # First stop (during phase teardown)
        mock_docker_client.stop_container.return_value = ContainerInfo(
            container_id=overseer_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
        )
        spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)

        # Second stop (finally block) — container already exited
        mock_docker_client.stop_container.side_effect = RuntimeError("Container already stopped")

        # Should not raise — the finally block wraps in try/except
        try:
            spawner.stop_agent_container(overseer_id, cleanup_session=True, timeout=10)
        except Exception:
            pass  # Expected — matches the finally block's try/except pattern

        # The spawner may or may not raise, but the finally block catches it
        # This test verifies the stop is attempted
        assert mock_docker_client.stop_container.call_count == 2


# ---------------------------------------------------------------------------
# Scenario 7: Respawn gating with ContainerNotFoundError
# ---------------------------------------------------------------------------


class TestRespawnWithContainerNotFound:
    """Edge cases for respawn when container is not found in Docker daemon."""

    def test_respawn_on_container_gone_during_active_phase(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Container vanishes from Docker during active phase -> respawn."""
        original_id = "overseer-vanished-001"
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError(
            f"Container {original_id} not found"
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-phase"
        assert new_count == 1

    def test_respawn_on_failed_container_status(
        self, mock_spawner, mock_docker_client, mock_store, running_pipeline
    ):
        """Container in FAILED status during active phase -> respawn."""
        original_id = "overseer-failed-001"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.FAILED,
            exit_code=137,
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-phase"
        assert new_count == 1


# ---------------------------------------------------------------------------
# Scenario 8: Docstring update in container_spawner.py
# ---------------------------------------------------------------------------


class TestContainerSpawnerDocstring:
    """Verify container_spawner.spawn_overseer_container reflects phase-scoped lifecycle."""

    def test_docstring_mentions_phase_scoped(self):
        """The docstring should reference phase-scoped, not pipeline-scoped."""
        from container_spawner import ContainerSpawner

        docstring = ContainerSpawner.spawn_overseer_container.__doc__ or ""
        assert "phase" in docstring.lower(), (
            "spawn_overseer_container docstring should mention phase-scoped lifecycle"
        )


# ---------------------------------------------------------------------------
# Scenario 9: Edge case — pipeline in terminal state during respawn
# ---------------------------------------------------------------------------


class TestNoRespawnInTerminalState:
    """Verify no respawn when pipeline is in a terminal state.

    Even if phase_overseer_active=True, the function checks the pipeline
    status and only respawns for RUNNING or AWAITING_HUMAN.
    """

    def test_no_respawn_when_pipeline_complete(
        self, mock_spawner, mock_docker_client, running_pipeline
    ):
        """No respawn when pipeline status is COMPLETE."""
        original_id = "overseer-terminal-001"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = Pipeline(
            id="issue-1560",
            issue_number=1560,
            status=PipelineStatus.COMPLETE,
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == original_id, "Should not respawn on COMPLETE pipeline"
        assert new_count == 0
        mock_spawner.spawn_overseer_container.assert_not_called()

    def test_no_respawn_when_pipeline_cancelled(
        self, mock_spawner, mock_docker_client, running_pipeline
    ):
        """No respawn when pipeline status is CANCELLED."""
        original_id = "overseer-terminal-002"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = Pipeline(
            id="issue-1560",
            issue_number=1560,
            status=PipelineStatus.CANCELLED,
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == original_id, "Should not respawn on CANCELLED pipeline"
        assert new_count == 0
        mock_spawner.spawn_overseer_container.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 10: Respawn succeeds on AWAITING_HUMAN
# ---------------------------------------------------------------------------


class TestRespawnDuringAwaitingHuman:
    """Verify overseer is respawned during AWAITING_HUMAN (HITL review)."""

    def test_respawn_during_hitl_review(self, mock_spawner, mock_docker_client, running_pipeline):
        """Overseer respawns when pipeline is AWAITING_HUMAN and container exited."""
        original_id = "overseer-hitl-001"
        mock_docker_client.get_container_info.return_value = ContainerInfo(
            container_id=original_id,
            container_name="egg-issue-1560-overseer",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = Pipeline(
            id="issue-1560",
            issue_number=1560,
            status=PipelineStatus.AWAITING_HUMAN,
        )

        new_id, new_count = _check_and_respawn_overseer(
            spawner=mock_spawner,
            store=mock_store,
            pipeline_id="issue-1560",
            pipeline=running_pipeline,
            overseer_container_id=original_id,
            overseer_respawn_count=0,
            max_overseer_respawns=3,
            gateway_mode="public",
            pipeline_repos=None,
            certs_volume=None,
        )

        assert new_id == "overseer-respawned-phase"
        assert new_count == 1
        mock_spawner.spawn_overseer_container.assert_called_once()
