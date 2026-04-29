"""Tests for overseer decision maker functions (Phase 4).

Validates that the Sonnet/Opus-tier decision functions correctly build
prompts, call the LLM, and parse responses for corrective actions,
redirect messages, and escalation level decisions.
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
# Conditional import
# ---------------------------------------------------------------------------

try:
    from overseer.decision_maker import (
        compose_redirect_message,
        decide_corrective_action,
        decide_escalation_level,
    )
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.decision_maker not available yet: {exc}",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _run(coro):
    return asyncio.run(coro)


_AGENT_PATCH = "overseer.decision_maker.run_agent_async"


# ===================================================================
# decide_corrective_action
# ===================================================================


class TestDecideCorrectiveActionNudge:
    """Test that low-severity classifications result in a nudge."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_decide_corrective_action_nudge(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "nudge",
                    "message": "Please check your progress and send a heartbeat.",
                    "priority": "low",
                }
            )
        )

        classification = {
            "classification": "working",
            "confidence": 0.6,
            "reasoning": "Agent might be slow but working",
        }
        context = {"pipeline_id": "test-123", "phase": "implement"}

        result = _run(decide_corrective_action(classification, context))

        assert result["action"] == "nudge"
        assert result["priority"] == "low"
        assert "message" in result
        mock_agent.assert_awaited_once()

        # Verify it uses sonnet model
        call_kwargs = mock_agent.call_args
        assert call_kwargs.kwargs.get("model") == "sonnet"


class TestDecideCorrectiveActionEscalate:
    """Test that high-severity classifications result in escalation."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_decide_corrective_action_escalate(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "hitl",
                    "message": "Agent is stuck in a loop and needs human intervention.",
                    "priority": "high",
                }
            )
        )

        classification = {
            "classification": "stuck",
            "confidence": 0.95,
            "reasoning": "Repeated edit/revert cycle detected",
        }
        context = {"pipeline_id": "test-123", "phase": "implement"}

        result = _run(decide_corrective_action(classification, context))

        assert result["action"] == "hitl"
        assert result["priority"] == "high"
        mock_agent.assert_awaited_once()


class TestDecideCorrectiveActionFirstStallDowngrade:
    """Issue #2190: a `restart_agent` recommendation on a first-occurrence
    `stuck` classification must be overridden to `hitl` to avoid
    destroying in-flight commits from agents mid-tool-call. Routing
    through HITL (rather than `nudge`) keeps operator-targeted text out
    of the agent's inbox and gives the operator a real decision surface.

    The decision-maker prompt asks the model not to recommend
    `restart_agent` for a first stall alert, but prompts are advisory.
    The post-hoc guard in `_enforce_no_first_stall_restart` is the
    load-bearing enforcement.
    """

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_first_stall_restart_overridden_to_hitl(self, mock_agent: AsyncMock) -> None:
        # Model disregards the prompt and emits restart_agent on a first
        # stall — the guard must rewrite the action.
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "Agent is stuck. Restart to recover.",
                    "priority": "high",
                }
            )
        )
        classification = {
            "classification": "stuck",
            "confidence": 0.9,
            "reasoning": "No heartbeat for 5 minutes",
        }

        result = _run(decide_corrective_action(classification, {}, redirect_history=[]))

        assert result["action"] == "hitl"
        # Original recommendation is preserved in the message body for the
        # operator to see what the model wanted to do.
        assert "Agent is stuck. Restart to recover." in result["message"]
        # Inspection-first guidance is included for the operator.
        assert "mcp__egg__get_container_logs" in result["message"]

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_first_stall_restart_with_empty_message_no_dangling_colon(
        self, mock_agent: AsyncMock
    ) -> None:
        # When the model returns an empty message, the override must not
        # leave a dangling ``Model's recommendation:`` suffix.
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "",
                    "priority": "high",
                }
            )
        )
        classification = {"classification": "stuck", "confidence": 0.9, "reasoning": ""}

        result = _run(decide_corrective_action(classification, {}, redirect_history=[]))

        assert result["action"] == "hitl"
        assert "Model's recommendation:" not in result["message"]
        # And no trailing colon at the end of the body.
        assert not result["message"].rstrip().endswith(":")

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_restart_allowed_after_prior_redirect(self, mock_agent: AsyncMock) -> None:
        # Once a nudge or redirect has already been sent, restart_agent
        # is permitted (the agent has had a chance to recover).
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "Restart after no response to redirect.",
                    "priority": "high",
                }
            )
        )
        classification = {"classification": "stuck", "confidence": 0.95, "reasoning": ""}
        history = [{"action": "redirect", "timestamp": 1000}]

        result = _run(decide_corrective_action(classification, {}, redirect_history=history))

        assert result["action"] == "restart_agent"

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_restart_allowed_after_prior_restart(self, mock_agent: AsyncMock) -> None:
        # Prior `restart_agent` history also bypasses the guard — the
        # "first occurrence" check spans every intervention type, so a
        # second restart isn't blocked once any corrective action has
        # already fired.
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "Second restart after first didn't help.",
                    "priority": "high",
                }
            )
        )
        classification = {"classification": "stuck", "confidence": 0.95, "reasoning": ""}
        history = [{"action": "restart_agent", "timestamp": 1000}]

        result = _run(decide_corrective_action(classification, {}, redirect_history=history))

        assert result["action"] == "restart_agent"

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_non_stall_classification_unchanged(self, mock_agent: AsyncMock) -> None:
        # The guard only triggers on stuck / needs_help. A "working"
        # classification (which does flow through the LLM path) leaves
        # the model's restart_agent recommendation alone.
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "restart_agent",
                    "message": "Agent crashed.",
                    "priority": "critical",
                }
            )
        )
        classification = {"classification": "working", "confidence": 0.7, "reasoning": ""}

        result = _run(decide_corrective_action(classification, {}, redirect_history=[]))

        assert result["action"] == "restart_agent"


# ===================================================================
# compose_redirect_message
# ===================================================================


class TestComposeRedirectMessage:
    """Test redirect message composition."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_compose_redirect_message(self, mock_agent: AsyncMock) -> None:
        expected_message = (
            "You are currently editing database files, but your assigned task "
            "is to fix the auth bug in auth/views.py. Please refocus on the "
            "auth module."
        )
        mock_agent.return_value = _make_result(expected_message)

        context = {
            "contract": {"task": "fix auth bug", "scope": "auth/"},
            "recent_files": ["db/models.py", "db/migrations/0001.py"],
        }

        result = _run(
            compose_redirect_message(
                agent_role="coder",
                issue="Agent editing database files instead of auth module",
                context=context,
            )
        )

        assert isinstance(result, str)
        assert len(result) > 0
        mock_agent.assert_awaited_once()

        # Verify it uses sonnet model
        call_kwargs = mock_agent.call_args
        assert call_kwargs.kwargs.get("model") == "sonnet"


# ===================================================================
# decide_escalation_level
# ===================================================================


class TestDecideEscalationLevel:
    """Test escalation level decisions."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_decide_escalation_level(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "escalate": True,
                    "level": "hitl",
                    "reasoning": "Two redirects have not resolved the issue.",
                }
            )
        )

        classification = {
            "classification": "stuck",
            "confidence": 0.9,
            "reasoning": "Still stuck after redirects",
        }
        redirect_history = [
            {"action": "redirect", "timestamp": 1000, "outcome": "no_change"},
            {"action": "redirect", "timestamp": 2000, "outcome": "no_change"},
        ]

        result = _run(decide_escalation_level(classification, redirect_history))

        assert result["escalate"] is True
        assert result["level"] == "hitl"
        assert "reasoning" in result
        mock_agent.assert_awaited_once()

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_decide_no_escalation(self, mock_agent: AsyncMock) -> None:
        """When redirects are working, no escalation needed."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "escalate": False,
                    "level": "redirect",
                    "reasoning": "Agent responded to last redirect.",
                }
            )
        )

        classification = {
            "classification": "working",
            "confidence": 0.8,
            "reasoning": "Agent resumed progress",
        }
        redirect_history = [
            {"action": "redirect", "timestamp": 1000, "outcome": "resolved"},
        ]

        result = _run(decide_escalation_level(classification, redirect_history))

        assert result["escalate"] is False
        assert result["level"] == "redirect"
