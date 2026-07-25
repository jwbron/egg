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
                "agent_last_commit_age_s": {"coder": 700, "tester": 30},  # Coder stalled, tester healthy
            },
        )
        result = detect_forward_progress(snap)

        assert result is not None
        assert result.finding_class == FINDING_FORWARD_PROGRESS_STALL
        assert result.evidence["agent_role"] == "coder"

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
        assert result.requires_adjudication is False
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
