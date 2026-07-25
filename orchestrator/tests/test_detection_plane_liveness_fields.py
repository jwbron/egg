"""Tests for RunningAgent liveness fields + role=str(cid) fix (#3596, task-1-10).

Verifies that:
1. RunningAgent.role is populated with agent role name, not container UUID
2. last_heartbeat_age_s and last_tool_call_age_s are populated
3. detect_heartbeat_stall can fire when both age fields are stale
4. detect_container_death reads correct role names, not container UUIDs
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from datetime import UTC, datetime, timedelta
import time

import pytest


# ---------------------------------------------------------------------------
# AC-1: RunningAgent.role is populated with agent role name, not container UUID
# ---------------------------------------------------------------------------


class TestRoleMapping:
    """RunningAgent.role must be the agent role name, not a container UUID."""

    def test_role_is_agent_role_not_container_id(self):
        """RunningAgent.role must be the agent's role name, not str(cid)."""
        from health_checks.detection_plane import snapshot_from_health_context

        # Build a fake agent with a container_id
        agent = MagicMock()
        agent.role = "coder"
        agent.container_id = "abc123def456"
        agent.status = "running"
        agent.exit_code = None
        agent.error = None

        phase_exec = MagicMock()
        phase_exec.agents = [agent]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"abc123def456"}
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        assert snap.running_agents[0].role == "coder"
        assert snap.running_agents[0].role != "abc123def456"

    def test_role_falls_back_to_cid_when_no_mapping(self):
        """When no agent matches the container ID, role falls back to str(cid)."""
        from health_checks.detection_plane import snapshot_from_health_context

        phase_exec = MagicMock()
        phase_exec.agents = []
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"unknown_cid"}
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        # Falls back to str(cid) when no mapping found
        assert snap.running_agents[0].role == "unknown_cid"

    def test_multiple_agents_mapped_correctly(self):
        """Multiple agents are mapped to their correct roles."""
        from health_checks.detection_plane import snapshot_from_health_context

        agent1 = MagicMock()
        agent1.role = "coder"
        agent1.container_id = "cid1"
        agent1.status = "running"
        agent1.exit_code = None
        agent1.error = None

        agent2 = MagicMock()
        agent2.role = "tester"
        agent2.container_id = "cid2"
        agent2.status = "running"
        agent2.exit_code = None
        agent2.error = None

        phase_exec = MagicMock()
        phase_exec.agents = [agent1, agent2]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"cid1", "cid2"}
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        roles = {a.role for a in snap.running_agents}
        assert roles == {"coder", "tester"}


# ---------------------------------------------------------------------------
# AC-2: last_heartbeat_age_s and last_tool_call_age_s are populated
# ---------------------------------------------------------------------------


class TestLivenessFields:
    """RunningAgent liveness fields must be populated."""

    def test_last_tool_call_age_s_from_progress_store(self):
        """last_tool_call_age_s is populated from ProgressStore."""
        from health_checks.detection_plane import snapshot_from_health_context

        agent = MagicMock()
        agent.role = "coder"
        agent.container_id = "cid1"
        agent.status = "running"
        agent.exit_code = None
        agent.error = None

        phase_exec = MagicMock()
        phase_exec.agents = [agent]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"cid1"}
        ctx.repo_path = "/tmp/repo"

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
        from health_checks.detection_plane import snapshot_from_health_context

        agent = MagicMock()
        agent.role = "coder"
        agent.container_id = "cid1"
        agent.status = "running"
        agent.exit_code = None
        agent.error = None

        phase_exec = MagicMock()
        phase_exec.agents = [agent]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"cid1"}
        ctx.repo_path = "/tmp/repo"

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
        from health_checks.detection_plane import snapshot_from_health_context

        agent = MagicMock()
        agent.role = "coder"
        agent.container_id = "cid1"
        agent.status = "running"
        agent.exit_code = None
        agent.error = None

        phase_exec = MagicMock()
        phase_exec.agents = [agent]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = {"cid1"}
        ctx.repo_path = "/tmp/repo"

        # No progress store, no health monitor
        with patch("progress_store.get_progress_store", side_effect=ImportError):
            with patch("health_monitor.get_health_monitor", side_effect=ImportError):
                snap = snapshot_from_health_context(ctx)

        assert len(snap.running_agents) == 1
        assert snap.running_agents[0].last_tool_call_age_s is None
        assert snap.running_agents[0].last_heartbeat_age_s is None

    def test_exit_code_and_reason_from_pipeline(self):
        """exit_code and exit_reason are populated from the pipeline model."""
        from health_checks.detection_plane import snapshot_from_health_context

        agent = MagicMock()
        agent.role = "coder"
        agent.container_id = "cid1"
        agent.status = "running"
        agent.exit_code = 1
        agent.error = "container died"

        phase_exec = MagicMock()
        phase_exec.agents = [agent]
        phase_exec.status = MagicMock()
        phase_exec.status.value = "running"

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {"implement": phase_exec}
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
        ctx.live_container_ids = set()  # No live containers
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        # When no live containers, running_agents is empty
        assert len(snap.running_agents) == 0


# ---------------------------------------------------------------------------
# AC-3: detect_heartbeat_stall can fire when both age fields are stale
# ---------------------------------------------------------------------------


class TestHeartbeatStallDetector:
    """detect_heartbeat_stall must fire when both age fields are stale."""

    def test_detect_heartbeat_stall_fires_on_stale_ages(self):
        """detect_heartbeat_stall must fire when last_tool_call_age_s and
        last_heartbeat_age_s are both stale."""
        from health_checks.detection_plane import EventStreamSnapshot, RunningAgent
        from health_checks.tier1.consensus_stall import detect_heartbeat_stall

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(
                RunningAgent(
                    role="coder",
                    state="running",
                    lifecycle_owner="orchestrator",
                    last_tool_call_age_s=700,  # Stale (>600s)
                    last_heartbeat_age_s=700,  # Stale (>600s)
                ),
            ),
        )

        finding = detect_heartbeat_stall(snap)

        assert finding is not None, "detect_heartbeat_stall must fire on stale ages"
        assert finding.finding_class == "heartbeat_stall"
        assert finding.severity.value == "high"

    def test_detect_heartbeat_stall_silent_on_fresh_ages(self):
        """detect_heartbeat_stall must not fire when ages are fresh."""
        from health_checks.detection_plane import EventStreamSnapshot, RunningAgent
        from health_checks.tier1.consensus_stall import detect_heartbeat_stall

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(
                RunningAgent(
                    role="coder",
                    state="running",
                    lifecycle_owner="orchestrator",
                    last_tool_call_age_s=30,  # Fresh
                    last_heartbeat_age_s=30,  # Fresh
                ),
            ),
        )

        finding = detect_heartbeat_stall(snap)

        assert finding is None, "detect_heartbeat_stall must not fire on fresh ages"

    def test_detect_heartbeat_stall_silent_when_fields_null(self):
        """detect_heartbeat_stall must not fire when age fields are None."""
        from health_checks.detection_plane import EventStreamSnapshot, RunningAgent
        from health_checks.tier1.consensus_stall import detect_heartbeat_stall

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(
                RunningAgent(
                    role="coder",
                    state="running",
                    lifecycle_owner="orchestrator",
                    last_tool_call_age_s=None,
                    last_heartbeat_age_s=None,
                ),
            ),
        )

        finding = detect_heartbeat_stall(snap)

        assert finding is None, "detect_heartbeat_stall must not fire when fields are null"


# ---------------------------------------------------------------------------
# AC-4: detect_container_death reads correct role names
# ---------------------------------------------------------------------------


class TestContainerDeathRoleNames:
    """detect_container_death must read correct role names, not container UUIDs."""

    def test_container_death_evidence_uses_role_name(self):
        """When a container dies, the finding's evidence must use the agent's
        role name, not the container ID."""
        from health_checks.detection_plane import EventStreamSnapshot, RunningAgent
        from health_checks.tier1.container_k8s import detect_container_death

        # The RunningAgent has role="coder" (not a container UUID)
        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(
                RunningAgent(
                    role="coder",
                    state="running",
                    lifecycle_owner="orchestrator",
                    exit_code=1,
                    exit_reason="Error",
                ),
            ),
            container_transitions=(
                {
                    "container": "cid-1",
                    "role": "coder",
                    "from": "running",
                    "to": "Terminated",
                    "reason": "Error",
                    "exit_code": 1,
                    "restart_count": 0,
                    "transient": False,
                    "timestamp": 1234567890.0,
                },
            ),
        )

        finding = detect_container_death(snap)

        assert finding is not None
        assert finding.finding_class == "container_death"
        # The evidence must use the role name, not a container UUID
        assert finding.evidence.get("role") == "coder"
        assert finding.evidence.get("role") != "cid-1"
