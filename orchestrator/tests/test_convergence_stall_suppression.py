"""Tests for convergence-stall suppression via agent activity (#3665)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestHasRecentAgentActivity:
    """Tests for _has_recent_agent_activity on OrchestratorEventLoop."""

    def test_returns_true_when_heartbeat_recent(self) -> None:
        from event_loop import OrchestratorEventLoop

        loop = MagicMock(spec=OrchestratorEventLoop)
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        # Bind the real method
        from event_loop._loop import _has_recent_agent_activity

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": None,
                "last_activity_age_s": None,
            }
        }
        hm._config = MagicMock()
        hm._config.orchestrator_activity_quiet_seconds = 120
        hm._is_brc_idle.return_value = False

        with patch("health_monitor.get_health_monitor", return_value=hm):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is True

    def test_returns_true_when_activity_recent(self) -> None:
        from event_loop._loop import _has_recent_agent_activity

        loop = MagicMock()
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 600.0,
                "last_progress_age_s": None,
                "last_activity_age_s": 5.0,
            }
        }
        hm._config = MagicMock()
        hm._config.orchestrator_activity_quiet_seconds = 120
        hm._is_brc_idle.return_value = False

        with patch("health_monitor.get_health_monitor", return_value=hm):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is True

    def test_returns_false_when_all_ages_stale(self) -> None:
        from event_loop._loop import _has_recent_agent_activity

        loop = MagicMock()
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 600.0,
                "last_progress_age_s": 600.0,
                "last_activity_age_s": 600.0,
            }
        }
        hm._config = MagicMock()
        hm._config.orchestrator_activity_quiet_seconds = 120
        hm._is_brc_idle.return_value = False

        with patch("health_monitor.get_health_monitor", return_value=hm):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is False

    def test_returns_false_when_agent_not_in_monitor(self) -> None:
        from event_loop._loop import _has_recent_agent_activity

        loop = MagicMock()
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {}
        hm._config = MagicMock()
        hm._config.orchestrator_activity_quiet_seconds = 120

        with patch("health_monitor.get_health_monitor", return_value=hm):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is False

    def test_returns_false_when_health_monitor_unavailable(self) -> None:
        from event_loop._loop import _has_recent_agent_activity

        loop = MagicMock()
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        with patch("health_monitor.get_health_monitor", return_value=None):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is False

    def test_returns_false_when_quiet_seconds_zero(self) -> None:
        from event_loop._loop import _has_recent_agent_activity

        loop = MagicMock()
        loop.pipeline_id = "issue-99"
        loop.slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": None,
                "last_activity_age_s": None,
            }
        }
        hm._config = MagicMock()
        hm._config.orchestrator_activity_quiet_seconds = 0

        with patch("health_monitor.get_health_monitor", return_value=hm):
            result = _has_recent_agent_activity(loop, "coder")
        assert result is False
