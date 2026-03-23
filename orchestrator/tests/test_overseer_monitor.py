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


# ===================================================================
# test_rerun_anomaly
# ===================================================================


class TestRerunAnomaly:
    """Test detection of suspiciously fast re-runs after request_changes."""

    def test_detects_fast_rerun(self) -> None:
        """Flags when agent completes in < min_work_seconds with content_changed=False."""
        config = _MockConfig()
        config.overseer_rerun_min_work_seconds = 60

        monitor = OverseerMonitor(
            pipeline_id="test-rerun-001",
            config=config,
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        decisions = [
            {
                "id": "d-1",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "status": "resolved",
                "resolved_at": "2026-03-18T10:00:00",
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {
                        "cycle": 1,
                        "started_at": "2026-03-18T10:00:05",
                        "completed_at": "2026-03-18T10:00:15",
                        "commit_sha": None,
                    }
                ]
            }
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))

        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()
        assert "d-1" in monitor._rerun_anomaly_reported

    def test_skips_when_content_changed(self) -> None:
        """No alert when content_changed is True."""
        monitor = OverseerMonitor(pipeline_id="test-rerun-002", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        decisions = [
            {
                "id": "d-2",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": True,
                "status": "resolved",
                "resolved_at": "2026-03-18T10:00:00",
            }
        ]
        phase_data = {"phase_execution": {"cycle_timings": []}}

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._create_hitl_decision.assert_not_awaited()

    def test_skips_when_work_duration_sufficient(self) -> None:
        """No alert when work took longer than min_work_seconds."""
        config = _MockConfig()
        config.overseer_rerun_min_work_seconds = 60

        monitor = OverseerMonitor(pipeline_id="test-rerun-003", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        decisions = [
            {
                "id": "d-3",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "status": "resolved",
                "resolved_at": "2026-03-18T10:00:00",
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {
                        "cycle": 1,
                        "started_at": "2026-03-18T10:00:05",
                        "completed_at": "2026-03-18T10:05:00",
                        "commit_sha": None,
                    }
                ]
            }
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._create_hitl_decision.assert_not_awaited()

    def test_deduplicates(self) -> None:
        """Same decision ID is not flagged twice."""
        config = _MockConfig()
        config.overseer_rerun_min_work_seconds = 60

        monitor = OverseerMonitor(pipeline_id="test-rerun-004", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._rerun_anomaly_reported.add("d-4")

        decisions = [
            {
                "id": "d-4",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "status": "resolved",
                "resolved_at": "2026-03-18T10:00:00",
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {
                        "cycle": 1,
                        "started_at": "2026-03-18T10:00:05",
                        "completed_at": "2026-03-18T10:00:15",
                        "commit_sha": None,
                    }
                ]
            }
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._create_hitl_decision.assert_not_awaited()


# ===================================================================
# test_status_consistency
# ===================================================================


class TestStatusConsistency:
    """Test detection of pipeline failed with all agents complete."""

    def test_detects_inconsistency_after_grace(self) -> None:
        """Flags when pipeline is failed but all agents are complete, after grace period."""
        monitor = OverseerMonitor(pipeline_id="test-status-001", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "failed",
            "concurrent": {
                "agents": [
                    {"role": "coder", "status": "complete"},
                    {"role": "tester", "status": "complete"},
                ]
            },
        }

        # First call: starts grace period
        _run(monitor._check_status_consistency(pipeline_data))
        monitor._create_hitl_decision.assert_not_awaited()
        assert monitor._status_inconsistency_first_seen is not None

        # Backdate first_seen past grace period
        monitor._status_inconsistency_first_seen = time.time() - 999

        # Second call: should flag
        _run(monitor._check_status_consistency(pipeline_data))
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()
        assert monitor._status_inconsistency_reported is True

    def test_resets_when_not_failed(self) -> None:
        """Tracking resets when pipeline is no longer in failed state."""
        monitor = OverseerMonitor(pipeline_id="test-status-002", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._status_inconsistency_first_seen = time.time() - 999
        monitor._status_inconsistency_reported = True

        _run(monitor._check_status_consistency({"status": "running"}))
        assert monitor._status_inconsistency_first_seen is None
        assert monitor._status_inconsistency_reported is False
        monitor._create_hitl_decision.assert_not_awaited()

    def test_no_flag_when_agent_not_complete(self) -> None:
        """No flag when at least one agent is not complete."""
        monitor = OverseerMonitor(pipeline_id="test-status-003", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "failed",
            "concurrent": {
                "agents": [
                    {"role": "coder", "status": "complete"},
                    {"role": "tester", "status": "failed"},
                ]
            },
        }

        _run(monitor._check_status_consistency(pipeline_data))
        monitor._create_hitl_decision.assert_not_awaited()

    def test_deduplicates(self) -> None:
        """Does not fire twice."""
        monitor = OverseerMonitor(pipeline_id="test-status-004", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._status_inconsistency_reported = True
        monitor._status_inconsistency_first_seen = time.time() - 999

        pipeline_data = {
            "status": "failed",
            "concurrent": {"agents": [{"role": "coder", "status": "complete"}]},
        }

        _run(monitor._check_status_consistency(pipeline_data))
        monitor._create_hitl_decision.assert_not_awaited()


# ===================================================================
# test_hitl_resolution_propagation
# ===================================================================


class TestHitlResolutionPropagation:
    """Test detection of resolved decisions not propagated to the contract."""

    def test_detects_missing_propagation(self) -> None:
        """Flags when resolved decision is not in contract after timeout."""
        config = _MockConfig()
        config.overseer_hitl_propagation_timeout_seconds = 10

        monitor = OverseerMonitor(pipeline_id="test-hitl-prop-001", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(return_value={"decisions": []})

        decisions = [
            {
                "id": "d-10",
                "decision_type": "phase_gate",
                "status": "resolved",
                "resolution": "approve",
            }
        ]

        # First call: starts timer
        _run(monitor._check_hitl_resolution_propagation(decisions))
        assert "d-10" in monitor._hitl_resolution_pending
        monitor._create_hitl_decision.assert_not_awaited()

        # Backdate past timeout
        monitor._hitl_resolution_pending["d-10"] = time.time() - 999

        # Second call: should flag
        _run(monitor._check_hitl_resolution_propagation(decisions))
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()
        assert "d-10" in monitor._hitl_resolution_alerted

    def test_no_flag_when_propagated(self) -> None:
        """No flag when contract has the resolved decision."""
        config = _MockConfig()
        config.overseer_hitl_propagation_timeout_seconds = 10

        monitor = OverseerMonitor(pipeline_id="test-hitl-prop-002", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(
            return_value={"decisions": [{"id": "d-11", "status": "resolved"}]}
        )

        decisions = [
            {
                "id": "d-11",
                "decision_type": "phase_gate",
                "status": "resolved",
                "resolution": "approve",
            }
        ]

        # Start timer and backdate
        monitor._hitl_resolution_pending["d-11"] = time.time() - 999

        _run(monitor._check_hitl_resolution_propagation(decisions))
        monitor._create_hitl_decision.assert_not_awaited()
        assert "d-11" in monitor._hitl_resolution_verified

    def test_skips_already_verified(self) -> None:
        """Skips decisions that have already been verified."""
        monitor = OverseerMonitor(pipeline_id="test-hitl-prop-003", config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._query_contract_data = AsyncMock()
        monitor._hitl_resolution_verified.add("d-12")

        decisions = [{"id": "d-12", "decision_type": "phase_gate", "status": "resolved"}]

        _run(monitor._check_hitl_resolution_propagation(decisions))
        monitor._query_contract_data.assert_not_awaited()


# ===================================================================
# test_cross_phase_consistency
# ===================================================================


class TestCrossPhaseConsistency:
    """Test LLM-based cross-phase decision consistency check."""

    def test_triggers_on_phase_transition(self) -> None:
        """Fires the classifier when a phase transition is detected."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock(
            return_value={
                "consistent": False,
                "concerns": ["Ignored prior feedback"],
                "confidence": 0.9,
            }
        )

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-001",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(return_value={"tasks": [{"id": "t1"}]})

        # Set initial phase
        monitor._last_phase_name = "plan"

        decisions = [{"id": "d-20", "status": "resolved", "phase": "plan", "resolution": "approve"}]
        phase_data = {"current_phase": "implement"}

        _run(monitor._check_cross_phase_consistency(phase_data, decisions))

        classifier.check_decision_consistency.assert_awaited_once()
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()

    def test_no_trigger_without_phase_change(self) -> None:
        """Does not fire when the phase has not changed."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock()

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-002",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._last_phase_name = "implement"

        _run(
            monitor._check_cross_phase_consistency(
                {"current_phase": "implement"}, [], contract_data=None
            )
        )

        classifier.check_decision_consistency.assert_not_awaited()

    def test_no_trigger_when_consistent(self) -> None:
        """No escalation when classifier says output is consistent."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock(
            return_value={"consistent": True, "concerns": [], "confidence": 0.95}
        )

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-003",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(return_value={"tasks": []})
        monitor._last_phase_name = "plan"

        decisions = [{"id": "d-21", "status": "resolved", "phase": "plan", "resolution": "approve"}]

        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))

        classifier.check_decision_consistency.assert_awaited_once()
        monitor._create_hitl_decision.assert_not_awaited()

    def test_no_trigger_low_confidence(self) -> None:
        """No escalation when confidence is below threshold."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock(
            return_value={"consistent": False, "concerns": ["Maybe"], "confidence": 0.5}
        )

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-004",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(return_value={"tasks": []})
        monitor._last_phase_name = "plan"

        decisions = [{"id": "d-22", "status": "resolved", "phase": "plan", "resolution": "approve"}]

        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))

        monitor._create_hitl_decision.assert_not_awaited()

    def test_skips_when_no_prior_decisions(self) -> None:
        """Does not call classifier when there are no resolved prior-phase decisions."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock()

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-005",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._last_phase_name = "plan"

        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, []))

        classifier.check_decision_consistency.assert_not_awaited()

    def test_deduplicates_same_transition(self) -> None:
        """Does not re-check the same phase transition pair."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock(
            return_value={"consistent": False, "concerns": ["Issue"], "confidence": 0.9}
        )

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-006",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._query_contract_data = AsyncMock(return_value={"tasks": [{"id": "t1"}]})
        monitor._last_phase_name = "plan"

        decisions = [{"id": "d-30", "status": "resolved", "phase": "plan", "resolution": "approve"}]

        # First transition: plan -> implement
        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))
        assert classifier.check_decision_consistency.await_count == 1

        # Simulate oscillation back to plan, then to implement again
        monitor._last_phase_name = "plan"
        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))

        # Should not fire again for the same pair
        assert classifier.check_decision_consistency.await_count == 1

    def test_returns_early_when_contract_empty(self) -> None:
        """Returns without calling classifier when contract query returns empty dict."""
        classifier = _MockClassifier()
        classifier.check_decision_consistency = AsyncMock()

        monitor = OverseerMonitor(
            pipeline_id="test-crossphase-007",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._query_contract_data = AsyncMock(return_value={})
        monitor._last_phase_name = "plan"

        decisions = [{"id": "d-31", "status": "resolved", "phase": "plan", "resolution": "approve"}]

        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))

        classifier.check_decision_consistency.assert_not_awaited()


# ===================================================================
# test_rerun_anomaly_missing_resolved_at
# ===================================================================


class TestRerunAnomalyMissingResolvedAt:
    """Test _check_rerun_anomaly with missing or None resolved_at."""

    def test_skips_missing_resolved_at(self) -> None:
        """Decision with resolved_at=None is silently skipped."""
        config = _MockConfig()
        config.overseer_rerun_min_work_seconds = 60

        monitor = OverseerMonitor(pipeline_id="test-rerun-missing-001", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        decisions = [
            {
                "id": "d-missing-1",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "status": "resolved",
                "resolved_at": None,
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {
                        "cycle": 1,
                        "started_at": "2026-03-18T10:00:05",
                        "completed_at": "2026-03-18T10:00:15",
                    }
                ]
            }
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._create_hitl_decision.assert_not_awaited()

    def test_skips_absent_resolved_at_key(self) -> None:
        """Decision without resolved_at key is silently skipped."""
        config = _MockConfig()
        config.overseer_rerun_min_work_seconds = 60

        monitor = OverseerMonitor(pipeline_id="test-rerun-missing-002", config=config)
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        decisions = [
            {
                "id": "d-missing-2",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "status": "resolved",
                # no resolved_at key at all
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {
                        "cycle": 1,
                        "started_at": "2026-03-18T10:00:05",
                        "completed_at": "2026-03-18T10:00:15",
                    }
                ]
            }
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._create_hitl_decision.assert_not_awaited()


class TestPrPhaseOutcomeCheck:
    """Tests for _check_pr_phase_outcome — detects pipeline completing without a PR."""

    def test_alerts_when_pr_phase_has_no_pr_url(self) -> None:
        """Should alert when pipeline completes with PR phase but no pr_url."""
        monitor = OverseerMonitor(
            pipeline_id="test-pr-outcome-001",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "complete",
            "current_phase": "pr",
            "phases": {
                "pr": {
                    "status": "complete",
                    "artifacts": {},
                }
            },
        }

        _run(monitor._check_pr_phase_outcome(pipeline_data))
        monitor._create_hitl_decision.assert_awaited_once()
        call_msg = monitor._create_hitl_decision.call_args[0][1]
        assert "no pr_url in phase artifacts" in call_msg
        monitor._send_slack_notification.assert_awaited_once()

    def test_no_alert_when_pr_url_present(self) -> None:
        """Should not alert when PR phase has a valid pr_url."""
        monitor = OverseerMonitor(
            pipeline_id="test-pr-outcome-002",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "complete",
            "current_phase": "pr",
            "phases": {
                "pr": {
                    "status": "complete",
                    "artifacts": {"pr_url": "https://github.com/owner/repo/pull/1"},
                }
            },
        }

        _run(monitor._check_pr_phase_outcome(pipeline_data))
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()

    def test_no_alert_when_not_pr_phase(self) -> None:
        """Should not alert when pipeline completed in a non-PR phase."""
        monitor = OverseerMonitor(
            pipeline_id="test-pr-outcome-003",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "complete",
            "current_phase": "implement",
            "phases": {},
        }

        _run(monitor._check_pr_phase_outcome(pipeline_data))
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()

    def test_alerts_when_artifacts_is_none(self) -> None:
        """Should alert when PR phase artifacts is None."""
        monitor = OverseerMonitor(
            pipeline_id="test-pr-outcome-004",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()

        pipeline_data = {
            "status": "complete",
            "current_phase": "pr",
            "phases": {
                "pr": {
                    "status": "complete",
                    "artifacts": None,
                }
            },
        }

        _run(monitor._check_pr_phase_outcome(pipeline_data))
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()
