"""Slice-1 contract tests for snapshot_from_health_context (issue #3665).

Verifies that ``snapshot_from_health_context`` populates the 5 in-scope
``EventStreamSnapshot`` fields (midturn_messages, runtime, consensus,
container_transitions, RunningAgent role+age) and leaves the 4 excluded
fields (decision_state, gateway_error_counters, cost_counters, git_state)
empty by decision.

These tests use lightweight stub objects rather than the full orchestrator
stack, so they exercise the snapshot builder in isolation.
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
# Test helpers — lightweight stubs that satisfy the duck-typed context interface
# ---------------------------------------------------------------------------


def _make_pipeline(
    *,
    pipeline_id: str = "issue-3665",
    phase: str = "implement",
    agents=None,
    containers=None,
    status: str = "running",
):
    """Build a minimal Pipeline stub with phases/agents/containers."""
    from models import AgentRole

    phase_exec = SimpleNamespace(
        status=SimpleNamespace(value=status),
        started_at=None,
        agents=agents or [],
        containers=containers or [],
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
    )


def _make_context(
    *,
    pipeline=None,
    pipeline_id: str = "issue-3665",
    phase: str = "implement",
    live_container_ids=None,
    trigger: str = "runtime_tick",
):
    """Build a minimal PipelineHealthContext-like stub."""
    if pipeline is None:
        pipeline = _make_pipeline(pipeline_id=pipeline_id, phase=phase)
    return SimpleNamespace(
        pipeline=pipeline,
        pipeline_id=pipeline_id,
        current_phase=SimpleNamespace(value=phase),
        trigger=trigger,
        repo_path=Path("/tmp/test"),
        docker_client=None,
        state_store=None,
        live_container_ids=live_container_ids or set(),
        lifecycle_owner="orchestrator",
        awaiting_spawn=False,
        event_loop_owner=None,
        phase_started_age_s=120.0,
    )


# ---------------------------------------------------------------------------
# TASK-1-1: midturn_messages populated
# ---------------------------------------------------------------------------


class TestMidturnMessages:
    """Verify midturn_messages is populated from agent log store."""

    def test_midturn_messages_populated_from_agent_logs(self):
        """midturn_messages contains tool-call records parsed from agent logs."""
        from agent_log_store import AgentLogStore

        # Build fake log content with JSON-structured tool call lines
        log_lines = [
            '{"timestamp": "2026-07-28T00:00:00.000Z", "severity": "INFO", '
            '"message": "Tool call", "extra": {"event_type": "tool_use", '
            '"tool_name": "bash", "tool_use_id": "call_1", "input": "ls -la"}}',
            '{"timestamp": "2026-07-28T00:00:01.000Z", "severity": "INFO", '
            '"message": "Tool call", "extra": {"event_type": "tool_use", '
            '"tool_name": "bash", "tool_use_id": "call_2", "input": "grep -rn foo"}}',
            '{"timestamp": "2026-07-28T00:00:02.000Z", "severity": "INFO", '
            '"message": "Assistant message", "extra": {"event_type": "assistant"}}',
        ]
        fake_logs = "\n".join(log_lines)

        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.list_records.return_value = [
            {
                "job_name": "job-1",
                "agent_role": "coder",
                "logs": fake_logs,
                "exit_code": 0,
            }
        ]

        ctx = _make_context(live_container_ids=set())

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.midturn_messages) == 2
        first = snapshot.midturn_messages[0]
        assert first["tool_name"] == "bash"
        assert first["input"] == "ls -la"
        assert "input_hash" in first
        assert first["agent_role"] == "coder"

    def test_midturn_messages_empty_when_no_logs(self):
        """midturn_messages is empty when agent_log_store has no records."""
        from agent_log_store import AgentLogStore

        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.list_records.return_value = []

        ctx = _make_context(live_container_ids=set())

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.midturn_messages == ()

    def test_midturn_messages_hash_is_full_sha256(self):
        """input_hash is a full SHA-256 of the (tool_name, input) pair."""
        import hashlib

        from agent_log_store import AgentLogStore

        log_line = (
            '{"timestamp": "2026-07-28T00:00:00.000Z", "severity": "INFO", '
            '"message": "Tool call", "extra": {"event_type": "tool_use", '
            '"tool_name": "bash", "tool_use_id": "call_1", "input": "echo hello"}}'
        )
        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.list_records.return_value = [
            {"job_name": "job-1", "agent_role": "coder", "logs": log_line, "exit_code": 0}
        ]

        ctx = _make_context(live_container_ids=set())

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        expected_hash = hashlib.sha256(b"bash:echo hello").hexdigest()
        assert snapshot.midturn_messages[0]["input_hash"] == expected_hash


# ---------------------------------------------------------------------------
# TASK-1-2: runtime section populated
# ---------------------------------------------------------------------------


class TestRuntimeSection:
    """Verify runtime section is populated from driver_heartbeat."""

    def test_runtime_contains_tick_and_spawn_ages(self):
        """runtime contains tick_age_s and spawn_age_s from driver_heartbeat."""
        ctx = _make_context(live_container_ids=set())

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0), \
             patch("driver_heartbeat.spawn_age_seconds", return_value=120.0):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.runtime == {"tick_age_s": 5.0, "spawn_age_s": 120.0}

    def test_runtime_empty_when_driver_heartbeat_unavailable(self):
        """runtime is empty when driver_heartbeat import fails."""
        ctx = _make_context(live_container_ids=set())

        with patch("driver_heartbeat.tick_age_seconds", side_effect=ImportError):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.runtime == {}


# ---------------------------------------------------------------------------
# TASK-1-3: consensus section populated
# ---------------------------------------------------------------------------


class TestConsensusSection:
    """Verify consensus section is populated from peer_consensus tracker."""

    def test_consensus_populated_from_tracker(self):
        """consensus contains tracker.evaluate() output."""
        fake_tracker = MagicMock()
        fake_tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": ["coder"],
            "has_unresolved_nacks": False,
        }

        ctx = _make_context(live_container_ids=set())

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=fake_tracker):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.consensus["is_complete"] is False
        assert snapshot.consensus["blocking_agents"] == ["coder"]

    def test_consensus_empty_when_tracker_none(self):
        """consensus is empty when no tracker exists for the pipeline."""
        ctx = _make_context(live_container_ids=set())

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=None):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.consensus == {}


# ---------------------------------------------------------------------------
# TASK-1-4: container_transitions populated
# ---------------------------------------------------------------------------


class TestContainerTransitions:
    """Verify container_transitions is populated from kubernetes_monitor."""

    def test_container_transitions_populated_from_pod_states(self):
        """container_transitions contains pod state records from the monitor."""
        from models import ContainerStatus

        fake_monitor = MagicMock()
        fake_monitor._pod_states = {
            "pod-1": ContainerStatus.RUNNING,
            "pod-2": ContainerStatus.EXITED,
        }

        ctx = _make_context(live_container_ids=set())

        with patch("kubernetes_monitor.get_kubernetes_monitor", return_value=fake_monitor):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.container_transitions) == 2
        statuses = {t["status"] for t in snapshot.container_transitions}
        assert "running" in statuses
        assert "exited" in statuses

    def test_container_transitions_empty_when_no_monitor(self):
        """container_transitions is empty when monitor is unavailable."""
        ctx = _make_context(live_container_ids=set())

        with patch("kubernetes_monitor.get_kubernetes_monitor", return_value=None):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.container_transitions == ()


# ---------------------------------------------------------------------------
# TASK-1-5: RunningAgent role + age fields
# ---------------------------------------------------------------------------


class TestRunningAgentFields:
    """Verify RunningAgent uses agent role (not container ID) and populates age fields."""

    def test_running_agent_role_is_agent_role_not_container_id(self):
        """RunningAgent.role is the agent role from pipeline state, not the container ID."""
        from models import AgentRole

        agent_exec = SimpleNamespace(
            role=AgentRole.CODER,
            container_id="cid-12345",
            status="running",
        )
        pipeline = _make_pipeline(
            agents=[agent_exec],
            containers=[],
        )
        ctx = _make_context(pipeline=pipeline, live_container_ids={"cid-12345"})

        with patch("health_monitor.get_health_monitor", return_value=None):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.running_agents) == 1
        agent = snapshot.running_agents[0]
        assert agent.role == "coder"
        assert agent.role != "cid-12345"

    def test_running_agent_age_fields_populated_from_health_monitor(self):
        """last_tool_call_age_s and last_heartbeat_age_s are populated from HealthMonitor."""
        import time

        from models import AgentRole

        agent_exec = SimpleNamespace(
            role=AgentRole.CODER,
            container_id="cid-12345",
            status="running",
        )
        pipeline = _make_pipeline(agents=[agent_exec], containers=[])
        ctx = _make_context(pipeline=pipeline, live_container_ids={"cid-12345"})

        # Create a fake health monitor with agent state
        fake_state = SimpleNamespace(
            last_heartbeat=time.time() - 30,
            last_progress=time.time() - 60,
        )
        fake_monitor = SimpleNamespace(_agents={"coder": fake_state})

        with patch("health_monitor.get_health_monitor", return_value=fake_monitor):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.running_agents) == 1
        agent = snapshot.running_agents[0]
        assert agent.last_heartbeat_age_s is not None
        assert agent.last_heartbeat_age_s > 0
        assert agent.last_tool_call_age_s is not None
        assert agent.last_tool_call_age_s > 0

    def test_running_agent_age_fields_none_when_no_health_monitor(self):
        """Age fields are None when health monitor is unavailable."""
        from models import AgentRole

        agent_exec = SimpleNamespace(
            role=AgentRole.CODER,
            container_id="cid-12345",
            status="running",
        )
        pipeline = _make_pipeline(agents=[agent_exec], containers=[])
        ctx = _make_context(pipeline=pipeline, live_container_ids={"cid-12345"})

        with patch("health_monitor.get_health_monitor", return_value=None):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.running_agents) == 1
        agent = snapshot.running_agents[0]
        assert agent.last_heartbeat_age_s is None
        assert agent.last_tool_call_age_s is None


# ---------------------------------------------------------------------------
# TASK-1-6: Excluded fields remain empty by decision
# ---------------------------------------------------------------------------


class TestExcludedFields:
    """Verify the 4 Tier 3-4 fields remain empty by decision."""

    def test_excluded_fields_are_empty(self):
        """decision_state, gateway_error_counters, cost_counters, git_state are empty."""
        ctx = _make_context(live_container_ids=set())

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0), \
             patch("driver_heartbeat.spawn_age_seconds", return_value=120.0), \
             patch("peer_consensus.get_peer_consensus_tracker", return_value=None), \
             patch("kubernetes_monitor.get_kubernetes_monitor", return_value=None):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert snapshot.decision_state == {}
        assert snapshot.gateway_error_counters == {}
        assert snapshot.cost_counters == {}
        assert snapshot.git_state == {}

    def test_in_scope_fields_are_populated(self):
        """The 5 in-scope fields are populated (non-empty where data exists)."""
        ctx = _make_context(live_container_ids=set())

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0), \
             patch("driver_heartbeat.spawn_age_seconds", return_value=120.0), \
             patch("peer_consensus.get_peer_consensus_tracker", return_value=None), \
             patch("kubernetes_monitor.get_kubernetes_monitor", return_value=None), \
             patch("agent_log_store.get_agent_log_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.list_records.return_value = []
            mock_store_fn.return_value = mock_store
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        # runtime is populated
        assert snapshot.runtime != {}
        assert "tick_age_s" in snapshot.runtime
        assert "spawn_age_s" in snapshot.runtime

        # consensus is empty (no tracker) but the field exists
        assert snapshot.consensus == {}

        # container_transitions is empty (no monitor) but the field exists
        assert snapshot.container_transitions == ()

        # midturn_messages is empty (no logs) but the field exists
        assert snapshot.midturn_messages == ()


# ---------------------------------------------------------------------------
# Integration: snapshot_from_health_context with a full context
# ---------------------------------------------------------------------------


class TestSnapshotIntegration:
    """End-to-end test of snapshot_from_health_context with all fields populated."""

    def test_full_snapshot_has_all_in_scope_fields(self):
        """A fully-populated context yields a snapshot with all 5 in-scope fields set."""
        from models import AgentRole, ContainerStatus

        # Agent execution with role + container
        agent_exec = SimpleNamespace(
            role=AgentRole.CODER,
            container_id="cid-12345",
            status="running",
        )
        container = SimpleNamespace(
            container_id="cid-12345",
            agent_role=AgentRole.CODER,
            status=ContainerStatus.RUNNING,
        )
        pipeline = _make_pipeline(agents=[agent_exec], containers=[container])
        ctx = _make_context(pipeline=pipeline, live_container_ids={"cid-12345"})

        # Fake agent logs
        log_line = (
            '{"timestamp": "2026-07-28T00:00:00.000Z", "severity": "INFO", '
            '"message": "Tool call", "extra": {"event_type": "tool_use", '
            '"tool_name": "bash", "tool_use_id": "call_1", "input": "ls -la"}}'
        )
        fake_store = MagicMock()
        fake_store.list_records.return_value = [
            {"job_name": "job-1", "agent_role": "coder", "logs": log_line, "exit_code": 0}
        ]

        # Fake tracker
        fake_tracker = MagicMock()
        fake_tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": [],
        }

        # Fake monitor
        fake_monitor = MagicMock()
        fake_monitor._pod_states = {"pod-1": ContainerStatus.RUNNING}

        # Fake health monitor with agent state
        import time as _time
        fake_state = SimpleNamespace(
            last_heartbeat=_time.time() - 30,
            last_progress=_time.time() - 60,
        )
        fake_health_monitor = SimpleNamespace(_agents={"coder": fake_state})

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0), \
             patch("driver_heartbeat.spawn_age_seconds", return_value=120.0), \
             patch("peer_consensus.get_peer_consensus_tracker", return_value=fake_tracker), \
             patch("kubernetes_monitor.get_kubernetes_monitor", return_value=fake_monitor), \
             patch("health_monitor.get_health_monitor", return_value=fake_health_monitor), \
             patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        # Verify all 5 in-scope fields are populated
        assert snapshot.runtime == {"tick_age_s": 5.0, "spawn_age_s": 120.0}
        assert snapshot.consensus["is_complete"] is False
        assert len(snapshot.container_transitions) == 1
        assert len(snapshot.midturn_messages) == 1
        assert len(snapshot.running_agents) == 1
        assert snapshot.running_agents[0].role == "coder"
        assert snapshot.running_agents[0].last_heartbeat_age_s is not None
        assert snapshot.running_agents[0].last_tool_call_age_s is not None

        # Verify excluded fields are empty
        assert snapshot.decision_state == {}
        assert snapshot.gateway_error_counters == {}
        assert snapshot.cost_counters == {}
        assert snapshot.git_state == {}
