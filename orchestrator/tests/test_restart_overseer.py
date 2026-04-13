"""Tests for overseer restart actions.

Covers:
- restart_agent and restart_phase actions in decision maker prompt (task-1-5)
- _execute_action handling for restart_agent (task-1-5)
- _execute_action handling for restart_phase (task-1-5)
- _execute_restart_agent with restart count tracking and limit enforcement
- Escalation from agent restart to phase restart when 2+ agents exhausted
- Issue #1695 gaps: restartable infra error routing, overseer reads count
  from API, no shadow counter, decide_escalation_level restartable routing
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
        """Create an OverseerMonitor with mocked dependencies.

        Note: _agent_restart_counts and _max_agent_restarts were removed in
        issue #1695 — the spawner is now the single source of truth.
        """
        monitor = OverseerMonitor.__new__(OverseerMonitor)
        monitor.pipeline_id = "issue-100"
        monitor._oversight_log = []
        monitor.self_monitor = MagicMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        monitor._log_oversight_event = MagicMock()
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
    def test_restart_agent_logs_on_success(self, mock_request_cls, mock_build_opener, monitor):
        """Successful restart should log the oversight event with restart count from API.

        The shadow counter was removed in issue #1695 — the spawner (via the REST
        API response) is now the single source of truth for restart counts.
        """
        api_response = {
            "success": True,
            "data": {"container_id": "new-abc", "agent_role": "coder", "restart_count": 1},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
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

        # Should log the oversight event with count from the API response
        monitor._log_oversight_event.assert_called()
        logged = monitor._log_oversight_event.call_args[0][0]
        assert logged["restart_count"] == 1

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

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_limit_from_api_creates_hitl(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """When spawner returns limit exceeded error, monitor should create HITL decision.

        The shadow counter was removed in issue #1695 — limit enforcement now
        comes from the spawner via the REST API response.
        """
        api_response = {
            "success": False,
            "message": "Restart limit (2) exceeded for coder in pipeline issue-100",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
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

        # Should escalate to HITL
        monitor._create_hitl_decision.assert_called()
        call_msg = str(monitor._create_hitl_decision.call_args)
        assert "failed" in call_msg.lower() or "restart" in call_msg.lower()

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_api_network_failure_creates_hitl(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Network failure during restart API call should fall back to HITL decision."""
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
        call_msg = str(monitor._create_hitl_decision.call_args)
        assert "coder" in call_msg.lower()


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


# ---------------------------------------------------------------------------
# Issue #1695 item 6: Restartable infra error routing in decision_maker
# ---------------------------------------------------------------------------


try:
    from overseer.decision_maker import (
        _RESTARTABLE_RE,
        RESTARTABLE_PATTERNS,
        decide_escalation_level,
    )

    _HAS_RESTARTABLE = True
except ImportError:
    _HAS_RESTARTABLE = False


class TestInfraErrorRestartRouting:
    """Tests that infrastructure errors with restartable keywords route to restart_agent."""

    @pytest.mark.parametrize(
        "reasoning",
        [
            "Agent is unresponsive after 15 minutes",
            "Container crashed with exit code 137",
            "Agent hit OOM limit",
            "Request timeout after 300 seconds",
            "Agent process hung during tool execution",
            "Agent is not responding to heartbeats",
        ],
    )
    def test_restartable_infra_error_returns_restart_agent(self, reasoning):
        """Infrastructure errors with restartable keywords should trigger restart_agent."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": reasoning,
        }
        result = _run(
            decide_corrective_action(
                classification=classification,
                context={"agent_role": "coder", "pipeline_id": "issue-100"},
            )
        )

        assert result["action"] == "restart_agent", (
            f"Expected restart_agent for reasoning '{reasoning}', got '{result['action']}'"
        )
        assert result["priority"] == "high"

    @pytest.mark.parametrize(
        "reasoning",
        [
            "Permission denied accessing /etc/passwd",
            "Read-only filesystem error on /home/egg/repos",
            "Docker daemon returned 403 Forbidden",
            "Certificate verification failed",
            "Network policy blocks egress to PyPI",
            "Infrastructure error detected",  # default/generic
        ],
    )
    def test_non_restartable_infra_error_returns_hitl(self, reasoning):
        """Infrastructure errors without restartable keywords should go to HITL."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": reasoning,
        }
        result = _run(
            decide_corrective_action(
                classification=classification,
                context={"agent_role": "coder", "pipeline_id": "issue-100"},
            )
        )

        assert result["action"] == "hitl", (
            f"Expected hitl for reasoning '{reasoning}', got '{result['action']}'"
        )
        assert result["priority"] == "critical"

    def test_non_infra_error_bypasses_fast_path(self):
        """Non-infrastructure errors should go through the LLM decision path."""
        with patch(_AGENT_PATCH, new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = _make_result(
                json.dumps({"action": "nudge", "message": "keep going", "priority": "low"})
            )
            result = _run(
                decide_corrective_action(
                    classification={
                        "classification": "stalled",
                        "reasoning": "No progress for 10 minutes",
                    },
                    context={"agent_role": "coder"},
                )
            )
            # Should call LLM, not the fast path
            mock_agent.assert_called_once()
            assert result["action"] == "nudge"

    @pytest.mark.skipif(not _HAS_RESTARTABLE, reason="RESTARTABLE_PATTERNS not importable")
    def test_restartable_patterns_list_exists(self):
        """RESTARTABLE_PATTERNS should be a non-empty list of strings."""
        assert isinstance(RESTARTABLE_PATTERNS, list)
        assert len(RESTARTABLE_PATTERNS) > 0
        for pattern in RESTARTABLE_PATTERNS:
            assert isinstance(pattern, str)

    @pytest.mark.skipif(not _HAS_RESTARTABLE, reason="_RESTARTABLE_RE not importable")
    def test_restartable_regex_case_insensitive(self):
        """Regex should match restartable patterns case-insensitively."""
        assert _RESTARTABLE_RE.search("Agent is UNRESPONSIVE")
        assert _RESTARTABLE_RE.search("OOM killed")
        assert _RESTARTABLE_RE.search("Process Timed Out — TIMEOUT reached")
        assert not _RESTARTABLE_RE.search("Permission denied")

    def test_infra_error_with_mixed_keywords(self):
        """Infra error with both restartable and non-restartable words should restart."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": "Agent crashed after permission error on filesystem",
        }
        result = _run(
            decide_corrective_action(
                classification=classification,
                context={"agent_role": "coder"},
            )
        )
        # "crashed" is a restartable keyword, should trigger restart
        assert result["action"] == "restart_agent"


# ---------------------------------------------------------------------------
# Issue #1695 item 6: Restartable routing in decide_escalation_level
# ---------------------------------------------------------------------------


class TestEscalationLevelRestartRouting:
    """Tests for restartable infra error routing in decide_escalation_level."""

    @pytest.mark.parametrize(
        "reasoning",
        [
            "Agent is unresponsive",
            "Container crashed",
            "OOM killed",
            "Agent hung during execution",
            "timeout waiting for response",
            "Agent not responding",
        ],
    )
    def test_restartable_escalation_returns_restart_agent_level(self, reasoning):
        """Restartable infra errors should return level='restart_agent' in escalation."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": reasoning,
        }
        result = _run(
            decide_escalation_level(
                classification=classification,
                redirect_history=[],
                context={"agent_role": "coder"},
            )
        )

        assert result["escalate"] is True
        assert result["level"] == "restart_agent", (
            f"Expected level 'restart_agent' for '{reasoning}', got '{result['level']}'"
        )

    @pytest.mark.parametrize(
        "reasoning",
        [
            "Permission denied",
            "EROFS: read-only filesystem",
            "Certificate expired",
        ],
    )
    def test_non_restartable_escalation_returns_hitl(self, reasoning):
        """Non-restartable infra errors should still return level='hitl' in escalation."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": reasoning,
        }
        result = _run(
            decide_escalation_level(
                classification=classification,
                redirect_history=[],
                context={"agent_role": "coder"},
            )
        )

        assert result["escalate"] is True
        assert result["level"] == "hitl", (
            f"Expected level 'hitl' for '{reasoning}', got '{result['level']}'"
        )


# ---------------------------------------------------------------------------
# Issue #1695 items 2+3: Overseer reads restart count from API, no shadow counter
# ---------------------------------------------------------------------------


class TestMonitorNoShadowCounter:
    """Tests that the overseer monitor has no independent restart counter."""

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
        return monitor

    def test_no_agent_restart_counts_attribute(self, monitor):
        """OverseerMonitor should no longer have _agent_restart_counts."""
        assert not hasattr(monitor, "_agent_restart_counts"), (
            "Shadow counter _agent_restart_counts should be removed (issue #1695 item 2)"
        )

    def test_no_max_agent_restarts_attribute(self, monitor):
        """OverseerMonitor should no longer have _max_agent_restarts."""
        assert not hasattr(monitor, "_max_agent_restarts"), (
            "Shadow limit _max_agent_restarts should be removed (issue #1695 item 2)"
        )

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_reads_count_from_api_response(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Successful restart should read restart_count from the API response data."""
        api_response = {
            "success": True,
            "data": {
                "container_id": "new-container-xyz",
                "agent_role": "coder",
                "restart_count": 2,
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
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

        # Should log the oversight event with the count from the API
        monitor._log_oversight_event.assert_called()
        logged = monitor._log_oversight_event.call_args[0][0]
        assert logged["restart_count"] == 2

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_restart_limit_error_creates_hitl(self, mock_request_cls, mock_build_opener, monitor):
        """API returning success=false (limit exceeded) should create HITL decision."""
        api_response = {
            "success": False,
            "message": "Restart limit (2) exceeded for coder in pipeline issue-100",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
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

        # Should escalate to HITL
        monitor._create_hitl_decision.assert_called()
        call_msg = str(monitor._create_hitl_decision.call_args)
        assert "failed" in call_msg.lower() or "restart" in call_msg.lower()
