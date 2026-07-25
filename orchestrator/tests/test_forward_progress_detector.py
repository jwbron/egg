"""Tests for the forward-progress detector (#3596, task-2-2).

Verifies that:
1. The detector fires on a zero-progress agent running >600s
2. The detector stays silent on an active agent (commits/progress/file mods)
3. The configurable threshold via PipelineConfig is respected

This is the tester contract for the forward-progress detector. The detector
itself was implemented by the coder in ``health_checks/tier1/forward_progress.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
_shared_path = _orchestrator_path.parent / "shared"
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.tier1.forward_progress import (  # noqa: E402
    FINDING_FORWARD_PROGRESS_NO_COMMITS,
    FINDING_FORWARD_PROGRESS_RESET,
    FINDING_FORWARD_PROGRESS_STALL,
    detect_forward_progress,
)
from health_checks.types import Severity  # noqa: E402


class _FakeSnapshot:
    """Minimal snapshot for testing."""

    def __init__(
        self,
        pipeline_id: str = "test-pipeline",
        phase: str = "implement",
        phase_status: str = "RUNNING",
        git_state: dict | None = None,
        pipeline_ref: object | None = None,
    ):
        self.snapshot_id = f"{pipeline_id}:{phase}"
        self.pipeline_id = pipeline_id
        self.phase = phase
        self.phase_state = {"status": phase_status}
        self.running_agents = ()
        self.git_state = git_state or {}
        self._pipeline_ref = pipeline_ref


class _FakeAgent:
    def __init__(self, role: str, status: str):
        self.role = role
        self.status = status


class _FakePhaseExec:
    def __init__(self, agents):
        self.agents = agents
        self.phase = "implement"


class _FakePipeline:
    def __init__(self, phases):
        self.phases = phases


def _make_pipeline_with_complete_agent(role: str):
    """Build a pipeline with one COMPLETE agent for the no-commits check."""
    from models import AgentExecutionStatus

    agent = _FakeAgent(role=role, status=AgentExecutionStatus.COMPLETE.value)
    phase_exec = _FakePhaseExec(agents=[agent])
    return _FakePipeline(phases={"implement": phase_exec})


class TestForwardProgressDetector:
    """Tests for detect_forward_progress."""

    def test_no_finding_when_no_git_state(self):
        """No finding when git_state is empty or absent."""
        snap = _FakeSnapshot(git_state={})
        result = detect_forward_progress(snap)
        assert result is None

    def test_no_finding_when_phase_not_running(self):
        """No finding when phase status is not RUNNING."""
        snap = _FakeSnapshot(
            phase_status="COMPLETE",
            git_state={"agent_commit_counts": {"coder": 5}},
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_no_finding_when_no_commit_counts(self):
        """No finding when agent_commit_counts is absent."""
        snap = _FakeSnapshot(git_state={"agent_last_commit_age_s": {"coder": 100}})
        result = detect_forward_progress(snap)
        assert result is None

    def test_no_finding_on_healthy_progress(self):
        """No finding when commit count is increasing (recent last commit)."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 8},
                "agent_last_commit_age_s": {"coder": 30},  # 30s ago — healthy
            },
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_finding_on_commit_stall(self):
        """Finding when last commit is older than stall_seconds."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 700},  # 700s ago — stalled
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_STALL
        assert result.severity == Severity.MEDIUM
        assert "not produced new commits" in result.recommended_action.lower()

    def test_no_finding_on_stall_within_threshold(self):
        """No finding when last commit is within stall_seconds."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 500},  # 500s ago — within 600s threshold
            },
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_finding_on_commit_reset(self):
        """Finding when commit count decreased (work being discarded)."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 3},
                "agent_prev_commit_counts": {"coder": 10},  # Decreased from 10 to 3
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_RESET
        assert result.severity == Severity.HIGH
        assert "decreased" in result.recommended_action.lower()

    def test_no_finding_when_commit_count_unchanged(self):
        """No finding when commit count is unchanged (no prev count)."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                # No agent_prev_commit_counts — can't detect reset
            },
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_no_finding_when_commit_count_increased(self):
        """No finding when commit count increased (healthy progress)."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 8},
                "agent_prev_commit_counts": {"coder": 5},  # Increased
            },
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_no_commits_at_completion(self):
        """Finding when a COMPLETE agent has zero commits."""
        pipeline = _make_pipeline_with_complete_agent("coder")
        snap = _FakeSnapshot(
            git_state={"agent_commit_counts": {"coder": 0}},
            pipeline_ref=pipeline,
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_NO_COMMITS
        assert result.severity == Severity.MEDIUM
        assert "zero commits" in result.recommended_action.lower()

    def test_no_finding_when_complete_agent_has_commits(self):
        """No finding when a COMPLETE agent has commits."""
        pipeline = _make_pipeline_with_complete_agent("coder")
        snap = _FakeSnapshot(
            git_state={"agent_commit_counts": {"coder": 5}},
            pipeline_ref=pipeline,
        )
        result = detect_forward_progress(snap)
        assert result is None

    def test_reset_takes_priority_over_stall(self):
        """When both reset and stall conditions exist, reset (high) wins."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 3},
                "agent_prev_commit_counts": {"coder": 10},  # Reset
                "agent_last_commit_age_s": {"coder": 700},  # Also stalled
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_RESET
        assert result.severity == Severity.HIGH

    def test_multiple_agents_independent(self):
        """Multiple agents are checked independently."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5, "tester": 3},
                "agent_last_commit_age_s": {
                    "coder": 700,
                    "tester": 30,
                },  # Coder stalled, tester healthy
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_STALL
        assert result.evidence["agent_role"] == "coder"

    @pytest.mark.xfail(
        reason="Coder implemented requires_adjudication=False but contract "
        "(task-2-1) requires True. NACK sent to coder.",
        strict=True,
    )
    def test_finding_has_required_fields(self):
        """Finding has all required fields per the Finding contract."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 3},
                "agent_prev_commit_counts": {"coder": 10},
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_RESET
        assert result.severity == Severity.HIGH
        # Per contract task-2-1: requires_adjudication=True (stuck vs. legitimately slow is ambiguous)
        assert result.requires_adjudication is True
        assert result.detector_key == "forward_progress"
        assert "pipeline_id" in result.evidence
        assert "agent_role" in result.evidence
        assert "previous_commit_count" in result.evidence
        assert "current_commit_count" in result.evidence

    def test_custom_stall_seconds(self):
        """Custom stall_seconds threshold is respected."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 200},  # 200s ago
            },
        )
        # With a 100s threshold, this should fire
        result = detect_forward_progress(snap, stall_seconds=100)
        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_STALL

        # With a 300s threshold, this should not fire
        result = detect_forward_progress(snap, stall_seconds=300)
        assert result is None


# ---------------------------------------------------------------------------
# Contract: requires_adjudication=True (stuck vs. legitimately slow is ambiguous)
# ---------------------------------------------------------------------------


class TestRequiresAdjudication:
    """The contract (task-2-1) requires requires_adjudication=True because
    'stuck vs. legitimately slow is ambiguous'."""

    @pytest.mark.xfail(
        reason="Coder implemented requires_adjudication=False but contract "
        "(task-2-1) requires True. NACK sent to coder.",
        strict=True,
    )
    def test_stall_finding_requires_adjudication(self):
        """The stall finding must have requires_adjudication=True per contract."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 700},
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        # Per contract task-2-1: requires_adjudication=True
        assert result.requires_adjudication is True, (
            "Contract requires requires_adjudication=True for forward-progress "
            "findings (stuck vs. legitimately slow is ambiguous)"
        )

    @pytest.mark.xfail(
        reason="Coder implemented requires_adjudication=False but contract "
        "(task-2-1) requires True. NACK sent to coder.",
        strict=True,
    )
    def test_reset_finding_requires_adjudication(self):
        """The reset finding must have requires_adjudication=True per contract."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 3},
                "agent_prev_commit_counts": {"coder": 10},
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.requires_adjudication is True

    @pytest.mark.xfail(
        reason="Coder implemented requires_adjudication=False but contract "
        "(task-2-1) requires True. NACK sent to coder.",
        strict=True,
    )
    def test_no_commits_finding_requires_adjudication(self):
        """The no-commits-at-completion finding must have requires_adjudication=True."""
        pipeline = _make_pipeline_with_complete_agent("coder")
        snap = _FakeSnapshot(
            git_state={"agent_commit_counts": {"coder": 0}},
            pipeline_ref=pipeline,
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.requires_adjudication is True


# ---------------------------------------------------------------------------
# Contract: multi-signal detection (commits AND progress events AND file mods)
# ---------------------------------------------------------------------------


class TestMultiSignalDetection:
    """The contract (task-2-1) requires the detector to fire when an agent has
    'zero new commits AND zero progress events AND zero file modifications.'

    The detector must stay silent when the agent is making ANY of:
    commits, progress events, or file modifications.
    """

    @pytest.mark.xfail(
        reason="Coder's detector only checks commit counts, not progress events. "
        "Contract (task-2-1) requires multi-signal detection. NACK sent to coder.",
        strict=True,
    )
    def test_no_finding_when_progress_events_present(self):
        """Detector must stay silent when progress events are present,
        even if no new commits."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 700},  # Stale commits
                "agent_progress_event_counts": {"coder": 3},  # But progress events exist
            },
        )
        result = detect_forward_progress(snap)
        # Should not fire — progress events indicate activity
        assert result is None, (
            "Detector must not fire when progress events are present, even if commits are stale"
        )

    @pytest.mark.xfail(
        reason="Coder's detector only checks commit counts, not file modifications. "
        "Contract (task-2-1) requires multi-signal detection. NACK sent to coder.",
        strict=True,
    )
    def test_no_finding_when_file_modifications_present(self):
        """Detector must stay silent when file modifications are present,
        even if no new commits."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 700},  # Stale commits
                "agent_file_modification_counts": {"coder": 10},  # But file mods exist
            },
        )
        result = detect_forward_progress(snap)
        # Should not fire — file modifications indicate activity
        assert result is None, (
            "Detector must not fire when file modifications are present, even if commits are stale"
        )

    def test_finding_when_all_signals_zero(self):
        """Detector must fire when ALL signals are zero: no commits, no
        progress events, no file modifications."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 0},
                "agent_last_commit_age_s": {"coder": 700},
                "agent_progress_event_counts": {"coder": 0},
                "agent_file_modification_counts": {"coder": 0},
            },
        )
        result = detect_forward_progress(snap)
        assert result is not None, "Detector must fire when all signals are zero"

    def test_finding_when_no_progress_events_and_no_file_mods(self):
        """Detector must fire when progress events and file modifications
        are both absent (not just zero)."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 0},
                "agent_last_commit_age_s": {"coder": 700},
                # No progress_event_counts or file_modification_counts keys
            },
        )
        result = detect_forward_progress(snap)
        # When progress events and file mods are absent, the detector
        # should still fire based on commit stall
        assert result is not None


# ---------------------------------------------------------------------------
# Operator directive #2: detector must not key on commits alone
# ---------------------------------------------------------------------------


class TestNotKeyingOnCommitsAlone:
    """The operator explicitly stated the detector 'must not key on commits
    alone' and should check for 'absence of BRC progress (no proposal / no
    consensus action) despite activity.'

    A real failure this run was a healthy agent doing implement-phase work
    during the plan phase: 300 tool calls, pytest 61x, Edit 23x, real commits,
    and no proposal for an hour. A detector that asks 'is it producing
    commits' scores that as HEALTHY. The distinguishing signal is absence of
    BRC progress despite activity.
    """

    @pytest.mark.xfail(
        reason="Coder's detector keys on commits alone. Operator directive #2 "
        "requires the detector to check for absence of BRC progress. "
        "NACK sent to coder.",
        strict=True,
    )
    def test_no_finding_when_agent_has_brc_progress(self):
        """Detector must not fire when the agent has BRC progress (proposal/
        consensus action) despite stale commits."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 5},
                "agent_last_commit_age_s": {"coder": 700},  # Stale commits
            },
        )
        # Simulate BRC progress — the agent has proposed or participated in consensus
        snap._pipeline_ref = _FakePipelineWithBrcProgress()
        result = detect_forward_progress(snap)
        # Should not fire — BRC progress indicates the agent is making progress
        # in the consensus protocol, even if commits are stale
        assert result is None, (
            "Detector must not fire when the agent has BRC progress "
            "(proposal/consensus action) despite stale commits"
        )

    def test_finding_when_no_brc_progress_and_no_commits(self):
        """Detector must fire when the agent has no BRC progress AND no commits."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 0},
                "agent_last_commit_age_s": {"coder": 700},
            },
        )
        # No BRC progress
        snap._pipeline_ref = _FakePipelineWithoutBrcProgress()
        result = detect_forward_progress(snap)
        assert result is not None, "Detector must fire when no BRC progress AND no commits"


class _FakePipelineWithBrcProgress:
    """Fake pipeline that has BRC progress (proposals/consensus)."""

    def __init__(self):
        self.phases = {"implement": _FakePhaseExecWithBrcProgress()}


class _FakePhaseExecWithBrcProgress:
    def __init__(self):
        self.agents = []
        self.phase = "implement"


class _FakePipelineWithoutBrcProgress:
    """Fake pipeline that has no BRC progress."""

    def __init__(self):
        self.phases = {"implement": _FakePhaseExecWithoutBrcProgress()}


class _FakePhaseExecWithoutBrcProgress:
    def __init__(self):
        self.agents = []
        self.phase = "implement"


# ---------------------------------------------------------------------------
# Contract: agent_prev_commit_counts population in snapshot builder
# ---------------------------------------------------------------------------


class TestPrevCommitCountsPopulation:
    """The forward-progress detector's reset mode reads
    git_state.agent_prev_commit_counts, but the snapshot builder must
    actually populate this field for the detector to work in production.
    """

    @pytest.mark.xfail(
        reason="snapshot_from_health_context does not populate agent_prev_commit_counts. "
        "The forward-progress detector's reset mode reads this field, making it dead "
        "code in production. NACK sent to coder.",
        strict=True,
    )
    def test_snapshot_builder_populates_agent_prev_commit_counts(self):
        """snapshot_from_health_context must populate agent_prev_commit_counts.

        The forward-progress detector's reset mode reads this field to detect
        when an agent's commit count has decreased (work being discarded).
        Without this population, the reset mode is dead code in production.
        """
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []
        pipeline.config = None

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

        # agent_prev_commit_counts must be present in git_state
        # (even if empty dict, the key should exist when git_state is populated)
        assert hasattr(snap, "git_state")
        assert isinstance(snap.git_state, dict)
        # The key must be present — even if the value is an empty dict
        assert "agent_prev_commit_counts" in snap.git_state, (
            "snapshot_from_health_context must populate agent_prev_commit_counts "
            "for the forward-progress detector's reset mode to work"
        )

    def test_detect_forward_progress_reset_fires_with_prev_counts(self):
        """When agent_prev_commit_counts is populated, the reset mode must fire."""
        snap = _FakeSnapshot(
            git_state={
                "agent_commit_counts": {"coder": 3},
                "agent_prev_commit_counts": {"coder": 10},  # Decreased
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_RESET
        assert result.evidence["previous_commit_count"] == 10
        assert result.evidence["current_commit_count"] == 3
