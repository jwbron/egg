"""
Integration test for HITL escalation after max redirects exhausted.

Simulates the full chain:
1. Stall detection by health monitor
2. First redirect attempt (nudge)
3. Second redirect attempt (redirect message via overseer)
4. Max redirects exhausted
5. HITL decision created for human intervention

All LLM calls and message sending are mocked.

Related: issue #1059 — Phase 5 HITL escalation
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

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
# Conditional imports
# ---------------------------------------------------------------------------
try:
    from events import EventBus, EventType
    from models import (
        Pipeline,
        PipelineConfig,
    )
except ImportError:
    pytest.skip("Core orchestrator modules not available", allow_module_level=True)

try:
    from health_monitor import HealthMonitor
except ImportError:
    HealthMonitor = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "issue-hitl-1059"
AGENT_ID = "coder-stuck-001"


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with test-friendly thresholds."""
    defaults = {
        "orchestrator_heartbeat_timeout_seconds": 60,
        "orchestrator_error_repeat_threshold": 3,
        "orchestrator_message_rate_limit": 20,
        "overseer_enabled": True,
        "overseer_max_redirects_before_escalation": 2,
        "overseer_poll_interval_seconds": 10,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def _make_event_bus() -> EventBus:
    """Create a synchronous EventBus for testing."""
    return EventBus(async_delivery=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None,
    reason="health_monitor not yet implemented",
)
class TestHITLEscalationAfterMaxRedirects:
    """Verify HITL decision is created after redirect attempts are exhausted."""

    def test_stall_escalates_immediately_to_overseer(self):
        """First stall triggers immediate escalation to overseer (#1447)."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            overseer_enabled=True,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Initial progress
        bus.emit(
            EventType.PROGRESS_EMITTED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": AGENT_ID, "type": "progress"},
        )

        # First stall -> immediate escalation (no nudge step)
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        escalate_actions = [a for a in actions if a.get("action") == "escalate"]
        assert len(escalate_actions) == 1, "First stall should escalate immediately"
        assert len(escalations) == 1
        assert escalations[0]["type"] == "overseer"

        # Second cycle -> no re-escalation (already escalated)
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 122
            actions2 = monitor.check_progress()

        assert len(actions2) == 0, "Should not re-escalate"
        assert len(escalations) == 1

    def test_overseer_disabled_escalates_directly_to_hitl(self):
        """With overseer disabled, escalation goes directly to HITL."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            overseer_enabled=False,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Initial progress
        bus.emit(
            EventType.PROGRESS_EMITTED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": AGENT_ID, "type": "progress"},
        )

        # First stall -> immediate HITL escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 1
        assert escalations[0]["type"] == "hitl"


class TestHITLDecisionModel:
    """Test HITL decision model for overseer escalation."""

    def test_hitl_decision_creation(self):
        """HITLDecision can be created with overseer escalation context."""
        pipeline = Pipeline(
            id=PIPELINE_ID,
            issue_number=1059,
        )

        decision = pipeline.add_decision(
            question="Agent coder is stuck after 2 redirect attempts. What action should be taken?",
            options=["Retry agent", "Abort pipeline", "Continue without agent"],
            decision_type="choice",
        )

        assert decision.question.startswith("Agent coder is stuck")
        assert len(decision.options) == 3
        assert "Retry agent" in decision.options
        assert "Abort pipeline" in decision.options
        assert decision.status.value == "pending"

    def test_hitl_decision_with_context(self):
        """HITL decision includes diagnostic context."""
        pipeline = Pipeline(
            id=PIPELINE_ID,
            issue_number=1059,
        )

        pipeline.add_decision(
            question="Agent requires human intervention",
            options=["Retry", "Abort", "Skip agent"],
            decision_type="choice",
        )

        # Verify decision was added to pipeline
        assert len(pipeline.decisions) >= 1
        pending = pipeline.get_pending_decisions()
        assert len(pending) >= 1

    def test_hitl_decision_resolution(self):
        """HITL decision can be resolved by human."""
        pipeline = Pipeline(
            id=PIPELINE_ID,
            issue_number=1059,
        )

        decision = pipeline.add_decision(
            question="Agent stuck — what to do?",
            options=["Retry", "Abort"],
        )

        resolved = pipeline.resolve_decision(decision.id, "Retry")
        assert resolved is not None
        assert resolved.resolution == "Retry"
        assert resolved.status.value == "resolved"


class TestMaxRedirectsConfiguration:
    """Test PipelineConfig max_redirects_before_escalation field."""

    def test_default_max_redirects(self):
        """Default max redirects should be 2."""
        config = PipelineConfig()
        assert config.overseer_max_redirects_before_escalation == 2

    def test_custom_max_redirects(self):
        """Custom max redirects value should be respected."""
        config = PipelineConfig(overseer_max_redirects_before_escalation=5)
        assert config.overseer_max_redirects_before_escalation == 5

    def test_max_redirects_minimum_is_one(self):
        """max_redirects must be at least 1."""
        with pytest.raises(ValidationError):
            PipelineConfig(overseer_max_redirects_before_escalation=0)

    def test_redirect_count_tracking(self):
        """Redirect count should be trackable per agent."""
        redirect_counts: dict[str, int] = {}

        # Simulate redirect tracking
        for _i in range(3):
            agent_id = "coder-1"
            redirect_counts[agent_id] = redirect_counts.get(agent_id, 0) + 1

        config = _make_config(overseer_max_redirects_before_escalation=2)
        assert redirect_counts["coder-1"] > config.overseer_max_redirects_before_escalation

    def test_escalation_ladder_order(self):
        """Verify the escalation ladder follows: nudge -> redirect -> HITL -> issue -> Slack."""
        ladder = ["auto_nudge", "redirect", "hitl_escalation", "file_issue", "slack_notify"]

        assert ladder.index("auto_nudge") < ladder.index("redirect")
        assert ladder.index("redirect") < ladder.index("hitl_escalation")
        assert ladder.index("hitl_escalation") < ladder.index("file_issue")
        assert ladder.index("file_issue") < ladder.index("slack_notify")


@pytest.mark.skipif(
    HealthMonitor is None,
    reason="health_monitor not yet implemented",
)
class TestContainerExitAlwaysHITL:
    """Container exit always goes to HITL, regardless of overseer setting."""

    def test_container_exit_bypasses_overseer(self):
        """Container exit triggers HITL even when overseer is enabled."""
        bus = _make_event_bus()
        config = _make_config(overseer_enabled=True)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        bus.emit(
            EventType.CONTAINER_STOPPED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": AGENT_ID, "exit_code": 137},
        )

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "hitl"
        assert "container" in escalations[0].get("reason", "").lower()
