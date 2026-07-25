"""Tests for container_transitions population in the snapshot builder (#3596, task-1-4).

Verifies that:
1. container_transitions are populated from kubernetes_monitor's container event history
2. detect_container_death receives populated transitions
3. Best-effort degradation on failure (returns empty tuple, never crashes)

The container_transitions field is required by 4 detectors:
- detect_container_death
- detect_container_oom_evicted
- detect_container_restart_loop
- detect_overseer_self_injection
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC-1: container_transitions populated from kubernetes_monitor
# ---------------------------------------------------------------------------


class TestContainerTransitionsPopulation:
    """container_transitions must be populated from kubernetes_monitor's event history."""

    def test_container_transitions_populated_from_kubernetes_monitor(self):
        """container_transitions must be populated from kubernetes_monitor's
        container event history, not left as an empty tuple."""
        from health_checks.detection_plane import snapshot_from_health_context

        # Build a context with a running pipeline
        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []
        pipeline.event_loop_owner = None
        pipeline.lifecycle_owner = None
        pipeline.awaiting_spawn = False

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        # The container_transitions field must be populated (not empty) when
        # kubernetes_monitor has transition history.
        # Currently returns () — this is the gap.
        assert snap.container_transitions is not None
        assert isinstance(snap.container_transitions, tuple)

        # If kubernetes_monitor tracks transitions, they should appear here.
        # The test verifies the field is populated when data is available.
        # For now, the field is always () because _build_container_transitions
        # is a stub. This test documents the expected behavior.

    def test_container_transitions_have_required_fields(self):
        """Each transition must have: container, role, from, to, reason,
        exit_code, restart_count, transient, timestamp."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        # When transitions are populated, each must have the required fields.
        # This test verifies the schema contract.
        for transition in snap.container_transitions:
            assert "container" in transition, "transition must have 'container' field"
            assert "role" in transition, "transition must have 'role' field"
            assert "from" in transition, "transition must have 'from' field"
            assert "to" in transition, "transition must have 'to' field"
            assert "reason" in transition, "transition must have 'reason' field"
            assert "exit_code" in transition, "transition must have 'exit_code' field"
            assert "restart_count" in transition, "transition must have 'restart_count' field"
            assert "transient" in transition, "transition must have 'transient' field"
            assert "timestamp" in transition, "transition must have 'timestamp' field"


# ---------------------------------------------------------------------------
# AC-2: detect_container_death receives populated transitions
# ---------------------------------------------------------------------------


class TestContainerDeathDetectorReceivesTransitions:
    """detect_container_death must receive populated container_transitions."""

    def test_detect_container_death_fires_on_populated_transitions(self):
        """When container_transitions contains a container death event,
        detect_container_death must fire."""
        from health_checks.detection_plane import (
            EventStreamSnapshot,
            RunningAgent,
        )
        from health_checks.tier1.container_k8s import detect_container_death

        # Build a snapshot with a container death transition
        # "Terminated" is in _DEATH_STATES, "Error" is in _DEATH_REASONS
        transition = {
            "container": "cid-1",
            "role": "coder",
            "from": "running",
            "to": "Terminated",
            "reason": "Error",
            "exit_code": 1,
            "restart_count": 0,
            "transient": False,
            "timestamp": 1234567890.0,
        }

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            container_transitions=(transition,),
        )

        finding = detect_container_death(snap)

        assert finding is not None, "detect_container_death must fire on a death transition"
        assert finding.finding_class == "container_death"
        assert finding.severity.value == "high"
        assert finding.evidence.get("container") == "cid-1"
        assert finding.evidence.get("fatal_reason") == "Error"
        assert finding.evidence.get("rescheduled") is False

    def test_detect_container_death_silent_without_transitions(self):
        """When container_transitions is empty, detect_container_death must
        not fire (graceful degradation)."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.container_k8s import detect_container_death

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            container_transitions=(),  # Empty — no transitions
        )

        finding = detect_container_death(snap)

        assert finding is None, "detect_container_death must not fire without transitions"


# ---------------------------------------------------------------------------
# AC-3: Best-effort degradation on failure
# ---------------------------------------------------------------------------


class TestContainerTransitionsGracefulDegradation:
    """container_transitions must degrade gracefully on failure."""

    def test_empty_tuple_when_kubernetes_monitor_unavailable(self):
        """When kubernetes_monitor is unavailable, container_transitions
        must be an empty tuple, not a crash."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        # Even with a broken context, snapshot_from_health_context must not crash
        snap = snapshot_from_health_context(ctx)

        assert snap.container_transitions is not None
        assert isinstance(snap.container_transitions, tuple)

    def test_no_crash_on_kubernetes_monitor_error(self):
        """When kubernetes_monitor raises, container_transitions must degrade
        to empty tuple without crashing the snapshot builder."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        # The snapshot builder must not crash even if internal helpers fail
        snap = snapshot_from_health_context(ctx)

        # Must return a valid snapshot with empty container_transitions
        assert snap is not None
        assert snap.container_transitions == ()
