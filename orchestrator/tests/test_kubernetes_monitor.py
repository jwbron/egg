"""
Tests for KubernetesMonitor.

Covers event handling, pod state tracking, health checks,
reconciliation, and singleton management.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from kubernetes_client import (
    KubernetesClientError,
    PodNotFoundError,
)
from models import ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_k8s_client():
    """Create a mock KubernetesClient."""
    client = MagicMock()
    client.list_containers.return_value = []
    client.get_container_info.return_value = ContainerInfo(
        container_id="uid-1",
        container_name="test-job",
        pod_name="test-pod-abc",
        job_name="test-job",
        status=ContainerStatus.RUNNING,
        started_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    client.cleanup_orphaned_containers.return_value = 0
    return client


@pytest.fixture()
def monitor(mock_k8s_client):
    """Create a KubernetesMonitor with a mock k8s client."""
    from kubernetes_monitor import KubernetesMonitor

    m = KubernetesMonitor(
        k8s_client=mock_k8s_client,
        check_interval=1,
        orphan_age_hours=24,
    )
    return m


# ---------------------------------------------------------------------------
# TestContainerEvent
# ---------------------------------------------------------------------------


class TestContainerEvent:
    """Test ContainerEvent class."""

    def test_event_type_constants(self):
        """ContainerEvent has the expected type constants."""
        from kubernetes_monitor import ContainerEvent

        assert ContainerEvent.STARTED == "started"
        assert ContainerEvent.STOPPED == "stopped"
        assert ContainerEvent.EXITED == "exited"
        assert ContainerEvent.FAILED == "failed"
        assert ContainerEvent.REMOVED == "removed"
        assert ContainerEvent.UNHEALTHY == "unhealthy"

    def test_event_creation(self):
        """ContainerEvent stores all fields."""
        from kubernetes_monitor import ContainerEvent

        info = ContainerInfo(
            container_id="uid-1",
            container_name="test",
            pod_name="pod-1",
        )
        event = ContainerEvent(
            event_type=ContainerEvent.STARTED,
            container_info=info,
            data={"key": "value"},
        )
        assert event.event_type == "started"
        assert event.container_info is info
        assert event.data == {"key": "value"}
        assert event.timestamp is not None

    def test_event_default_timestamp(self):
        """ContainerEvent gets a default timestamp."""
        from kubernetes_monitor import ContainerEvent

        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.FAILED, info)
        assert isinstance(event.timestamp, datetime)

    def test_event_custom_timestamp(self):
        """ContainerEvent accepts a custom timestamp."""
        from kubernetes_monitor import ContainerEvent

        ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.STOPPED, info, timestamp=ts)
        assert event.timestamp == ts


# ---------------------------------------------------------------------------
# TestEventHandlers
# ---------------------------------------------------------------------------


class TestEventHandlers:
    """Test event handler management."""

    def test_add_handler(self, monitor):
        """add_handler registers a handler."""
        handler = MagicMock()
        monitor.add_handler(handler)
        assert handler in monitor._handlers

    def test_remove_handler(self, monitor):
        """remove_handler unregisters a handler."""
        handler = MagicMock()
        monitor.add_handler(handler)
        monitor.remove_handler(handler)
        assert handler not in monitor._handlers

    def test_remove_nonexistent_handler(self, monitor):
        """remove_handler is safe for non-registered handlers."""
        handler = MagicMock()
        monitor.remove_handler(handler)  # Should not raise

    def test_emit_calls_handlers(self, monitor):
        """_emit_event calls all registered handlers."""
        from kubernetes_monitor import ContainerEvent

        handler1 = MagicMock()
        handler2 = MagicMock()
        monitor.add_handler(handler1)
        monitor.add_handler(handler2)

        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.STARTED, info)
        monitor._emit_event(event)

        handler1.assert_called_once_with(event)
        handler2.assert_called_once_with(event)

    def test_emit_handles_handler_error(self, monitor):
        """_emit_event catches handler exceptions."""
        from kubernetes_monitor import ContainerEvent

        handler = MagicMock(side_effect=ValueError("handler crash"))
        monitor.add_handler(handler)

        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.STARTED, info)
        monitor._emit_event(event)  # Should not raise

        handler.assert_called_once()


# ---------------------------------------------------------------------------
# TestCheckPod
# ---------------------------------------------------------------------------


class TestCheckPod:
    """Test _check_pod state change detection."""

    def test_new_pod_running(self, monitor):
        """Newly seen RUNNING pod emits STARTED event."""
        handler = MagicMock()
        monitor.add_handler(handler)

        info = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.RUNNING,
        )
        monitor._check_pod(info)

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == "started"

    def test_pending_to_running(self, monitor):
        """PENDING → RUNNING transition emits STARTED."""
        handler = MagicMock()
        monitor.add_handler(handler)

        # First: PENDING
        info_pending = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.PENDING,
        )
        monitor._check_pod(info_pending)

        # Then: RUNNING
        info_running = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.RUNNING,
        )
        monitor._check_pod(info_running)

        events = [call[0][0] for call in handler.call_args_list]
        assert events[-1].event_type == "started"

    def test_running_to_exited_clean(self, monitor):
        """RUNNING → EXITED (exit_code=0) emits STOPPED."""
        handler = MagicMock()
        monitor.add_handler(handler)

        # Start as RUNNING
        monitor._pod_states["pod-1"] = ContainerStatus.RUNNING

        info = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )
        monitor._check_pod(info)

        event = handler.call_args[0][0]
        assert event.event_type == "stopped"

    def test_running_to_exited_error(self, monitor):
        """RUNNING → EXITED (exit_code=1) emits FAILED."""
        handler = MagicMock()
        monitor.add_handler(handler)

        monitor._pod_states["pod-1"] = ContainerStatus.RUNNING

        info = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.EXITED,
            exit_code=1,
        )
        monitor._check_pod(info)

        event = handler.call_args[0][0]
        assert event.event_type == "failed"
        assert event.data["exit_code"] == 1

    def test_running_to_failed(self, monitor):
        """RUNNING → FAILED emits FAILED."""
        handler = MagicMock()
        monitor.add_handler(handler)

        monitor._pod_states["pod-1"] = ContainerStatus.RUNNING

        info = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.FAILED,
            exit_code=137,
        )
        monitor._check_pod(info)

        event = handler.call_args[0][0]
        assert event.event_type == "failed"

    def test_no_event_for_same_status(self, monitor):
        """No event is emitted when status hasn't changed."""
        handler = MagicMock()
        monitor.add_handler(handler)

        monitor._pod_states["pod-1"] = ContainerStatus.RUNNING

        info = ContainerInfo(
            container_id="u1",
            container_name="j1",
            pod_name="pod-1",
            status=ContainerStatus.RUNNING,
        )
        monitor._check_pod(info)
        handler.assert_not_called()


# ---------------------------------------------------------------------------
# TestCheckAllPods
# ---------------------------------------------------------------------------


class TestCheckAllPods:
    """Test _check_all_pods method."""

    def test_checks_all_pods(self, monitor, mock_k8s_client):
        """_check_all_pods queries and checks each pod."""
        mock_k8s_client.list_containers.return_value = [
            ContainerInfo(
                container_id="u1",
                container_name="j1",
                pod_name="pod-1",
                status=ContainerStatus.RUNNING,
            ),
            ContainerInfo(
                container_id="u2",
                container_name="j2",
                pod_name="pod-2",
                status=ContainerStatus.PENDING,
            ),
        ]

        handler = MagicMock()
        monitor.add_handler(handler)
        monitor._check_all_pods()

        # Both pods should have been checked; RUNNING → STARTED event
        assert handler.call_count >= 1

    def test_removes_disappeared_pods(self, monitor, mock_k8s_client):
        """_check_all_pods cleans state for removed pods."""
        monitor._pod_states["old-pod"] = ContainerStatus.RUNNING
        mock_k8s_client.list_containers.return_value = []

        monitor._check_all_pods()
        assert "old-pod" not in monitor._pod_states

    def test_handles_k8s_error(self, monitor, mock_k8s_client):
        """_check_all_pods handles KubernetesClientError gracefully."""
        mock_k8s_client.list_containers.side_effect = KubernetesClientError("API down")
        monitor._check_all_pods()  # Should not raise


# ---------------------------------------------------------------------------
# TestCleanupOrphaned
# ---------------------------------------------------------------------------


class TestCleanupOrphaned:
    """Test _cleanup_orphaned method."""

    def test_cleanup_delegates(self, monitor, mock_k8s_client):
        """_cleanup_orphaned delegates to k8s client."""
        mock_k8s_client.cleanup_orphaned_containers.return_value = 3
        result = monitor._cleanup_orphaned()
        assert result == 3
        mock_k8s_client.cleanup_orphaned_containers.assert_called_once_with(
            max_age_hours=24,
        )

    def test_cleanup_handles_error(self, monitor, mock_k8s_client):
        """_cleanup_orphaned returns 0 on error."""
        mock_k8s_client.cleanup_orphaned_containers.side_effect = KubernetesClientError("fail")
        result = monitor._cleanup_orphaned()
        assert result == 0


# ---------------------------------------------------------------------------
# TestStartStop
# ---------------------------------------------------------------------------


class TestStartStop:
    """Test monitor start/stop lifecycle."""

    def test_start_sets_running(self, monitor):
        """start() sets _running flag and creates thread."""
        monitor.start()
        try:
            assert monitor._running is True
            assert monitor._thread is not None
            assert monitor._thread.is_alive()
        finally:
            monitor.stop()

    def test_stop_clears_running(self, monitor):
        """stop() clears _running flag and joins thread."""
        monitor.start()
        monitor.stop()
        assert monitor._running is False

    def test_start_idempotent(self, monitor):
        """Calling start() twice is safe."""
        monitor.start()
        thread1 = monitor._thread
        monitor.start()  # Should be a no-op
        assert monitor._thread is thread1
        monitor.stop()

    def test_is_running(self, monitor):
        """is_running reflects the monitor state."""
        assert monitor.is_running() is False
        monitor.start()
        assert monitor.is_running() is True
        monitor.stop()
        assert monitor.is_running() is False


# ---------------------------------------------------------------------------
# TestGetPodStatus
# ---------------------------------------------------------------------------


class TestGetPodStatus:
    """Test get_pod_status method."""

    def test_cached_status(self, monitor):
        """get_pod_status returns cached status."""
        monitor._pod_states["pod-1"] = ContainerStatus.RUNNING
        assert monitor.get_pod_status("pod-1") == ContainerStatus.RUNNING

    def test_unknown_pod(self, monitor):
        """get_pod_status returns None for unknown pods."""
        assert monitor.get_pod_status("unknown-pod") is None


# ---------------------------------------------------------------------------
# TestCheckContainerHealth
# ---------------------------------------------------------------------------


class TestCheckContainerHealth:
    """Test check_container_health method."""

    def test_healthy_pod(self, monitor, mock_k8s_client):
        """Healthy running pod returns healthy=True."""
        result = monitor.check_container_health("job-1")
        assert result["healthy"] is True
        assert result["status"] == "running"
        assert result["pod_name"] == "test-pod-abc"

    def test_not_found_pod(self, monitor, mock_k8s_client):
        """Pod not found returns healthy=False, status=not_found."""
        mock_k8s_client.get_container_info.side_effect = PodNotFoundError("gone")
        result = monitor.check_container_health("job-1")
        assert result["healthy"] is False
        assert result["status"] == "not_found"

    def test_k8s_error(self, monitor, mock_k8s_client):
        """K8s error returns healthy=False, status=error."""
        mock_k8s_client.get_container_info.side_effect = KubernetesClientError("API err")
        result = monitor.check_container_health("job-1")
        assert result["healthy"] is False
        assert result["status"] == "error"
        assert "API err" in result["error"]

    def test_exited_pod(self, monitor, mock_k8s_client):
        """Exited pod returns healthy=False."""
        mock_k8s_client.get_container_info.return_value = ContainerInfo(
            container_id="uid-1",
            container_name="test-job",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC),
        )
        result = monitor.check_container_health("job-1")
        assert result["healthy"] is False
        assert result["status"] == "exited"


# ---------------------------------------------------------------------------
# TestGetPodExitCode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestGetKubernetesMonitor
# ---------------------------------------------------------------------------


class TestGetKubernetesMonitor:
    """Test get_kubernetes_monitor singleton."""

    def test_returns_monitor(self):
        """get_kubernetes_monitor returns a KubernetesMonitor."""
        import kubernetes_monitor
        from kubernetes_monitor import KubernetesMonitor, get_kubernetes_monitor

        kubernetes_monitor._kubernetes_monitor = None

        with patch.object(KubernetesMonitor, "__init__", return_value=None):
            result = get_kubernetes_monitor()
            assert isinstance(result, KubernetesMonitor)

        kubernetes_monitor._kubernetes_monitor = None

    def test_singleton_reuses_instance(self):
        """Repeated calls return the same instance."""
        import kubernetes_monitor
        from kubernetes_monitor import KubernetesMonitor, get_kubernetes_monitor

        kubernetes_monitor._kubernetes_monitor = None

        with patch.object(KubernetesMonitor, "__init__", return_value=None):
            first = get_kubernetes_monitor()
            second = get_kubernetes_monitor()
            assert first is second

        kubernetes_monitor._kubernetes_monitor = None


# ---------------------------------------------------------------------------
# TestReconcilePodState
# ---------------------------------------------------------------------------


class TestReconcilePodState:
    """Test _reconcile_pod_state function."""

    def _make_mock_store(self, pipeline, pipeline_ids=None):
        """Create a mock StateStore with a pipeline."""
        store = MagicMock()
        store.list_pipelines.return_value = pipeline_ids or [pipeline.id]
        store.load_pipeline.return_value = pipeline
        return store

    def test_reconcile_marks_pipeline_failed(self):
        """_reconcile_pod_state marks the pipeline as FAILED."""
        from kubernetes_monitor import _reconcile_pod_state
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        container_info = ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=ContainerStatus.FAILED,
            exit_code=1,
            exited_at=datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC),
        )

        agent = AgentExecution(
            role="coder",
            status=AgentExecutionStatus.RUNNING,
            container_id="uid-1",
        )
        ci = ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=ContainerStatus.RUNNING,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[agent],
            containers=[ci],
        )
        pipeline = Pipeline(
            id="pipe-1",
            issue_number=1,
            repo="owner/repo",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            phases={"implement": phase_exec},
        )

        store = self._make_mock_store(pipeline)

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            result = _reconcile_pod_state(store, container_info)

        assert result is True
        store.save_pipeline.assert_called_once()
        saved_pipeline = store.save_pipeline.call_args[0][0]
        assert saved_pipeline.status == PipelineStatus.FAILED

    def test_reconcile_skips_completed_agents(self):
        """_reconcile_pod_state skips pods whose agent is COMPLETE."""
        from kubernetes_monitor import _reconcile_pod_state
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        container_info = ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=ContainerStatus.FAILED,
            exit_code=0,
        )

        agent = AgentExecution(
            role="coder",
            status=AgentExecutionStatus.COMPLETE,
            container_id="uid-1",
        )
        ci = ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=ContainerStatus.RUNNING,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[agent],
            containers=[ci],
        )
        pipeline = Pipeline(
            id="pipe-1",
            issue_number=1,
            repo="owner/repo",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            phases={"implement": phase_exec},
        )

        store = self._make_mock_store(pipeline)

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            result = _reconcile_pod_state(store, container_info)

        assert result is False
        store.save_pipeline.assert_not_called()

    def test_reconcile_skips_non_running_pipelines(self):
        """_reconcile_pod_state skips pipelines that are not RUNNING."""
        from kubernetes_monitor import _reconcile_pod_state
        from models import Pipeline, PipelinePhase, PipelineStatus

        container_info = ContainerInfo(
            container_id="uid-1",
            container_name="job-1",
            status=ContainerStatus.FAILED,
        )

        pipeline = Pipeline(
            id="pipe-1",
            issue_number=1,
            repo="owner/repo",
            status=PipelineStatus.COMPLETE,
            current_phase=PipelinePhase.IMPLEMENT,
            phases={},
        )

        store = self._make_mock_store(pipeline)

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            result = _reconcile_pod_state(store, container_info)

        assert result is False


# ---------------------------------------------------------------------------
# TestCreatePipelineReconciliationHandler
# ---------------------------------------------------------------------------


class TestCreatePipelineReconciliationHandler:
    """Test create_pipeline_reconciliation_handler factory."""

    def test_returns_callable(self):
        """create_pipeline_reconciliation_handler returns a callable."""
        from kubernetes_monitor import create_pipeline_reconciliation_handler

        handler = create_pipeline_reconciliation_handler("/path/to/repo")
        assert callable(handler)

    def test_handler_ignores_non_failed(self):
        """Handler only processes FAILED events."""
        from kubernetes_monitor import ContainerEvent, create_pipeline_reconciliation_handler

        handler = create_pipeline_reconciliation_handler("/path/to/repo")

        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.STOPPED, info)

        with patch("kubernetes_monitor._reconcile_pod_state") as mock_reconcile:
            handler(event)
            mock_reconcile.assert_not_called()

    def test_handler_processes_failed(self):
        """Handler processes FAILED events via _reconcile_pod_state."""
        from kubernetes_monitor import ContainerEvent, create_pipeline_reconciliation_handler

        handler = create_pipeline_reconciliation_handler("/path/to/repo")

        info = ContainerInfo(container_id="u", container_name="n")
        event = ContainerEvent(ContainerEvent.FAILED, info)

        with (
            patch("kubernetes_monitor._reconcile_pod_state") as mock_reconcile,
            patch("state_store.get_state_store") as mock_store_fn,
        ):
            mock_reconcile.return_value = True
            handler(event)
            mock_store_fn.assert_called_once_with("/path/to/repo")
            mock_reconcile.assert_called_once()


# ---------------------------------------------------------------------------
# TestPeriodicReconciliation
# ---------------------------------------------------------------------------


class TestPeriodicReconciliation:
    """Test start_periodic_reconciliation method."""

    def test_start_sets_flag(self, monitor):
        """start_periodic_reconciliation sets the running flag."""
        mock_store = MagicMock()
        monitor.start_periodic_reconciliation(mock_store, interval=1)
        try:
            assert monitor._reconciliation_running is True
            assert monitor._reconciliation_thread is not None
        finally:
            monitor._reconciliation_running = False
            if monitor._reconciliation_thread:
                monitor._reconciliation_thread.join(timeout=3)

    def test_start_idempotent(self, monitor):
        """Calling start_periodic_reconciliation twice is safe."""
        mock_store = MagicMock()
        monitor.start_periodic_reconciliation(mock_store, interval=1)
        thread1 = monitor._reconciliation_thread
        monitor.start_periodic_reconciliation(mock_store, interval=1)
        assert monitor._reconciliation_thread is thread1
        monitor._reconciliation_running = False
        if thread1:
            thread1.join(timeout=3)

    def test_accepts_list_of_stores(self, monitor):
        """start_periodic_reconciliation accepts a list of stores."""
        stores = [MagicMock(), MagicMock()]
        monitor.start_periodic_reconciliation(stores, interval=1)
        assert monitor._reconciliation_stores == stores
        monitor._reconciliation_running = False
        if monitor._reconciliation_thread:
            monitor._reconciliation_thread.join(timeout=3)

    def test_stop_clears_reconciliation(self, monitor):
        """stop() also stops periodic reconciliation."""
        mock_store = MagicMock()
        monitor.start_periodic_reconciliation(mock_store, interval=1)
        monitor.stop()
        assert monitor._reconciliation_running is False


# ---------------------------------------------------------------------------
# TestReconciliationSweep
#
# Covers the false-positive fixes from issue #1760: agents register their
# ``container_id`` as the Job UID, but ``list_containers`` returns Pod
# UIDs — so without the Job-UID index and the termination guard, every
# Pending/Running pod was being marked FAILED within ~15s of spawn.
# ---------------------------------------------------------------------------


class TestReconciliationSweep:
    """Test _reconciliation_sweep termination and grace-period logic."""

    def _make_pipeline(
        self,
        *,
        container_id: str,
        ci_status: ContainerStatus = ContainerStatus.RUNNING,
        agent_started_at: datetime | None = None,
    ):
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        agent = AgentExecution(
            role="coder",
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=agent_started_at,
        )
        ci = ContainerInfo(
            container_id=container_id,
            container_name="job-1",
            status=ci_status,
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[agent],
            containers=[ci],
        )
        pipeline = Pipeline(
            id="pipe-1",
            issue_number=1,
            repo="owner/repo",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            phases={"implement": phase_exec},
        )
        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        return pipeline, store

    def test_running_pod_not_reconciled(self, monitor, mock_k8s_client):
        """A pod whose state is still Running must not be marked FAILED.

        Regression test for #1760: the reconciler previously flagged
        newly-spawned pods as exited because get_container_info returned
        no exit_code for Running pods and the code fell through to the
        FAILED path.
        """
        # live_ids is empty — simulating the old bug where Job UIDs
        # were not indexed.
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.return_value = ContainerInfo(
            container_id="job-uid-1",
            container_name="job-1",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_not_called()

    def test_pending_pod_not_reconciled(self, monitor, mock_k8s_client):
        """A Pending / ContainerCreating pod must not be marked FAILED."""
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.return_value = ContainerInfo(
            container_id="job-uid-1",
            container_name="job-1",
            status=ContainerStatus.PENDING,
        )
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_not_called()

    def test_grace_period_skips_recent_agents(self, monitor, mock_k8s_client):
        """Agents spawned within POD_STARTUP_GRACE_SECONDS are skipped.

        The termination check should never be reached during the grace
        period — so we set get_container_info to raise to prove it.
        """
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.side_effect = AssertionError(
            "should not be called inside grace period"
        )
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=datetime.now(UTC),
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_not_called()

    def test_grace_period_skipped_when_started_at_none(self, monitor, mock_k8s_client):
        """When started_at is None the grace period is bypassed.

        An agent with ``started_at=None`` (e.g. incomplete timestamp
        initialisation) should be treated as "old" — the sweep should
        fall through to the termination check rather than skipping
        indefinitely.
        """
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        # Pod is truly gone — should proceed to FAILED reconciliation.
        mock_k8s_client.get_container_info.side_effect = PodNotFoundError("gone")
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=None,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        # started_at=None means the grace period is not applied, so the
        # sweep should reach the termination check.  With PodNotFoundError
        # the pod is confirmed gone and reconciliation should fire.
        store.save_pipeline.assert_called()

    def test_job_uid_in_live_ids_skips_reconciliation(self, monitor, mock_k8s_client):
        """list_jobs results are indexed, so Job UID matches live_ids."""
        from kubernetes_monitor import LABEL_ORCHESTRATOR

        mock_k8s_client.namespace = "egg-agents"
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = [
            ContainerInfo(
                container_id="job-uid-1",
                container_name="egg-sandbox-1",
                status=ContainerStatus.RUNNING,
                job_name="egg-sandbox-1",
            )
        ]
        mock_k8s_client.get_container_info.side_effect = AssertionError(
            "should not be called when Job UID is in live_ids"
        )
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_not_called()
        # Verify list_jobs was queried with the orchestrator label
        call_kwargs = mock_k8s_client.list_jobs.call_args
        assert call_kwargs.kwargs["label_selector"] == f"{LABEL_ORCHESTRATOR}=true"

    def test_terminated_pod_marks_failed(self, monitor, mock_k8s_client):
        """A pod with exited_at set and non-zero exit is reconciled."""
        from models import PipelineStatus

        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.return_value = ContainerInfo(
            container_id="job-uid-1",
            container_name="job-1",
            status=ContainerStatus.FAILED,
            exit_code=1,
            exited_at=datetime.now(UTC),
        )
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_called_once()
        saved = store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.FAILED

    def test_missing_pod_marks_failed(self, monitor, mock_k8s_client):
        """A pod that has been deleted (PodNotFoundError) is reconciled."""
        from models import PipelineStatus

        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.side_effect = PodNotFoundError("gone")
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_called_once()
        saved = store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.FAILED

    def test_terminal_status_without_exited_at_not_reconciled(self, monitor, mock_k8s_client):
        """A half-populated terminal status (no exited_at) is skipped.

        Guards against edge cases where the pod phase mapping produced
        ``EXITED`` from ``Succeeded`` without the API having populated
        ``containerStatuses[0].state.terminated.finished_at`` yet.
        """
        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []
        mock_k8s_client.get_container_info.return_value = ContainerInfo(
            container_id="job-uid-1",
            container_name="job-1",
            status=ContainerStatus.EXITED,
            exit_code=None,
            exited_at=None,
        )
        spawned_long_ago = datetime.now(UTC).replace(year=2024)
        _, store = self._make_pipeline(
            container_id="job-uid-1",
            agent_started_at=spawned_long_ago,
        )
        monitor._reconciliation_stores = [store]

        with patch("state_store.get_pipeline_state_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)
            monitor._reconciliation_sweep()

        store.save_pipeline.assert_not_called()
