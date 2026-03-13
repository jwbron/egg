"""
Functional tests for CoordinatorExecutor lifecycle management.

Tests the coordinator container lifecycle including spawning,
completion handling, crash recovery, respawn logic, and guardrails.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from coordinator_executor import CoordinatorConfig, CoordinatorExecutor
from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    GuardrailCounters,
    Pipeline,
    PipelineConfig,
    PipelineStatus,
)


def _make_pipeline(
    pipeline_id="test-pipeline",
    coordinator_enabled=True,
    coordinator_state=None,
    status=PipelineStatus.PENDING,
    max_respawns=2,
):
    config = PipelineConfig(
        coordinator_enabled=coordinator_enabled,
        coordinator_max_respawns=max_respawns,
    )
    return Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        config=config,
        coordinator_state=coordinator_state,
        status=status,
    )


# ── CoordinatorConfig tests ─────────────────────────────────────────


class TestCoordinatorConfig:
    """Tests for CoordinatorConfig dataclass."""

    def test_default_values(self):
        config = CoordinatorConfig()
        assert config.max_agents == 10
        assert config.max_retries_per_role == 2
        assert config.max_respawns == 2
        assert config.max_wall_clock_minutes == 120

    def test_custom_values(self):
        config = CoordinatorConfig(max_agents=5, max_respawns=0)
        assert config.max_agents == 5
        assert config.max_respawns == 0


# ── should_use_coordinator tests ─────────────────────────────────────


class TestShouldUseCoordinator:
    """Tests for CoordinatorExecutor.should_use_coordinator."""

    @patch("coordinator_executor.get_state_store")
    def test_returns_true_when_enabled(self, mock_store_fn, tmp_path):
        pipeline = _make_pipeline(coordinator_enabled=True)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        assert executor.should_use_coordinator("test-pipeline") is True

    @patch("coordinator_executor.get_state_store")
    def test_returns_false_when_disabled(self, mock_store_fn, tmp_path):
        pipeline = _make_pipeline(coordinator_enabled=False)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        assert executor.should_use_coordinator("test-pipeline") is False


# ── start_coordinator tests ──────────────────────────────────────────


class TestStartCoordinator:
    """Tests for CoordinatorExecutor.start_coordinator."""

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_success_with_spawner(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        container = MagicMock()
        container.container_info.container_id = "coord-abc123"
        spawner.spawn_agent_container.return_value = container

        executor = CoordinatorExecutor(tmp_path)
        result = executor.start_coordinator(
            pipeline_id="test-pipeline",
            spawner=spawner,
            issue_number=42,
        )

        assert result == container
        spawner.spawn_agent_container.assert_called_once()
        call_kwargs = spawner.spawn_agent_container.call_args[1]
        assert call_kwargs["agent_role"] == AgentRole.COORDINATOR
        assert call_kwargs["phase"] == "coordinator"
        assert "EGG_COORDINATOR_MODE" in call_kwargs["extra_env"]

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_without_spawner(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.start_coordinator(pipeline_id="test-pipeline")

        assert result is None
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_raises_when_not_enabled(self, mock_store_fn, mock_lock, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_enabled=False)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        with pytest.raises(ValueError, match="does not have coordinator enabled"):
            executor.start_coordinator(pipeline_id="test-pipeline")

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_initializes_coordinator_state(
        self, mock_store_fn, mock_lock, mock_emit, tmp_path
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(coordinator_state=None)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        executor.start_coordinator(pipeline_id="test-pipeline")

        assert pipeline.coordinator_state is not None
        assert isinstance(pipeline.coordinator_state, CoordinatorState)

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_sets_running_status(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(status=PipelineStatus.PENDING)
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        executor.start_coordinator(pipeline_id="test-pipeline")

        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_called_once_with(pipeline)

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_spawner_failure_propagates(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        spawner.spawn_agent_container.side_effect = Exception("Docker socket gone")

        executor = CoordinatorExecutor(tmp_path)
        with pytest.raises(Exception, match="Docker socket gone"):
            executor.start_coordinator(pipeline_id="test-pipeline", spawner=spawner)

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_start_injects_issue_number_env(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        spawner = MagicMock()
        container = MagicMock()
        container.container_info.container_id = "coord-xyz"
        spawner.spawn_agent_container.return_value = container

        executor = CoordinatorExecutor(tmp_path)
        executor.start_coordinator(pipeline_id="test-pipeline", spawner=spawner, issue_number=99)

        call_kwargs = spawner.spawn_agent_container.call_args[1]
        assert call_kwargs["extra_env"]["EGG_ISSUE_NUMBER"] == "99"


# ── handle_coordinator_completion tests ──────────────────────────────


class TestHandleCompletion:
    """Tests for CoordinatorExecutor.handle_coordinator_completion."""

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_success_exit_completes_pipeline(self, mock_store_fn, mock_lock, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=0)

        assert result == "complete"
        assert pipeline.status == PipelineStatus.COMPLETE

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_success_exit_with_running_agents(self, mock_store_fn, mock_lock, tmp_path):
        """Exit code 0 with running agents should still complete but log warning."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            coordinator_state=CoordinatorState(
                agents_spawned=[
                    AgentSpawnRecord(role=AgentRole.CODER, status="running"),
                ],
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=0)

        assert result == "complete"
        assert pipeline.status == PipelineStatus.COMPLETE

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_crash_triggers_respawn(self, mock_store_fn, mock_lock, mock_emit, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            max_respawns=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(coordinator_respawns=0),
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "respawn"
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.coordinator_state.guardrail_counters.coordinator_respawns == 1

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_crash_max_respawns_reached_fails(self, mock_store_fn, mock_lock, tmp_path):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            max_respawns=2,
            coordinator_state=CoordinatorState(
                guardrail_counters=GuardrailCounters(coordinator_respawns=2),
            ),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "failed"
        assert pipeline.status == PipelineStatus.FAILED
        assert "max respawns" in pipeline.error.lower()

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_crash_no_coordinator_state_creates_default(self, mock_store_fn, mock_lock, tmp_path):
        """Crash with no coordinator_state should create default state."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            max_respawns=2,
            coordinator_state=None,
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        # Should respawn since no previous respawns
        assert result == "respawn"
        assert pipeline.coordinator_state is not None
        assert pipeline.coordinator_state.guardrail_counters.coordinator_respawns == 1

    @patch("coordinator_executor.emit_event")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_crash_respawn_emits_loopback_event(
        self, mock_store_fn, mock_lock, mock_emit, tmp_path
    ):
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            max_respawns=2,
            coordinator_state=CoordinatorState(),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        from events import EventType

        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == EventType.COORDINATOR_LOOPBACK
        assert call_args[0][1] == "test-pipeline"

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_crash_max_respawns_zero(self, mock_store_fn, mock_lock, tmp_path):
        """With max_respawns=0, first crash should fail immediately."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            max_respawns=0,
            coordinator_state=CoordinatorState(),
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=1)

        assert result == "failed"
        assert pipeline.status == PipelineStatus.FAILED

    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.get_state_store")
    def test_success_without_coordinator_state(self, mock_store_fn, mock_lock, tmp_path):
        """Exit code 0 without coordinator_state should still complete."""
        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        pipeline = _make_pipeline(
            status=PipelineStatus.RUNNING,
            coordinator_state=None,
        )
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = store

        executor = CoordinatorExecutor(tmp_path)
        result = executor.handle_coordinator_completion("test-pipeline", exit_code=0)

        assert result == "complete"
        assert pipeline.status == PipelineStatus.COMPLETE
