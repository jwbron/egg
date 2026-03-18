"""Tests for the overseer main monitoring loop (Phase 4).

Validates the OverseerMonitor poll cycle, escalation handling,
hallucination guard, and health summary generation.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    return asyncio.run(coro)


class _MockConfig:
    """Minimal config for testing."""

    overseer_poll_interval_seconds = 1
    overseer_max_redirects_before_escalation = 2
    overseer_decision_maker_model = "sonnet"


class _MockClassifier:
    """Mock classifier that returns predetermined results."""

    def __init__(self) -> None:
        self.classify_stall = AsyncMock(
            return_value={
                "classification": "working",
                "confidence": 0.8,
                "reasoning": "Agent is making progress",
            }
        )
        self.classify_error = AsyncMock(
            return_value={
                "error_type": "timeout",
                "severity": "medium",
                "recommended_action": "Retry",
            }
        )
        self.detect_loop = AsyncMock(
            return_value={
                "is_loop": False,
                "loop_pattern": None,
                "confidence": 0.9,
            }
        )
        self.check_alignment = AsyncMock(
            return_value={
                "aligned": True,
                "concerns": [],
                "suggested_redirect": None,
            }
        )


class _MockDecisionMaker:
    """Mock decision maker that returns predetermined results."""

    def __init__(self) -> None:
        self.decide_corrective_action = AsyncMock(
            return_value={
                "action": "nudge",
                "message": "Please check your progress.",
                "priority": "low",
            }
        )
        self.compose_redirect_message = AsyncMock(
            return_value="Please refocus on your assigned task."
        )
        self.decide_escalation_level = AsyncMock(
            return_value={
                "escalate": True,
                "level": "hitl",
                "reasoning": "Redirects exhausted.",
            }
        )


def _mock_run_cli_empty():
    """Create an AsyncMock for _run_cli that returns empty results."""
    return AsyncMock(return_value=(0, "[]", ""))


# ===================================================================
# test_poll_cycle_no_anomalies
# ===================================================================


class TestPollCycleNoAnomalies:
    """Test that a poll cycle with no anomalies completes cleanly."""

    def test_poll_cycle_no_anomalies(self) -> None:
        """When no alerts or escalations, poll cycle completes without actions."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-001",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )
        monitor._run_cli = AsyncMock(return_value=(0, "[]", ""))

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

    def test_handle_escalation_routes_through_classifier(self) -> None:
        """Escalation handling must call classifier before decision maker."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-002",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

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

    def test_hallucination_guard(self) -> None:
        """Decision maker receives classifier output, not raw escalation data."""
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
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

        escalation = {
            "agent_role": "coder",
            "logs": [{"msg": "stuck"}],
            "progress": [],
        }

        _run(monitor.handle_escalation(escalation))

        # The decision maker must receive the CLASSIFIER output
        dm_call = decision_maker.decide_corrective_action.call_args
        classification_arg = (
            dm_call.args[0] if dm_call.args else dm_call.kwargs.get("classification")
        )

        assert classification_arg["classification"] == "stuck"
        assert classification_arg["confidence"] == 0.95
        assert classification_arg["reasoning"] == "No tool calls for 15 minutes"

    def test_escalation_respects_redirect_limit(self) -> None:
        """After max redirects, monitor escalates instead of redirecting."""
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
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

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


# ===================================================================
# test_consensus_override
# ===================================================================


class TestConsensusOverride:
    """Test that consensus state is passed to classifier instead of relying on stale data."""

    def test_classifier_receives_consensus_data(self) -> None:
        """When consensus is available, classifier receives it alongside progress."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-consensus-001",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        # Mock CLI responses
        consensus_data = {
            "is_complete": True,
            "blocking_agents": [],
            "agents": {"coder": {"confirmed": True}, "tester": {"confirmed": True}},
        }
        phase_data = {"name": "implement", "status": "active"}
        pipeline_data = {
            "status": "running",
            "concurrent": {"agents": [{"role": "coder"}, {"role": "tester"}]},
        }
        alert = {
            "agent_role": "coder",
            "agent_id": "coder",
            "logs": [{"msg": "no recent activity"}],
        }

        async def mock_run_cli(*args, **kwargs):
            cmd = " ".join(args)
            if "consensus status" in cmd:
                return (0, json.dumps(consensus_data), "")
            if "phase get" in cmd:
                return (0, json.dumps(phase_data), "")
            if "health alerts" in cmd:
                return (0, json.dumps([alert]), "")
            if "pipeline status" in cmd:
                return (0, json.dumps(pipeline_data), "")
            if "message poll" in cmd:
                return (0, "[]", "")
            if "progress query" in cmd:
                return (0, "[]", "")
            return (0, "[]", "")

        monitor._run_cli = AsyncMock(side_effect=mock_run_cli)

        _run(monitor._poll_cycle())

        # Classifier should have been called with consensus data
        classifier.classify_stall.assert_awaited_once()
        call_kwargs = classifier.classify_stall.call_args.kwargs
        assert call_kwargs.get("consensus") == consensus_data


# ===================================================================
# test_phase_scoping
# ===================================================================


class TestPhaseScoping:
    """Test that alerts for agents in completed phases are filtered out."""

    def test_filters_completed_phase_agents(self) -> None:
        """Only alerts for agents in the current phase should be processed."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-phase-001",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        phase_data = {"name": "test", "status": "active"}
        pipeline_data = {
            "status": "running",
            "concurrent": {"agents": [{"role": "tester"}]},
        }
        alerts = [
            {"agent_role": "coder", "agent_id": "coder", "logs": []},  # completed phase
            {"agent_role": "tester", "agent_id": "tester", "logs": []},  # current phase
        ]

        async def mock_run_cli(*args, **kwargs):
            cmd = " ".join(args)
            if "consensus status" in cmd:
                return (0, "{}", "")
            if "phase get" in cmd:
                return (0, json.dumps(phase_data), "")
            if "health alerts" in cmd:
                return (0, json.dumps(alerts), "")
            if "pipeline status" in cmd:
                return (0, json.dumps(pipeline_data), "")
            if "message poll" in cmd:
                return (0, "[]", "")
            if "progress query" in cmd:
                return (0, "[]", "")
            return (0, "[]", "")

        monitor._run_cli = AsyncMock(side_effect=mock_run_cli)

        _run(monitor._poll_cycle())

        # Classifier should only be called once (for tester, not coder)
        assert classifier.classify_stall.await_count == 1
        call_args = classifier.classify_stall.call_args
        # The alert processed should be for the tester
        logs_arg = call_args.kwargs.get("logs") or call_args.args[0]
        assert logs_arg == []  # tester's logs

    def test_filters_with_agent_id_only(self) -> None:
        """Alerts with only agent_id (production format) use the fallback correctly."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-phase-003",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        phase_data = {"name": "test", "status": "active"}
        pipeline_data = {
            "status": "running",
            "concurrent": {"agents": [{"role": "tester"}]},
        }
        # Production-realistic alerts: only agent_id, no agent_role
        alerts = [
            {"agent_id": "coder", "logs": []},
            {"agent_id": "tester", "logs": []},
        ]

        async def mock_run_cli(*args, **kwargs):
            cmd = " ".join(args)
            if "consensus status" in cmd:
                return (0, "{}", "")
            if "phase get" in cmd:
                return (0, json.dumps(phase_data), "")
            if "health alerts" in cmd:
                return (0, json.dumps(alerts), "")
            if "pipeline status" in cmd:
                return (0, json.dumps(pipeline_data), "")
            if "message poll" in cmd:
                return (0, "[]", "")
            if "progress query" in cmd:
                return (0, "[]", "")
            return (0, "[]", "")

        monitor._run_cli = AsyncMock(side_effect=mock_run_cli)

        _run(monitor._poll_cycle())

        # Only tester alert should be processed (coder filtered out via agent_id fallback)
        assert classifier.classify_stall.await_count == 1

    def test_no_filter_when_no_agent_list(self) -> None:
        """When pipeline status has no agent list, all alerts are processed."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        monitor = OverseerMonitor(
            pipeline_id="test-phase-002",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )

        phase_data = {"name": "implement", "status": "active"}
        pipeline_data = {"status": "running"}  # No concurrent.agents
        alerts = [
            {"agent_role": "coder", "agent_id": "coder", "logs": []},
            {"agent_role": "tester", "agent_id": "tester", "logs": []},
        ]

        async def mock_run_cli(*args, **kwargs):
            cmd = " ".join(args)
            if "consensus status" in cmd:
                return (0, "{}", "")
            if "phase get" in cmd:
                return (0, json.dumps(phase_data), "")
            if "health alerts" in cmd:
                return (0, json.dumps(alerts), "")
            if "pipeline status" in cmd:
                return (0, json.dumps(pipeline_data), "")
            if "message poll" in cmd:
                return (0, "[]", "")
            if "progress query" in cmd:
                return (0, "[]", "")
            return (0, "[]", "")

        monitor._run_cli = AsyncMock(side_effect=mock_run_cli)

        _run(monitor._poll_cycle())

        # Both alerts should be processed
        assert classifier.classify_stall.await_count == 2


# ===================================================================
# test_post_consensus_stall
# ===================================================================


class TestPostConsensusStall:
    """Test detection of post-consensus stalls."""

    def test_detects_post_consensus_stall_after_grace_period(self) -> None:
        """After grace period, consensus stall creates HITL and sends Slack."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-001",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        consensus = {"is_complete": True}

        # First call: records first_seen, does not escalate
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()

        # Simulate grace period elapsed (backdate first_seen)
        monitor._post_consensus_stall_first_seen = time.time() - 999

        # Second call after grace period: should escalate
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()

    def test_does_not_fire_repeatedly(self) -> None:
        """After escalating once, subsequent calls should not fire again."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-dedup",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        consensus = {"is_complete": True}

        # Backdate first_seen so grace period is already elapsed
        monitor._post_consensus_stall_first_seen = time.time() - 999

        # First call: escalates
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        assert monitor._create_hitl_decision.await_count == 1
        assert monitor._send_slack_notification.await_count == 1

        # Second call: should NOT escalate again
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        assert monitor._create_hitl_decision.await_count == 1
        assert monitor._send_slack_notification.await_count == 1

    def test_resets_when_consensus_changes(self) -> None:
        """Flag resets when consensus becomes incomplete (e.g. new phase)."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-reset",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        # Mark as already reported
        monitor._post_consensus_stall_reported = True
        monitor._post_consensus_stall_first_seen = time.time() - 999

        # Consensus becomes incomplete — should reset state
        _run(monitor._check_post_consensus_stall({"is_complete": False}, "running"))
        assert monitor._post_consensus_stall_reported is False
        assert monitor._post_consensus_stall_first_seen is None

    def test_no_stall_when_pipeline_transitioning(self) -> None:
        """No stall detected when pipeline is not in 'running' status."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-002",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        consensus = {"is_complete": True}

        _run(monitor._check_post_consensus_stall(consensus, "complete"))

        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()

    def test_no_stall_when_consensus_incomplete(self) -> None:
        """No stall detected when consensus is not complete."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-003",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        consensus = {"is_complete": False}

        _run(monitor._check_post_consensus_stall(consensus, "running"))

        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()


# ===================================================================
# test_escalation_safety_net
# ===================================================================


class TestEscalationSafetyNet:
    """Test that nudge/redirect actions are upgraded when message indicates human intervention."""

    def test_upgrades_nudge_to_hitl(self) -> None:
        """Nudge with 'human intervention' in message should be upgraded to hitl."""
        monitor = OverseerMonitor(
            pipeline_id="test-safetynet-001",
            config=_MockConfig(),
        )
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

        decision = {
            "action": "nudge",
            "message": "Agent is stuck. Human intervention required to resolve.",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        # Should have been upgraded to hitl
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_message.assert_not_awaited()

    def test_upgrades_redirect_to_hitl(self) -> None:
        """Redirect with 'manual intervention' in message should be upgraded to hitl."""
        monitor = OverseerMonitor(
            pipeline_id="test-safetynet-002",
            config=_MockConfig(),
        )
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

        decision = {
            "action": "redirect",
            "message": "Multiple failures detected. Manual intervention needed.",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_message.assert_not_awaited()

    def test_upgrades_on_alternative_phrasings(self) -> None:
        """Safety net catches varied LLM phrasings like 'requires human attention'."""
        monitor = OverseerMonitor(
            pipeline_id="test-safetynet-004",
            config=_MockConfig(),
        )
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

        decision = {
            "action": "nudge",
            "message": "This issue requires human attention to resolve.",
            "priority": "high",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_message.assert_not_awaited()

    def test_no_upgrade_without_intervention_keywords(self) -> None:
        """Nudge without intervention keywords should not be upgraded."""
        monitor = OverseerMonitor(
            pipeline_id="test-safetynet-003",
            config=_MockConfig(),
        )
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._run_cli = AsyncMock(return_value=(0, "{}", ""))

        decision = {
            "action": "nudge",
            "message": "Please check your progress.",
            "priority": "low",
        }

        _run(monitor._execute_action(decision, "coder"))

        monitor._send_message.assert_awaited_once()
        monitor._create_hitl_decision.assert_not_awaited()


# ===================================================================
# test_respawn_scenario
# ===================================================================


class TestRespawnScenario:
    """Test that a freshly respawned monitor handles existing consensus correctly."""

    def test_respawn_with_consensus_complete(self) -> None:
        """A fresh monitor encountering already-complete consensus detects the stall after grace period."""
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()

        # Fresh monitor (simulates respawn - no prior state)
        monitor = OverseerMonitor(
            pipeline_id="test-respawn-001",
            config=_MockConfig(),
            classifier=classifier,
            decision_maker=decision_maker,
        )
        assert len(monitor._escalation_history) == 0  # fresh state
        assert monitor._post_consensus_stall_reported is False

        consensus_data = {
            "is_complete": True,
            "blocking_agents": [],
            "agents": {"coder": {"confirmed": True}, "tester": {"confirmed": True}},
        }

        async def mock_run_cli(*args, **kwargs):
            cmd = " ".join(args)
            if "consensus status" in cmd:
                return (0, json.dumps(consensus_data), "")
            if "phase get" in cmd:
                return (0, '{"name": "implement", "status": "active"}', "")
            if "health alerts" in cmd:
                return (0, "[]", "")
            if "pipeline status" in cmd:
                return (0, '{"status": "running"}', "")
            if "message poll" in cmd:
                return (0, "[]", "")
            if "progress query" in cmd:
                return (0, "[]", "")
            if "decision create" in cmd:
                return (0, "{}", "")
            return (0, "[]", "")

        monitor._run_cli = AsyncMock(side_effect=mock_run_cli)

        # First poll: starts grace period, should NOT escalate yet
        _run(monitor._poll_cycle())
        assert monitor._post_consensus_stall_first_seen is not None
        assert monitor._post_consensus_stall_reported is False

        # Simulate grace period elapsed
        monitor._post_consensus_stall_first_seen = time.time() - 999

        # Second poll after grace period: should escalate
        _run(monitor._poll_cycle())
        assert monitor._post_consensus_stall_reported is True

        calls = [
            " ".join(c.args[0] if isinstance(c.args[0], tuple) else c.args)
            for c in monitor._run_cli.call_args_list
        ]
        decision_calls = [c for c in calls if "decision create" in c]
        assert len(decision_calls) >= 1, (
            "Respawned monitor should detect post-consensus stall after grace period"
        )
