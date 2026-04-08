"""
Tests for the deterministic health monitor (tripwire processor).

Covers the five tripwire rules enforced by HealthMonitor:
1. Heartbeat timeout - escalate to overseer/HITL when no heartbeat within threshold
2. Container exit - immediate HITL escalation
3. Repeated identical errors - escalate after threshold
4. Message volume spike - auto-throttle above rate limit
5. Progress stall - escalate to overseer/HITL on stall detection

Related: issue #1059, #1447
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Import with resilience
# ---------------------------------------------------------------------------
try:
    from health_monitor import HealthMonitor
except ImportError:
    pytest.skip(
        "health_monitor module not yet implemented (issue #1059)",
        allow_module_level=True,
    )

from events import Event, EventBus, EventType
from models import PipelineConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "issue-1059"
AGENT_ID = "coder-abc123"
AGENT_ID_2 = "tester-def456"


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


def _emit_container_stopped(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
    exit_code: int = 1,
) -> Event:
    """Emit a container stopped event."""
    return event_bus.emit(
        EventType.CONTAINER_STOPPED,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "exit_code": exit_code},
    )


def _emit_message(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit a message sent event."""
    return event_bus.emit(
        EventType.MESSAGE_SENT,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "content": "test message"},
    )


def _emit_progress(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit a structured progress event."""
    return event_bus.emit(
        EventType.PROGRESS_EMITTED,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "type": "progress", "description": "working"},
    )


# ---------------------------------------------------------------------------
# Tests: Heartbeat timeout
# ---------------------------------------------------------------------------


class TestHeartbeatTimeout:
    """Tripwire #1: heartbeat timeout triggers immediate escalation."""

    def test_heartbeat_timeout_triggers_escalation(self):
        """When no heartbeat within threshold, monitor escalates immediately."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
        assert actions[0]["agent_id"] == AGENT_ID

    def test_heartbeat_within_threshold_no_alert(self):
        """Agent sends heartbeat just within threshold - no alert."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=120)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 119
            actions = monitor.check_heartbeats()

        assert len(actions) == 0


# ---------------------------------------------------------------------------
# Tests: Container exit
# ---------------------------------------------------------------------------


class TestContainerExit:
    """Tripwire #2: container exit triggers immediate HITL escalation."""

    def test_container_exit_creates_hitl_escalation(self):
        """CONTAINER_STOPPED event triggers immediate HITL escalation."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_container_stopped(bus, agent_id=AGENT_ID, exit_code=137)

        assert len(escalations) == 1
        esc = escalations[0]
        assert esc["type"] == "hitl"
        assert esc["agent_id"] == AGENT_ID
        assert "container" in esc.get("reason", "").lower()

    def test_container_exit_zero_still_escalates(self):
        """Even exit code 0 is unexpected if the pipeline is running."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_container_stopped(bus, agent_id=AGENT_ID, exit_code=0)

        # Container death during pipeline run should still escalate
        assert len(escalations) >= 1


# ---------------------------------------------------------------------------
# Tests: Repeated identical errors
# ---------------------------------------------------------------------------


class TestRepeatedErrors:
    """Tripwire #3: repeated identical errors trigger escalation."""

    def test_repeated_errors_at_threshold_triggers_escalation(self):
        """Emitting the same error N times (= threshold) triggers escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        error_msg = "connection refused to database"
        for _ in range(3):
            _emit_error(bus, error_msg)

        assert len(escalations) >= 1
        esc = escalations[0]
        assert esc["agent_id"] == AGENT_ID
        assert "error" in esc.get("reason", "").lower() or "repeat" in esc.get("reason", "").lower()

    def test_errors_below_threshold_no_escalation(self):
        """Fewer than threshold identical errors should NOT escalate."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        error_msg = "connection refused to database"
        for _ in range(2):
            _emit_error(bus, error_msg)

        assert len(escalations) == 0

    def test_different_errors_dont_trigger(self):
        """N different error messages should NOT trigger escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        for i in range(5):
            _emit_error(bus, f"unique error number {i}")

        assert len(escalations) == 0


# ---------------------------------------------------------------------------
# Tests: Message volume spike
# ---------------------------------------------------------------------------


class TestMessageVolumeSpike:
    """Tripwire #4: message volume exceeding rate limit triggers throttle."""

    def test_message_volume_over_limit_triggers_throttle(self):
        """Sending more than rate_limit messages in 1 minute triggers throttle."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_message_rate_limit=5)
        monitor = _make_monitor(bus, config)

        throttle_actions = []
        monitor.on_throttle(lambda t: throttle_actions.append(t))

        # Send 6 messages (exceeds limit of 5)
        for _ in range(6):
            _emit_message(bus, agent_id=AGENT_ID)

        assert len(throttle_actions) >= 1
        assert throttle_actions[0]["agent_id"] == AGENT_ID

    def test_message_volume_under_limit_no_throttle(self):
        """Sending fewer than rate_limit messages does NOT trigger throttle."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_message_rate_limit=20)
        monitor = _make_monitor(bus, config)

        throttle_actions = []
        monitor.on_throttle(lambda t: throttle_actions.append(t))

        for _ in range(19):
            _emit_message(bus, agent_id=AGENT_ID)

        assert len(throttle_actions) == 0


# ---------------------------------------------------------------------------
# Tests: Progress stall
# ---------------------------------------------------------------------------


class TestProgressStall:
    """Tripwire #5: no structured progress triggers immediate escalation."""

    def test_progress_stall_triggers_escalation(self):
        """No progress within threshold triggers immediate escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
        assert actions[0]["agent_id"] == AGENT_ID

    def test_progress_stall_resolved_no_second_escalation(self):
        """After escalation + agent recovery, no second escalation occurs."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_progress(bus, agent_id=AGENT_ID)

        # First stall -> escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 1

        # Agent resumes progress
        _emit_progress(bus, agent_id=AGENT_ID)

        # Check again - agent is active, no escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 30
            actions = monitor.check_progress()

        assert len(actions) == 0

    def test_progress_escalation_deduplication(self):
        """Escalation is not repeated on subsequent poll cycles."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(bus, agent_id=AGENT_ID)

        base = time.time()
        with patch("health_monitor.time") as mock_time:
            # First check at t=61 — should escalate
            mock_time.time.return_value = base + 61
            actions1 = monitor.check_progress()

            # Second check at t=90 — should NOT re-escalate
            mock_time.time.return_value = base + 90
            actions2 = monitor.check_progress()

            # Third check at t=200 — still should NOT re-escalate
            mock_time.time.return_value = base + 200
            actions3 = monitor.check_progress()

        assert len(actions1) == 1
        assert len(actions2) == 0
        assert len(actions3) == 0
        assert len(escalations) == 1

    def test_progress_re_escalates_after_recovery(self):
        """After recovery, a second stall triggers a fresh escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(bus, agent_id=AGENT_ID)

        # First stall -> escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 1

        # Agent recovers
        _emit_progress(bus, agent_id=AGENT_ID)

        # Second stall -> fresh escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 2


# ---------------------------------------------------------------------------
# Tests: Escalation routing (overseer vs HITL)
# ---------------------------------------------------------------------------


class TestEscalationRouting:
    """Escalation targets overseer when enabled, HITL when disabled."""

    def test_escalation_to_overseer_when_enabled(self):
        """With overseer_enabled=True, escalate to overseer."""
        bus = _make_event_bus()
        config = _make_config(
            overseer_enabled=True,
            orchestrator_error_repeat_threshold=2,
        )
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Trigger repeated error escalation
        for _ in range(2):
            _emit_error(bus, "same error", agent_id=AGENT_ID)

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "overseer"

    def test_escalation_to_hitl_when_overseer_disabled(self):
        """With overseer_enabled=False, escalate directly to HITL."""
        bus = _make_event_bus()
        config = _make_config(
            overseer_enabled=False,
            orchestrator_error_repeat_threshold=2,
        )
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Trigger repeated error escalation
        for _ in range(2):
            _emit_error(bus, "same error", agent_id=AGENT_ID)

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "hitl"

    def test_container_exit_always_hitl(self):
        """Container exit always goes to HITL regardless of overseer setting."""
        bus = _make_event_bus()
        config = _make_config(overseer_enabled=True)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_container_stopped(bus, agent_id=AGENT_ID)

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "hitl"


# ---------------------------------------------------------------------------
# Tests: Alert management
# ---------------------------------------------------------------------------


class TestAlertManagement:
    """Active alerts can be queried and resolved alerts are removed."""

    def test_active_alerts_queryable(self):
        """Alerts created by tripwires are queryable."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=2)
        monitor = _make_monitor(bus, config)

        # Trigger an alert via repeated errors
        for _ in range(2):
            _emit_error(bus, "db timeout", agent_id=AGENT_ID)

        alerts = monitor.get_active_alerts()
        assert len(alerts) >= 1
        assert any(a["agent_id"] == AGENT_ID for a in alerts)

    def test_resolved_alerts_removed(self):
        """When a condition resolves, the alert is removed from active list."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # Register agent
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Trigger heartbeat timeout alert
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_heartbeats()

        assert len(monitor.get_active_alerts()) >= 1

        # Agent sends heartbeat - alert should resolve
        _emit_heartbeat(bus, agent_id=AGENT_ID)
        monitor.resolve_alerts(AGENT_ID, "heartbeat_timeout")

        remaining = monitor.get_active_alerts()
        heartbeat_alerts = [
            a
            for a in remaining
            if a["agent_id"] == AGENT_ID and a.get("alert_type") == "heartbeat_timeout"
        ]
        assert len(heartbeat_alerts) == 0


# ---------------------------------------------------------------------------
# Tests: Multiple agents tracked independently
# ---------------------------------------------------------------------------


class TestMultiAgentTracking:
    """Each agent is tracked independently by the monitor."""

    def test_agents_tracked_independently(self):
        """Error count for one agent does not affect another."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Agent 1 sends 2 errors (below threshold)
        for _ in range(2):
            _emit_error(bus, "same error", agent_id=AGENT_ID)

        # Agent 2 sends 2 errors (below threshold)
        for _ in range(2):
            _emit_error(bus, "same error", agent_id=AGENT_ID_2)

        # Neither should have escalated
        assert len(escalations) == 0

    def test_one_agent_escalates_other_unaffected(self):
        """Only the agent exceeding the threshold triggers escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=3)
        monitor = _make_monitor(bus, config)

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Agent 1 hits threshold
        for _ in range(3):
            _emit_error(bus, "same error", agent_id=AGENT_ID)

        # Agent 2 stays below
        for _ in range(2):
            _emit_error(bus, "same error", agent_id=AGENT_ID_2)

        # Only agent 1 escalated
        assert len(escalations) == 1
        assert escalations[0]["agent_id"] == AGENT_ID

    def test_heartbeat_timeout_per_agent(self):
        """Heartbeat timeout only fires for the agent that stalled."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # Both agents send heartbeats
        _emit_heartbeat(bus, agent_id=AGENT_ID)
        _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        # Only agent 2 sends a fresh heartbeat within window
        with patch("health_monitor.time") as mock_time:
            now = time.time() + 61
            mock_time.time.return_value = now

            # Simulate agent 2 heartbeat at t=55 (within window)
            if hasattr(monitor, "_last_heartbeat"):
                monitor._last_heartbeat[AGENT_ID_2] = now - 10

            actions = monitor.check_heartbeats()

        escalated_agents = {a["agent_id"] for a in actions if a.get("action") == "escalate"}
        assert AGENT_ID in escalated_agents
        assert AGENT_ID_2 not in escalated_agents

    def test_message_rate_per_agent(self):
        """Message rate limit is tracked per agent."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_message_rate_limit=5)
        monitor = _make_monitor(bus, config)

        throttle_actions = []
        monitor.on_throttle(lambda t: throttle_actions.append(t))

        # Agent 1 sends 6 messages (over limit)
        for _ in range(6):
            _emit_message(bus, agent_id=AGENT_ID)

        # Agent 2 sends 3 messages (under limit)
        for _ in range(3):
            _emit_message(bus, agent_id=AGENT_ID_2)

        # Only agent 1 should be throttled
        throttled_agents = {t["agent_id"] for t in throttle_actions}
        assert AGENT_ID in throttled_agents
        assert AGENT_ID_2 not in throttled_agents


# ---------------------------------------------------------------------------
# Tests: Escalation on stall (#1447 — replaces nudge callbacks from #1428)
# ---------------------------------------------------------------------------


class TestEscalationOnStall:
    """Verify stall detection escalates immediately without nudging."""

    def test_heartbeat_timeout_fires_escalation_callback(self):
        """Escalation callback fires on heartbeat timeout."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_heartbeats()

        assert len(escalations) == 1
        assert escalations[0]["agent_id"] == AGENT_ID

    def test_progress_stall_fires_escalation_callback(self):
        """Escalation callback fires on first progress stall."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 1
        assert escalations[0]["agent_id"] == AGENT_ID

    def test_heartbeat_no_re_escalation_on_subsequent_cycles(self):
        """Subsequent poll cycles do not re-escalate the same stall."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        base = time.time()
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 61
            monitor.check_heartbeats()

            mock_time.time.return_value = base + 90
            monitor.check_heartbeats()

            mock_time.time.return_value = base + 200
            monitor.check_heartbeats()

        assert len(escalations) == 1

    def test_heartbeat_escalation_resets_on_heartbeat(self):
        """After heartbeat recovery, a new stall triggers fresh escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # First stall
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_heartbeats()

        assert len(escalations) == 1

        # Agent recovers
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Second stall
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_heartbeats()

        assert len(escalations) == 2

    def test_no_nudge_callbacks_exist(self):
        """HealthMonitor no longer has on_nudge — Tier 1 never nudges."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        assert not hasattr(monitor, "on_nudge")


# ---------------------------------------------------------------------------
# Tests: Alert resolution (#1428)
# ---------------------------------------------------------------------------


class TestAlertResolution:
    """Verify alerts can be resolved and don't accumulate."""

    def test_resolve_alerts_clears_matching(self):
        """resolve_alerts removes alerts matching agent_id and alert_type."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_heartbeats()

        alerts = monitor.get_active_alerts()
        assert len(alerts) >= 1

        monitor.resolve_alerts(AGENT_ID, "heartbeat_timeout")

        alerts_after = monitor.get_active_alerts()
        matching = [
            a
            for a in alerts_after
            if a["agent_id"] == AGENT_ID and a["alert_type"] == "heartbeat_timeout"
        ]
        assert len(matching) == 0


# ---------------------------------------------------------------------------
# Tests: Phase-aware stall detection (#1526)
# ---------------------------------------------------------------------------

REVIEWER_ID = "reviewer_code-abc123"
REVIEWER_ID_2 = "reviewer_contract-abc123"
PRODUCER_ID = "coder-abc123"


class TestImplementPhaseThreshold:
    """Task-1-1 / Task-1-2: implement phase uses a higher heartbeat threshold."""

    def test_implement_phase_uses_higher_threshold(self):
        """During implement phase, threshold is orchestrator_implement_heartbeat_timeout_seconds."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # At 200s — beyond default 120s but within implement 600s
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 200
            actions = monitor.check_heartbeats()

        assert len(actions) == 0, (
            "Should NOT escalate at 200s during implement phase (threshold=600s)"
        )

    def test_implement_phase_escalates_beyond_threshold(self):
        """During implement phase, escalation fires after implement threshold."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 601
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
        assert actions[0]["agent_id"] == AGENT_ID

    def test_non_implement_phase_uses_default_threshold(self):
        """During non-implement phases, default threshold is used."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("plan")

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 121
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

    def test_no_phase_set_uses_default_threshold(self):
        """When no phase is set, default threshold is used."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        # No set_current_phase call

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 121
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Should escalate at 121s with default threshold (120s)"

    def test_implement_phase_progress_stall_uses_higher_threshold(self):
        """Progress stall also respects phase-aware threshold during implement."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 200
            actions = monitor.check_progress()

        assert len(actions) == 0, "Progress stall should not fire at 200s during implement phase"

    def test_implement_phase_progress_stall_escalates_beyond_threshold(self):
        """Progress stall fires after implement threshold."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 601
            actions = monitor.check_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

    def test_phase_transition_changes_threshold(self):
        """Switching phase changes the effective threshold."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)

        # Start in implement phase - no escalation at 200s
        monitor.set_current_phase("implement")
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 200
            actions = monitor.check_heartbeats()
        assert len(actions) == 0

        # Switch to PR phase - should escalate at 200s
        monitor.set_current_phase("pr")
        _emit_heartbeat(bus, agent_id=AGENT_ID)  # Reset heartbeat

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 121
            actions = monitor.check_heartbeats()
        assert len(actions) == 1

    def test_set_current_phase_method_exists(self):
        """HealthMonitor has set_current_phase method."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)
        assert hasattr(monitor, "set_current_phase")
        assert callable(monitor.set_current_phase)

    def test_get_heartbeat_threshold_private_method(self):
        """_get_heartbeat_threshold returns correct value per phase."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)

        # No phase set - default
        assert monitor._get_heartbeat_threshold() == 120

        # Implement phase
        monitor.set_current_phase("implement")
        assert monitor._get_heartbeat_threshold() == 600

        # Other phases
        for phase in ["plan", "refine", "pr"]:
            monitor.set_current_phase(phase)
            assert monitor._get_heartbeat_threshold() == 120, f"Phase {phase} should use default"


class TestPipelineConfigImplementThreshold:
    """Task-1-1: PipelineConfig has orchestrator_implement_heartbeat_timeout_seconds."""

    def test_field_exists_with_default_600(self):
        """orchestrator_implement_heartbeat_timeout_seconds defaults to 600."""
        config = PipelineConfig()
        assert config.orchestrator_implement_heartbeat_timeout_seconds == 600

    def test_field_accepts_custom_value(self):
        """Custom values are accepted for implement threshold."""
        config = PipelineConfig(orchestrator_implement_heartbeat_timeout_seconds=300)
        assert config.orchestrator_implement_heartbeat_timeout_seconds == 300

    def test_field_validation_minimum_10(self):
        """Value must be >= 10."""
        with pytest.raises(ValueError):
            PipelineConfig(orchestrator_implement_heartbeat_timeout_seconds=5)

    def test_field_validation_accepts_10(self):
        """Value of exactly 10 is accepted."""
        config = PipelineConfig(orchestrator_implement_heartbeat_timeout_seconds=10)
        assert config.orchestrator_implement_heartbeat_timeout_seconds == 10


class TestBRCIdleSuppression:
    """Task-1-3: BRC-idle agents are excluded from heartbeat/progress alerts."""

    def _make_mock_tracker(
        self,
        *,
        reviewer_roles: list[str] | None = None,
        producer_roles: list[str] | None = None,
        producer_phases: dict | None = None,
        producers_for_reviewer: dict[str, list[str]] | None = None,
    ):
        """Create a mock PeerConsensusTracker with a ReviewGraph."""
        import threading

        from egg_orchestrator.types import ConsensusPhase

        reviewer_roles = reviewer_roles or []
        producer_roles = producer_roles or []
        producer_phases = producer_phases or {}
        producers_for_reviewer = producers_for_reviewer or {}

        mock_tracker = MagicMock()
        mock_tracker._lock = threading.RLock()
        mock_tracker._producer_phases = dict.fromkeys(producer_roles, ConsensusPhase.WORKING)
        mock_tracker._producer_phases.update(producer_phases)

        # Create a mock graph
        mock_graph = MagicMock()
        mock_graph.is_reviewer.side_effect = lambda r: r in reviewer_roles
        mock_graph.is_producer.side_effect = lambda r: r in producer_roles
        mock_graph.producers_for.side_effect = lambda r: producers_for_reviewer.get(r, [])
        mock_tracker.graph = mock_graph

        return mock_tracker

    def test_reviewer_only_suppressed_when_producers_working(self):
        """Reviewer-only agent with all producers in WORKING is suppressed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.WORKING},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 0, "BRC-idle reviewer should be suppressed"

    def test_reviewer_not_suppressed_when_producer_proposed(self):
        """Reviewer is NOT suppressed when a producer has proposed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.PROPOSED},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Reviewer should escalate when producer has proposed"

    def test_dual_role_agent_not_suppressed(self):
        """Dual-role agent (producer + reviewer) is NOT suppressed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        dual_role_id = "tester-xyz"
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[dual_role_id],
            producer_roles=[dual_role_id, PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.WORKING},
            producers_for_reviewer={dual_role_id: [PRODUCER_ID]},
        )

        _emit_heartbeat(bus, agent_id=dual_role_id)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Dual-role agent should NOT be suppressed"

    def test_brc_suppression_on_progress_check(self):
        """BRC-idle suppression also applies to progress stall checks."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.WORKING},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

        _emit_progress(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 0, "BRC-idle reviewer should be suppressed in progress check"

    def test_no_tracker_means_no_suppression(self):
        """When no peer consensus tracker exists, no suppression occurs."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=None,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Without tracker, should escalate normally"

    def test_reviewer_with_multiple_producers_partial_proposed(self):
        """Reviewer with multiple producers — one proposed, one working — is NOT suppressed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        producer_2 = "documenter-xyz"
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID, producer_2],
            producer_phases={
                PRODUCER_ID: ConsensusPhase.PROPOSED,
                producer_2: ConsensusPhase.WORKING,
            },
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID, producer_2]},
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Reviewer should escalate when at least one producer has proposed"

    def test_reviewer_with_multiple_producers_all_working(self):
        """Reviewer with multiple producers all working — IS suppressed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        producer_2 = "documenter-xyz"
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID, producer_2],
            producer_phases={
                PRODUCER_ID: ConsensusPhase.WORKING,
                producer_2: ConsensusPhase.WORKING,
            },
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID, producer_2]},
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 0, "Reviewer should be suppressed when all producers are working"

    def test_producer_not_suppressed_even_when_working(self):
        """A pure producer agent is never suppressed by BRC-idle logic."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.WORKING},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Producer should never be suppressed by BRC-idle"

    def test_brc_suppression_resumes_monitoring_after_proposal(self):
        """After producer proposes, previously suppressed reviewer resumes monitoring."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # Phase 1: producer is WORKING — reviewer suppressed
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.WORKING},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()
        assert len(actions) == 0, "Reviewer should be suppressed initially"

        # Phase 2: producer transitions to PROPOSED — reviewer should be monitored
        mock_tracker._producer_phases[PRODUCER_ID] = ConsensusPhase.PROPOSED

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()
        assert len(actions) == 1, "Reviewer should resume monitoring after proposal"


class TestCombinedPhaseAndBRCSuppression:
    """Test interaction between phase-aware thresholds and BRC-idle suppression."""

    def test_implement_phase_with_brc_suppression(self):
        """During implement phase, both features work together."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        # Setup BRC tracker
        mock_tracker = MagicMock()
        mock_tracker._lock = __import__("threading").RLock()
        mock_tracker._producer_phases = {PRODUCER_ID: ConsensusPhase.WORKING}
        mock_graph = MagicMock()
        mock_graph.is_reviewer.side_effect = lambda r: r == REVIEWER_ID
        mock_graph.is_producer.side_effect = lambda r: r == PRODUCER_ID
        mock_graph.producers_for.side_effect = lambda r: [PRODUCER_ID] if r == REVIEWER_ID else []
        mock_tracker.graph = mock_graph

        # Both agents emit heartbeats
        _emit_heartbeat(bus, agent_id=PRODUCER_ID)
        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        # At 200s: producer within implement threshold, reviewer BRC-idle
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 200
            actions = monitor.check_heartbeats()

        assert len(actions) == 0, "Producer within threshold, reviewer BRC-idle"

    def test_implement_producer_exceeds_threshold_reviewer_idle(self):
        """Producer exceeds implement threshold while reviewer is BRC-idle."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        mock_tracker = MagicMock()
        mock_tracker._lock = __import__("threading").RLock()
        mock_tracker._producer_phases = {PRODUCER_ID: ConsensusPhase.WORKING}
        mock_graph = MagicMock()
        mock_graph.is_reviewer.side_effect = lambda r: r == REVIEWER_ID
        mock_graph.is_producer.side_effect = lambda r: r == PRODUCER_ID
        mock_graph.producers_for.side_effect = lambda r: [PRODUCER_ID] if r == REVIEWER_ID else []
        mock_tracker.graph = mock_graph

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)
        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        # At 601s: producer exceeds implement threshold, reviewer still suppressed
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = time.time() + 601
            actions = monitor.check_heartbeats()

        # Only producer should escalate — reviewer is BRC-idle
        escalated_agents = {a["agent_id"] for a in actions}
        assert PRODUCER_ID in escalated_agents, "Producer should escalate at 601s"
        assert REVIEWER_ID not in escalated_agents, "BRC-idle reviewer should still be suppressed"
