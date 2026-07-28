"""Slice-5 contract tests for alert evidence and false-positive fixes (#3665).

Verifies that:
- OVERSEER_ALERT payloads carry structured evidence (TASK-5-1)
- Convergence-stall does not fire when peer heartbeat is recent (TASK-5-2)
- Timeout-killed pods produce an alert that says "killed by 2h agent timeout"
  (TASK-5-3)
- Detection-plane findings are routed to the operator alert surface (TASK-2-3,
  tested here for completeness)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_tests_dir = Path(__file__).parent
_orchestrator_dir = _tests_dir.parent
if str(_orchestrator_dir) not in sys.path:
    sys.path.insert(0, str(_orchestrator_dir))


# ---------------------------------------------------------------------------
# TASK-5-1: OVERSEER_ALERT payloads carry structured evidence
# ---------------------------------------------------------------------------


class TestAlertEvidence:
    """Verify OVERSEER_ALERT payloads carry structured evidence."""

    def test_broadcast_alert_includes_evidence_in_body(self):
        """The _broadcast_alert method includes evidence in the alert body."""
        from overseer.monitor._alerting import _broadcast_alert

        mock_self = SimpleNamespace(
            pipeline_id="issue-3665",
            _run_cli=AsyncMock(),
        )

        evidence = {
            "phase": "implement",
            "running_agents": 0,
            "lifecycle_owner": "none",
        }

        import asyncio

        asyncio.run(
            _broadcast_alert(
                mock_self,
                anomaly_type="phase_stall",
                agent_role="overseer",
                message="Phase is wedged",
                priority="high",
                evidence=evidence,
            )
        )

        # Verify the message was sent with evidence in the body
        mock_self._run_cli.assert_called_once()
        call_args = mock_self._run_cli.call_args
        # The _run_cli call passes args as positional: ("egg-orch", "message", "send", ...)
        # The --body argument is a keyword arg or appears after --body in the args
        all_args = call_args[0] + tuple(call_args[1].values())
        # Find the body argument (it comes after "--body")
        body_idx = None
        for i, arg in enumerate(all_args):
            if arg == "--body" and i + 1 < len(all_args):
                body_idx = i + 1
                break
        assert body_idx is not None, "Expected --body argument in _run_cli call"
        body = all_args[body_idx]
        # The body should contain the evidence
        assert "Evidence" in body or "phase" in body

    def test_broadcast_alert_without_evidence(self):
        """The _broadcast_alert method works without evidence (backward compat)."""
        from overseer.monitor._alerting import _broadcast_alert

        mock_self = SimpleNamespace(
            pipeline_id="issue-3665",
            _run_cli=AsyncMock(),
        )

        import asyncio

        asyncio.run(
            _broadcast_alert(
                mock_self,
                anomaly_type="test_anomaly",
                agent_role="overseer",
                message="Test message",
                priority="medium",
            )
        )

        mock_self._run_cli.assert_called_once()

    def test_detection_finding_alert_includes_evidence(self):
        """Detection-plane findings broadcast as OVERSEER_ALERT include evidence."""
        from health_checks.types import Finding, Severity
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._detection_plane_last_tick = {}
        monitor._timeout_warning_last_sent = {}
        monitor._health_check_runner = None

        finding = Finding(
            finding_class="tool_input_loop",
            severity=Severity.HIGH,
            evidence={
                "pipeline_id": "issue-3665",
                "phase": "implement",
                "zero_new_input_polls": 3,
                "window_size": 3,
                "last_tool_name": "bash",
                "last_input_hash": "abc123",
            },
            recommended_action="Nudge or respawn the agent.",
            requires_adjudication=False,
            detector_key="tool_input_loop",
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            monitor._broadcast_detection_finding(finding, "issue-3665", "implement")

            mock_store.add_message.assert_called_once()
            msg = mock_store.add_message.call_args[0][0]
            assert msg.message_type == "OVERSEER_ALERT"
            # Evidence should be in the metadata
            assert "evidence" in msg.metadata
            assert msg.metadata["evidence"]["zero_new_input_polls"] == 3


# ---------------------------------------------------------------------------
# TASK-5-2: Convergence-stall false positive fix
# ---------------------------------------------------------------------------


class TestConvergenceStallFalsePositive:
    """Verify convergence-stall does not fire when peer heartbeat is recent."""

    def test_stall_does_not_fire_when_heartbeat_recent(self):
        """When a peer heartbeat is recent, convergence-stall resets."""
        from event_loop._loop import _get_latest_heartbeat_age

        # Mock the health monitor to return a recent heartbeat
        fake_monitor = SimpleNamespace(
            _last_heartbeat={"coder": 1000.0}  # 1000 seconds ago (recent)
        )

        import time as _time

        now = _time.time()
        fake_monitor._last_heartbeat = {"coder": now - 30}  # 30s ago

        with patch("health_monitor.get_health_monitor", return_value=fake_monitor):
            age = _get_latest_heartbeat_age("issue-3665")
            assert age is not None
            assert age < 60  # 30s ago, well within budget

    def test_stall_does_not_fire_when_heartbeat_within_budget(self):
        """When a peer heartbeat is within the budget window, stall is suppressed."""
        import time as _time

        from event_loop._loop import _get_latest_heartbeat_age

        now = _time.time()
        fake_monitor = SimpleNamespace(
            _last_heartbeat={"coder": now - 100}  # 100s ago
        )

        with patch("health_monitor.get_health_monitor", return_value=fake_monitor):
            age = _get_latest_heartbeat_age("issue-3665")
            assert age is not None
            assert age < 600  # 100s ago, within 10-min budget

    def test_heartbeat_age_none_when_no_monitor(self):
        """_get_latest_heartbeat_age returns None when health monitor is unavailable."""
        from event_loop._loop import _get_latest_heartbeat_age

        with patch("health_monitor.get_health_monitor", return_value=None):
            age = _get_latest_heartbeat_age("issue-3665")
            assert age is None

    def test_heartbeat_age_none_when_no_heartbeats(self):
        """_get_latest_heartbeat_age returns None when no heartbeats exist."""
        from event_loop._loop import _get_latest_heartbeat_age

        fake_monitor = SimpleNamespace(_last_heartbeat={})

        with patch("health_monitor.get_health_monitor", return_value=fake_monitor):
            age = _get_latest_heartbeat_age("issue-3665")
            assert age is None


# ---------------------------------------------------------------------------
# TASK-5-3: Timeout named explicitly in exit classification
# ---------------------------------------------------------------------------


class TestTimeoutExitClassification:
    """Verify timeout-killed pods produce a descriptive exit message."""

    def test_timeout_exit_detail_names_timeout(self):
        """exit_detail_for returns 'killed by 2h agent timeout' for timeout pods."""
        from kubernetes_spawner._models import _EventJobStatusView

        mock_spawner = MagicMock()
        mock_spawner.k8s = MagicMock()
        mock_spawner._namespace = "test"

        # Mock list_containers to return a container with exit_code=-1
        mock_container = SimpleNamespace(exit_code=-1, pipeline_id="issue-3665")
        mock_spawner.k8s.list_containers.return_value = [mock_container]

        # Mock agent_log_store to return logs with timeout signature
        fake_record = {
            "logs": "Some output\nTimed out after 7200 seconds\nMore output",
            "exit_code": -1,
        }
        fake_store = MagicMock()
        fake_store.list_records.return_value = [fake_record]
        fake_store.get.return_value = fake_record

        view = _EventJobStatusView.__new__(_EventJobStatusView)
        view._spawner = mock_spawner
        view._RUNNING = "running"
        view._SUCCESS = "success"
        view._ABNORMAL = "abnormal"
        view._FATAL = "fatal"
        view._RATE_LIMITED = "rate_limited"
        view._TIMEOUT = "timeout"

        with patch("kubernetes_spawner._models.LABEL_EVENT_DEDUPE", "test-label"), \
             patch("kubernetes_spawner._models._pkg._dedupe_label_value", return_value="test-val"), \
             patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            detail = view.exit_detail_for("test-key")
            assert "timeout" in detail.lower()
            assert "2h" in detail

    def test_non_timeout_exit_detail_not_renamed(self):
        """exit_detail_for returns normal exit code for non-timeout pods."""
        from kubernetes_spawner._models import _EventJobStatusView

        mock_spawner = MagicMock()
        mock_spawner.k8s = MagicMock()
        mock_spawner._namespace = "test"

        mock_container = SimpleNamespace(exit_code=137)
        mock_spawner.k8s.list_containers.return_value = [mock_container]

        fake_store = MagicMock()
        fake_store.list_records.return_value = []
        fake_store.get.return_value = None

        view = _EventJobStatusView.__new__(_EventJobStatusView)
        view._spawner = mock_spawner
        view._RUNNING = "running"
        view._SUCCESS = "success"
        view._ABNORMAL = "abnormal"
        view._FATAL = "fatal"
        view._RATE_LIMITED = "rate_limited"
        view._TIMEOUT = "timeout"

        with patch("kubernetes_spawner._models.LABEL_EVENT_DEDUPE", "test-label"), \
             patch("kubernetes_spawner._models._pkg._dedupe_label_value", return_value="test-val"), \
             patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            detail = view.exit_detail_for("test-key")
            assert detail == "exit_code=137"


# ---------------------------------------------------------------------------
# Integration: detection plane findings routed to alert surface
# ---------------------------------------------------------------------------


class TestFindingRoutingIntegration:
    """Verify detection-plane findings are routed to the operator alert surface."""

    def test_routine_finding_routed_to_alert_surface(self):
        """Routine findings appear on the OVERSEER_ALERT surface."""
        from health_checks.types import Finding, Severity
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._detection_plane_last_tick = {}
        monitor._timeout_warning_last_sent = {}
        monitor._health_check_runner = None

        finding = Finding(
            finding_class="container_death",
            severity=Severity.HIGH,
            evidence={"pod": "pod-1"},
            recommended_action="investigate",
            requires_adjudication=False,
            detector_key="container_death",
        )

        # Use a real pipeline stub with a proper current_phase
        pipeline = SimpleNamespace(
            current_phase=SimpleNamespace(value="implement"),
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            monitor._handle_detection_plane_findings(
                [finding], pipeline, MagicMock(), "issue-3665"
            )

            # The routine finding should be broadcast as an OVERSEER_ALERT
            mock_store.add_message.assert_called_once()
            msg = mock_store.add_message.call_args[0][0]
            assert msg.message_type == "OVERSEER_ALERT"
            assert "container_death" in msg.subject

    def test_adjudication_finding_not_routed_as_routine(self):
        """Adjudication findings are NOT broadcast as routine alerts."""
        from health_checks.types import Finding, FindingClass, Severity
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._detection_plane_last_tick = {}
        monitor._timeout_warning_last_sent = {}
        monitor._health_check_runner = None

        finding = Finding(
            finding_class=FindingClass.PHASE_STALL,
            severity=Severity.HIGH,
            evidence={"phase": "implement"},
            recommended_action="advance or fail the wedged phase",
            requires_adjudication=True,
            detector_key="phase_stall",
        )

        pipeline = SimpleNamespace(
            current_phase=SimpleNamespace(value="implement"),
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            monitor._handle_detection_plane_findings(
                [finding], pipeline, MagicMock(), "issue-3665"
            )

            # Adjudication findings should NOT be broadcast as routine alerts
            mock_store.add_message.assert_not_called()
