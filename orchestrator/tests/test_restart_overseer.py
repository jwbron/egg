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
        monitor._agents_restart_exhausted = set()
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
        monitor._agents_restart_exhausted = set()
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
        NON_RESTARTABLE_PATTERNS,
        RESTARTABLE_PATTERNS,
        _is_restartable,
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

    def test_infra_error_with_mixed_keywords_prefers_non_restartable(self):
        """Infra error with both restartable and non-restartable words should go to HITL.

        The deny-list (NON_RESTARTABLE_PATTERNS) takes priority to prevent
        restart loops on persistent failures (e.g. "crashed after permission error").
        """
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
        # "permission" in deny-list overrides "crashed" in allow-list
        assert result["action"] == "hitl"


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
        monitor._agents_restart_exhausted = set()
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


# ---------------------------------------------------------------------------
# Issue #1695 review: deny-list for non-restartable patterns
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RESTARTABLE, reason="decision_maker not importable")
class TestNonRestartableDenyList:
    """Tests for the deny-list that prevents restart loops on persistent failures."""

    def test_deny_list_exists_and_is_non_empty(self):
        """NON_RESTARTABLE_PATTERNS should be a non-empty list."""
        assert isinstance(NON_RESTARTABLE_PATTERNS, list)
        assert len(NON_RESTARTABLE_PATTERNS) > 0

    @pytest.mark.parametrize(
        "error_text",
        [
            "Agent crashed after permission error on filesystem",
            "Container crashed with EROFS: read-only file system",
            "Agent hung due to certificate verification failure",
            "OOM but also config file not found",
            "Agent timeout during authentication failure",
            "Agent crashed: authorization denied for resource",
            "OOM crash: no space left on device",
        ],
    )
    def test_mixed_keywords_deny_list_wins(self, error_text):
        """When both restartable and non-restartable patterns are present, deny-list wins."""
        assert not _is_restartable(error_text), (
            f"Expected non-restartable for '{error_text}' (deny-list should override)"
        )

    @pytest.mark.parametrize(
        "error_text",
        [
            "Agent is unresponsive after 15 minutes",
            "Container crashed with exit code 137",
            "Agent hit OOM limit",
            "timeout waiting for response",
        ],
    )
    def test_pure_restartable_still_works(self, error_text):
        """Pure restartable errors (no deny-list match) should still be restartable."""
        assert _is_restartable(error_text), f"Expected restartable for '{error_text}'"

    def test_no_restartable_keyword_returns_false(self):
        """Errors without any restartable keyword should return False."""
        assert not _is_restartable("Permission denied accessing /etc/passwd")
        assert not _is_restartable("Network policy blocks egress")

    def test_mixed_keywords_in_decide_corrective_action(self):
        """End-to-end: mixed keywords should route to HITL, not restart_agent."""
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
        assert result["action"] == "hitl"

    def test_mixed_keywords_in_decide_escalation_level(self):
        """End-to-end: mixed keywords should route to HITL in escalation too."""
        classification = {
            "classification": "infrastructure_error",
            "reasoning": "Container crashed with EROFS on workspace mount",
        }
        result = _run(
            decide_escalation_level(
                classification=classification,
                redirect_history=[],
                context={"agent_role": "coder"},
            )
        )
        assert result["escalate"] is True
        assert result["level"] == "hitl"


# ---------------------------------------------------------------------------
# Issue #1695 review: multi-agent exhaustion -> phase restart escalation
# ---------------------------------------------------------------------------


class TestMultiAgentExhaustionEscalation:
    """Tests that 2+ agents exhausting restart limits triggers phase restart HITL."""

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
        monitor._agents_restart_exhausted = set()
        return monitor

    def _make_limit_exceeded_response(self):
        """Create a mock urllib response for restart limit exceeded."""
        api_response = {
            "success": False,
            "message": "Restart limit (2) exceeded",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_single_agent_exhaustion_creates_agent_hitl(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Single agent limit exceeded should create HITL for that agent, not phase."""
        mock_opener = MagicMock()
        mock_opener.open.return_value = self._make_limit_exceeded_response()
        mock_build_opener.return_value = mock_opener

        _run(monitor._execute_restart_agent("coder", "Agent stalled"))

        monitor._create_hitl_decision.assert_called_once()
        call_args = monitor._create_hitl_decision.call_args[0]
        # Should be for the specific agent, not "orchestrator"
        assert call_args[0] == "coder"

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_two_agents_exhausted_escalates_to_phase_restart(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """When 2+ agents exhaust limits, should escalate to phase restart HITL."""
        mock_opener = MagicMock()
        mock_opener.open.return_value = self._make_limit_exceeded_response()
        mock_build_opener.return_value = mock_opener

        # First agent exhausts limits
        _run(monitor._execute_restart_agent("coder", "Coder stalled"))
        assert len(monitor._agents_restart_exhausted) == 1

        # Reset mock for second call
        monitor._create_hitl_decision.reset_mock()
        mock_opener.open.return_value = self._make_limit_exceeded_response()

        # Second agent exhausts limits — should trigger phase restart escalation
        _run(monitor._execute_restart_agent("tester", "Tester stalled"))

        assert len(monitor._agents_restart_exhausted) == 2
        monitor._create_hitl_decision.assert_called_once()
        call_args = monitor._create_hitl_decision.call_args[0]
        # Should target "orchestrator" for phase-level escalation
        assert call_args[0] == "orchestrator"
        assert "phase" in call_args[1].lower()
        assert "coder" in call_args[1]
        assert "tester" in call_args[1]

    @patch("urllib.request.build_opener")
    @patch("urllib.request.Request")
    def test_successful_restart_does_not_mark_exhausted(
        self, mock_request_cls, mock_build_opener, monitor
    ):
        """Successful restart should not add agent to exhausted set."""
        api_response = {"success": True, "data": {"restart_count": 1}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        _run(monitor._execute_restart_agent("coder", "Agent stalled"))

        assert "coder" not in monitor._agents_restart_exhausted
