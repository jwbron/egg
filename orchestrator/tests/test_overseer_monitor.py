"""Tests for the overseer main monitoring loop (Phase 4).

Validates the OverseerMonitor poll cycle, escalation handling,
hallucination guard, and health summary generation.
"""

import asyncio
import json
import os
import sys
import time
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scope_egg_repo_path(monkeypatch, tmp_path):
    """Scope EGG_REPO_PATH to tmp_path for every test in this file.

    OverseerMonitor.__init__ resolves _oversight_dir from EGG_REPO_PATH and
    writes JSONL records to {EGG_REPO_PATH}/.egg-state/oversight/{pipeline_id}-oversight.jsonl
    via _log_oversight_event. Without this scoping the writes land in the
    real repo, dirtying tracked content and blocking `git rebase` (#2244).
    """
    monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))


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

    @pytest.fixture(autouse=True)
    def _insulate_from_state_store(self):
        """Prevent the transition-completion short-circuit from reaching a
        real state store when EGG_REPO_PATH happens to be set in the test
        environment.  Returning None makes the detector fall through to the
        grace-period logic that these tests exercise."""
        with patch.object(
            OverseerMonitor,
            "_load_pipeline_for_transition_check",
            return_value=None,
        ):
            yield

    def test_detects_post_consensus_stall_after_grace_period(self) -> None:
        """After grace period, consensus stall creates HITL and sends Slack."""
        monitor = OverseerMonitor(
            pipeline_id="test-postconsensus-001",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

        consensus = {"is_complete": False}

        _run(monitor._check_post_consensus_stall(consensus, "running"))

        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()


# ===================================================================
# test_post_consensus_stall_transition_completion_shortcircuit (#1911)
# ===================================================================


class TestPostConsensusStallTransitionCompletionShortcircuit:
    """Regression tests for jwbron/egg#1911 task-1-2.

    When consensus is complete and the pipeline is still ``running``, the
    post-consensus stall detector used to fire after its grace period even
    when the implement phase had already transitioned into PR-creation.
    The symptom: ``post_consensus_stall`` alerts / HITL / Slack firing
    during the normal implement→PR transition window.

    The short-circuit added in #1911 loads the pipeline inside the detector
    and returns early — with no alert, no HITL, no Slack — whenever any of
    the three "transition is done" signals is set:

      (a) ``pipeline.current_phase.value != 'implement'`` — already moved on
      (b) ``pipeline.pr_number is not None`` — auto-PR finalized
      (c) ``pipeline.phases.get('pr').artifacts['pr_url']`` populated

    It also resets ``_post_consensus_stall_first_seen`` in the short-circuit
    so a later genuine stall gets a fresh grace period. If the pipeline
    load raises, the detector falls through to the existing grace-period
    logic (fail open) — we never want a state-store hiccup to suppress a
    real stall alert indefinitely.
    """

    @staticmethod
    def _pipeline(
        *,
        current_phase="implement",
        pr_number=None,
        pr_artifact=None,
    ):
        """Build a MagicMock pipeline matching the attribute accesses in
        ``_check_post_consensus_stall``."""
        phases: dict = {}
        if pr_artifact is not None:
            pr_phase = MagicMock()
            pr_phase.artifacts = {"pr_url": pr_artifact}
            phases["pr"] = pr_phase
        pipeline = MagicMock()
        pipeline.current_phase = MagicMock(value=current_phase)
        pipeline.pr_number = pr_number
        pipeline.phases = phases
        return pipeline

    def _monitor_with_store(self, pipeline, pipeline_id: str) -> tuple[OverseerMonitor, MagicMock]:
        """Build a monitor with HITL/broadcast/Slack stubbed and a store
        patched in that returns ``pipeline`` from load_pipeline."""
        monitor = OverseerMonitor(pipeline_id=pipeline_id, config=_MockConfig())
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        # Pre-age first_seen so the grace period would normally have
        # elapsed — the short-circuit must fire *before* the grace check.
        monitor._post_consensus_stall_first_seen = time.time() - 999
        return monitor, store

    def _invoke(self, monitor, store):
        """Invoke the detector with consensus complete + running status,
        patching the state store resolution to return our fake store."""
        consensus = {"is_complete": True}
        with (
            patch("overseer.monitor._get_state_store", return_value=store),
            patch.dict(os.environ, {"EGG_REPO_PATH": "/fake/repo"}),
        ):
            _run(monitor._check_post_consensus_stall(consensus, "running"))

    def test_shortcircuits_when_phase_already_advanced(self) -> None:
        """current_phase != 'implement' — detector must NOT broadcast / HITL
        / Slack.  The phase has already finished transitioning out of
        implement so any "consensus complete but still running" signal is
        stale."""
        pipeline = self._pipeline(current_phase="pr")
        monitor, store = self._monitor_with_store(pipeline, "test-1911-phase-advanced")

        self._invoke(monitor, store)

        monitor._broadcast_alert.assert_not_awaited()
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()
        # Short-circuit reset first_seen — subsequent genuine stalls get a
        # fresh grace period.
        assert monitor._post_consensus_stall_first_seen is None

    def test_shortcircuits_when_pr_number_populated(self) -> None:
        """pipeline.pr_number is not None — auto-PR finalized, the
        implement→PR transition is done.  No alert."""
        pipeline = self._pipeline(pr_number=99)
        monitor, store = self._monitor_with_store(pipeline, "test-1911-pr-number")

        self._invoke(monitor, store)

        monitor._broadcast_alert.assert_not_awaited()
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()
        assert monitor._post_consensus_stall_first_seen is None

    def test_shortcircuits_when_pr_url_artifact_present(self) -> None:
        """phases['pr'].artifacts['pr_url'] is set — the artifact write
        that happens inside _finalize_pr_phase_failed's lock has landed,
        so the transition is done.  No alert."""
        pipeline = self._pipeline(pr_artifact="https://github.com/owner/repo/pull/99")
        monitor, store = self._monitor_with_store(pipeline, "test-1911-pr-url")

        self._invoke(monitor, store)

        monitor._broadcast_alert.assert_not_awaited()
        monitor._create_hitl_decision.assert_not_awaited()
        monitor._send_slack_notification.assert_not_awaited()
        assert monitor._post_consensus_stall_first_seen is None

    def test_fails_open_when_pipeline_load_raises(self) -> None:
        """If the state store raises (e.g. transient FS error), the
        detector falls through to the existing grace-period / broadcast
        logic — we don't want infrastructure hiccups to indefinitely
        suppress real stall alerts."""
        monitor = OverseerMonitor(
            pipeline_id="test-1911-load-raises",
            config=_MockConfig(),
        )
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        monitor._post_consensus_stall_first_seen = time.time() - 999

        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("transient storage error")
        consensus = {"is_complete": True}

        with (
            patch("overseer.monitor._get_state_store", return_value=store),
            patch.dict(os.environ, {"EGG_REPO_PATH": "/fake/repo"}),
        ):
            _run(monitor._check_post_consensus_stall(consensus, "running"))

        # Grace period elapsed + no short-circuit applied — existing
        # behavior is to escalate.
        monitor._broadcast_alert.assert_awaited_once()
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()

    def test_no_shortcircuit_when_phase_implement_and_no_pr_markers(self) -> None:
        """Sanity check: when NONE of the three transition-completion
        signals is set — implement phase, no pr_number, no pr_url artifact —
        the detector must still fire after the grace period.  This is the
        original bug-reproduction path; the short-circuit must not
        accidentally swallow genuine stalls."""
        pipeline = self._pipeline(current_phase="implement", pr_number=None, pr_artifact=None)
        monitor, store = self._monitor_with_store(pipeline, "test-1911-genuine-stall")

        self._invoke(monitor, store)

        monitor._broadcast_alert.assert_awaited_once()
        monitor._create_hitl_decision.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()


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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()
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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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
        monitor._broadcast_alert = AsyncMock()

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


# ===================================================================
# test_orchestrator_reachability (issue #1371)
# ===================================================================


class TestOrchestratorReachability:
    """Tests for orchestrator unreachability detection."""

    def _make_monitor(self):
        monitor = OverseerMonitor(
            pipeline_id="test-reach",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        monitor._broadcast_alert = AsyncMock()
        return monitor

    def test_reachable_resets_counter(self) -> None:
        """Successful orchestrator response resets the failure counter."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2

        _run(
            monitor._check_orchestrator_reachability(
                pipeline_data={"status": "running"},
                phase_data={},
            )
        )

        assert monitor._consecutive_orch_failures == 0

    def test_unreachable_increments_counter(self) -> None:
        """Empty responses from both queries increment the failure counter."""
        monitor = self._make_monitor()

        _run(
            monitor._check_orchestrator_reachability(
                pipeline_data={},
                phase_data={},
            )
        )

        assert monitor._consecutive_orch_failures == 1
        monitor._send_slack_notification.assert_not_awaited()

    def test_unreachable_escalates_at_threshold(self) -> None:
        """After threshold consecutive failures, escalate via Slack."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2  # one below threshold

        _run(
            monitor._check_orchestrator_reachability(
                pipeline_data={},
                phase_data={},
            )
        )

        assert monitor._consecutive_orch_failures == 3
        monitor._send_slack_notification.assert_awaited_once()
        call_args = monitor._send_slack_notification.call_args
        assert call_args[0][0] == "orchestrator"
        assert "unreachable" in call_args[0][1].lower()

    def test_non_threshold_cycles_do_not_alert(self) -> None:
        """Cycles that don't land on a threshold multiple don't trigger alerts."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2

        # Cycle 3 hits threshold — alerts
        _run(monitor._check_orchestrator_reachability({}, {}))
        assert monitor._send_slack_notification.await_count == 1

        # Cycle 4 (4 % 3 != 0) — no alert
        _run(monitor._check_orchestrator_reachability({}, {}))
        assert monitor._send_slack_notification.await_count == 1

    def test_recovery_after_alert_re_enables_alerting(self) -> None:
        """After recovery, a new outage can trigger a fresh alert."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2

        # Hit threshold
        _run(monitor._check_orchestrator_reachability({}, {}))

        # Recover
        _run(monitor._check_orchestrator_reachability({"status": "running"}, {}))
        assert monitor._consecutive_orch_failures == 0

        # New outage cycle
        for _ in range(3):
            _run(monitor._check_orchestrator_reachability({}, {}))
        assert monitor._send_slack_notification.await_count == 2

    def test_phase_data_alone_counts_as_reachable(self) -> None:
        """If phase query succeeds but pipeline query fails, still reachable."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2

        _run(
            monitor._check_orchestrator_reachability(
                pipeline_data={},
                phase_data={"phase": "implement"},
            )
        )

        assert monitor._consecutive_orch_failures == 0

    def test_oversight_event_logged_on_unreachable(self) -> None:
        """An oversight event is logged when the threshold is breached."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2

        _run(monitor._check_orchestrator_reachability({}, {}))

        logged_events = [call.args[0] for call in monitor._log_oversight_event.call_args_list]
        assert any(e.get("event") == "orchestrator_unreachable" for e in logged_events)

    def test_periodic_re_alerting(self) -> None:
        """After initial alert, re-alert every threshold cycles."""
        monitor = self._make_monitor()

        # Drive through 2 * threshold (6) cycles of unreachability
        for _ in range(6):
            _run(monitor._check_orchestrator_reachability({}, {}))

        # Should have alerted at cycle 3 and cycle 6
        assert monitor._send_slack_notification.await_count == 2

        # One more cycle (7) — no alert (7 % 3 != 0)
        _run(monitor._check_orchestrator_reachability({}, {}))
        assert monitor._send_slack_notification.await_count == 2

    def test_oversight_event_logged_on_recovery(self) -> None:
        """An oversight event is logged when the orchestrator recovers."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 5

        _run(monitor._check_orchestrator_reachability({"status": "running"}, {}))

        logged_events = [call.args[0] for call in monitor._log_oversight_event.call_args_list]
        assert any(e.get("event") == "orchestrator_recovered" for e in logged_events)


# ===================================================================
# test_broadcast_alert (issue #1413)
# ===================================================================


class TestBroadcastAlert:
    """Verify _broadcast_alert sends OVERSEER_ALERT messages."""

    def test_broadcast_alert_sends_correct_cli_command(self) -> None:
        """_broadcast_alert sends a message with type OVERSEER_ALERT to 'all'."""
        monitor = OverseerMonitor(
            pipeline_id="test-broadcast-001",
            config=_MockConfig(),
        )
        monitor._run_cli = AsyncMock(return_value=(0, "", ""))

        _run(
            monitor._broadcast_alert(
                anomaly_type="stall",
                agent_role="coder",
                message="Agent appears stuck",
                priority="high",
            )
        )

        monitor._run_cli.assert_awaited_once()
        args = monitor._run_cli.call_args.args
        assert "egg-orch" in args
        assert "message" in args
        assert "send" in args
        assert "--to" in args
        idx_to = args.index("--to")
        assert args[idx_to + 1] == "all"
        idx_type = args.index("--type")
        assert args[idx_type + 1] == "OVERSEER_ALERT"
        idx_subject = args.index("--subject")
        assert "stall" in args[idx_subject + 1]
        assert "coder" in args[idx_subject + 1]
        assert "high" in args[idx_subject + 1]
        idx_body = args.index("--body")
        assert args[idx_body + 1] == "Agent appears stuck"

    def test_broadcast_alert_failure_does_not_raise(self) -> None:
        """_broadcast_alert gracefully handles CLI failures."""
        monitor = OverseerMonitor(
            pipeline_id="test-broadcast-002",
            config=_MockConfig(),
        )
        monitor._run_cli = AsyncMock(side_effect=OSError("CLI not found"))

        # Should not raise
        _run(monitor._broadcast_alert(anomaly_type="test", agent_role="coder", message="test"))


# ===================================================================
# test_execute_action_broadcasts (issue #1413)
# ===================================================================


class TestExecuteActionBroadcasts:
    """Verify _execute_action broadcasts an OVERSEER_ALERT for every action."""

    @staticmethod
    def _make_monitor():
        monitor = OverseerMonitor(
            pipeline_id="test-exec-001",
            config=_MockConfig(),
        )
        monitor._run_cli = AsyncMock(return_value=(0, "", ""))
        monitor._broadcast_alert = AsyncMock()
        monitor._send_message = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        return monitor

    def test_nudge_broadcasts(self) -> None:
        """Nudge action broadcasts an alert."""
        monitor = self._make_monitor()
        decision = {"action": "nudge", "message": "Check progress", "priority": "low"}
        _run(monitor._execute_action(decision, "coder"))
        monitor._broadcast_alert.assert_awaited_once_with(
            anomaly_type="action:nudge",
            agent_role="coder",
            message="Check progress",
            priority="low",
        )
        monitor._send_message.assert_awaited_once()

    def test_hitl_broadcasts(self) -> None:
        """HITL action broadcasts an alert."""
        monitor = self._make_monitor()
        decision = {"action": "hitl", "message": "Need human help", "priority": "high"}
        _run(monitor._execute_action(decision, "tester"))
        monitor._broadcast_alert.assert_awaited_once_with(
            anomaly_type="action:hitl",
            agent_role="tester",
            message="Need human help",
            priority="high",
        )
        monitor._create_hitl_decision.assert_awaited_once()

    def test_slack_broadcasts(self) -> None:
        """Slack action broadcasts an alert."""
        monitor = self._make_monitor()
        decision = {"action": "slack", "message": "Urgent issue", "priority": "critical"}
        _run(monitor._execute_action(decision, "orchestrator"))
        monitor._broadcast_alert.assert_awaited_once()
        monitor._send_slack_notification.assert_awaited_once()

    def test_redirect_broadcasts(self) -> None:
        """Redirect action broadcasts an alert."""
        monitor = self._make_monitor()
        decision = {"action": "redirect", "message": "Try different approach", "priority": "medium"}
        _run(monitor._execute_action(decision, "coder"))
        monitor._broadcast_alert.assert_awaited_once_with(
            anomaly_type="action:redirect",
            agent_role="coder",
            message="Try different approach",
            priority="medium",
        )
        monitor._send_message.assert_awaited_once()

    def test_issue_broadcasts(self) -> None:
        """Issue action broadcasts an alert."""
        monitor = self._make_monitor()
        decision = {"action": "issue", "message": "Persistent failure", "priority": "high"}
        with pytest.MonkeyPatch.context() as mp:
            mock_filer = AsyncMock()
            mp.setattr(
                "overseer.monitor.file_diagnostic_issue",
                mock_filer,
            )
            _run(monitor._execute_action(decision, "tester"))
        monitor._broadcast_alert.assert_awaited_once_with(
            anomaly_type="action:issue",
            agent_role="tester",
            message="Persistent failure",
            priority="high",
        )


# ===================================================================
# test_health_checks_broadcast (issue #1413)
# ===================================================================


class TestHealthChecksBroadcast:
    """Verify deterministic health checks broadcast OVERSEER_ALERT messages."""

    @staticmethod
    def _make_monitor():
        monitor = OverseerMonitor(
            pipeline_id="test-hc-001",
            config=_MockConfig(),
        )
        monitor._run_cli = AsyncMock(return_value=(0, "", ""))
        monitor._broadcast_alert = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        return monitor

    def test_post_consensus_stall_broadcasts(self) -> None:
        """Post-consensus stall broadcasts an alert when grace period expires."""
        monitor = self._make_monitor()
        consensus = {"is_complete": True}

        # First call: sets first-seen timestamp
        _run(monitor._check_post_consensus_stall(consensus, "running"))
        assert monitor._broadcast_alert.await_count == 0

        # Simulate grace period expiry
        monitor._post_consensus_stall_first_seen = time.time() - 200

        _run(monitor._check_post_consensus_stall(consensus, "running"))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "post_consensus_stall"  # anomaly_type
        assert call_args.args[3] == "high"  # priority

    def test_orchestrator_unreachable_broadcasts(self) -> None:
        """Orchestrator unreachability broadcasts an alert at threshold."""
        monitor = self._make_monitor()
        monitor._consecutive_orch_failures = 2  # one below threshold

        _run(monitor._check_orchestrator_reachability({}, {}))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "orchestrator_unreachable"  # anomaly_type
        assert call_args.args[3] == "critical"  # priority

    def test_status_inconsistency_broadcasts(self) -> None:
        """Status inconsistency broadcasts an alert after grace period."""
        monitor = self._make_monitor()
        pipeline_data = {
            "status": "failed",
            "concurrent": {
                "agents": [
                    {"role": "coder", "status": "complete"},
                    {"role": "tester", "status": "complete"},
                ],
            },
        }

        # First call: sets first-seen timestamp
        _run(monitor._check_status_consistency(pipeline_data))
        assert monitor._broadcast_alert.await_count == 0

        # Simulate grace period expiry
        monitor._status_inconsistency_first_seen = time.time() - 100

        _run(monitor._check_status_consistency(pipeline_data))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "status_inconsistency"  # anomaly_type

    def test_rerun_anomaly_broadcasts(self) -> None:
        """Rerun anomaly broadcasts an alert when detected."""
        monitor = self._make_monitor()
        import datetime as _dt

        now = _dt.datetime.now(_dt.UTC)
        resolved_at = (now - _dt.timedelta(seconds=120)).isoformat()
        started_at = (now - _dt.timedelta(seconds=30)).isoformat()
        completed_at = (now - _dt.timedelta(seconds=5)).isoformat()

        decisions = [
            {
                "id": "d-rerun-1",
                "decision_type": "phase_gate",
                "resolution": "request_changes",
                "content_changed": False,
                "resolved_at": resolved_at,
            }
        ]
        phase_data = {
            "phase_execution": {
                "cycle_timings": [
                    {"started_at": started_at, "completed_at": completed_at},
                ],
            },
        }

        _run(monitor._check_rerun_anomaly(decisions, phase_data))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "rerun_anomaly"
        assert call_args.args[3] == "high"

    def test_hitl_propagation_failure_broadcasts(self) -> None:
        """HITL propagation failure broadcasts an alert after timeout."""
        monitor = self._make_monitor()
        monitor._query_contract_data = AsyncMock(return_value={"decisions": []})

        decisions = [
            {
                "id": "d-hitl-prop-1",
                "decision_type": "phase_gate",
                "status": "resolved",
            }
        ]

        # First call: registers the pending decision
        _run(monitor._check_hitl_resolution_propagation(decisions))
        assert monitor._broadcast_alert.await_count == 0

        # Simulate timeout expiry
        monitor._hitl_resolution_pending["d-hitl-prop-1"] = time.time() - 400

        _run(monitor._check_hitl_resolution_propagation(decisions))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "hitl_propagation_failure"
        assert call_args.args[3] == "high"

    def test_pr_phase_no_pr_broadcasts(self) -> None:
        """PR phase without PR broadcasts a critical alert."""
        monitor = self._make_monitor()
        pipeline_data = {
            "current_phase": "pr",
            "phases": {
                "pr": {"artifacts": {}},
            },
        }

        _run(monitor._check_pr_phase_outcome(pipeline_data))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "pr_phase_no_pr"
        assert call_args.args[3] == "critical"

    def test_cross_phase_inconsistency_broadcasts(self) -> None:
        """Cross-phase inconsistency broadcasts an alert."""
        from unittest.mock import AsyncMock as _AM

        classifier = _MockClassifier()
        classifier.check_decision_consistency = _AM(
            return_value={
                "consistent": False,
                "concerns": ["Ignored prior feedback"],
                "confidence": 0.9,
            }
        )

        monitor = OverseerMonitor(
            pipeline_id="test-hc-cross-001",
            config=_MockConfig(),
            classifier=classifier,
        )
        monitor._broadcast_alert = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        monitor._query_contract_data = AsyncMock(return_value={"tasks": [{"id": "t1"}]})
        monitor._last_phase_name = "plan"

        decisions = [
            {"id": "d-cross-1", "status": "resolved", "phase": "plan", "resolution": "approve"}
        ]

        _run(monitor._check_cross_phase_consistency({"current_phase": "implement"}, decisions))
        monitor._broadcast_alert.assert_awaited_once()
        call_args = monitor._broadcast_alert.call_args
        assert call_args.args[0] == "cross_phase_inconsistency"
        assert call_args.args[3] == "high"


# ===================================================================
# Incomplete consensus activity-aware tests (#1609)
# ===================================================================


class _MockConfigWithGrace(_MockConfig):
    """Config with post-proposal grace and activity extension fields."""

    post_proposal_grace_seconds = 300
    active_agent_stall_extension_seconds = 120


class TestIncompleteConsensusActivityAware:
    """Test activity-aware deferral in _check_incomplete_consensus_stall."""

    def _make_monitor(self):
        classifier = _MockClassifier()
        decision_maker = _MockDecisionMaker()
        monitor = OverseerMonitor(
            pipeline_id="test-1609",
            config=_MockConfigWithGrace(),
            classifier=classifier,
            decision_maker=decision_maker,
        )
        monitor._send_message = AsyncMock()
        monitor._broadcast_alert = AsyncMock()
        monitor._create_hitl_decision = AsyncMock()
        monitor._send_slack_notification = AsyncMock()
        monitor._log_oversight_event = MagicMock()
        return monitor

    def test_nudge_deferred_when_agents_active(self):
        """Blocking agents with recent progress events — nudge deferred."""
        monitor = self._make_monitor()

        # Set up tracking: blocking agents seen just past nudge threshold
        # (poll_interval=1, nudge_threshold=10, hitl_threshold=20)
        now = time.time()
        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = now - 15  # past nudge, below HITL
        monitor._incomplete_consensus_absolute_start = now - 15

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Mock progress store to show recent activity
        mock_progress_store = MagicMock()
        mock_event = MagicMock()
        mock_progress_store.get_events.return_value = [mock_event]
        mock_ps = MagicMock()
        mock_ps.get_progress_store.return_value = mock_progress_store

        # Mock peer_consensus to return no recent proposals
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        with patch.dict(
            "sys.modules",
            {
                "progress_store": mock_ps,
                "peer_consensus": mock_pc,
            },
        ):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # Nudge should NOT have been sent
        monitor._send_message.assert_not_awaited()
        # first_seen should have been reset (activity extension)
        assert monitor._incomplete_consensus_first_seen > time.time() - 5

    def test_nudge_sent_when_agents_inactive(self):
        """Blocking agents without recent progress — nudge sent normally."""
        monitor = self._make_monitor()

        # Set elapsed past nudge threshold (10) but below HITL threshold (20)
        now = time.time()
        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = now - 15
        monitor._incomplete_consensus_absolute_start = now - 15

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Mock progress store — no recent activity
        mock_progress_store = MagicMock()
        mock_progress_store.get_events.return_value = []
        mock_ps = MagicMock()
        mock_ps.get_progress_store.return_value = mock_progress_store

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        with patch.dict(
            "sys.modules",
            {
                "progress_store": mock_ps,
                "peer_consensus": mock_pc,
            },
        ):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # Nudge SHOULD have been sent
        monitor._send_message.assert_awaited()
        assert monitor._incomplete_consensus_nudged is True

    def test_post_proposal_grace_resets_tracking(self):
        """A recent CONSENSUS_PROPOSE resets the incomplete consensus tracking."""
        from datetime import UTC, datetime, timedelta

        monitor = self._make_monitor()

        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = time.time() - 400
        monitor._incomplete_consensus_absolute_start = time.time() - 400
        monitor._incomplete_consensus_nudged = True  # was nudged before

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Mock tracker with a recent proposal (60s ago, grace is 300s)
        mock_tracker = MagicMock()
        mock_tracker.get_latest_proposal_timestamp.return_value = datetime.now(UTC) - timedelta(
            seconds=60
        )
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict("sys.modules", {"peer_consensus": mock_pc}):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # Tracking should have been reset — nudged flag cleared
        assert monitor._incomplete_consensus_nudged is False
        # No nudge or HITL should have been created
        monitor._send_message.assert_not_awaited()
        monitor._create_hitl_decision.assert_not_awaited()

    def test_hitl_deferral_capped_when_agents_active_too_long(self):
        """HITL escalation fires even if agents are active once deferral cap is exceeded."""
        monitor = self._make_monitor()

        # poll_interval=1, hitl_threshold=20, max_deferral=40
        # Set elapsed past HITL threshold AND past the 2x deferral cap
        now = time.time()
        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = now - 25  # past hitl_threshold (20)
        monitor._incomplete_consensus_absolute_start = now - 45  # past 2x cap (40)
        monitor._incomplete_consensus_nudged = True

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Mock progress store — agents ARE active (but cap is exceeded)
        mock_progress_store = MagicMock()
        mock_event = MagicMock()
        mock_progress_store.get_events.return_value = [mock_event]
        mock_ps = MagicMock()
        mock_ps.get_progress_store.return_value = mock_progress_store

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        with patch.dict(
            "sys.modules",
            {
                "progress_store": mock_ps,
                "peer_consensus": mock_pc,
            },
        ):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # HITL SHOULD have been created despite active agents — cap exceeded
        monitor._create_hitl_decision.assert_awaited()
        assert monitor._incomplete_consensus_hitl_created is True

    def test_hitl_fires_after_grace_reset_and_deferral_cap(self):
        """HITL fires when: grace resets tracking → grace expires → agent active past deferral cap."""
        from datetime import UTC, datetime, timedelta

        monitor = self._make_monitor()

        # --- Step 1: Simulate a proposal arriving, triggering grace reset ---
        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = time.time() - 400
        monitor._incomplete_consensus_absolute_start = time.time() - 400
        monitor._incomplete_consensus_nudged = True

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Recent proposal (60s ago) triggers grace
        mock_tracker = MagicMock()
        mock_tracker.get_latest_proposal_timestamp.return_value = datetime.now(UTC) - timedelta(
            seconds=60
        )
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict("sys.modules", {"peer_consensus": mock_pc}):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # Grace fired — tracking was reset, absolute_start should be set to ~now
        assert monitor._incomplete_consensus_nudged is False
        assert monitor._incomplete_consensus_absolute_start is not None
        grace_absolute_start = monitor._incomplete_consensus_absolute_start

        # --- Step 2: Simulate time passing past HITL + deferral cap ---
        # poll_interval=1, hitl_threshold=20, max_deferral=40
        # Set first_seen and absolute_start so elapsed > hitl_threshold
        # and absolute_elapsed > 2 * hitl_threshold
        monitor._incomplete_consensus_first_seen = grace_absolute_start  # from grace reset
        monitor._incomplete_consensus_absolute_start = (
            grace_absolute_start - 45
        )  # simulate 45s of total time
        monitor._incomplete_consensus_nudged = True  # nudge was sent

        # Agents are still active
        mock_progress_store = MagicMock()
        mock_event = MagicMock()
        mock_progress_store.get_events.return_value = [mock_event]
        mock_ps = MagicMock()
        mock_ps.get_progress_store.return_value = mock_progress_store

        mock_pc2 = MagicMock()
        mock_pc2.get_peer_consensus_tracker.return_value = None

        # Advance first_seen so elapsed > hitl_threshold (20)
        monitor._incomplete_consensus_first_seen = time.time() - 25

        with patch.dict(
            "sys.modules",
            {
                "progress_store": mock_ps,
                "peer_consensus": mock_pc2,
            },
        ):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # HITL SHOULD fire — deferral cap exceeded even though agents are active
        monitor._create_hitl_decision.assert_awaited()
        assert monitor._incomplete_consensus_hitl_created is True

    def test_nudge_deferral_capped_at_hitl_threshold(self):
        """Nudge fires when absolute elapsed exceeds HITL threshold despite activity."""
        monitor = self._make_monitor()

        # poll_interval=1, nudge_threshold=10, hitl_threshold=20
        # Absolute elapsed past hitl_threshold — nudge deferral should stop
        now = time.time()
        monitor._incomplete_consensus_blocking = frozenset(["reviewer_refine"])
        monitor._incomplete_consensus_first_seen = now - 15  # past nudge (10)
        monitor._incomplete_consensus_absolute_start = now - 25  # past hitl_threshold (20)

        consensus = {
            "is_complete": False,
            "blocking_agents": ["reviewer_refine"],
        }

        # Mock progress store — agents ARE active
        mock_progress_store = MagicMock()
        mock_event = MagicMock()
        mock_progress_store.get_events.return_value = [mock_event]
        mock_ps = MagicMock()
        mock_ps.get_progress_store.return_value = mock_progress_store

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        with patch.dict(
            "sys.modules",
            {
                "progress_store": mock_ps,
                "peer_consensus": mock_pc,
            },
        ):
            _run(monitor._check_incomplete_consensus_stall(consensus, "running"))

        # Nudge SHOULD have been sent despite active agents — cap exceeded
        monitor._send_message.assert_awaited()
        assert monitor._incomplete_consensus_nudged is True


# ===================================================================
# test_query_container_list
# ===================================================================


class TestQueryContainerList:
    """Tests for _query_container_list."""

    def test_returns_containers_from_data_envelope(self) -> None:
        """Parses containers from {data: {containers: [...]}} envelope."""
        monitor = OverseerMonitor(
            pipeline_id="test-cl-1",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        payload = json.dumps({"data": {"containers": [{"container_id": "c1"}]}})
        monitor._run_cli = AsyncMock(return_value=(0, payload, ""))

        result = _run(monitor._query_container_list())
        assert result == [{"container_id": "c1"}]

    def test_returns_containers_from_raw_list(self) -> None:
        """Parses containers from a raw JSON list."""
        monitor = OverseerMonitor(
            pipeline_id="test-cl-2",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        payload = json.dumps([{"container_id": "c1"}, {"container_id": "c2"}])
        monitor._run_cli = AsyncMock(return_value=(0, payload, ""))

        result = _run(monitor._query_container_list())
        assert result == [{"container_id": "c1"}, {"container_id": "c2"}]

    def test_returns_empty_on_cli_failure(self) -> None:
        """Returns empty list when CLI returns non-zero."""
        monitor = OverseerMonitor(
            pipeline_id="test-cl-3",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        monitor._run_cli = AsyncMock(return_value=(1, "", "error"))

        result = _run(monitor._query_container_list())
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        """Returns empty list when CLI raises an exception."""
        monitor = OverseerMonitor(
            pipeline_id="test-cl-4",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        monitor._run_cli = AsyncMock(side_effect=RuntimeError("boom"))

        result = _run(monitor._query_container_list())
        assert result == []

    def test_returns_empty_on_empty_stdout(self) -> None:
        """Returns empty list when CLI returns empty stdout."""
        monitor = OverseerMonitor(
            pipeline_id="test-cl-5",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )
        monitor._run_cli = AsyncMock(return_value=(0, "", ""))

        result = _run(monitor._query_container_list())
        assert result == []


# ===================================================================
# test_query_container_logs
# ===================================================================


class TestQueryContainerLogs:
    """Tests for _query_container_logs."""

    def _make_monitor(self) -> OverseerMonitor:
        return OverseerMonitor(
            pipeline_id="test-ql-1",
            config=_MockConfig(),
            classifier=_MockClassifier(),
            decision_maker=_MockDecisionMaker(),
        )

    def test_selects_running_container(self) -> None:
        """Prefers a running container over a stopped one."""
        monitor = self._make_monitor()
        containers = [
            {
                "container_id": "stopped1",
                "agent_role": "coder",
                "status": "stopped",
                "started_at": "2026-04-10T10:00:00Z",
            },
            {
                "container_id": "running1",
                "agent_role": "coder",
                "status": "running",
                "started_at": "2026-04-10T09:00:00Z",
            },
        ]
        log_payload = json.dumps({"data": {"logs": "some log output"}})

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                # Verify we're fetching from the running container
                assert "running1" in args
                return (0, log_payload, "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == "some log output"

    def test_selects_newest_running_container(self) -> None:
        """When multiple running containers exist, selects newest by started_at."""
        monitor = self._make_monitor()
        containers = [
            {
                "container_id": "old",
                "agent_role": "coder",
                "status": "running",
                "started_at": "2026-04-10T08:00:00Z",
            },
            {
                "container_id": "new",
                "agent_role": "coder",
                "status": "running",
                "started_at": "2026-04-10T10:00:00Z",
            },
        ]
        log_payload = json.dumps({"data": {"logs": "newest logs"}})

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                assert "new" in args
                return (0, log_payload, "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == "newest logs"

    def test_falls_back_to_most_recent_stopped(self) -> None:
        """When no running containers, selects most recently started stopped one."""
        monitor = self._make_monitor()
        containers = [
            {
                "container_id": "old_stopped",
                "agent_role": "coder",
                "status": "stopped",
                "started_at": "2026-04-10T08:00:00Z",
            },
            {
                "container_id": "new_stopped",
                "agent_role": "coder",
                "status": "stopped",
                "started_at": "2026-04-10T10:00:00Z",
            },
        ]
        log_payload = json.dumps({"data": {"logs": "stopped logs"}})

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                assert "new_stopped" in args
                return (0, log_payload, "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == "stopped logs"

    def test_returns_empty_when_no_containers(self) -> None:
        """Returns empty string when no containers exist."""
        monitor = self._make_monitor()
        monitor._run_cli = AsyncMock(return_value=(0, json.dumps({"data": {"containers": []}}), ""))

        result = _run(monitor._query_container_logs("coder"))
        assert result == ""

    def test_returns_empty_when_no_matching_role(self) -> None:
        """Returns empty string when no containers match the requested role."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "tester", "status": "running"}]
        monitor._run_cli = AsyncMock(
            return_value=(0, json.dumps({"data": {"containers": containers}}), "")
        )

        result = _run(monitor._query_container_logs("coder"))
        assert result == ""

    def test_returns_empty_when_container_has_no_id(self) -> None:
        """Returns empty string when matching container has no container_id."""
        monitor = self._make_monitor()
        containers = [{"agent_role": "coder", "status": "running"}]
        monitor._run_cli = AsyncMock(
            return_value=(0, json.dumps({"data": {"containers": containers}}), "")
        )

        result = _run(monitor._query_container_logs("coder"))
        assert result == ""

    def test_returns_empty_on_log_fetch_failure(self) -> None:
        """Returns empty string when log fetch CLI call fails."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "coder", "status": "running"}]

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                return (1, "", "error")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == ""

    def test_returns_empty_on_exception(self) -> None:
        """Returns empty string when an exception is raised."""
        monitor = self._make_monitor()
        monitor._run_cli = AsyncMock(side_effect=RuntimeError("boom"))

        result = _run(monitor._query_container_logs("coder"))
        assert result == ""

    def test_parses_logs_from_raw_stdout(self) -> None:
        """Falls back to raw stdout when response is not a dict."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "coder", "status": "running"}]

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                return (0, '"just a string"', "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == '"just a string"'

    def test_parses_logs_key_fallback(self) -> None:
        """Handles {logs: ...} response without data envelope."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "coder", "status": "running"}]
        log_payload = json.dumps({"logs": "fallback logs"})

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                return (0, log_payload, "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == "fallback logs"

    def test_returns_raw_stdout_on_json_decode_error(self) -> None:
        """Falls back to raw stdout when CLI output is not valid JSON."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "coder", "status": "running"}]
        raw_log_text = (
            "2026-04-10 ERROR: connection refused\nTraceback (most recent call last):\n  ..."
        )

        async def mock_run_cli(*args, **kwargs):
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                return (0, raw_log_text, "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        result = _run(monitor._query_container_logs("coder"))
        assert result == raw_log_text

    def test_passes_tail_as_lines_arg(self) -> None:
        """Verifies --lines <tail> is passed to the container logs CLI call."""
        monitor = self._make_monitor()
        containers = [{"container_id": "c1", "agent_role": "coder", "status": "running"}]
        captured_args: list[tuple] = []

        async def mock_run_cli(*args, **kwargs):
            captured_args.append(args)
            if "list" in args:
                return (0, json.dumps({"data": {"containers": containers}}), "")
            if "logs" in args:
                return (0, json.dumps({"data": {"logs": "ok"}}), "")
            return (0, "[]", "")

        monitor._run_cli = mock_run_cli

        _run(monitor._query_container_logs("coder", tail=500))
        # Find the logs call and verify --lines and the tail value are present
        logs_call = [a for a in captured_args if "logs" in a]
        assert len(logs_call) == 1
        assert "--lines" in logs_call[0]
        lines_idx = logs_call[0].index("--lines")
        assert logs_call[0][lines_idx + 1] == "500"

    def test_container_logs_cache_dedup_in_poll_cycle(self) -> None:
        """Two alerts for the same agent should only fetch container logs once."""
        monitor = self._make_monitor()

        # Track how many times _query_container_logs is called
        query_count = 0

        async def counting_query(agent_role: str, tail: int = 200) -> str:
            nonlocal query_count
            query_count += 1
            return "some logs"

        monitor._query_container_logs = counting_query

        alerts = [
            {"agent_role": "coder", "alert_type": "heartbeat_stale", "agent_id": "coder"},
            {"agent_role": "coder", "alert_type": "progress_stall", "agent_id": "coder"},
        ]

        # Mock all the other _poll_cycle dependencies to isolate the cache behavior
        monitor._query_consensus_status = AsyncMock(return_value={})
        monitor._query_current_phase = AsyncMock(return_value="implement")
        monitor._query_progress = AsyncMock(return_value=[])
        monitor._query_health_alerts = AsyncMock(return_value=alerts)
        monitor._query_pipeline_data = AsyncMock(
            return_value={"status": "running", "phase": "implement", "agents": [{"role": "coder"}]}
        )
        monitor._check_orchestrator_reachability = AsyncMock()
        monitor._query_decisions = AsyncMock(return_value=[])
        monitor._check_rerun_anomaly = AsyncMock()
        monitor._check_status_consistency = AsyncMock()
        monitor._check_hitl_resolution_propagation = AsyncMock()
        monitor._poll_escalation_messages = AsyncMock(return_value=[])
        monitor._check_post_consensus_stalls = AsyncMock()
        monitor._check_incomplete_consensus_stalls = AsyncMock()
        monitor._check_cross_phase_consistency = AsyncMock()
        monitor._is_infra_error_deduped = MagicMock(return_value=False)
        monitor._record_infra_error_dedup = MagicMock()
        monitor._resolve_alert = AsyncMock()
        monitor._execute_action = AsyncMock()

        _run(monitor._poll_cycle())

        # Despite two alerts for "coder", logs should be fetched only once
        assert query_count == 1

        # Both alerts should have received the cached container_logs value
        classify_calls = monitor._classifier.classify_stall.call_args_list
        assert len(classify_calls) == 2
        for call in classify_calls:
            assert call.kwargs.get("container_logs") == "some logs"
