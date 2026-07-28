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
    agents=None,
    containers=None,
):
    """Build a minimal Pipeline stub."""
    phase_exec = SimpleNamespace(
        status=SimpleNamespace(value=status),
        started_at=None,
        agents=agents if agents is not None else [],
        containers=containers if containers is not None else [],
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


def _make_context(
    pipeline=None,
    pipeline_id: str = "issue-3665",
    phase: str = "implement",
    live_container_ids=None,
):
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
        live_container_ids=live_container_ids if live_container_ids is not None else set(),
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

        # runtime is populated with the correct field names for detectors
        assert snapshot.runtime != {}
        assert snapshot.runtime.get("thread_last_tick_age_s") == 5.0
        assert snapshot.runtime.get("run_pipeline_thread_alive") is True
        # Also check raw["runtime"] for the _runtime() helper
        assert snapshot.raw.get("runtime", {}).get("thread_last_tick_age_s") == 5.0

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
        # Augmented fields for BRC thrash detection
        assert "nack_cycles" in snapshot.consensus
        assert "late_confirmed_then_renack" in snapshot.consensus

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
        fake_monitor._lock = MagicMock()

        ctx = _make_context(live_container_ids=set())

        with patch("kubernetes_monitor.get_kubernetes_monitor", return_value=fake_monitor):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        assert len(snapshot.container_transitions) == 2
        # Check that records have the fields detectors expect
        first = snapshot.container_transitions[0]
        assert "container" in first
        assert "to" in first
        assert "to_state" in first
        assert "reason" in first
        assert "transient" in first
        assert "restart_count" in first

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

        # Create a fake health monitor with agent state and a lock
        fake_state = SimpleNamespace(
            last_heartbeat=time.time() - 30,
            last_progress=time.time() - 60,
        )
        fake_monitor = SimpleNamespace(
            _agents={"coder": fake_state},
            _lock=MagicMock(),
        )

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
        assert "thread_last_tick_age_s" in snapshot.runtime

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

        # Fake monitor with lock
        fake_monitor = MagicMock()
        fake_monitor._pod_states = {"pod-1": ContainerStatus.RUNNING}
        fake_monitor._lock = MagicMock()

        # Fake health monitor with agent state and lock
        import time as _time
        fake_state = SimpleNamespace(
            last_heartbeat=_time.time() - 30,
            last_progress=_time.time() - 60,
        )
        fake_health_monitor = SimpleNamespace(
            _agents={"coder": fake_state},
            _lock=MagicMock(),
        )

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0), \
             patch("driver_heartbeat.spawn_age_seconds", return_value=120.0), \
             patch("peer_consensus.get_peer_consensus_tracker", return_value=fake_tracker), \
             patch("kubernetes_monitor.get_kubernetes_monitor", return_value=fake_monitor), \
             patch("health_monitor.get_health_monitor", return_value=fake_health_monitor), \
             patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            snapshot = detection_plane.snapshot_from_health_context(ctx)

        # Verify all 5 in-scope fields are populated
        assert snapshot.runtime != {}
        assert "thread_last_tick_age_s" in snapshot.runtime
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
        monitor._lock = MagicMock()
        monitor._reconciliation_stores = []
        monitor.k8s_client = MagicMock()
        monitor._lock = MagicMock()

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
        monitor._lock = MagicMock()
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
        monitor._lock = MagicMock()
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
        monitor._lock = MagicMock()
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
        from health_checks.detection_plane import (
            EventStreamSnapshot,
            Finding,
            FindingClass,
            Severity,
        )

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._lock = MagicMock()
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
        from health_checks.detection_plane import (
            EventStreamSnapshot,
            Finding,
            FindingClass,
            Severity,
        )

        ctx = _make_context()
        pipeline = ctx.pipeline
        store = ctx.state_store

        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._health_check_runner = MagicMock()
        monitor._detection_plane_last_tick = {}
        monitor._lock = MagicMock()
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
        monitor._lock = MagicMock()
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
        monitor._lock = MagicMock()
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
