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
from typing import cast
from unittest.mock import patch

import pytest

# Add sandbox/ to sys.path so overseer_monitor is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from overseer_monitor import (
    TERMINAL_STATES,
    main,
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

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_run_once_includes_running_agents(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        """Issue #2084: cycle report carries per-agent ``container_id`` /
        ``started_at`` / ``elapsed_seconds`` so the overseer can anchor
        stall-duration math on the live container, not message-bus history.
        Only agents with ``status=='running'`` are surfaced."""
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {
                "consensus": {},
                "agents": [
                    {
                        "role": "coder",
                        "status": "running",
                        "container_id": "f98c4fe6abcdef",
                        "started_at": "2026-04-25T21:04:23+00:00",
                        "elapsed_seconds": 152,
                    },
                    {
                        "role": "tester",
                        "status": "complete",
                        "container_id": "old-finished",
                    },
                ],
            },
        }
        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        running = report["running_agents"]
        assert len(running) == 1
        assert running[0]["role"] == "coder"
        assert running[0]["container_id"] == "f98c4fe6abcdef"
        assert running[0]["elapsed_seconds"] == 152

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_run_once_running_agents_empty_when_concurrent_absent(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, capsys
    ):
        """Sequential pipelines have no ``concurrent`` block — must default
        to an empty ``running_agents`` list, not raise."""
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "refine"},
        }
        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        assert report["running_agents"] == []


class TestRunOnceConfigTripwire:
    """Issue #2118: emit a config-unavailable alert when the migrated
    detectors would otherwise run silently against an empty config_subset.
    The three causes (pipeline_unreachable / config_key_missing /
    config_block_empty) must be distinguishable from the alert detail."""

    @staticmethod
    def _config_alerts(report: dict[str, object]) -> list[dict[str, object]]:
        return [a for a in cast(list[dict[str, object]], report["detector_alerts"]) if a.get("anomaly") == "config-unavailable"]

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_pipeline_unreachable_triggers_tripwire(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        """Empty pipeline_data — _orch_get swallowed the upstream error."""
        monkeypatch.setenv("EGG_OVERSEER_TEST_MODE", "1")
        mock_status.return_value = {}

        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        tripwires = self._config_alerts(report)
        assert len(tripwires) == 1
        assert tripwires[0]["priority"] == "high"
        assert tripwires[0]["calibration_only"] is False
        assert "pipeline_unreachable" in cast(str, tripwires[0]["detail"])

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_config_key_missing_triggers_tripwire(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        """Server returned 200 but the status route omitted the config block."""
        monkeypatch.setenv("EGG_OVERSEER_TEST_MODE", "1")
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {"consensus": {}},
        }

        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        tripwires = self._config_alerts(report)
        assert len(tripwires) == 1
        assert "config_key_missing" in cast(str, tripwires[0]["detail"])

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_config_block_empty_triggers_tripwire(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        """Server returned config={} — same calibration-blind state."""
        monkeypatch.setenv("EGG_OVERSEER_TEST_MODE", "1")
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {"consensus": {}},
            "config": {},
        }

        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        tripwires = self._config_alerts(report)
        assert len(tripwires) == 1
        assert "config_block_empty" in cast(str, tripwires[0]["detail"])

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_populated_config_does_not_trigger_tripwire(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        """Happy path: config block present and populated — no tripwire."""
        monkeypatch.setenv("EGG_OVERSEER_TEST_MODE", "1")
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {"consensus": {}},
            "config": {"overseer_agent_stall_seconds": 240},
        }

        report = run_once(PIPELINE_ID, base_url=BASE_URL)

        assert self._config_alerts(report) == []


class TestMainExitCodes:
    """Test main() exit code mapping for --once mode."""

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_main_once_running_exits_zero(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        mock_status.return_value = {
            "status": "running",
            "phase": {"name": "implement"},
            "concurrent": {},
        }
        monkeypatch.setenv("EGG_PIPELINE_ID", PIPELINE_ID)
        monkeypatch.setattr("sys.argv", ["overseer_monitor.py", "--once"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_main_once_complete_exits_zero(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        mock_status.return_value = {
            "status": "complete",
            "phase": {"name": "pr"},
            "concurrent": {},
        }
        monkeypatch.setenv("EGG_PIPELINE_ID", PIPELINE_ID)
        monkeypatch.setattr("sys.argv", ["overseer_monitor.py", "--once"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_main_once_failed_exits_one(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        mock_status.return_value = {"status": "failed", "phase": {}, "concurrent": {}}
        monkeypatch.setenv("EGG_PIPELINE_ID", PIPELINE_ID)
        monkeypatch.setattr("sys.argv", ["overseer_monitor.py", "--once"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    @patch("overseer_monitor.send_heartbeat", return_value=True)
    @patch("overseer_monitor.poll_messages", return_value=[])
    @patch("overseer_monitor.query_progress", return_value=[])
    @patch("overseer_monitor.query_health_alerts", return_value=[])
    @patch("overseer_monitor.query_pipeline_status")
    def test_main_once_unknown_exits_one(
        self, mock_status, mock_alerts, mock_progress, mock_msgs, mock_hb, monkeypatch
    ):
        """Unknown status (API failure) should exit 1, not 0."""
        mock_status.return_value = {}
        monkeypatch.setenv("EGG_PIPELINE_ID", PIPELINE_ID)
        monkeypatch.setattr("sys.argv", ["overseer_monitor.py", "--once"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


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
