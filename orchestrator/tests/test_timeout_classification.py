"""Slice-4 contract tests for timeout visibility and classification (#3665).

Verifies that:
- PipelineConfig has an agent_timeout_seconds field (default 7200) (TASK-4-1)
- The spawner passes EGG_AGENT_TIMEOUT env to the agent (TASK-4-2)
- Timeout-killed pods (exit -1 from asyncio.timeout) are classified as clean
  timeouts, not crashes, and do not increment the failure streak (TASK-4-3)
- Agents receive a heartbeat warning at 90 minutes (TASK-4-4)
- Timeout-killed pods produce an alert that says "killed by 2h agent timeout"
  (TASK-5-3, tested here for classification)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_tests_dir = Path(__file__).parent
_orchestrator_dir = _tests_dir.parent
if str(_orchestrator_dir) not in sys.path:
    sys.path.insert(0, str(_orchestrator_dir))


# ---------------------------------------------------------------------------
# TASK-4-1: PipelineConfig.agent_timeout_seconds
# ---------------------------------------------------------------------------


class TestAgentTimeoutConfig:
    """Verify PipelineConfig has agent_timeout_seconds with default 7200."""

    def test_default_agent_timeout_seconds(self):
        """PipelineConfig.agent_timeout_seconds defaults to 7200 (2 hours)."""
        from models import PipelineConfig

        config = PipelineConfig()
        assert config.agent_timeout_seconds == 7200

    def test_agent_timeout_seconds_validated(self):
        """agent_timeout_seconds must be >= 60."""
        from models import PipelineConfig

        with pytest.raises(ValueError):
            PipelineConfig(agent_timeout_seconds=30)

    def test_agent_timeout_seconds_custom(self):
        """agent_timeout_seconds can be set to a custom value."""
        from models import PipelineConfig

        config = PipelineConfig(agent_timeout_seconds=3600)
        assert config.agent_timeout_seconds == 3600


# ---------------------------------------------------------------------------
# TASK-4-2: EGG_AGENT_TIMEOUT env passed through spawner
# ---------------------------------------------------------------------------


class TestAgentTimeoutEnv:
    """Verify the spawner passes EGG_AGENT_TIMEOUT to the agent."""

    def test_egg_agent_timeout_env_set(self):
        """The spawner sets EGG_AGENT_TIMEOUT in the agent's environment."""
        from kubernetes_spawner._spawn import _DEFAULT_AGENT_TIMEOUT_SECONDS, _load_agent_timeout

        # When pipeline config is unavailable, falls back to default
        with patch("state_store.get_state_store", side_effect=Exception):
            timeout = _load_agent_timeout("test-pipeline", "/tmp/test")
            assert timeout == _DEFAULT_AGENT_TIMEOUT_SECONDS

    def test_egg_agent_timeout_from_pipeline_config(self):
        """The spawner reads agent_timeout_seconds from the pipeline config."""
        from kubernetes_spawner._spawn import _load_agent_timeout

        fake_config = SimpleNamespace(agent_timeout_seconds=3600)
        fake_pipeline = SimpleNamespace(config=fake_config)
        fake_store = MagicMock()
        fake_store.load_pipeline.return_value = fake_pipeline

        with patch("state_store.get_state_store", return_value=fake_store):
            timeout = _load_agent_timeout("test-pipeline", "/tmp/test")
            assert timeout == 3600

    def test_egg_agent_timeout_fallback_on_error(self):
        """The spawner falls back to default when config read fails."""
        from kubernetes_spawner._spawn import _DEFAULT_AGENT_TIMEOUT_SECONDS, _load_agent_timeout

        fake_store = MagicMock()
        fake_store.load_pipeline.side_effect = Exception("DB error")

        with patch("state_store.get_state_store", return_value=fake_store):
            timeout = _load_agent_timeout("test-pipeline", "/tmp/test")
            assert timeout == _DEFAULT_AGENT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# TASK-4-3: Timeout classification
# ---------------------------------------------------------------------------


class TestTimeoutClassification:
    """Verify timeout-killed pods are classified as clean timeouts."""

    def test_timeout_exit_code_classified_as_clean(self):
        """Exit code -1 with timeout log is classified as clean timeout."""
        from kubernetes_monitor import _classify_exit_with_context

        # Simulate a timeout: exit code -1 + log contains "Timed out after"
        with patch("kubernetes_monitor._check_timeout_in_logs", return_value=True):
            is_clean, error_msg, is_timeout = _classify_exit_with_context(
                -1, container_id="cid-123", pipeline_id="issue-3665"
            )
            assert is_clean is True
            assert is_timeout is True
            assert "timeout" in error_msg.lower()

    def test_non_timeout_exit_code_not_clean(self):
        """Exit code 1 (crash) is NOT classified as a timeout."""
        from kubernetes_monitor import _classify_exit_with_context

        is_clean, error_msg, is_timeout = _classify_exit_with_context(
            1, container_id="cid-123", pipeline_id="issue-3665"
        )
        assert is_clean is False
        assert is_timeout is False

    def test_exit_code_zero_is_clean(self):
        """Exit code 0 is always clean, regardless of logs."""
        from kubernetes_monitor import _classify_exit_with_context

        is_clean, error_msg, is_timeout = _classify_exit_with_context(
            0, container_id="cid-123", pipeline_id="issue-3665"
        )
        assert is_clean is True
        assert is_timeout is False

    def test_exit_code_143_is_clean(self):
        """Exit code 143 (SIGTERM) is always clean."""
        from kubernetes_monitor import _classify_exit_with_context

        is_clean, error_msg, is_timeout = _classify_exit_with_context(
            143, container_id="cid-123", pipeline_id="issue-3665"
        )
        assert is_clean is True
        assert is_timeout is False

    def test_exit_code_minus_one_without_timeout_log_is_crash(self):
        """Exit code -1 WITHOUT timeout log is classified as a crash."""
        from kubernetes_monitor import _classify_exit_with_context

        with patch("kubernetes_monitor._check_timeout_in_logs", return_value=False):
            is_clean, error_msg, is_timeout = _classify_exit_with_context(
                -1, container_id="cid-123", pipeline_id="issue-3665"
            )
            assert is_clean is False
            assert is_timeout is False

    def test_timeout_log_detection(self):
        """_check_timeout_in_logs detects the timeout signature in agent logs."""
        from agent_log_store import AgentLogStore
        from kubernetes_monitor import _check_timeout_in_logs

        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.get.return_value = {
            "logs": "Some log output\nTimed out after 7200 seconds\nMore output",
            "exit_code": -1,
        }

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            result = _check_timeout_in_logs("cid-123", "issue-3665")
            assert result is True

    def test_timeout_log_detection_no_match(self):
        """_check_timeout_in_logs returns False when no timeout signature."""
        from agent_log_store import AgentLogStore
        from kubernetes_monitor import _check_timeout_in_logs

        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.get.return_value = {
            "logs": "Some log output\nAgent crashed\nMore output",
            "exit_code": -1,
        }

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            result = _check_timeout_in_logs("cid-123", "issue-3665")
            assert result is False

    def test_timeout_log_detection_no_logs(self):
        """_check_timeout_in_logs returns False when no logs are available."""
        from agent_log_store import AgentLogStore
        from kubernetes_monitor import _check_timeout_in_logs

        fake_store = MagicMock(spec=AgentLogStore)
        fake_store.get.return_value = None

        with patch("agent_log_store.get_agent_log_store", return_value=fake_store):
            result = _check_timeout_in_logs("cid-123", "issue-3665")
            assert result is False


# ---------------------------------------------------------------------------
# TASK-4-3: record_timeout does not increment failure streak
# ---------------------------------------------------------------------------


class TestRecordTimeout:
    """Verify record_timeout does not increment the failure streak."""

    def _make_supervisor(self):
        """Build a minimal JobSupervisor with all required attributes."""
        from event_loop import JobSupervisor

        supervisor = JobSupervisor.__new__(JobSupervisor)
        supervisor._streaks = {}
        supervisor._last_abort_time = {}
        supervisor._last_action = {}
        supervisor._exhausted = set()
        supervisor._exit_history = {}
        supervisor._noop_streaks = {}
        supervisor._noop_fingerprint = {}
        supervisor._noop_brc_fingerprint = {}
        supervisor._noop_last_probe = {}
        supervisor._noop_release_context = {}
        supervisor._alerted_noop = {}
        supervisor._alerted_warn = {}
        supervisor._alerted_10 = {}
        supervisor._alerted_rate_limit = {}
        supervisor._rate_limit_backoff = {}
        supervisor._rate_limit_last_time = {}
        supervisor._rate_limit_wait_total = {}
        supervisor._rate_limit_fingerprint = {}
        supervisor._rate_limit_repeat = {}
        supervisor._rate_limit_escalated = set()
        supervisor._never_seen_escalated = set()
        supervisor._job_active_since = {}
        supervisor._on_exhausted = None
        supervisor._agent_failed = None
        supervisor.clock = lambda: 0.0
        supervisor._key_meta = {}
        supervisor._live_keys = set()
        return supervisor

    def test_record_timeout_does_not_increment_streak(self):
        """record_timeout leaves the abnormal streak untouched."""
        supervisor = self._make_supervisor()

        # Record a timeout — should NOT increment the streak
        supervisor.record_timeout("test-key", "propose", "coder", exit_detail="killed by 2h agent timeout")

        # The streak should be 0 (not incremented)
        assert supervisor._streaks.get("test-key", 0) == 0
        # But the exit history should record it
        assert "test-key" in supervisor._exit_history
        assert supervisor._exit_history["test-key"][0]["category"] == "timeout"

    def test_record_abort_increments_streak(self):
        """record_abort DOES increment the streak (for comparison)."""
        supervisor = self._make_supervisor()

        # Record an abort — should increment the streak
        supervisor.record_abort("test-key", "propose", "coder", exit_detail="crash")

        # The streak should be 1 (incremented)
        assert supervisor._streaks.get("test-key", 0) == 1


# ---------------------------------------------------------------------------
# TASK-4-3: JOB_OUTCOME_TIMEOUT constant
# ---------------------------------------------------------------------------


class TestTimeoutOutcomeConstant:
    """Verify JOB_OUTCOME_TIMEOUT is defined in the event loop."""

    def test_job_outcome_timeout_exists(self):
        """JOB_OUTCOME_TIMEOUT is defined in the event_loop module."""
        from event_loop import JOB_OUTCOME_TIMEOUT

        assert JOB_OUTCOME_TIMEOUT == "timeout"


# ---------------------------------------------------------------------------
# TASK-4-3: Event loop handles timeout outcome
# ---------------------------------------------------------------------------


class TestEventLoopTimeoutHandling:
    """Verify the event loop routes timeout outcomes to record_timeout."""

    def test_timeout_outcome_routes_to_record_timeout(self):
        """JOB_OUTCOME_TIMEOUT is handled by record_timeout, not record_abort."""
        from event_loop import JOB_OUTCOME_TIMEOUT
        from event_loop._loop import _observe_jobs

        # Build a minimal OrchestratorEventLoop-like object
        mock_loop = MagicMock()
        mock_loop._job_status_view = MagicMock()
        mock_loop._job_status_view.outcome_for.return_value = JOB_OUTCOME_TIMEOUT
        mock_loop._job_status_view.reap_terminated = MagicMock(return_value=1)
        mock_loop._job_status_view.exit_detail_for = MagicMock(return_value="killed by 2h agent timeout")
        mock_loop._live_keys = {"test-key"}
        mock_loop._key_meta = {"test-key": ("propose", "coder")}
        mock_loop.pipeline_id = "issue-3665"
        mock_loop.slice_id = None
        mock_loop.supervisor = MagicMock()
        mock_loop.clock = lambda: 0.0

        _observe_jobs(mock_loop)

        # record_timeout should have been called, NOT record_abort
        mock_loop.supervisor.record_timeout.assert_called_once()
        mock_loop.supervisor.record_abort.assert_not_called()
        # The key should be dropped from the live set
        assert "test-key" not in mock_loop._live_keys


# ---------------------------------------------------------------------------
# TASK-4-4: Timeout warning heartbeat
# ---------------------------------------------------------------------------


class TestTimeoutWarning:
    """Verify the timeout warning heartbeat is sent at 90 minutes."""

    def test_timeout_warning_sent_at_90_minutes(self):
        """A HEARTBEAT with WAITING_FOR_EVENT is sent at 90 minutes."""
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._timeout_warning_last_sent = {}
        monitor._lock = MagicMock()
        monitor._health_check_runner = None
        monitor._lock = MagicMock()

        # Create a pipeline that started 91 minutes ago
        import time as _time

        phase_exec = SimpleNamespace(
            work_started_at=_time.time() - 91 * 60,  # 91 minutes ago
        )
        pipeline = SimpleNamespace(
            current_phase=SimpleNamespace(value="implement"),
            phases={"implement": phase_exec},
            config=SimpleNamespace(agent_timeout_seconds=7200),
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            monitor._send_timeout_warnings(pipeline, "issue-3665")

            mock_store.add_message.assert_called_once()
            msg = mock_store.add_message.call_args[0][0]
            assert msg.message_type == "HEARTBEAT"
            assert msg.metadata["state"] == "WAITING_FOR_EVENT"
            assert "timeout" in msg.body.lower()

    def test_timeout_warning_not_sent_before_90_minutes(self):
        """No warning is sent before 90 minutes."""
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._timeout_warning_last_sent = {}
        monitor._lock = MagicMock()
        monitor._health_check_runner = None

        import time as _time

        phase_exec = SimpleNamespace(
            work_started_at=_time.time() - 30 * 60,  # 30 minutes ago
        )
        pipeline = SimpleNamespace(
            current_phase=SimpleNamespace(value="implement"),
            phases={"implement": phase_exec},
            config=SimpleNamespace(agent_timeout_seconds=7200),
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            monitor._send_timeout_warnings(pipeline, "issue-3665")

            mock_store.add_message.assert_not_called()

    def test_timeout_warning_not_sent_twice_in_window(self):
        """A second warning is not sent within the 90-minute interval."""
        from kubernetes_monitor import KubernetesMonitor

        monitor = KubernetesMonitor.__new__(KubernetesMonitor)
        monitor._timeout_warning_last_sent = {}
        monitor._lock = MagicMock()
        monitor._health_check_runner = None

        import time as _time

        phase_exec = SimpleNamespace(
            work_started_at=_time.time() - 91 * 60,  # 91 minutes ago
        )
        pipeline = SimpleNamespace(
            current_phase=SimpleNamespace(value="implement"),
            phases={"implement": phase_exec},
            config=SimpleNamespace(agent_timeout_seconds=7200),
        )

        with patch("message_store.get_message_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            # First call should send the warning
            monitor._send_timeout_warnings(pipeline, "issue-3665")
            assert mock_store.add_message.call_count == 1

            # Second call within the window should NOT send
            monitor._send_timeout_warnings(pipeline, "issue-3665")
            assert mock_store.add_message.call_count == 1
