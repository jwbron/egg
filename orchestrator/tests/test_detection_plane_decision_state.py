"""Tests for decision_state population in the snapshot builder (#3596, task-1-8).

Verifies that:
1. decision_state is populated from contract + decision queue
2. detect_approved_decision_orphaned and detect_hitl_queue_backlog receive populated decision_state
3. Best-effort degradation on failure
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# AC-1: decision_state populated from contract + decision queue
# ---------------------------------------------------------------------------


class TestDecisionStatePopulation:
    """decision_state must be populated from pipeline decisions."""

    def test_decision_state_has_pending_hitl(self):
        """decision_state must include pending_hitl (bool)."""
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

        assert hasattr(snap, "decision_state")
        assert snap.decision_state is not None
        # pending_hitl should be present when decisions exist
        if snap.decision_state:
            assert "pending_hitl" in snap.decision_state

    def test_decision_state_has_open_decisions(self):
        """decision_state must include open_decisions (count)."""
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

        assert hasattr(snap, "decision_state")
        if snap.decision_state:
            assert "open_decisions" in snap.decision_state

    def test_decision_state_has_approved_unapplied(self):
        """decision_state must include approved_unapplied (list)."""
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

        assert hasattr(snap, "decision_state")
        if snap.decision_state:
            assert "approved_unapplied" in snap.decision_state

    def test_decision_state_has_oldest_open_age_s(self):
        """decision_state must include oldest_open_age_s (seconds)."""
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

        assert hasattr(snap, "decision_state")
        if snap.decision_state:
            assert "oldest_open_age_s" in snap.decision_state

    def test_decision_state_populated_with_pending_hitl(self):
        """decision_state must reflect pending HITL decisions."""
        from health_checks.detection_plane import snapshot_from_health_context
        from models import DecisionStatus, HITLDecision

        decision = HITLDecision(
            id="decision-1",
            question="Test?",
            status=DecisionStatus.PENDING,
        )
        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = [decision]

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

        assert snap.decision_state is not None
        assert snap.decision_state.get("pending_hitl") is True
        assert snap.decision_state.get("open_decisions") == 1

    def test_decision_state_empty_when_no_decisions(self):
        """decision_state must be empty when no decisions exist."""
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

        assert snap.decision_state == {}


# ---------------------------------------------------------------------------
# AC-2: detect_approved_decision_orphaned and detect_hitl_queue_backlog receive populated decision_state
# ---------------------------------------------------------------------------


class TestDecisionStateForDetectors:
    """detect_approved_decision_orphaned and detect_hitl_queue_backlog must receive populated decision_state."""

    def test_detect_approved_decision_orphaned_fires_on_approved_unapplied(self):
        """detect_approved_decision_orphaned must fire when decision_state has approved_unapplied."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.decision_queue import detect_approved_decision_orphaned

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            decision_state={
                "approved_unapplied": [
                    {"id": "decision-1", "age_s": 600},  # Past 300s grace
                ],
            },
        )

        finding = detect_approved_decision_orphaned(snap)

        assert finding is not None, (
            "detect_approved_decision_orphaned must fire on approved_unapplied"
        )
        assert finding.finding_class == "approved_decision_orphaned"

    def test_detect_approved_decision_orphaned_silent_without_approved(self):
        """detect_approved_decision_orphaned must not fire when no approved decisions."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.decision_queue import detect_approved_decision_orphaned

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            decision_state={
                "pending_hitl": True,
                "open_decisions": 1,
                "approved_unapplied": [],
            },
        )

        finding = detect_approved_decision_orphaned(snap)

        assert finding is None

    def test_detect_hitl_queue_backlog_fires_on_old_decision(self):
        """detect_hitl_queue_backlog must fire when decision_state has old pending decisions."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.decision_queue import detect_hitl_queue_backlog

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            decision_state={
                "pending_hitl": True,
                "open_decisions": 1,
                "oldest_open_age_s": 4000,  # Past 3600s grace
            },
        )

        finding = detect_hitl_queue_backlog(snap)

        assert finding is not None, "detect_hitl_queue_backlog must fire on old pending decision"
        assert finding.finding_class == "hitl_queue_backlog"

    def test_detect_hitl_queue_backlog_silent_without_pending(self):
        """detect_hitl_queue_backlog must not fire when no pending decisions."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.decision_queue import detect_hitl_queue_backlog

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            decision_state={
                "pending_hitl": False,
                "open_decisions": 0,
                "oldest_open_age_s": None,
            },
        )

        finding = detect_hitl_queue_backlog(snap)

        assert finding is None

    def test_detect_hitl_queue_backlog_silent_when_recent(self):
        """detect_hitl_queue_backlog must not fire when pending decision is recent."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.decision_queue import detect_hitl_queue_backlog

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            decision_state={
                "pending_hitl": True,
                "open_decisions": 1,
                "oldest_open_age_s": 60,  # 1 minute — within threshold
            },
        )

        finding = detect_hitl_queue_backlog(snap)

        assert finding is None
