"""Tests for the overseer main monitoring loop (Phase 4).

Validates the OverseerMonitor poll cycle, escalation handling,
hallucination guard, and health summary generation.
"""

import asyncio
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
    return asyncio.get_event_loop().run_until_complete(coro)


class _MockConfig:
    """Minimal config for testing."""
    overseer_poll_interval_seconds = 1
    overseer_max_redirects_before_escalation = 2
    overseer_decision_maker_model = "sonnet"


class _MockClassifier:
    """Mock classifier that returns predetermined results."""

    def __init__(self) -> None:
        self.classify_stall = AsyncMock(return_value={
            "classification": "working",
            "confidence": 0.8,
            "reasoning": "Agent is making progress",
        })
        self.classify_error = AsyncMock(return_value={
            "error_type": "timeout",
            "severity": "medium",
            "recommended_action": "Retry",
        })
        self.detect_loop = AsyncMock(return_value={
            "is_loop": False,
            "loop_pattern": None,
            "confidence": 0.9,
        })
        self.check_alignment = AsyncMock(return_value={
            "aligned": True,
            "concerns": [],
            "suggested_redirect": None,
        })


class _MockDecisionMaker:
    """Mock decision maker that returns predetermined results."""

    def __init__(self) -> None:
        self.decide_corrective_action = AsyncMock(return_value={
            "action": "nudge",
            "message": "Please check your progress.",
            "priority": "low",
        })
        self.compose_redirect_message = AsyncMock(
            return_value="Please refocus on your assigned task."
        )
        self.decide_escalation_level = AsyncMock(return_value={
            "escalate": True,
            "level": "hitl",
            "reasoning": "Redirects exhausted.",
        })


# ===================================================================
# test_poll_cycle_no_anomalies
# ===================================================================


class TestPollCycleNoAnomalies:
    """Test that a poll cycle with no anomalies completes cleanly."""

    @patch("overseer.monitor.subprocess")
    def test_poll_cycle_no_anomalies(self, mock_subprocess) -> None:
        """When no alerts or escalations, poll cycle completes without actions."""
        # All subprocess calls return empty results
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"
        mock_subprocess.run.return_value = mock_result

        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-001",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        _run(monitor._poll_cycle())

        # Classifier should not have been called (no anomalies)
        classifier.classify_stall.assert_not_awaited()
        decision_maker.decide_corrective_action.assert_not_awaited()

        # Self-monitor should have recorded the cycle
        health = monitor.self_monitor.check_health()
        assert health["metrics"]["cycle_count"] == 1


# ===================================================================
# test_handle_escalation_routes_through_classifier
# ===================================================================


class TestHandleEscalationRoutesClassifier:
    """Test that escalations always go through the classifier first."""

    @patch("overseer.monitor.subprocess")
    def test_handle_escalation_routes_through_classifier(
        self, mock_subprocess
    ) -> None:
        """Escalation handling must call classifier before decision maker."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_subprocess.run.return_value = mock_result

        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-002",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        escalation = {
            "agent_role": "coder",
            "logs": [{"msg": "stuck on test"}],
            "progress": [],
            "reason": "heartbeat_timeout",
        }

        _run(monitor.handle_escalation(escalation))

        # Classifier MUST be called first
        classifier.classify_stall.assert_awaited_once()

        # Decision maker should be called after classification
        decision_maker.decide_corrective_action.assert_awaited_once()

        # Verify classifier was called with the escalation data
        call_args = classifier.classify_stall.call_args
        # May be passed as positional or keyword args
        logs_arg = call_args.kwargs.get("logs") or call_args.args[0]
        assert logs_arg == [{"msg": "stuck on test"}]


# ===================================================================
# test_hallucination_guard
# ===================================================================


class TestHallucinationGuard:
    """Verify Sonnet only acts on classifier output, never raw data."""

    @patch("overseer.monitor.subprocess")
    def test_hallucination_guard(self, mock_subprocess) -> None:
        """Decision maker receives classifier output, not raw escalation data."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_subprocess.run.return_value = mock_result

        classifier = _MockClassifier()
        classifier.classify_stall.return_value = {
            "classification": "stuck",
            "confidence": 0.95,
            "reasoning": "No tool calls for 15 minutes",
        }

        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-003",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        escalation = {
            "agent_role": "coder",
            "logs": [{"msg": "stuck"}],
            "progress": [],
        }

        _run(monitor.handle_escalation(escalation))

        # The decision maker must receive the CLASSIFIER output
        dm_call = decision_maker.decide_corrective_action.call_args
        classification_arg = dm_call.args[0] if dm_call.args else dm_call.kwargs.get("classification")

        assert classification_arg["classification"] == "stuck"
        assert classification_arg["confidence"] == 0.95
        assert classification_arg["reasoning"] == "No tool calls for 15 minutes"

    @patch("overseer.monitor.subprocess")
    def test_escalation_respects_redirect_limit(self, mock_subprocess) -> None:
        """After max redirects, monitor escalates instead of redirecting."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_subprocess.run.return_value = mock_result

        classifier = _MockClassifier()
        classifier.classify_stall.return_value = {
            "classification": "stuck",
            "confidence": 0.9,
            "reasoning": "Still stuck",
        }

        decision_maker = _MockDecisionMaker()
        decision_maker.decide_corrective_action.return_value = {
            "action": "redirect",
            "message": "Try again",
            "priority": "medium",
        }
        decision_maker.decide_escalation_level.return_value = {
            "escalate": True,
            "level": "hitl",
            "reasoning": "Redirects exhausted",
        }

        config = _MockConfig()
        config.overseer_max_redirects_before_escalation = 2

        monitor = OverseerMonitor(
            pipeline_id="test-004",
            config=config,
            classifier=classifier,
            decision_maker=decision_maker,
        )

        # Pre-populate redirect history
        monitor._escalation_history["coder"] = [
            {"action": "redirect", "timestamp": 1000},
            {"action": "redirect", "timestamp": 2000},
        ]

        escalation = {
            "agent_role": "coder",
            "logs": [],
            "progress": [],
        }

        _run(monitor.handle_escalation(escalation))

        # Should have called decide_escalation_level instead of decide_corrective_action
        decision_maker.decide_escalation_level.assert_awaited_once()


# ===================================================================
# test_generate_health_summary
# ===================================================================


class TestGenerateHealthSummary:
    """Test health summary generation."""

    def test_generate_health_summary(self) -> None:
        """Health summary should include pipeline info and metrics."""
        monitor = OverseerMonitor(
            pipeline_id="test-005",
            config=_MockConfig(),
        )

        # Simulate some activity
        monitor.self_monitor.record_poll_cycle(5.0)
        monitor.self_monitor.record_poll_cycle(3.0)
        monitor.self_monitor.record_message_sent()
        monitor.self_monitor.record_llm_call("haiku", 100, 0.001)

        # Simulate escalation history
        monitor._escalation_history["coder"] = [
            {"action": "nudge", "timestamp": 1000},
            {"action": "redirect", "timestamp": 2000},
        ]

        summary = monitor.generate_health_summary()

        assert "## Pipeline Health Summary" in summary
        assert "`test-005`" in summary
        assert "Monitor cycles" in summary
        assert "coder" in summary
        assert "nudge" in summary
        assert "redirect" in summary
        assert "Avg poll duration" in summary

    def test_generate_health_summary_no_escalations(self) -> None:
        """Summary without escalations should indicate that."""
        monitor = OverseerMonitor(
            pipeline_id="test-006",
            config=_MockConfig(),
        )

        summary = monitor.generate_health_summary()

        assert "No escalations" in summary
