"""Slice-2 contract tests for detection plane wiring (issue #3665).

Verifies that:
- The detection plane is invoked from the RUNTIME_TICK path (TASK-2-1)
- Double-evaluation is guarded (no duplicate findings from dual call sites)
- Consensus-stall double-fire guard prevents duplicate reporting (TASK-2-2)
- Findings are routed to the operator alert surface (TASK-2-3)

These tests use lightweight stubs rather than the full orchestrator stack.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Make orchestrator/ importable
_tests_dir = Path(__file__).parent
_orchestrator_dir = _tests_dir.parent
if str(_orchestrator_dir) not in sys.path:
    sys.path.insert(0, str(_orchestrator_dir))

detection_plane = pytest.importorskip("health_checks.detection_plane")


# ---------------------------------------------------------------------------
# Test helpers — lightweight stubs
# ---------------------------------------------------------------------------


def _make_pipeline(
    *,
    pipeline_id: str = "issue-3665",
    phase: str = "implement",
    status: str = "running",
):
    """Build a minimal Pipeline stub."""
    phase_exec = SimpleNamespace(
        status=SimpleNamespace(value=status),
        started_at=None,
        agents=[],
        containers=[],
    )
    phases = {phase: phase_exec}
    return SimpleNamespace(
        id=pipeline_id,
        status=SimpleNamespace(value=status),
        current_phase=SimpleNamespace(value=phase),
        phases=phases,
        repo=None,
        branch=None,
        event_loop_owner=None,
        lifecycle_owner=None,
        awaiting_spawn=False,
        issue_number=3665,
        network_mode="public",
        repos=None,
    )


def _make_context(pipeline=None, pipeline_id: str = "issue-3665", phase: str = "implement"):
    """Build a minimal PipelineHealthContext-like stub."""
    if pipeline is None:
        pipeline = _make_pipeline(pipeline_id=pipeline_id, phase=phase)
    return SimpleNamespace(
        pipeline=pipeline,
        pipeline_id=pipeline_id,
        current_phase=SimpleNamespace(value=phase),
        trigger="runtime_tick",
        repo_path=Path("/tmp/test"),
        docker_client=None,
        state_store=SimpleNamespace(repo_path=Path("/tmp/test")),
        live_container_ids=set(),
        lifecycle_owner="orchestrator",
        awaiting_spawn=False,
        event_loop_owner=None,
        phase_started_age_s=120.0,
    )


# ---------------------------------------------------------------------------
# TASK-2-1: Detection plane invoked on RUNTIME_TICK
# ---------------------------------------------------------------------------


class TestDetectionPlaneInvocation:
    """Verify the detection plane is called from the RUNTIME_TICK path."""

    def test_detection_plane_runs_on_runtime_tick(self):
        """_run_detection_plane_for_pipeline evaluates the plane and returns findings."""
        from health_checks.detection_plane import EventStreamSnapshot

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        # Create a monitor with a mock runner
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        # Mock the runner.run_detection_plane to return findings
        mock_finding = MagicMock()
        mock_finding.requires_adjudication = False
        mock_finding.finding_class = "test_finding"
        mock_finding.severity = "high"
        mock_finding.evidence = {}
        mock_finding.recommended_action = "test action"
        mock_finding.detector_key = "test_detector"

        monitor._health_check_runner.run_detection_plane.return_value = [mock_finding]

        # Mock snapshot_from_health_context to return a simple snapshot
        fake_snapshot = EventStreamSnapshot(snapshot_id="test:implement")

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane

            monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")

            # Verify run_detection_plane was called
            monitor._health_check_runner.run_detection_plane.assert_called_once()
            call_args = monitor._health_check_runner.run_detection_plane.call_args
            assert call_args[0][0] is fake_snapshot  # snapshot
            assert call_args[1]["pipeline_id"] == "issue-3665"

    def test_detection_plane_skipped_when_no_runner(self):
        """Detection plane is skipped when no health check runner is set."""
        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = None
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        # Should not raise
        monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")


# ---------------------------------------------------------------------------
# Double-evaluation guard
# ---------------------------------------------------------------------------


class TestDoubleEvaluationGuard:
    """Verify the double-evaluation guard prevents duplicate findings."""

    def test_double_evaluation_guard_prevents_duplicate(self):
        """Running the plane twice within 5s skips the second call."""
        from health_checks.detection_plane import EventStreamSnapshot

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        fake_snapshot = EventStreamSnapshot(snapshot_id="test:implement")

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane
            monitor._health_check_runner.run_detection_plane.return_value = []

            # First call should run
            monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")
            assert monitor._health_check_runner.run_detection_plane.call_count == 1

            # Second call within 5s should be skipped
            monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")
            assert monitor._health_check_runner.run_detection_plane.call_count == 1

    def test_double_evaluation_guard_allows_after_window(self):
        """Running the plane after 5s is allowed."""
        from health_checks.detection_plane import EventStreamSnapshot

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        fake_snapshot = EventStreamSnapshot(snapshot_id="test:implement")

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane
            monitor._health_check_runner.run_detection_plane.return_value = []

            # First call
            monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")
            assert monitor._health_check_runner.run_detection_plane.call_count == 1

            # Simulate time passing > 5s
            import time
            monitor._detection_plane_last_tick["issue-3665"] = time.monotonic() - 6.0

            # Second call should run
            monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")
            assert monitor._health_check_runner.run_detection_plane.call_count == 2


# ---------------------------------------------------------------------------
# TASK-2-2: Consensus-stall double-fire guard
# ---------------------------------------------------------------------------


class TestConsensusStallDoubleFireGuard:
    """Verify detect_heartbeat_stall is suppressed when ConsensusStallCheck fires."""

    def test_heartbeat_stall_suppressed_when_consensus_stall_fired(self):
        """When ConsensusStallCheck reports DEGRADED, heartbeat_stall findings are filtered."""
        from health_checks.detection_plane import EventStreamSnapshot, Finding, FindingClass, Severity

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        # Create a heartbeat_stall finding
        hb_finding = Finding(
            finding_class=FindingClass.HEARTBEAT_STALL,
            severity=Severity.HIGH,
            evidence={"role": "coder"},
            recommended_action="nudge",
            requires_adjudication=False,
            detector_key="heartbeat_stall",
        )
        # Create a routine finding (should NOT be suppressed)
        routine_finding = Finding(
            finding_class="container_death",
            severity=Severity.HIGH,
            evidence={"pod": "pod-1"},
            recommended_action="investigate",
            requires_adjudication=False,
            detector_key="container_death",
        )

        monitor._health_check_runner.run_detection_plane.return_value = [
            hb_finding, routine_finding
        ]

        # Create a mock HealthResult for ConsensusStallCheck that is DEGRADED
        from health_checks.types import HealthStatus

        mock_consensus_result = SimpleNamespace(
            check_name="consensus_stall",
            status=HealthStatus.DEGRADED,
        )
        mock_healthy_result = SimpleNamespace(
            check_name="driver_liveness",
            status=HealthStatus.HEALTHY,
        )

        fake_snapshot = EventStreamSnapshot(snapshot_id="test:implement")

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane

            # Capture the findings passed to _handle_detection_plane_findings
            captured_findings = []
            original_handler = monitor._handle_detection_plane_findings

            def capture_findings(findings, *args, **kwargs):
                captured_findings.extend(findings)

            monitor._handle_detection_plane_findings = capture_findings

            monitor._run_detection_plane_for_pipeline(
                ctx, pipeline, store, "issue-3665",
                health_results=[mock_consensus_result, mock_healthy_result]
            )

            # heartbeat_stall should be filtered out, routine finding should remain
            finding_keys = [f.detector_key for f in captured_findings]
            assert "heartbeat_stall" not in finding_keys
            assert "container_death" in finding_keys

    def test_heartbeat_stall_not_suppressed_when_consensus_stall_healthy(self):
        """When ConsensusStallCheck is HEALTHY, heartbeat_stall findings are NOT filtered."""
        from health_checks.detection_plane import EventStreamSnapshot, Finding, FindingClass, Severity

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        hb_finding = Finding(
            finding_class=FindingClass.HEARTBEAT_STALL,
            severity=Severity.HIGH,
            evidence={"role": "coder"},
            recommended_action="nudge",
            requires_adjudication=False,
            detector_key="heartbeat_stall",
        )

        monitor._health_check_runner.run_detection_plane.return_value = [hb_finding]

        # ConsensusStallCheck is HEALTHY (not DEGRADED)
        from health_checks.types import HealthStatus

        mock_consensus_result = SimpleNamespace(
            check_name="consensus_stall",
            status=HealthStatus.HEALTHY,
        )

        fake_snapshot = EventStreamSnapshot(snapshot_id="test:implement")

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane

            captured_findings = []

            def capture_findings(findings, *args, **kwargs):
                captured_findings.extend(findings)

            monitor._handle_detection_plane_findings = capture_findings

            monitor._run_detection_plane_for_pipeline(
                ctx, pipeline, store, "issue-3665",
                health_results=[mock_consensus_result]
            )

            # heartbeat_stall should NOT be filtered out
            finding_keys = [f.detector_key for f in captured_findings]
            assert "heartbeat_stall" in finding_keys


# ---------------------------------------------------------------------------
# TASK-2-3: Findings routed to alert surface
# ---------------------------------------------------------------------------


class TestFindingRouting:
    """Verify detection-plane findings are routed to the operator alert surface."""

    def test_routine_finding_broadcast_as_alert(self):
        """Routine findings (requires_adjudication=False) are broadcast as OVERSEER_ALERT."""
        from health_checks.detection_plane import Finding, FindingClass, Severity

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        routine_finding = Finding(
            finding_class=FindingClass.CONTAINER_DEATH,
            severity=Severity.HIGH,
            evidence={"pod": "pod-1"},
            recommended_action="investigate container death",
            requires_adjudication=False,
            detector_key="container_death",
        )

        fake_snapshot = SimpleNamespace()

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane
            monitor._health_check_runner.run_detection_plane.return_value = [routine_finding]

            with patch("message_store.get_message_store") as mock_store_fn:
                mock_msg_store = MagicMock()
                mock_store_fn.return_value = mock_msg_store

                monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")

                # Verify a message was added to the message store
                mock_msg_store.add_message.assert_called_once()
                msg = mock_msg_store.add_message.call_args[0][0]
                assert msg.message_type == "OVERSEER_ALERT"
                assert "container_death" in msg.subject

    def test_adjudication_finding_not_broadcast_as_routine(self):
        """Findings with requires_adjudication=True are NOT broadcast as routine alerts."""
        from health_checks.detection_plane import Finding, FindingClass, Severity

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()

        adjudication_finding = Finding(
            finding_class=FindingClass.PHASE_STALL,
            severity=Severity.HIGH,
            evidence={"phase": "implement"},
            recommended_action="advance or fail the wedged phase",
            requires_adjudication=True,
            detector_key="phase_stall",
        )

        fake_snapshot = SimpleNamespace()

        with patch(
            "health_checks.detection_plane.snapshot_from_health_context",
            return_value=fake_snapshot,
        ), patch(
            "health_checks.detection_plane.default_detection_plane"
        ) as mock_plane_fn:
            mock_plane = MagicMock()
            mock_plane_fn.return_value = mock_plane
            monitor._health_check_runner.run_detection_plane.return_value = [adjudication_finding]

            with patch("message_store.get_message_store") as mock_store_fn:
                mock_msg_store = MagicMock()
                mock_store_fn.return_value = mock_msg_store

                # The adjudication finding should NOT be broadcast as a routine alert
                monitor._run_detection_plane_for_pipeline(ctx, pipeline, store, "issue-3665")

                # No routine broadcast for adjudication findings
                # (they go through _escalate_detection_findings instead)
                mock_msg_store.add_message.assert_not_called()
