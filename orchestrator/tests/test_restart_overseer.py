"""Tests for overseer restart actions.

Covers:
- restart_agent and restart_phase actions in decision maker prompt (task-1-5)
- _execute_action handling for restart_agent (task-1-5)
- _execute_action handling for restart_phase (task-1-5)
- _execute_restart_agent with restart count tracking and limit enforcement
- Escalation from agent restart to phase restart when 2+ agents exhausted
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())


# ---------------------------------------------------------------------------
# Conditional imports
# ---------------------------------------------------------------------------

try:
    from overseer.decision_maker import decide_corrective_action
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.decision_maker not available yet: {exc}",
        allow_module_level=True,
    )

try:
    from overseer.monitor import OverseerMonitor
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.monitor not available yet: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


try:
    from egg_agent.result import AgentResult
except ImportError:
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class AgentResult:  # type: ignore[no-redef]
        success: bool
        stdout: str
        stderr: str = ""
        returncode: int = 0
        error: str | None = None
        metadata: dict[str, Any] | None = None
        cost_usd: float | None = None
        num_turns: int | None = None
        duration_ms: int | None = None
        session_id: str | None = None


def _make_result(stdout: str, *, success: bool = True) -> AgentResult:
    return AgentResult(
        success=success,
        stdout=stdout,
        stderr="",
        returncode=0 if success else 1,
    )


_AGENT_PATCH = "overseer.decision_maker.run_agent_async"


# ---------------------------------------------------------------------------
# Decision maker tests — restart actions in prompt
# ---------------------------------------------------------------------------


class TestDecisionMakerRestartActions:
    """Tests that restart_agent and restart_phase are in the decision maker prompt."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_restart_agent_action_accepted(self, mock_agent: AsyncMock) -> None:
        """Decision maker should accept restart_agent as a valid action."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "Agent stalled after 3 heartbeat failures",
                    "priority": "high",
                }
            )
        )

        result = _run(
            decide_corrective_action(
                classification={
                    "classification": "stalled",
                    "confidence": 0.95,
                    "reasoning": "No heartbeat for 15 minutes",
                },
                context={
                    "agent_role": "coder",
                    "pipeline_id": "issue-100",
                },
            )
        )

        assert result["action"] == "restart_agent"
        assert result["priority"] in ("high", "critical")

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_restart_phase_action_accepted(self, mock_agent: AsyncMock) -> None:
        """Decision maker should accept restart_phase as a valid action."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_phase",
                    "message": "Multiple agents stalled",
                    "priority": "critical",
                }
            )
        )

        result = _run(
            decide_corrective_action(
                classification={
                    "classification": "stalled",
                    "confidence": 0.95,
                },
                context={
                    "pipeline_id": "issue-100",
                    "stalled_agents": ["coder", "tester"],
                },
            )
        )

        assert result["action"] == "restart_phase"

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_prompt_includes_restart_agent_description(self, mock_agent: AsyncMock) -> None:
        """The prompt sent to the LLM should mention restart_agent."""
        mock_agent.return_value = _make_result(
            json.dumps({"action": "nudge", "message": "ok", "priority": "low"})
        )

        _run(
            decide_corrective_action(
                classification={"classification": "stalled"},
                context={},
            )
        )

        # Check the prompt passed to the agent
        call_args = mock_agent.call_args
        prompt_text = str(call_args)
        assert "restart_agent" in prompt_text

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_prompt_includes_restart_phase_description(self, mock_agent: AsyncMock) -> None:
        """The prompt sent to the LLM should mention restart_phase."""
        mock_agent.return_value = _make_result(
            json.dumps({"action": "nudge", "message": "ok", "priority": "low"})
        )

        _run(
            decide_corrective_action(
                classification={"classification": "stalled"},
                context={},
            )
        )

        call_args = mock_agent.call_args
        prompt_text = str(call_args)
        assert "restart_phase" in prompt_text


# ---------------------------------------------------------------------------
# Monitor _execute_action tests for restart
# ---------------------------------------------------------------------------


class TestMonitorExecuteRestartAgent:
    """Tests for _execute_action handling of restart_agent."""

    @pytest.fixture
    def monitor(self):
        """Create an OverseerMonitor with mocked dependencies."""
        monitor = OverseerMonitor.__new__(OverseerMonitor)
        monitor.pipeline_id = "issue-100"
        monitor._oversight_log = []
        monitor.self_monitor = MagicMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        monitor._agent_restart_counts = {}
        monitor._max_agent_restarts = 2
        return monitor

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_agent_calls_rest_api(self, mock_request_cls, mock_build_opener, monitor):
        """restart_agent action should POST to the REST API endpoint."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        # Should create a Request to the restart endpoint
        mock_request_cls.assert_called_once()
        call_args = mock_request_cls.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/agents/coder/restart" in url, f"Expected restart URL, got: {url}"
        assert "issue-100" in url, f"Expected pipeline_id in URL, got: {url}"

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_agent_increments_count_on_success(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Successful restart should increment the agent's restart count."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        assert monitor._agent_restart_counts.get("coder", 0) == 1

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_agent_creates_hitl_on_api_failure(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Failed API call should fall back to HITL decision."""
        import urllib.error

        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("connection refused")
        mock_build_opener.return_value = mock_opener

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._create_hitl_decision.assert_called()

    def test_restart_agent_limit_creates_hitl(self, monitor):
        """When restart limit is reached, should create HITL decision."""
        monitor._agent_restart_counts["coder"] = 2  # At limit

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        # Should not call CLI — limit reached
        monitor._run_cli.assert_not_called()
        # Should escalate to HITL
        monitor._create_hitl_decision.assert_called()

    def test_restart_multi_agent_exhaustion_escalates_to_phase(self, monitor):
        """When 2+ agents exhaust restart limits, should mention phase restart."""
        monitor._agent_restart_counts["coder"] = 2
        monitor._agent_restart_counts["tester"] = 2

        decision = {
            "action": "restart_agent",
            "message": "Documenter also stalled",
            "priority": "high",
        }

        # Now a 3rd agent hits its limit
        monitor._agent_restart_counts["documenter"] = 2

        _run(monitor._execute_action(decision, "documenter"))

        monitor._create_hitl_decision.assert_called()
        call_args = monitor._create_hitl_decision.call_args
        message = str(call_args)
        message_lower = message.lower()
        # Should mention that restart limits were exhausted
        assert "exhausted" in message_lower or "limit" in message_lower, (
            f"Expected 'exhausted' or 'limit' in HITL message: {message}"
        )
        # Should reference at least two of the exhausted agents by name
        exhausted_mentioned = sum(
            1 for agent in ("coder", "tester", "documenter") if agent in message_lower
        )
        assert exhausted_mentioned >= 2, (
            f"Expected at least 2 exhausted agent names in HITL message, "
            f"found {exhausted_mentioned}: {message}"
        )

    def test_restart_agent_uses_async_to_thread(self, monitor):
        """_execute_restart_agent must not block the event loop.

        Verifies that the synchronous HTTP call runs via asyncio.to_thread
        (i.e. _sync_restart_request is called, not inline urllib).
        """
        monitor._sync_restart_request = MagicMock(return_value={"success": True})

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._sync_restart_request.assert_called_once()
        call_args = monitor._sync_restart_request.call_args
        url = call_args[0][0]
        assert "/agents/coder/restart" in url
        assert monitor._agent_restart_counts.get("coder", 0) == 1


class TestMonitorExecuteRestartPhase:
    """Tests for _execute_action handling of restart_phase."""

    @pytest.fixture
    def monitor(self):
        """Create an OverseerMonitor with mocked dependencies."""
        monitor = OverseerMonitor.__new__(OverseerMonitor)
        monitor.pipeline_id = "issue-100"
        monitor._oversight_log = []
        monitor.self_monitor = MagicMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        monitor._agent_restart_counts = {}
        monitor._max_agent_restarts = 2
        return monitor

    def test_restart_phase_creates_hitl_by_default(self, monitor):
        """restart_phase action should create HITL decision (requires approval)."""
        monitor._create_phase_restart_decision = AsyncMock()
        decision = {
            "action": "restart_phase",
            "message": "Phase restart needed — all agents stalled",
            "priority": "critical",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._create_phase_restart_decision.assert_called()
        call_args = monitor._create_phase_restart_decision.call_args
        message = str(call_args)
        assert "restart" in message.lower() or "phase" in message.lower()

    def test_restart_phase_does_not_call_cli_directly(self, monitor):
        """restart_phase should NOT directly call CLI — it goes through HITL."""
        decision = {
            "action": "restart_phase",
            "message": "Phase restart needed",
            "priority": "critical",
        }

        _run(monitor._execute_action(decision, "coder"))

        # The CLI should not be called directly for phase restart
        # (it goes through HITL approval first)
        # _run_cli should not be called with restart-phase args
        for call in monitor._run_cli.call_args_list:
            args = call[0] if call[0] else []
            cli_str = " ".join(str(a) for a in args)
            assert "restart-phase" not in cli_str
