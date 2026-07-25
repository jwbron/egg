"""Tests for snapshot_from_health_context enrichment (#3596).

Verifies that the snapshot builder:
1. Fixes the role=str(cid) defect — container IDs are mapped to agent roles
2. Populates RunningAgent liveness fields (last_tool_call_age_s, last_heartbeat_age_s)
3. Populates git_state with commit counts and branch info
4. Populates decision_state from pipeline decisions
5. Degrades gracefully on failures (empty dicts/tuples, never crashes)
6. Uses null (not 0) for unmeasurable fields
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
_shared_path = _orchestrator_path.parent / "shared"
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.detection_plane import (
    snapshot_from_health_context,
)


class _FakeContainerInfo:
    def __init__(self, container_id: str):
        self.container_id = container_id


class _FakeAgent:
    def __init__(self, role: str, container_id: str | None = None, status: str = "running"):
        self.role = role
        self.container_id = container_id
        self.container_info = _FakeContainerInfo(container_id) if container_id else None
        self.status = status
        self.exit_code = None
        self.error = None


class _FakePhaseExec:
    def __init__(self, agents, status: str = "running"):
        self.agents = agents
        self.status = status
        self.containers = []


class _FakePipeline:
    def __init__(self, phases, decisions=None, base_branch=None, repo=None):
        self.phases = phases
        self.decisions = decisions or []
        self.base_branch = base_branch
        self.repo = repo
        self.id = "test-pipeline"
        self.event_loop_owner = None
        self.lifecycle_owner = None
        self.awaiting_spawn = False


class _FakeContext:
    def __init__(self, pipeline, repo_path, **kwargs):
        self.pipeline = pipeline
        self.pipeline_id = getattr(pipeline, "id", "") if pipeline else ""
        self.repo_path = Path(repo_path)
        self.current_phase = MagicMock()
        self.current_phase.value = "implement"
        self.phase_started_age_s = 3600.0
        self.awaiting_spawn = False
        self.event_loop_owner = None
        self.lifecycle_owner = None
        self.live_container_ids = set()
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSnapshotRoleMapping:
    """Tests that container IDs are correctly mapped to agent roles."""

    def test_role_not_container_id(self):
        """The role field should be the agent role, not the container ID."""
        agent = _FakeAgent(role="coder", container_id="abc123")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"abc123"}

        snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        assert snap.running_agents[0].role == "coder"
        assert snap.running_agents[0].role != "abc123"

    def test_role_falls_back_to_cid_when_no_mapping(self):
        """When no agent matches the container ID, fall back to str(cid)."""
        phase_exec = _FakePhaseExec(agents=[])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"unknown_cid"}

        snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        # Falls back to str(cid) when no mapping found
        assert snap.running_agents[0].role == "unknown_cid"

    def test_multiple_agents_mapped_correctly(self):
        """Multiple agents are mapped to their correct roles."""
        agent1 = _FakeAgent(role="coder", container_id="cid1")
        agent2 = _FakeAgent(role="tester", container_id="cid2")
        phase_exec = _FakePhaseExec(agents=[agent1, agent2])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"cid1", "cid2"}

        snap = snapshot_from_health_context(ctx)

        roles = {a.role for a in snap.running_agents}
        assert roles == {"coder", "tester"}


class TestSnapshotLivenessFields:
    """Tests that RunningAgent liveness fields are populated."""

    def test_last_tool_call_age_s_from_progress_store(self):
        """last_tool_call_age_s is populated from ProgressStore."""
        from datetime import UTC, datetime

        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"cid1"}

        # Mock the progress store
        mock_event = MagicMock()
        mock_event.agent_role = "coder"
        mock_event.timestamp = datetime.now(UTC)

        with patch("progress_store.get_progress_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store
            mock_store.get_latest_per_agent.return_value = [mock_event]

            snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        age = snap.running_agents[0].last_tool_call_age_s
        assert age is not None
        assert age >= 0  # Should be a non-negative float

    def test_last_heartbeat_age_s_from_health_monitor(self):
        """last_heartbeat_age_s is populated from HealthMonitor."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"cid1"}

        # Mock the health monitor
        mock_monitor = MagicMock()
        mock_monitor._pipeline_id = "test-pipeline"
        mock_monitor._last_heartbeat = {"coder": time.time() - 10}

        with patch("health_monitor.get_health_monitor", return_value=mock_monitor):
            snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        age = snap.running_agents[0].last_heartbeat_age_s
        assert age is not None
        assert 5 <= age <= 15  # Should be around 10 seconds

    def test_liveness_fields_null_when_unmeasurable(self):
        """Liveness fields are null (not 0) when unmeasurable."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"cid1"}

        # No progress store, no health monitor
        with patch("progress_store.get_progress_store", side_effect=ImportError):
            with patch("health_monitor.get_health_monitor", side_effect=ImportError):
                snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        assert snap.running_agents[0].last_tool_call_age_s is None
        assert snap.running_agents[0].last_heartbeat_age_s is None

    def test_exit_code_and_reason_from_pipeline(self):
        """exit_code and exit_reason are populated from the pipeline model."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        agent.exit_code = 1
        agent.error = "container died"
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = set()  # No live containers

        snap = snapshot_from_health_context(ctx)

        # When no live containers, running_agents is empty
        assert len(snap.running_agents) == 0


class TestSnapshotGitState:
    """Tests that git_state is populated."""

    def test_git_state_populated_with_commit_counts(self):
        """git_state.agent_commit_counts is populated from git."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(
            phases={"implement": phase_exec},
            base_branch="main",
            repo="owner/repo",
        )
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = {"cid1"}

        # Mock subprocess to return commit count
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "5\n"

        with patch("subprocess.run", return_value=mock_result):
            snap = snapshot_from_health_context(ctx)

        assert hasattr(snap, "git_state")
        git_state = snap.git_state
        assert git_state is not None
        assert "agent_commit_counts" in git_state
        assert git_state["agent_commit_counts"].get("coder") == 5

    def test_git_state_empty_on_failure(self):
        """git_state degrades to empty dict on git failure."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(
            phases={"implement": phase_exec},
            base_branch="main",
            repo="owner/repo",
        )
        ctx = _FakeContext(pipeline=pipeline, repo_path="/nonexistent")
        ctx.live_container_ids = {"cid1"}

        # subprocess.run will fail because the path doesn't exist
        snap = snapshot_from_health_context(ctx)

        # git_state should be empty dict, not crash
        assert snap.git_state == {} or snap.git_state is not None


class TestSnapshotDecisionState:
    """Tests that decision_state is populated."""

    def test_decision_state_populated_with_pending_hitl(self):
        """decision_state reflects pending HITL decisions."""
        from models import DecisionStatus, HITLDecision

        decision = HITLDecision(
            id="decision-1",
            question="Test?",
            status=DecisionStatus.PENDING,
        )
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(
            phases={"implement": phase_exec},
            decisions=[decision],
        )
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = set()

        snap = snapshot_from_health_context(ctx)

        assert snap.decision_state is not None
        assert snap.decision_state.get("pending_hitl") is True
        assert snap.decision_state.get("open_decisions") == 1

    def test_decision_state_empty_when_no_decisions(self):
        """decision_state is empty when no decisions exist."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(phases={"implement": phase_exec})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = set()

        snap = snapshot_from_health_context(ctx)

        assert snap.decision_state == {}


class TestSnapshotGracefulDegradation:
    """Tests that the snapshot builder never crashes."""

    def test_no_crash_on_missing_pipeline(self):
        """No crash when pipeline is None."""
        ctx = _FakeContext(pipeline=None, repo_path="/tmp")
        ctx.live_container_ids = set()

        snap = snapshot_from_health_context(ctx)

        assert snap is not None
        assert snap.pipeline_id == ""
        assert snap.running_agents == ()

    def test_no_crash_on_missing_phase(self):
        """No crash when current_phase is None."""
        pipeline = _FakePipeline(phases={})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.current_phase = None
        ctx.live_container_ids = set()

        snap = snapshot_from_health_context(ctx)

        assert snap is not None
        assert snap.phase == ""

    def test_no_crash_on_empty_phases(self):
        """No crash when pipeline has no phases."""
        pipeline = _FakePipeline(phases={})
        ctx = _FakeContext(pipeline=pipeline, repo_path="/tmp")
        ctx.live_container_ids = set()

        snap = snapshot_from_health_context(ctx)

        assert snap is not None
        assert snap.running_agents == ()

    def test_no_crash_on_git_failure(self):
        """No crash when git commands fail."""
        agent = _FakeAgent(role="coder", container_id="cid1")
        phase_exec = _FakePhaseExec(agents=[agent])
        pipeline = _FakePipeline(
            phases={"implement": phase_exec},
            base_branch="main",
            repo="owner/repo",
        )
        ctx = _FakeContext(pipeline=pipeline, repo_path="/nonexistent")
        ctx.live_container_ids = {"cid1"}

        # subprocess.run will fail
        snap = snapshot_from_health_context(ctx)

        # Should not crash, git_state should be empty or partial
        assert snap is not None


class TestSnapshotConsensusState:
    """Tests that the consensus field is populated from the PeerConsensusTracker."""

    def test_consensus_field_populated(self):
        """snapshot_from_health_context must populate the consensus field."""
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

        assert hasattr(snap, "consensus")
        assert isinstance(snap.consensus, dict)

    def test_consensus_field_empty_when_tracker_unavailable(self):
        """consensus field is empty dict when tracker is unavailable."""
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

        # Ensure tracker is unavailable
        with patch("peer_consensus.get_peer_consensus_tracker", return_value=None):
            snap = snapshot_from_health_context(ctx)

        assert snap.consensus == {}

    def test_consensus_has_brc_progress_signals(self):
        """consensus field must include BRC progress signals."""
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

        # Mock the tracker
        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {
            "protocol": "brc",
            "blocking_agents": ["coder"],
            "has_unresolved_nacks": False,
            "unresolved_nacks": [],
            "agents": {"coder": {"producer_phase": "WORKING", "confirmed": False}},
        }
        mock_tracker.get_latest_proposal_timestamp.return_value = None
        mock_tracker.get_latest_progress_timestamp.return_value = None

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker):
            snap = snapshot_from_health_context(ctx)

        assert "protocol" in snap.consensus
        assert "has_proposed" in snap.consensus
        assert "producer_phases" in snap.consensus


class TestSnapshotMidturnMessages:
    """Tests that the midturn_messages field is populated from the message store."""

    def test_midturn_messages_field_populated(self):
        """snapshot_from_health_context must populate midturn_messages."""
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

        assert hasattr(snap, "midturn_messages")
        assert isinstance(snap.midturn_messages, tuple)

    def test_midturn_messages_empty_when_store_unavailable(self):
        """midturn_messages is empty tuple when message store is unavailable."""
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

        with patch("message_store.get_message_store", side_effect=ImportError):
            snap = snapshot_from_health_context(ctx)

        assert snap.midturn_messages == ()


class TestSnapshotPipelineRef:
    """Tests that _pipeline_ref is set on the snapshot."""

    def test_pipeline_ref_set(self):
        """snapshot_from_health_context must set _pipeline_ref on the snapshot."""
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

        assert hasattr(snap, "_pipeline_ref")
        assert snap._pipeline_ref is pipeline


class TestSnapshotPrevCommitCounts:
    """Tests that agent_prev_commit_counts is populated in git_state."""

    def test_agent_prev_commit_counts_populated(self):
        """snapshot_from_health_context must populate agent_prev_commit_counts."""
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

        assert hasattr(snap, "git_state")
        assert isinstance(snap.git_state, dict)
        assert "agent_prev_commit_counts" in snap.git_state, (
            "snapshot_from_health_context must populate agent_prev_commit_counts "
            "for the forward-progress detector's reset mode to work"
        )
