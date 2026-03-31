"""Tests for the pre-built overseer monitoring script.

Validates terminal state detection, cycle output format, heartbeat sending,
SIGTERM handling, and error resilience.
"""

import json
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

# Add sandbox/ to sys.path so overseer_monitor is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from overseer_monitor import (
    TERMINAL_STATES,
    query_health_alerts,
    query_pipeline_status,
    query_progress,
    run_monitor,
    run_once,
    send_heartbeat,
    send_message,
)

BASE_URL = "http://test-orchestrator:9090"
PIPELINE_ID = "test-pipeline-001"


class TestQueryFunctions:
    """Test individual query functions handle errors gracefully."""

    @patch("overseer_monitor.api_request")
    def test_query_pipeline_status_success(self, mock_req):
        mock_req.return_value = {"data": {"status": "running", "phase": {"name": "implement"}}}
        result = query_pipeline_status(BASE_URL, PIPELINE_ID)
        assert result["status"] == "running"

    @patch("overseer_monitor.api_request", side_effect=Exception("connection refused"))
    def test_query_pipeline_status_failure(self, mock_req):
        result = query_pipeline_status(BASE_URL, PIPELINE_ID)
        assert result == {}

    @patch("overseer_monitor.api_request")
    def test_query_health_alerts_success(self, mock_req):
        mock_req.return_value = {"alerts": [{"severity": "critical", "message": "stall"}]}
        result = query_health_alerts(BASE_URL, PIPELINE_ID)
        assert len(result) == 1
        assert result[0]["severity"] == "critical"

    @patch("overseer_monitor.api_request", side_effect=Exception("timeout"))
    def test_query_health_alerts_failure(self, mock_req):
        result = query_health_alerts(BASE_URL, PIPELINE_ID)
        assert result == []

    @patch("overseer_monitor.api_request")
    def test_query_progress_success(self, mock_req):
        mock_req.return_value = {"data": {"events": [{"step": "running tests"}]}}
        result = query_progress(BASE_URL, PIPELINE_ID)
        assert len(result) == 1

    @patch("overseer_monitor.api_request", side_effect=Exception("error"))
    def test_query_progress_failure(self, mock_req):
        result = query_progress(BASE_URL, PIPELINE_ID)
        assert result == []

    @patch("overseer_monitor.api_request")
    def test_send_heartbeat_success(self, mock_req):
        mock_req.return_value = {"success": True}
        assert send_heartbeat(BASE_URL, PIPELINE_ID, "overseer") is True

    @patch("overseer_monitor.api_request", side_effect=Exception("error"))
    def test_send_heartbeat_failure(self, mock_req):
        assert send_heartbeat(BASE_URL, PIPELINE_ID, "overseer") is False

    @patch("overseer_monitor.api_request")
    def test_send_message_success(self, mock_req):
        mock_req.return_value = {"success": True}
        assert send_message(BASE_URL, PIPELINE_ID, "overseer", "coder", "test", "body") is True


class TestRunMonitor:
    """Test the main monitoring loop."""

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_exits_on_complete(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {
            "status": "complete",
            "phase": {"name": "pr"},
            "concurrent": {},
        }
        code = run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=1)
        assert code == 0

        output = capsys.readouterr().out.strip()
        report = json.loads(output)
        assert report["terminal"] is True
        assert report["status"] == "complete"
        assert report["cycle"] == 1

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_exits_on_failed(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {"status": "failed", "phase": {}, "concurrent": {}}
        code = run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=1)
        assert code == 1

        report = json.loads(capsys.readouterr().out.strip())
        assert report["terminal"] is True
        assert report["status"] == "failed"

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_exits_on_cancelled(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {"status": "cancelled", "phase": {}, "concurrent": {}}
        code = run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=1)
        assert code == 1

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[{"step": "coding"}])
    @patch("overseer_monitor.query_health_alerts")
    @patch("overseer_monitor.query_pipeline_status")
    def test_reports_alerts(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        """After one running cycle, the next cycle returns complete."""
        call_count = 0

        def status_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status": "running", "phase": {"name": "implement"}, "concurrent": {}}
            return {"status": "complete", "phase": {"name": "pr"}, "concurrent": {}}

        mock_status.side_effect = status_side_effect
        mock_alerts.return_value = [{"severity": "warning", "message": "slow agent"}]

        code = run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=0)
        assert code == 0

        lines = capsys.readouterr().out.strip().split("\n")
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["cycle"] == 1
        assert first["status"] == "running"
        assert first["alerts"] == 1
        assert first["progress_events"] == 1

        second = json.loads(lines[1])
        assert second["terminal"] is True

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_includes_consensus(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {
            "status": "complete",
            "phase": {"name": "implement"},
            "concurrent": {"consensus": {"is_complete": True, "confirmed": ["coder"]}},
        }
        run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=1)

        report = json.loads(capsys.readouterr().out.strip())
        assert report["consensus"]["is_complete"] is True

    @patch("overseer_monitor.send_heartbeat", return_value=False)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_heartbeat_failure_reported(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {"status": "complete", "phase": {}, "concurrent": {}}
        run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=1)

        report = json.loads(capsys.readouterr().out.strip())
        assert report["heartbeat_ok"] is False


class TestTerminalStates:
    """Verify the terminal states constant."""

    def test_terminal_states(self):
        assert "complete" in TERMINAL_STATES
        assert "failed" in TERMINAL_STATES
        assert "cancelled" in TERMINAL_STATES
        assert "running" not in TERMINAL_STATES


class TestRunOnce:
    """Test the single-cycle --once mode."""

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[{"step": "coding"}])
    @patch("overseer_monitor.query_health_alerts", return_value=[{"severity": "warning"}])
    @patch("overseer_monitor.query_pipeline_status")
    def test_run_once_returns_report(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {"consensus": {}},
        }
        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        assert report["cycle"] == 1
        assert report["status"] == "running"
        assert report["phase"] == "implement"
        assert report["alerts"] == 1
        assert report["progress_events"] == 1
        assert report["terminal"] is False
        assert report["heartbeat_ok"] is True

        # Also emitted to stdout
        output = capsys.readouterr().out.strip()
        assert json.loads(output) == report

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_run_once_terminal_state(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {
            "status": "complete",
            "phase": {"name": "pr"},
            "concurrent": {},
        }
        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        assert report["terminal"] is True
        assert report["status"] == "complete"

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_run_once_failed_state(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {
            "status": "failed",
            "phase": {},
            "concurrent": {},
        }
        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        assert report["terminal"] is True
        assert report["status"] == "failed"


class TestSignalHandling:
    """Test SIGTERM causes clean shutdown."""

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_sigterm_causes_shutdown(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        mock_status.return_value = {"status": "running", "phase": {}, "concurrent": {}}

        def send_sigterm():
            time.sleep(0.3)
            signal.raise_signal(signal.SIGTERM)

        t = threading.Thread(target=send_sigterm)
        t.start()

        code = run_monitor(PIPELINE_ID, base_url=BASE_URL, poll_interval=60)
        t.join()

        assert code == 0
        lines = capsys.readouterr().out.strip().split("\n")
        last = json.loads(lines[-1])
        assert last["shutdown"] is True
        assert last["reason"] == "signal"
