"""Tests for detection plane wiring into the runtime tick (#3596, task-1-2).

Verifies that:
1. ``run_detection_plane()`` is called from ``_run_runtime_tick_checks``
2. Findings are emitted as DETECTION_FINDING events on the event bus
3. Evaluation is idempotent per tick (no double-firing from two call sites)

The detection plane must be invoked on every RUNTIME_TICK so that the 27
registered detectors can actually fire. Without this wiring, all detectors
remain dormant regardless of how rich the snapshot is.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_k8s_client():
    """Create a mock KubernetesClient."""
    from kubernetes_client import ContainerInfo, ContainerStatus
    from datetime import UTC, datetime

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


def _make_running_pipeline():
    """Build a mock pipeline + store pair that reports RUNNING status."""
    from models import PipelineStatus

    pipeline = MagicMock()
    pipeline.id = "test-pipeline"
    pipeline.status = PipelineStatus.RUNNING
    pipeline.phases = {}
    pipeline.base_branch = "main"
    pipeline.repo = "owner/repo"
    pipeline.decisions = []
    pipeline.event_loop_owner = None
    pipeline.lifecycle_owner = None
    pipeline.awaiting_spawn = False

    store = MagicMock()
    store.list_pipelines.return_value = ["test-pipeline"]
    store.load_pipeline.return_value = pipeline
    store.repo_path = "/tmp/repo"

    return pipeline, store


# ---------------------------------------------------------------------------
# AC-1: run_detection_plane() is called from _run_runtime_tick_checks
# ---------------------------------------------------------------------------


class TestDetectionPlaneWiring:
    """The detection plane must be invoked from the runtime tick."""

    def test_run_detection_plane_called_from_runtime_tick(self, monitor):
        """``_run_runtime_tick_checks`` must call ``runner.run_detection_plane()``.

        The detection plane has 27 registered detectors but is never invoked
        in production — this is the critical wiring that makes them live.
        """
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        # run_detection_plane returns a list of findings (possibly empty)
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        # The runner must have been asked to evaluate the detection plane
        mock_runner.run_detection_plane.assert_called_once()

    def test_run_detection_plane_receives_snapshot(self, monitor):
        """The snapshot passed to ``run_detection_plane`` must be built from
        the pipeline's health context."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        call_args = mock_runner.run_detection_plane.call_args
        snapshot = call_args.args[0] if call_args.args else call_args.kwargs.get("snapshot")

        # The snapshot must be an EventStreamSnapshot with the pipeline_id set
        assert snapshot is not None
        assert snapshot.pipeline_id == "test-pipeline"

    def test_run_detection_plane_receives_plane(self, monitor):
        """The detection plane passed must be the default plane with
        registered detectors."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        call_args = mock_runner.run_detection_plane.call_args
        plane = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("plane")

        # The plane must have detectors registered
        assert plane is not None
        assert len(plane.detectors) > 0
        # forward_progress must be among them
        assert "forward_progress" in plane.detectors


# ---------------------------------------------------------------------------
# AC-2: Findings are emitted as DETECTION_FINDING events
# ---------------------------------------------------------------------------


class TestDetectionFindingEvents:
    """Detection findings must be emitted on the event bus."""

    def test_findings_emitted_as_events(self, monitor):
        """Findings from the detection plane must be emitted on the event bus
        as DETECTION_FINDING events."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        # Create a real finding
        from health_checks.types import Finding, Severity

        finding = Finding(
            finding_class="forward_progress_stall",
            severity=Severity.MEDIUM,
            evidence={"agent_role": "coder", "last_commit_age_s": 700},
            recommended_action="Agent not making progress",
            requires_adjudication=False,
            detector_key="forward_progress",
        )

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = [finding]
        monitor.set_health_check_runner(mock_runner)

        with patch("events.get_event_bus") as mock_bus_fn:
            mock_bus = MagicMock()
            mock_bus_fn.return_value = mock_bus

            monitor._run_runtime_tick_checks()

            # The finding must have been emitted on the event bus
            mock_bus.emit.assert_called()
            # Check that at least one emit call has the finding data
            emit_calls = mock_bus.emit.call_args_list
            finding_emitted = False
            for ec in emit_calls:
                args = ec.args
                if len(args) >= 3:
                    data = args[2] if len(args) > 2 else ec.kwargs.get("data")
                    if isinstance(data, dict) and data.get("finding_class") == "forward_progress_stall":
                        finding_emitted = True
                        break
            assert finding_emitted, "Finding must be emitted on the event bus"

    def test_no_findings_no_emit(self, monitor):
        """When no findings, no DETECTION_FINDING events are emitted."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []  # No findings
        monitor.set_health_check_runner(mock_runner)

        with patch("events.get_event_bus") as mock_bus_fn:
            mock_bus = MagicMock()
            mock_bus_fn.return_value = mock_bus

            monitor._run_runtime_tick_checks()

            # No finding-emit calls should have been made
            emit_calls = mock_bus.emit.call_args_list
            for ec in emit_calls:
                args = ec.args
                if len(args) >= 3:
                    data = args[2] if len(args) > 2 else ec.kwargs.get("data")
                    if isinstance(data, dict) and "finding_class" in data:
                        pytest.fail("Should not emit findings when detection plane returns empty")


# ---------------------------------------------------------------------------
# AC-3: Idempotent evaluation (no double-firing from two call sites)
# ---------------------------------------------------------------------------


class TestIdempotentEvaluation:
    """Evaluation must be idempotent per tick — no double-firing from the
    two call sites (_check_pod and _reconciliation_sweep)."""

    def test_no_double_evaluation_from_check_pod(self, monitor):
        """When _check_pod triggers _run_runtime_tick_checks, the detection
        plane should be evaluated exactly once, not twice."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        # Simulate _check_pod calling _run_runtime_tick_checks
        monitor._run_runtime_tick_checks()

        # Should be called exactly once
        assert mock_runner.run_detection_plane.call_count == 1

    def test_no_double_evaluation_from_reconciliation_sweep(self, monitor, mock_k8s_client):
        """When _reconciliation_sweep triggers _run_runtime_tick_checks, the
        detection plane should be evaluated exactly once."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        with patch.object(monitor, "_run_runtime_tick_checks") as mock_tick:
            monitor._reconciliation_sweep()

        # _reconciliation_sweep should call _run_runtime_tick_checks exactly once
        mock_tick.assert_called_once()
