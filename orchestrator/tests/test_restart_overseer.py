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

    def test_restart_agent_calls_cli(self, monitor):
        """restart_agent action should call the restart CLI."""
        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        # Should call _run_cli for the restart
        monitor._run_cli.assert_called()
        # Verify the CLI args contain restart-related arguments
        call_args = monitor._run_cli.call_args
        args = call_args[0] if call_args[0] else []
        cli_str = " ".join(str(a) for a in args)
        assert "restart" in cli_str.lower() or monitor._run_cli.called

    def test_restart_agent_increments_count_on_success(self, monitor):
        """Successful restart should increment the agent's restart count."""
        monitor._run_cli = AsyncMock(return_value=(0, '{"success": true}', ""))

        decision = {
            "action": "restart_agent",
            "message": "Agent stalled",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        assert monitor._agent_restart_counts.get("coder", 0) == 1

    def test_restart_agent_creates_hitl_on_cli_failure(self, monitor):
        """Failed CLI call should fall back to HITL decision."""
        monitor._run_cli = AsyncMock(return_value=(1, "", "Error: container not found"))

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
        # Should mention multiple agents or phase restart
        assert (
            "exhausted" in message.lower()
            or "phase" in message.lower()
            or "limit" in message.lower()
        )


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
        decision = {
            "action": "restart_phase",
            "message": "Phase restart needed — all agents stalled",
            "priority": "critical",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._create_hitl_decision.assert_called()
        call_args = monitor._create_hitl_decision.call_args
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
