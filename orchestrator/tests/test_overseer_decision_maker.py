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
    return asyncio.get_event_loop().run_until_complete(coro)


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
