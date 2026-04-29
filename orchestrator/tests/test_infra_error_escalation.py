"""Tests for infrastructure error escalation to HITL (issue #1489).

Validates the three-tier infrastructure error handling:
1. Tier 1 — Deterministic tripwire in health_monitor.py that detects
   blocked progress events with infrastructure-related keywords.
2. Tier 2 — Overseer classifier/decision-maker enhancement to recognise
   and auto-escalate infrastructure errors.
3. Cross-tier — Deduplication between Tier 1 and Tier 2.

Related: issue #1489
"""

import asyncio
import json
import re
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

# Mock docker before importing modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Import with resilience
# ---------------------------------------------------------------------------

try:
    from health_monitor import AgentState, HealthMonitor
except ImportError:
    pytest.skip(
        "health_monitor module not available",
        allow_module_level=True,
    )

from events import Event, EventBus, EventType
from models import PipelineConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

try:
    from egg_config import GATEWAY_PORT
except ImportError:
    GATEWAY_PORT = 9848  # noqa: EGG002

PIPELINE_ID = "issue-1489"
AGENT_ID = "coder-abc123"
AGENT_ID_2 = "tester-def456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with optional overrides."""
    defaults = {
        "orchestrator_heartbeat_timeout_seconds": 120,
        "orchestrator_error_repeat_threshold": 3,
        "orchestrator_message_rate_limit": 20,
        "overseer_enabled": True,
        "overseer_max_redirects_before_escalation": 2,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def _make_event_bus() -> EventBus:
    """Create a synchronous EventBus for testing."""
    return EventBus(async_delivery=False)


def _make_monitor(
    event_bus: EventBus,
    config: PipelineConfig | None = None,
    pipeline_id: str = PIPELINE_ID,
) -> HealthMonitor:
    """Instantiate a HealthMonitor wired to the given bus."""
    config = config or _make_config()
    return HealthMonitor(
        event_bus=event_bus,
        pipeline_id=pipeline_id,
        config=config,
    )


def _emit_heartbeat(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit a heartbeat event for an agent."""
    return event_bus.emit(
        EventType.PROGRESS_EMITTED,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "type": "heartbeat"},
    )


def _emit_progress(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
    state: str = "working",
    blocker: str | None = None,
) -> Event:
    """Emit a structured progress event."""
    data: dict = {
        "agent_id": agent_id,
        "type": "progress",
        "state": state,
        "description": "working on task",
    }
    if blocker is not None:
        data["blocker"] = blocker
    return event_bus.emit(
        EventType.PROGRESS_EMITTED,
        pipeline_id=pipeline_id,
        data=data,
    )


def _pattern_search(pattern, text: str):
    """Search text with a pattern that may be compiled or a raw string."""
    if isinstance(pattern, re.Pattern):
        return pattern.search(text)
    return re.search(pattern, text, re.IGNORECASE)


def _emit_error(
    event_bus: EventBus,
    error_msg: str,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit an error event for an agent."""
    return event_bus.emit(
        EventType.ERROR,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "error": error_msg},
    )


# ===================================================================
# TIER 1: Deterministic Infrastructure Error Tripwire
# ===================================================================


class TestInfraErrorPatternsExist:
    """Verify that INFRA_ERROR_PATTERNS constant is defined."""

    def test_infra_error_patterns_constant_exists(self):
        """INFRA_ERROR_PATTERNS should be a list/tuple of regex patterns."""
        from health_monitor import INFRA_ERROR_PATTERNS

        assert isinstance(INFRA_ERROR_PATTERNS, (list, tuple))
        assert len(INFRA_ERROR_PATTERNS) > 0, (
            "INFRA_ERROR_PATTERNS should contain at least one pattern"
        )

    def test_patterns_are_valid_regex(self):
        """Each pattern in INFRA_ERROR_PATTERNS should be a valid compiled regex."""
        from health_monitor import INFRA_ERROR_PATTERNS

        for pattern in INFRA_ERROR_PATTERNS:
            # Patterns may be pre-compiled re.Pattern or raw strings
            if isinstance(pattern, re.Pattern):
                # Already compiled — just verify it's usable
                assert pattern.search("") is not None or pattern.search("") is None
            else:
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    pytest.fail(f"Invalid regex pattern '{pattern}': {exc}")

    def test_patterns_match_known_infra_errors(self):
        """Patterns should match the known infrastructure error strings."""
        from health_monitor import INFRA_ERROR_PATTERNS

        known_infra_errors = [
            "git add failed: .gitignore",
            "permission denied",
            "EROFS: read-only file system",
            "403 Forbidden",
            "gateway error: connection refused",
            "git push failed",
        ]

        for error_str in known_infra_errors:
            matched = any(_pattern_search(pattern, error_str) for pattern in INFRA_ERROR_PATTERNS)
            assert matched, (
                f"Expected INFRA_ERROR_PATTERNS to match '{error_str}' but no pattern matched"
            )

    def test_patterns_do_not_match_non_infra_errors(self):
        """Patterns should NOT match non-infrastructure errors."""
        from health_monitor import INFRA_ERROR_PATTERNS

        non_infra_strings = [
            "waiting for reviewer",
            "compiling code",
            "running tests",
            "agent is making progress",
        ]

        for non_infra in non_infra_strings:
            matched = any(_pattern_search(pattern, non_infra) for pattern in INFRA_ERROR_PATTERNS)
            assert not matched, (
                f"INFRA_ERROR_PATTERNS should NOT match '{non_infra}' "
                f"but a pattern matched (false positive)"
            )


class TestAgentStateInfraErrorFlag:
    """Verify the infra_error_escalated flag on AgentState."""

    def test_agent_state_has_infra_error_escalated(self):
        """AgentState should have an infra_error_escalated field."""
        state = AgentState(agent_id="test-agent")
        assert hasattr(state, "infra_error_escalated"), (
            "AgentState missing infra_error_escalated field"
        )

    def test_infra_error_escalated_defaults_false(self):
        """infra_error_escalated should default to False."""
        state = AgentState(agent_id="test-agent")
        assert state.infra_error_escalated is False


class TestInfraErrorDetection:
    """Tier 1 tripwire: blocked progress events with infra keywords."""

    def test_blocked_with_git_error_triggers_alert(self):
        """A blocked progress event with a git error triggers infra alert."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Emit a blocked progress event with git infrastructure error
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )

        actions = monitor.check_tripwires()

        # Should produce an infrastructure error alert
        infra_actions = [
            a
            for a in actions
            if a.get("reason", "").lower().find("infrastructure") >= 0
            or a.get("type") == "infrastructure_error"
            or a.get("alert_type") == "infrastructure_error"
        ]
        infra_escalations = [
            e
            for e in escalations
            if e.get("type") == "hitl" or e.get("reason", "").lower().find("infrastructure") >= 0
        ]

        assert len(infra_actions) > 0 or len(infra_escalations) > 0, (
            "Expected an infrastructure error alert or escalation for "
            "'git add failed: .gitignore' but got none"
        )

    def test_blocked_with_erofs_triggers_alert(self):
        """EROFS (read-only filesystem) error triggers infra alert."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="EROFS: read-only file system, open '/egg-state/drafts/foo.md'",
        )

        monitor.check_tripwires()
        all_alerts = monitor.get_active_alerts()

        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        assert len(infra_alerts) > 0 or any(
            "infrastructure" in str(e.get("reason", "")).lower() for e in escalations
        ), "EROFS error should trigger infrastructure error alert"

    def test_blocked_with_permission_denied_triggers_alert(self):
        """Permission denied error triggers infra alert."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="permission denied: /home/egg/repos/egg/.egg-state/contracts",
        )

        monitor.check_tripwires()
        all_alerts = monitor.get_active_alerts()

        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        assert len(infra_alerts) > 0 or len(escalations) > 0, (
            "Permission denied error should trigger infrastructure error alert"
        )

    def test_blocked_with_gateway_error_triggers_alert(self):
        """Gateway error triggers infra alert."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="gateway error: 403 Forbidden on git push",
        )

        monitor.check_tripwires()
        all_alerts = monitor.get_active_alerts()

        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        assert len(infra_alerts) > 0 or len(escalations) > 0, (
            "Gateway error should trigger infrastructure error alert"
        )

    def test_non_infra_blocked_does_not_trigger(self):
        """A blocked event with non-infrastructure reason is NOT flagged."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="waiting for reviewer response",
        )

        monitor.check_tripwires()
        all_alerts = monitor.get_active_alerts()

        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        assert len(infra_alerts) == 0, (
            "'waiting for reviewer response' should NOT trigger an infrastructure error alert"
        )

    def test_working_progress_does_not_trigger(self):
        """A normal working progress event should not trigger infra alert."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        _emit_progress(bus, agent_id=AGENT_ID, state="working")

        monitor.check_tripwires()
        all_alerts = monitor.get_active_alerts()

        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        assert len(infra_alerts) == 0


class TestInfraErrorDeduplication:
    """Tier 1: Deduplication via infra_error_escalated flag."""

    def test_duplicate_infra_error_suppressed_same_cycle(self):
        """Repeated check_tripwires without new event should NOT re-escalate."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Emit a blocked event with infra error
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )

        # First tripwire check — should escalate
        monitor.check_tripwires()
        first_count = len(escalations)
        assert first_count >= 1, "First check should escalate"

        # Second tripwire check WITHOUT new event — should NOT re-escalate
        # (infra_error_escalated flag prevents it)
        actions = monitor.check_tripwires()
        second_count = len(escalations)

        infra_actions = [a for a in actions if a.get("alert_type") == "infrastructure_error"]
        assert len(infra_actions) == 0, (
            "Repeated check_tripwires without new progress event "
            "should NOT produce duplicate infrastructure error actions"
        )
        assert second_count == first_count, (
            f"Expected deduplication: first={first_count}, second={second_count}. "
            "Duplicate infra error should be suppressed."
        )

    def test_infra_error_flag_resets_on_recovery(self):
        """After agent emits working progress, infra_error_escalated resets."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # First infra error -> escalation
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()
        first_count = len(escalations)
        assert first_count >= 1, "First infra error should trigger escalation"

        # Agent recovers (emits working progress)
        _emit_progress(bus, agent_id=AGENT_ID, state="working")

        # Second infra error (should escalate again because flag was reset)
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git push failed: permission denied",
        )
        monitor.check_tripwires()

        assert len(escalations) > first_count, (
            "After recovery, a new infra error should trigger fresh escalation"
        )

    def test_different_agents_escalate_independently(self):
        """Infra error dedup is per-agent, not global."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Agent 1 hits infra error
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()

        # Agent 2 hits infra error
        _emit_progress(
            bus,
            agent_id=AGENT_ID_2,
            state="blocked",
            blocker="EROFS: read-only file system",
        )
        monitor.check_tripwires()

        # Both should have escalated
        escalated_agents = {e.get("agent_id") for e in escalations}
        assert AGENT_ID in escalated_agents, "Agent 1 should have escalated"
        assert AGENT_ID_2 in escalated_agents, "Agent 2 should have escalated"


class TestInfraErrorDedupBlockedReemission:
    """Tier 1: Re-emitted blocked events should NOT reset the dedup flag."""

    def test_reemitted_blocked_event_does_not_cause_duplicate(self):
        """Agent re-emitting the same blocked progress should not re-escalate."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # First blocked event with infra error
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()
        first_count = len(escalations)
        assert first_count >= 1, "First check should escalate"

        # Agent re-emits the same blocked event (periodic progress reporting)
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()

        assert len(escalations) == first_count, (
            "Re-emitted blocked event should NOT cause duplicate escalation"
        )


class TestInfraErrorDedupBlockerChange:
    """Tier 1: Changing blocker text while still blocked should allow re-escalation."""

    def test_different_blocker_while_blocked_resets_dedup(self):
        """Agent hitting a new infra error while still blocked should escalate again."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # First blocked event with infra error
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()
        first_count = len(escalations)
        assert first_count >= 1, "First check should escalate"

        # Agent hits a DIFFERENT infra error while still blocked
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="permission denied on /tmp/workspace",
        )
        monitor.check_tripwires()

        assert len(escalations) > first_count, (
            "Different blocker text while still blocked should trigger new escalation"
        )


class TestInfraErrorAlertSeverity:
    """Infrastructure errors should produce critical severity alerts."""

    def test_infra_error_alert_is_critical(self):
        """Infrastructure error alerts should have severity=critical."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()

        all_alerts = monitor.get_active_alerts()
        infra_alerts = [a for a in all_alerts if a.get("alert_type") == "infrastructure_error"]

        if infra_alerts:
            assert infra_alerts[0]["severity"] == "critical", (
                f"Infrastructure error alert should be critical, "
                f"got {infra_alerts[0].get('severity')}"
            )

    def test_infra_error_escalation_type_is_hitl(self):
        """Infrastructure error should escalate directly to HITL."""
        bus = _make_event_bus()
        config = _make_config(overseer_enabled=True)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )
        monitor.check_tripwires()

        # Infra errors should go to HITL directly, even with overseer enabled
        infra_escalations = [
            e
            for e in escalations
            if "infrastructure" in str(e.get("reason", "")).lower()
            or "git" in str(e.get("reason", "")).lower()
        ]

        if infra_escalations:
            assert infra_escalations[0]["type"] == "hitl", (
                "Infrastructure errors should escalate directly to HITL, "
                f"got type={infra_escalations[0].get('type')}"
            )


class TestCheckTripwiresIncludesInfraErrors:
    """check_tripwires() should include infrastructure error checks."""

    def test_check_tripwires_calls_infra_check(self):
        """check_tripwires() should call _check_infra_errors()."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        # Verify _check_infra_errors exists
        assert hasattr(monitor, "_check_infra_errors"), (
            "HealthMonitor should have a _check_infra_errors method"
        )

        # Emit an infra error and verify it's caught by check_tripwires
        _emit_progress(
            bus,
            agent_id=AGENT_ID,
            state="blocked",
            blocker="git add failed: .gitignore",
        )

        actions = monitor.check_tripwires()
        # The infra error should be in the actions from check_tripwires
        assert (
            any(
                "infrastructure" in str(a).lower() or "git" in str(a.get("reason", "")).lower()
                for a in actions
            )
            or len(monitor.get_active_alerts()) > 0
        ), "check_tripwires() should detect infrastructure errors"


# ===================================================================
# TIER 2: Overseer Classifier and Decision Maker
# ===================================================================

# Conditional imports for Tier 2 tests
_tier2_available = True

try:
    from overseer.classifier import classify_error, classify_stall, clear_cache
except ImportError, ModuleNotFoundError:
    _tier2_available = False

try:
    from overseer.decision_maker import decide_corrective_action, decide_escalation_level
except ImportError, ModuleNotFoundError:
    _tier2_available = False

# AgentResult helper for mocking
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


_CLASSIFIER_AGENT_PATCH = "overseer.classifier.run_agent_async"
_DECISION_AGENT_PATCH = "overseer.decision_maker.run_agent_async"


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear classifier cache before each test."""
    if _tier2_available:
        clear_cache()
    yield
    if _tier2_available:
        clear_cache()


@pytest.mark.skipif(not _tier2_available, reason="Tier 2 overseer modules not available")
class TestClassifierInfrastructureError:
    """Tier 2: classify_stall() and classify_error() recognise infra errors."""

    @patch(_CLASSIFIER_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_stall_infrastructure_error(self, mock_agent: AsyncMock) -> None:
        """classify_stall() should accept 'infrastructure_error' classification."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "classification": "infrastructure_error",
                    "confidence": 0.95,
                    "reasoning": "Agent is blocked by git add failure due to .gitignore",
                }
            )
        )

        result = _run(
            classify_stall(
                logs=[{"msg": "git add failed: .gitignore"}],
                progress=[{"state": "blocked", "blocker": "git add failed"}],
            )
        )

        assert result["classification"] == "infrastructure_error"
        assert result["confidence"] >= 0.9
        assert "reasoning" in result
        mock_agent.assert_awaited_once()

    @patch(_CLASSIFIER_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_error_infrastructure_type(self, mock_agent: AsyncMock) -> None:
        """classify_error() should return error_type=infrastructure_error."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "error_type": "infrastructure_error",
                    "severity": "critical",
                    "recommended_action": "escalate_hitl",
                }
            )
        )

        result = _run(
            classify_error(
                error_context={
                    "msg": "EROFS: read-only file system",
                    "code": 30,
                    "file": "/egg-state/drafts/plan.md",
                }
            )
        )

        assert result["error_type"] == "infrastructure_error"
        assert result["severity"] == "critical"
        assert result["recommended_action"] == "escalate_hitl"
        mock_agent.assert_awaited_once()

    @patch(_CLASSIFIER_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_stall_prompt_mentions_infrastructure(self, mock_agent: AsyncMock) -> None:
        """classify_stall() prompt should mention infrastructure errors."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "classification": "working",
                    "confidence": 0.8,
                    "reasoning": "Agent is compiling",
                }
            )
        )

        _run(
            classify_stall(
                logs=[{"msg": "compiling..."}],
                progress=[{"state": "working"}],
            )
        )

        # Inspect the prompt sent to the LLM
        call_args = mock_agent.call_args
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        # The prompt should reference infrastructure_error as a valid classification
        assert "infrastructure" in prompt.lower(), (
            "classify_stall() prompt should mention 'infrastructure_error' "
            "as a valid classification category"
        )


@pytest.mark.skipif(not _tier2_available, reason="Tier 2 overseer modules not available")
class TestDecisionMakerInfraFastPath:
    """Tier 2: decide_corrective_action() has an infra error fast-path."""

    @patch(_DECISION_AGENT_PATCH, new_callable=AsyncMock)
    def test_infra_error_classification_returns_hitl(self, mock_agent: AsyncMock) -> None:
        """Infrastructure error classification should produce HITL action."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "hitl",
                    "message": "Infrastructure error: git add failed. Escalating to human.",
                    "priority": "high",
                }
            )
        )

        classification = {
            "classification": "infrastructure_error",
            "confidence": 0.95,
            "reasoning": "Agent blocked by git failure",
        }
        context = {"pipeline_id": "issue-1489", "phase": "implement"}

        result = _run(decide_corrective_action(classification, context))

        assert result["action"] == "hitl", (
            f"Infrastructure error should result in HITL action, got action={result.get('action')}"
        )
        assert result["priority"] in ("high", "critical"), (
            f"Infrastructure error priority should be high or critical, "
            f"got {result.get('priority')}"
        )
        # The fast-path should bypass the LLM entirely
        mock_agent.assert_not_awaited()

    @patch(_DECISION_AGENT_PATCH, new_callable=AsyncMock)
    def test_infra_error_escalation_level_always_hitl(self, mock_agent: AsyncMock) -> None:
        """Infrastructure error in decide_escalation_level returns hitl."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "escalate": True,
                    "level": "hitl",
                    "reasoning": "Infrastructure errors always escalate to HITL.",
                }
            )
        )

        classification = {
            "classification": "infrastructure_error",
            "confidence": 0.95,
            "reasoning": "Permission denied on filesystem",
        }
        # Even with no prior redirects, infra errors should escalate
        redirect_history: list[dict] = []

        result = _run(decide_escalation_level(classification, redirect_history))

        assert result["escalate"] is True
        assert result["level"] == "hitl"

    @patch(_DECISION_AGENT_PATCH, new_callable=AsyncMock)
    def test_non_infra_still_follows_normal_ladder(self, mock_agent: AsyncMock) -> None:
        """Non-infrastructure classifications should follow normal ladder."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "action": "nudge",
                    "message": "Please check your progress.",
                    "priority": "low",
                }
            )
        )

        classification = {
            "classification": "working",
            "confidence": 0.6,
            "reasoning": "Agent might be slow",
        }
        context = {"pipeline_id": "issue-1489", "phase": "implement"}

        result = _run(decide_corrective_action(classification, context))

        # Non-infra should NOT bypass the ladder
        assert result["action"] == "nudge"
        assert result["priority"] == "low"


# ===================================================================
# TIER 1 + TIER 2: Integration and Error Event Handling
# ===================================================================


class TestInfraErrorViaErrorEvent:
    """Test infrastructure errors detected via ERROR events."""

    def test_infra_error_string_in_error_event(self):
        """ERROR event with infra error message should still be tracked."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=1)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Emit error event with infrastructure error content
        _emit_error(bus, "git push failed: permission denied", agent_id=AGENT_ID)

        # At minimum, the repeated-error threshold should catch it
        assert len(escalations) >= 1, (
            "Infrastructure error in ERROR event should trigger escalation "
            "(at minimum via repeated error threshold=1)"
        )


class TestInfraErrorPatternCoverage:
    """Verify specific infrastructure error patterns from the observed incident."""

    INFRA_ERROR_EXAMPLES = [
        "git add failed: The following paths are ignored by one of your .gitignore files",
        "EROFS: read-only file system, open '/egg-state/drafts/1481-plan.md'",
        "permission denied: cannot write to /home/egg/repos/egg/.egg-state/contracts",
        "403 Forbidden",
        f"gateway error: connection refused to egg-gateway:{GATEWAY_PORT}",
        "git push failed: remote rejected",
        "git commit failed: pre-commit hook error",
    ]

    NON_INFRA_EXAMPLES = [
        "waiting for code review",
        "test failed: assert 1 == 2",
        "compilation error: syntax error",
        "agent is processing large file",
        "timeout waiting for LLM response",
    ]

    @pytest.mark.parametrize("error_string", INFRA_ERROR_EXAMPLES)
    def test_infra_error_pattern_detected(self, error_string: str):
        """Known infrastructure error patterns should be detected."""
        try:
            from health_monitor import INFRA_ERROR_PATTERNS
        except ImportError:
            pytest.skip("INFRA_ERROR_PATTERNS not yet defined")

        matched = any(_pattern_search(pattern, error_string) for pattern in INFRA_ERROR_PATTERNS)
        assert matched, f"INFRA_ERROR_PATTERNS should match '{error_string}'"

    @pytest.mark.parametrize("error_string", NON_INFRA_EXAMPLES)
    def test_non_infra_pattern_not_detected(self, error_string: str):
        """Non-infrastructure error patterns should NOT be detected."""
        try:
            from health_monitor import INFRA_ERROR_PATTERNS
        except ImportError:
            pytest.skip("INFRA_ERROR_PATTERNS not yet defined")

        matched = any(_pattern_search(pattern, error_string) for pattern in INFRA_ERROR_PATTERNS)
        assert not matched, (
            f"INFRA_ERROR_PATTERNS should NOT match '{error_string}' (false positive)"
        )


# ===================================================================
# EXISTING FUNCTIONALITY REGRESSION
# ===================================================================


class TestExistingFunctionalityPreserved:
    """Ensure existing tripwire behaviour is not broken by changes."""

    def test_heartbeat_timeout_still_works(self):
        """Heartbeat timeout tripwire should still function."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

    def test_container_exit_still_works(self):
        """Container exit tripwire should still function."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        bus.emit(
            EventType.CONTAINER_STOPPED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": AGENT_ID, "exit_code": 137},
        )

        assert len(escalations) == 1
        assert escalations[0]["type"] == "hitl"

    def test_repeated_error_still_works(self):
        """Repeated error tripwire should still function."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        for _ in range(3):
            _emit_error(bus, "some repeated error")

        assert len(escalations) >= 1

    def test_message_rate_limit_still_works(self):
        """Message rate limit tripwire should still function."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_message_rate_limit=5)
        monitor = _make_monitor(bus, config)

        throttles: list[dict] = []
        monitor.on_throttle(lambda t: throttles.append(t))

        for _ in range(6):
            bus.emit(
                EventType.MESSAGE_SENT,
                pipeline_id=PIPELINE_ID,
                data={"agent_id": AGENT_ID, "content": "msg"},
            )

        assert len(throttles) >= 1

    def test_progress_stall_still_works(self):
        """Progress stall tripwire should still function."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
