"""Tests for container_monitor runtime reconciliation handler."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it.
# Use proper exception classes so that except clauses work correctly.
if "docker" not in sys.modules:
    from types import ModuleType

    _docker_mock = MagicMock()
    _docker_errors = ModuleType("docker.errors")
    _docker_errors.DockerException = type("DockerException", (Exception,), {})  # type: ignore[attr-defined]
    _docker_errors.APIError = type("APIError", (_docker_errors.DockerException,), {})  # type: ignore[attr-defined]
    _docker_errors.ImageNotFound = type("ImageNotFound", (_docker_errors.DockerException,), {})  # type: ignore[attr-defined]
    _docker_errors.NotFound = type("NotFound", (_docker_errors.DockerException,), {})  # type: ignore[attr-defined]
    _docker_mock.errors = _docker_errors
    sys.modules["docker"] = _docker_mock
    sys.modules["docker.errors"] = _docker_errors
sys.modules.setdefault("docker.types", MagicMock())

from container_monitor import (
    ContainerEvent,
    ContainerMonitor,
    _reconcile_container_state,
    create_pipeline_reconciliation_handler,
)
from docker_client import DockerClientError
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
    phase.started_at = datetime.now(UTC)

    phase.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name="egg-coder-issue-99",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
    )
    phase.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=datetime.now(UTC),
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
        exited_at=datetime.now(UTC),
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

    def test_skips_container_with_complete_agent(self):
        """Reconciliation skips containers whose agent is already COMPLETE.

        Regression test for issue #1294: when agents complete via consensus,
        the container monitor should not mark those containers as FAILED even
        if the container exits with a non-zero code (from SIGTERM/SIGKILL).
        """
        container_id = "consensus_done_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)

        # Simulate consensus completion: agent is COMPLETE, container still RUNNING
        phase.agents[0].status = AgentExecutionStatus.COMPLETE
        phase.agents[0].completed_at = datetime.now(UTC)

        store = _make_store(pipeline)
        exited_info = _make_container_info(container_id, exit_code=137)  # SIGKILL

        result = _reconcile_container_state(store, exited_info)

        # No changes should be saved — the container was skipped
        assert result is False
        # Pipeline should remain RUNNING (not FAILED)
        assert pipeline.status == PipelineStatus.RUNNING
        # Container status should still be RUNNING (unchanged by reconciler)
        ci = phase.containers[0]
        assert ci.status == ContainerStatus.RUNNING
        # Agent should still be COMPLETE (not overwritten to FAILED)
        assert phase.agents[0].status == AgentExecutionStatus.COMPLETE


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
            started_at=datetime.now(UTC),
        )
        # Second call: container has exited
        exited_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.EXITED,
            exit_code=1,
            exited_at=datetime.now(UTC),
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
            started_at=datetime.now(UTC),
        )
        exited_info = ContainerInfo(
            container_id=container_id,
            container_name="egg-test",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
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


# ---------------------------------------------------------------------------
# Tests: Periodic reconciliation
# ---------------------------------------------------------------------------


def _run_one_reconciliation_sweep(monitor):
    """Run exactly one reconciliation sweep deterministically.

    Patches ``time.sleep`` so the initial delay returns immediately
    and the loop exits after completing a single sweep — no timing
    dependencies on CI machine speed.
    """
    call_count = 0

    def _fake_sleep(_seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # First call = initial delay, second = end of first sweep
            monitor._reconciliation_running = False

    with patch("container_monitor.time.sleep", side_effect=_fake_sleep):
        monitor._reconciliation_loop()


class TestPeriodicReconciliation:
    """Tests for the _reconciliation_loop background thread."""

    def test_detects_stale_container_in_current_phase(self):
        """Loop detects a stale container in the current phase and reconciles it."""
        container_id = "stale_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        # No live containers — the agent's container is missing
        mock_docker.list_containers.return_value = []

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # Should have called _reconcile with the matching ContainerInfo
            mock_reconcile.assert_called()
            call_args = mock_reconcile.call_args
            assert call_args[0][0] is store
            assert call_args[0][1].container_id == container_id

    def test_skips_non_running_pipelines(self):
        """Loop skips pipelines that are not in RUNNING status."""
        container_id = "stale_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)
        pipeline.status = PipelineStatus.COMPLETE
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            mock_reconcile.assert_not_called()

    def test_handles_store_load_pipeline_exception(self):
        """Loop continues without crashing when store.load_pipeline raises."""
        store = MagicMock()
        store.list_pipelines.return_value = ["bad-pipeline"]
        store.load_pipeline.side_effect = Exception("corrupt state")

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # Should not crash, should not reconcile anything
            mock_reconcile.assert_not_called()

    def test_stop_joins_reconciliation_thread(self):
        """stop() properly terminates and joins the reconciliation thread."""
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        store = MagicMock()
        store.list_pipelines.return_value = []

        monitor.start_periodic_reconciliation(store, interval=1)
        assert monitor._reconciliation_running is True
        assert monitor._reconciliation_thread is not None
        assert monitor._reconciliation_thread.is_alive()

        monitor.stop()
        assert monitor._reconciliation_running is False
        assert monitor._reconciliation_thread is None

    def test_logs_missing_container_info(self):
        """Loop logs debug message when agent has no matching ContainerInfo."""
        container_id = "orphan_agent_abc"
        pipeline = Pipeline(
            id="issue-300",
            issue_number=300,
            repo="owner/repo",
            branch="egg/issue-300",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.started_at = datetime.now(UTC)
        # Agent has a container_id but no matching ContainerInfo in phase.containers
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=container_id,
                started_at=datetime.now(UTC),
            )
        )
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with (
            patch("container_monitor._reconcile_container_state") as mock_reconcile,
            patch("container_monitor.logger") as mock_logger,
        ):
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # Should NOT have called _reconcile (no matching ContainerInfo)
            mock_reconcile.assert_not_called()
            # Should have logged a debug message about missing ContainerInfo
            mock_logger.debug.assert_called()
            debug_calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any("no matching ContainerInfo" in c for c in debug_calls)

    def test_skips_reconciliation_for_clean_exit(self):
        """Loop skips FAILED reconciliation when container exited with code 0.

        Regression test for issue #1273: containers exiting cleanly (e.g. consensus
        wrapper after agent confirmed) should not mark the pipeline FAILED.
        """
        container_id = "clean_exit_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        # Container is NOT in live list (it exited)
        mock_docker.list_containers.return_value = []
        # But when we inspect it, exit code is 0
        mock_docker.get_container_info.return_value = ContainerInfo(
            container_id=container_id,
            container_name="egg-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # Should NOT have called _reconcile — clean exit
            mock_reconcile.assert_not_called()

    def test_reconciles_nonzero_exit(self):
        """Loop reconciles when container exited with non-zero code.

        Ensures the exit-code check doesn't accidentally skip real failures.
        """
        container_id = "failed_exit_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []
        # Non-zero exit code
        mock_docker.get_container_info.return_value = ContainerInfo(
            container_id=container_id,
            container_name="egg-coder",
            status=ContainerStatus.EXITED,
            exit_code=1,
            exited_at=datetime.now(UTC),
        )

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # SHOULD have called _reconcile — non-zero exit
            mock_reconcile.assert_called()

    def test_reconciles_when_exit_code_unknown(self):
        """Loop reconciles when container exit code cannot be determined.

        If get_container_info raises, we err on the side of reconciling.
        """
        container_id = "unknown_exit_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)
        store = _make_store(pipeline)

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []
        # Docker can't inspect the container (already removed)
        mock_docker.get_container_info.side_effect = DockerClientError("container removed")

        monitor = ContainerMonitor(docker_client=mock_docker, check_interval=1)

        with patch("container_monitor._reconcile_container_state") as mock_reconcile:
            monitor._reconciliation_stores = [store]
            monitor._reconciliation_running = True
            monitor._reconciliation_interval = 0.01

            _run_one_reconciliation_sweep(monitor)

            # SHOULD have called _reconcile — unknown exit code errs on caution
            mock_reconcile.assert_called()
            # Verify the correct store and container were reconciled
            call_args = mock_reconcile.call_args
            assert call_args[0][0] is store  # first positional arg is the store
            assert call_args[0][1].container_id == container_id
