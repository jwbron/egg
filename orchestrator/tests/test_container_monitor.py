"""Tests for container_monitor runtime reconciliation handler."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from container_monitor import (
    ContainerEvent,
    ContainerMonitor,
    _reconcile_container_state,
    create_pipeline_reconciliation_handler,
)
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_with_running_agent(container_id: str = "abc123") -> Pipeline:
    """Return a RUNNING pipeline with one RUNNING coder agent."""
    pipeline = Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    phase.status = PipelineStatus.RUNNING
    phase.started_at = datetime.utcnow()

    phase.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name="egg-coder-issue-99",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
    )
    phase.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=datetime.utcnow(),
        )
    )
    return pipeline


def _make_store(pipeline: Pipeline) -> MagicMock:
    store = MagicMock()
    store.list_pipelines.return_value = [pipeline.id]
    store.load_pipeline.return_value = pipeline
    return store


def _make_container_info(container_id: str, exit_code: int = 1) -> ContainerInfo:
    """Build a ContainerInfo for a container that has exited."""
    return ContainerInfo(
        container_id=container_id,
        container_name=f"egg-container-{container_id[:8]}",
        status=ContainerStatus.EXITED,
        exit_code=exit_code,
        exited_at=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Tests: _reconcile_container_state
# ---------------------------------------------------------------------------


class TestReconcileContainerState:
    """Tests for the _reconcile_container_state helper."""

    def test_marks_pipeline_failed_when_container_exits(self):
        """A RUNNING pipeline whose container exits is marked FAILED."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        result = _reconcile_container_state(store, exited_info)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.error is not None
        store.save_pipeline.assert_called_once_with(
            pipeline,
            expected_version=pipeline.version,
        )

    def test_marks_agent_failed_when_container_exits(self):
        """The agent whose container exited is marked FAILED with an error."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        _reconcile_container_state(store, exited_info)

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        agent = phase.agents[0]
        assert agent.status == AgentExecutionStatus.FAILED
        assert agent.error is not None
        assert "runtime container monitor" in agent.error
        assert agent.completed_at is not None

    def test_marks_container_info_failed(self):
        """The ContainerInfo entry is marked FAILED with exit_code from event."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id, exit_code=137)

        _reconcile_container_state(store, exited_info)

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        ci = phase.containers[0]
        assert ci.status == ContainerStatus.FAILED
        assert ci.exit_code == 137

    def test_ignores_untracked_containers(self):
        """A container not tracked by any pipeline is silently ignored."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent("other_container")
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        result = _reconcile_container_state(store, exited_info)

        assert result is False
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_ignores_non_running_pipelines(self):
        """A COMPLETE pipeline is not affected by container exits."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        pipeline.status = PipelineStatus.COMPLETE
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        result = _reconcile_container_state(store, exited_info)

        assert result is False
        store.save_pipeline.assert_not_called()

    def test_handles_store_list_error(self):
        """Returns False without crashing when store.list_pipelines fails."""
        store = MagicMock()
        store.list_pipelines.side_effect = Exception("Store unavailable")
        exited_info = _make_container_info("some_id")

        result = _reconcile_container_state(store, exited_info)

        assert result is False

    def test_handles_store_load_error(self):
        """Skips pipelines that fail to load."""
        store = MagicMock()
        store.list_pipelines.return_value = ["bad-pipeline"]
        store.load_pipeline.side_effect = Exception("corrupt state")
        exited_info = _make_container_info("some_id")

        result = _reconcile_container_state(store, exited_info)

        assert result is False

    @patch("state_store.get_pipeline_state_lock")
    def test_acquires_pipeline_lock(self, mock_get_lock):
        """Reconciliation acquires the per-pipeline lock during load-modify-save."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        mock_lock = MagicMock()
        mock_get_lock.return_value = mock_lock

        _reconcile_container_state(store, exited_info)

        mock_get_lock.assert_called_once_with(pipeline.id)
        mock_lock.__enter__.assert_called_once()
        mock_lock.__exit__.assert_called_once()

    def test_handles_version_conflict(self):
        """Returns False on VersionConflictError (concurrent writer won)."""
        from state_store import VersionConflictError

        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        store.save_pipeline.side_effect = VersionConflictError("conflict")
        exited_info = _make_container_info(container_id)

        result = _reconcile_container_state(store, exited_info)

        assert result is False

    def test_reconciles_running_container_in_completed_phase(self):
        """A RUNNING agent in a COMPLETE phase with an exited container is marked FAILED.

        Reviewers run inside phases already marked complete. The reconciler
        must still scan completed phases for exited containers.
        """
        container_id = "reviewer_dead_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        # Phase is complete, but reviewer container is still RUNNING
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.COMPLETE

        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id)

        result = _reconcile_container_state(store, exited_info)

        assert result is True
        assert pipeline.status == PipelineStatus.FAILED
        agent = phase.agents[0]
        assert agent.status == AgentExecutionStatus.FAILED
        assert agent.completed_at is not None


# ---------------------------------------------------------------------------
# Tests: create_pipeline_reconciliation_handler
# ---------------------------------------------------------------------------


class TestCreatePipelineReconciliationHandler:
    """Tests for the handler factory function."""

    @patch("state_store.get_state_store")
    def test_handler_calls_reconcile_on_failed_event(self, mock_get_store):
        """Handler processes FAILED events (non-zero exit)."""
        container_id = "dead_container"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)
        mock_get_store.return_value = store

        handler = create_pipeline_reconciliation_handler("/repo")
        event = ContainerEvent(
            ContainerEvent.FAILED,
            _make_container_info(container_id),
        )
        handler(event)

        assert pipeline.status == PipelineStatus.FAILED

    @patch("state_store.get_state_store")
    def test_handler_ignores_started_event(self, mock_get_store):
        """Handler does NOT process STARTED events."""
        handler = create_pipeline_reconciliation_handler("/repo")
        event = ContainerEvent(
            ContainerEvent.STARTED,
            _make_container_info("some_id"),
        )
        handler(event)

        mock_get_store.assert_not_called()

    @patch("state_store.get_state_store")
    def test_handler_ignores_stopped_event(self, mock_get_store):
        """Handler does NOT process STOPPED events (graceful exit code 0)."""
        handler = create_pipeline_reconciliation_handler("/repo")
        event = ContainerEvent(
            ContainerEvent.STOPPED,
            _make_container_info("some_id", exit_code=0),
        )
        handler(event)

        mock_get_store.assert_not_called()

    @patch("state_store.get_state_store")
    def test_handler_ignores_exited_event(self, mock_get_store):
        """Handler does NOT process EXITED events (never emitted by monitor)."""
        handler = create_pipeline_reconciliation_handler("/repo")
        event = ContainerEvent(
            ContainerEvent.EXITED,
            _make_container_info("some_id"),
        )
        handler(event)

        mock_get_store.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: ContainerMonitor integration
# ---------------------------------------------------------------------------


class TestContainerMonitorDetection:
    """Tests that the monitor detects container state changes."""

    def test_monitor_detects_exited_container(self):
        """Monitor emits FAILED event when a running container exits with non-zero."""
        mock_docker = MagicMock()
        container_id = "test_container_123"

        # First call: container is running
        running_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        # Second call: container has exited
        exited_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.EXITED,
            exit_code=1,
            exited_at=datetime.utcnow(),
        )
        mock_docker.list_containers.side_effect = [
            [running_info],
            [exited_info],
        ]

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)
        events_received: list[ContainerEvent] = []
        monitor.add_handler(lambda e: events_received.append(e))

        # Simulate two check cycles
        monitor._check_all_containers()  # First: STARTED
        monitor._check_all_containers()  # Second: FAILED (non-zero exit)

        event_types = [e.event_type for e in events_received]
        assert ContainerEvent.STARTED in event_types
        assert ContainerEvent.FAILED in event_types

    def test_monitor_emits_stopped_for_zero_exit(self):
        """Monitor emits STOPPED event when a running container exits with code 0."""
        mock_docker = MagicMock()
        container_id = "test_container_456"

        running_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        exited_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.utcnow(),
        )
        mock_docker.list_containers.side_effect = [
            [running_info],
            [exited_info],
        ]

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)
        events_received: list[ContainerEvent] = []
        monitor.add_handler(lambda e: events_received.append(e))

        monitor._check_all_containers()
        monitor._check_all_containers()

        event_types = [e.event_type for e in events_received]
        assert ContainerEvent.STOPPED in event_types
