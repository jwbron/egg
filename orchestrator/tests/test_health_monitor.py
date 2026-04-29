"""
Tests for the deterministic health monitor (tripwire processor).

Covers the six tripwire rules enforced by HealthMonitor:
1. Heartbeat timeout - escalate to overseer/HITL when no heartbeat within threshold
2. Container exit - immediate HITL escalation
3. Repeated identical errors - escalate after threshold
4. Message volume spike - auto-throttle above rate limit
5. Progress stall - escalate to overseer/HITL on stall detection
6. Infrastructure error - escalate on blocked progress with infra error keywords

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
# Tests: Container-activity suppression (issue #2190)
# ---------------------------------------------------------------------------


def _emit_container_activity(
    event_bus: EventBus,
    agent_id: str = AGENT_ID,
    pipeline_id: str = PIPELINE_ID,
    kind: str = "git_commit",
) -> Event:
    """Emit a CONTAINER_ACTIVITY event for an agent."""
    return event_bus.emit(
        EventType.CONTAINER_ACTIVITY,
        pipeline_id=pipeline_id,
        data={"agent_role": agent_id, "kind": kind},
    )


class TestContainerActivitySuppression:
    """Issue #2190: a fresh CONTAINER_ACTIVITY event suppresses heartbeat
    and progress stall alerts even when bus-level HEARTBEATs are absent.

    Repro from the issue: a coder mid-pytest is making commits but not
    emitting heartbeats during a 10-minute blocking ``TaskOutput`` call;
    the detector previously fired ``agent-heartbeat-stall`` and
    recommended container restart, which would destroy in-flight work.
    """

    def test_recent_activity_suppresses_heartbeat_alert(self):
        """Activity within the quiet window defers the heartbeat alert."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Heartbeat is stale (61s old) but a fresh activity event arrives.
        with patch("health_monitor.time") as mock_time:
            base = time.time()
            mock_time.time.return_value = base + 61
            _emit_container_activity(bus, agent_id=AGENT_ID)

            actions = monitor.check_heartbeats()

        assert actions == []

        # And the agent is not flagged escalated, so a later poll
        # (after activity has gone stale) still escalates.
        with patch("health_monitor.time") as mock_time:
            base = time.time()
            mock_time.time.return_value = base + 1000
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["agent_id"] == AGENT_ID

    def test_recent_activity_suppresses_progress_alert(self):
        """Same suppression applies to the progress stall detector."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            base = time.time()
            mock_time.time.return_value = base + 61
            _emit_container_activity(bus, agent_id=AGENT_ID)

            actions = monitor.check_progress()

        assert actions == []

    def test_stale_activity_does_not_suppress(self):
        """An activity event older than the quiet window does not suppress."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)
        _emit_container_activity(bus, agent_id=AGENT_ID)

        # Both heartbeat and activity are now > heartbeat threshold (60s)
        # AND > activity quiet window (120s).
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 200
            actions = monitor.check_heartbeats()

        assert len(actions) == 1

    def test_no_activity_event_does_not_suppress(self):
        """An agent that has never emitted CONTAINER_ACTIVITY still escalates
        on the heartbeat anchor — last_activity defaults to 0.0."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1

    def test_disabled_gate_no_suppression(self):
        """``orchestrator_activity_quiet_seconds=0`` disables the gate.

        Even a CONTAINER_ACTIVITY event one second old must NOT suppress
        a heartbeat alert — operators set the threshold to 0 as an
        escape hatch when the gate is producing false negatives.
        """
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=0,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            base = time.time()
            # Heartbeat is stale (61s old) and a brand-new activity event
            # arrives — but the gate is disabled, so the alert still fires.
            mock_time.time.return_value = base + 61
            _emit_container_activity(bus, agent_id=AGENT_ID)
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["agent_id"] == AGENT_ID

    def test_activity_event_for_other_pipeline_ignored(self):
        """CONTAINER_ACTIVITY for a different pipeline does not suppress."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)
        _emit_container_activity(bus, agent_id=AGENT_ID, pipeline_id="some-other-pipeline")

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1

    def test_activity_event_for_other_agent_does_not_suppress_focal(self):
        """Activity from a peer agent does not suppress an agent's own alert
        (focal-agent gate is per-agent; peer signals belong to #2242)."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_activity_quiet_seconds=120,
        )
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)
        _emit_heartbeat(bus, agent_id=AGENT_ID_2)
        _emit_container_activity(bus, agent_id=AGENT_ID_2)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        # AGENT_ID has no activity of its own → escalates.
        agent_ids = {a["agent_id"] for a in actions}
        assert AGENT_ID in agent_ids


# ---------------------------------------------------------------------------
# Tests: reset_agent (issue #2084)
# ---------------------------------------------------------------------------


class TestResetAgent:
    """``reset_agent`` drops per-agent state so a respawned container is not
    judged against the prior container's heartbeat clock."""

    def test_reset_clears_heartbeat_anchor(self):
        """After reset, an agent that was beyond threshold no longer escalates."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)
        assert AGENT_ID in monitor._last_heartbeat
        assert AGENT_ID in monitor._agents

        # Without reset, the next check would fire — confirm baseline.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            pre_reset_actions = monitor.check_heartbeats()
        assert len(pre_reset_actions) == 1

        # Reset and verify the next check (with no fresh heartbeat) does not
        # synthesize an alert from the dead anchor.
        monitor.reset_agent(AGENT_ID)
        assert AGENT_ID not in monitor._last_heartbeat
        assert AGENT_ID not in monitor._agents

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 200
            post_reset_actions = monitor.check_heartbeats()

        assert post_reset_actions == []

    def test_reset_drops_active_alerts_for_role(self):
        """Active alerts pinned to the reset agent are removed; others kept."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        # Manually seed two alerts.
        monitor._active_alerts.append(
            {"id": "1", "agent_id": AGENT_ID, "alert_type": "heartbeat_timeout"}
        )
        monitor._active_alerts.append(
            {"id": "2", "agent_id": AGENT_ID_2, "alert_type": "heartbeat_timeout"}
        )

        monitor.reset_agent(AGENT_ID)

        remaining = list(monitor._active_alerts)
        assert len(remaining) == 1
        assert remaining[0]["agent_id"] == AGENT_ID_2

    def test_reset_unknown_agent_is_noop(self):
        """Resetting an agent that was never tracked must not raise."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        # No state, no exception.
        monitor.reset_agent("never-existed")
        assert "never-existed" not in monitor._agents


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
        # Disable the alive-signal gate (#2242) so this test isolates the
        # per-agent isolation behavior from the peer-progress deferral.
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=0,
        )
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


def _make_mock_tracker(
    *,
    reviewer_roles: list[str] | None = None,
    producer_roles: list[str] | None = None,
    producer_phases: dict | None = None,
    producers_for_reviewer: dict[str, list[str]] | None = None,
    proposal_timestamps: dict[str, float] | None = None,
):
    """Create a mock PeerConsensusTracker with a ReviewGraph.

    The mock supports ``are_all_producers_working()`` and
    ``get_earliest_proposal_time()`` via the public API (not private
    internals).  The returned mock exposes an ``_effective_phases``
    dict that tests can mutate to simulate phase transitions.

    Args:
        proposal_timestamps: Mapping of producer role -> epoch timestamp
            of proposal. Used by ``get_earliest_proposal_time()``.
    """
    from egg_orchestrator.types import ConsensusPhase

    reviewer_roles = reviewer_roles or []
    producer_roles = producer_roles or []
    producer_phases = producer_phases or {}
    producers_for_reviewer = producers_for_reviewer or {}
    proposal_timestamps = proposal_timestamps or {}

    effective_phases: dict[str, ConsensusPhase] = dict.fromkeys(
        producer_roles, ConsensusPhase.WORKING
    )
    effective_phases.update(producer_phases)

    mock_tracker = MagicMock()

    # Expose phases for mutation by phase-transition tests
    mock_tracker._effective_phases = effective_phases

    def _are_all_working(reviewer: str) -> bool:
        producers = producers_for_reviewer.get(reviewer, [])
        if not producers:
            return False
        return all(effective_phases.get(p) == ConsensusPhase.WORKING for p in producers)

    mock_tracker.are_all_producers_working.side_effect = _are_all_working

    def _get_earliest_proposal_time(reviewer: str) -> float | None:
        producers = producers_for_reviewer.get(reviewer, [])
        timestamps = [proposal_timestamps[p] for p in producers if p in proposal_timestamps]
        if not timestamps:
            return None
        return min(timestamps)

    mock_tracker.get_earliest_proposal_time.side_effect = _get_earliest_proposal_time

    # Create a mock graph
    mock_graph = MagicMock()
    mock_graph.is_reviewer.side_effect = lambda r: r in reviewer_roles
    mock_graph.is_producer.side_effect = lambda r: r in producer_roles
    mock_graph.producers_for.side_effect = lambda r: producers_for_reviewer.get(r, [])
    mock_tracker.graph = mock_graph

    return mock_tracker


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

    def test_get_current_phase_round_trips(self):
        """get_current_phase returns the value last set via set_current_phase (#2079)."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)
        assert monitor.get_current_phase() is None
        monitor.set_current_phase("implement")
        assert monitor.get_current_phase() == "implement"
        monitor.set_current_phase("plan")
        assert monitor.get_current_phase() == "plan"

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
        proposal_timestamps: dict[str, float] | None = None,
    ):
        """Create a mock PeerConsensusTracker with a ReviewGraph."""
        return _make_mock_tracker(
            reviewer_roles=reviewer_roles,
            producer_roles=producer_roles,
            producer_phases=producer_phases,
            producers_for_reviewer=producers_for_reviewer,
            proposal_timestamps=proposal_timestamps,
        )

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
        """Reviewer is NOT suppressed when a producer proposed PAST grace period."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.PROPOSED},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
            proposal_timestamps={PRODUCER_ID: now - 400},  # well past 300s grace
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Reviewer should escalate when producer proposed past grace"

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
        """Reviewer with multiple producers — one proposed (past grace), one working — NOT suppressed."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        producer_2 = "documenter-xyz"
        mock_tracker = self._make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID, producer_2],
            producer_phases={
                PRODUCER_ID: ConsensusPhase.PROPOSED,
                producer_2: ConsensusPhase.WORKING,
            },
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID, producer_2]},
            proposal_timestamps={PRODUCER_ID: now - 400},  # past grace
        )

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, (
            "Reviewer should escalate when at least one producer has proposed past grace"
        )

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
        """After producer proposes and grace expires, previously suppressed reviewer is monitored."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()

        # Phase 1: producer is WORKING — reviewer suppressed
        mock_tracker = _make_mock_tracker(
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
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()
        assert len(actions) == 0, "Reviewer should be suppressed initially"

        # Phase 2: producer transitions to PROPOSED, grace expired
        mock_tracker._effective_phases[PRODUCER_ID] = ConsensusPhase.PROPOSED
        # Set proposal timestamp well past grace
        mock_tracker.get_earliest_proposal_time.side_effect = lambda r: now - 400

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()
        assert len(actions) == 1, "Reviewer should resume monitoring after grace expires"


class TestCombinedPhaseAndBRCSuppression:
    """Test interaction between phase-aware thresholds and BRC-idle suppression."""

    def test_implement_phase_with_brc_suppression(self):
        """During implement phase, both features work together."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        mock_tracker = _make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

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
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=120,
            orchestrator_implement_heartbeat_timeout_seconds=600,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("implement")

        mock_tracker = _make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )

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


# ---------------------------------------------------------------------------
# Tests: Post-propose grace period (#1613 — task-1-3)
# ---------------------------------------------------------------------------


class TestPostProposeGrace:
    """Reviewer-only agents within post-propose grace period are suppressed.

    After a producer proposes (CONSENSUS_PROPOSE), reviewers need time to
    analyse the proposal before producing BRC messages. During this grace
    window, heartbeat/progress stall checks should NOT flag the reviewer.
    """

    def _make_tracker_with_proposal(
        self,
        *,
        proposal_epoch: float,
        reviewer: str = REVIEWER_ID,
        producer: str = PRODUCER_ID,
    ):
        """Create a mock tracker where the producer has already proposed."""
        from egg_orchestrator.types import ConsensusPhase

        mock_tracker = _make_mock_tracker(
            reviewer_roles=[reviewer],
            producer_roles=[producer],
            producer_phases={producer: ConsensusPhase.PROPOSED},
            producers_for_reviewer={reviewer: [producer]},
            proposal_timestamps={producer: proposal_epoch},
        )
        return mock_tracker

    def test_reviewer_within_grace_suppressed_heartbeat(self):
        """Reviewer within post-propose grace is NOT flagged by check_heartbeats."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        # Producer proposed 100s ago — within 300s grace
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=now - 100)

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 0, "Reviewer within post-propose grace should be suppressed"

    def test_reviewer_within_grace_suppressed_progress(self):
        """Reviewer within post-propose grace is NOT flagged by check_progress."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=now - 100)

        _emit_progress(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_progress()

        assert len(actions) == 0, "Reviewer within post-propose grace should be suppressed"

    def test_reviewer_past_grace_is_flagged(self):
        """Reviewer PAST post-propose grace IS flagged by check_heartbeats."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        # Producer proposed 400s ago — past 300s grace
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=now - 400)

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Reviewer past grace period should be flagged"
        assert actions[0]["agent_id"] == REVIEWER_ID

    def test_reviewer_past_grace_flagged_progress(self):
        """Reviewer past grace is flagged by check_progress too."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=now - 400)

        _emit_progress(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_progress()

        assert len(actions) == 1, "Reviewer past grace should be flagged by progress check"

    def test_grace_boundary_exactly_at_threshold(self):
        """At the exact grace boundary, reviewer should NOT be suppressed."""
        bus = _make_event_bus()
        grace = 300
        hb_timeout = 60
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=hb_timeout,
            post_proposal_grace_seconds=grace,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        # Set mock_time so heartbeat is stale (past hb_timeout)
        check_time = base + hb_timeout + 1
        # Set proposal_epoch so that check_time - proposal_epoch == grace exactly
        proposal_epoch = check_time - grace
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=proposal_epoch)

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # time.time() - proposal_epoch == grace exactly, so NOT < grace
            mock_time.time.return_value = check_time
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "At exact grace boundary, reviewer should be flagged"

    def test_custom_grace_period_config(self):
        """Custom grace period value is respected."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=60,
        )
        monitor = _make_monitor(bus, config)

        now = time.time()
        # Proposed 80s ago — past custom 60s grace
        mock_tracker = self._make_tracker_with_proposal(proposal_epoch=now - 80)

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Past custom 60s grace should be flagged"

    def test_no_proposals_means_not_suppressed(self):
        """If no upstream producers have proposed, grace period doesn't apply."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        # PROPOSED phase but no proposal_timestamps → get_earliest_proposal_time returns None
        mock_tracker = _make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.PROPOSED},
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
            # No proposal_timestamps → returns None
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

        assert len(actions) == 1, "Without proposals, grace doesn't apply"

    def test_dual_role_not_suppressed_by_grace(self):
        """Dual-role agents (producer + reviewer) are NOT suppressed by grace."""
        from egg_orchestrator.types import ConsensusPhase

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        dual_id = "tester-xyz"
        now = time.time()
        # Even with a recent proposal, dual-role should not be suppressed
        mock_tracker = _make_mock_tracker(
            reviewer_roles=[dual_id],
            producer_roles=[dual_id, PRODUCER_ID],
            producer_phases={PRODUCER_ID: ConsensusPhase.PROPOSED},
            producers_for_reviewer={dual_id: [PRODUCER_ID]},
            proposal_timestamps={PRODUCER_ID: now - 10},
        )

        _emit_heartbeat(bus, agent_id=dual_id)

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Dual-role agent should NOT be suppressed by grace"


# ---------------------------------------------------------------------------
# Tests: BRC progress check — post-ACK confirmation timeout (#1613 — task-1-4)
# ---------------------------------------------------------------------------


class TestBRCProgressCheck:
    """check_brc_progress escalates fully-ACKed producers that don't confirm."""

    def _make_tracker_with_fully_acked(
        self,
        *,
        fully_acked: dict[str, float] | None = None,
    ):
        """Create a mock tracker with configurable fully_acked producers."""
        mock_tracker = MagicMock()
        mock_tracker.get_fully_acked_producers.return_value = fully_acked or {}
        return mock_tracker

    def test_escalates_stuck_producer(self):
        """Fully-ACKed producer past timeout triggers escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Register the producer so it has an AgentState
        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time() - 200},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # First call at base: records first-seen time as base
            mock_time.time.return_value = base
            monitor.check_brc_progress()

            # Second call 181s later: past 180s timeout
            mock_time.time.return_value = base + 181
            actions = monitor.check_brc_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
        assert actions[0]["agent_id"] == PRODUCER_ID
        assert "brc_confirmation_timeout" in actions[0].get("alert_type", "")
        assert len(escalations) >= 1

    def test_within_timeout_no_escalation(self):
        """Fully-ACKed producer within timeout is NOT escalated."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=mock_tracker,
        ):
            actions = monitor.check_brc_progress()

        assert len(actions) == 0, "Within timeout, should NOT escalate"
        assert len(escalations) == 0

    def test_resets_on_confirm(self):
        """Producer that confirms (leaves fully-acked set) clears tracking."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        # Phase 1: producer is fully-acked
        tracker_acked = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker_acked,
        ):
            monitor.check_brc_progress()

        # Verify tracking is active
        assert PRODUCER_ID in monitor._fully_acked_first_seen

        # Phase 2: producer confirms — no longer in fully-acked set
        tracker_empty = self._make_tracker_with_fully_acked(fully_acked={})
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker_empty,
        ):
            monitor.check_brc_progress()

        # Tracking should be cleaned up
        assert PRODUCER_ID not in monitor._fully_acked_first_seen

    def test_escalation_deduplication(self):
        """Already-escalated producer is NOT re-escalated on subsequent checks."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time() - 200},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # First call: record first-seen
            mock_time.time.return_value = base
            monitor.check_brc_progress()

            # Second call at base+181: escalate
            mock_time.time.return_value = base + 181
            actions1 = monitor.check_brc_progress()

            # Third check — should NOT re-escalate
            mock_time.time.return_value = base + 300
            actions2 = monitor.check_brc_progress()

            # Fourth check — still should NOT re-escalate
            mock_time.time.return_value = base + 500
            actions3 = monitor.check_brc_progress()

        assert len(actions1) == 1
        assert len(actions2) == 0
        assert len(actions3) == 0
        assert len(escalations) == 1

    def test_re_escalation_after_confirm_and_re_ack(self):
        """After confirm + re-ack cycle, a new timeout triggers fresh escalation."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        base = time.time()

        # Phase 1: fully-acked → register first-seen → timeout → escalation
        tracker_acked = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: base},
        )
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker_acked,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 1

        # Phase 2: producer confirms (leaves fully-acked)
        tracker_empty = self._make_tracker_with_fully_acked(fully_acked={})
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker_empty,
        ):
            monitor.check_brc_progress()

        # Phase 3: producer gets re-acked after new proposal round
        new_base = base + 400
        tracker_reacked = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: new_base},
        )
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker_reacked,
            ),
        ):
            # Record new first-seen
            mock_time.time.return_value = new_base
            monitor.check_brc_progress()

            # Within timeout — no escalation
            mock_time.time.return_value = new_base + 100
            monitor.check_brc_progress()

            # Past timeout — fresh escalation
            mock_time.time.return_value = new_base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 2, "Should get a fresh escalation after confirm/re-ack"

    def test_multiple_producers_only_timed_out_escalated(self):
        """Only producers past timeout are escalated; within-timeout ones are not."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        producer_2 = "documenter-xyz"
        _emit_heartbeat(bus, agent_id=PRODUCER_ID)
        _emit_heartbeat(bus, agent_id=producer_2)

        base = time.time()

        # Both fully-acked initially
        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={
                PRODUCER_ID: base,
                producer_2: base,
            },
        )

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # First check: registers both at base
            mock_time.time.return_value = base
            actions = monitor.check_brc_progress()
            assert len(actions) == 0

            # Both past timeout
            mock_time.time.return_value = base + 181
            actions = monitor.check_brc_progress()

        assert len(actions) == 2, "Both producers past timeout should escalate"
        escalated = {a["agent_id"] for a in actions}
        assert PRODUCER_ID in escalated
        assert producer_2 in escalated

    def test_no_tracker_returns_empty(self):
        """Without a PeerConsensusTracker, check_brc_progress returns empty."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            actions = monitor.check_brc_progress()

        assert actions == []

    def test_escalation_callback_fires(self):
        """Escalation callback is invoked on BRC confirmation timeout."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 1
        esc = escalations[0]
        assert esc["agent_id"] == PRODUCER_ID
        assert (
            "fully acked" in esc.get("reason", "").lower()
            or "confirmed" in esc.get("reason", "").lower()
        )

    def test_alert_created_on_escalation(self):
        """An active alert is created when BRC confirmation timeout fires."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        alerts = monitor.get_active_alerts()
        brc_alerts = [a for a in alerts if a.get("alert_type") == "brc_confirmation_timeout"]
        assert len(brc_alerts) >= 1
        assert brc_alerts[0]["agent_id"] == PRODUCER_ID

    def test_wired_into_check_tripwires(self):
        """check_brc_progress is called as part of check_tripwires."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_tripwires()  # records first-seen

            mock_time.time.return_value = base + 181
            actions = monitor.check_tripwires()

        brc_actions = [a for a in actions if a.get("alert_type") == "brc_confirmation_timeout"]
        assert len(brc_actions) >= 1, "check_brc_progress should be part of check_tripwires"

    def test_escalation_routes_to_overseer_when_enabled(self):
        """BRC confirmation timeout routes to overseer when enabled."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_post_ack_confirmation_timeout_seconds=180,
            overseer_enabled=True,
        )
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 1
        assert escalations[0]["type"] == "overseer"

    def test_escalation_routes_to_hitl_when_overseer_disabled(self):
        """BRC confirmation timeout routes to HITL when overseer disabled."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_post_ack_confirmation_timeout_seconds=180,
            overseer_enabled=False,
        )
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 1
        assert escalations[0]["type"] == "hitl"

    def test_escalation_includes_alert_type_and_elapsed(self):
        """Escalation dict carries alert_type + elapsed_seconds for callbacks (#2079)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        assert len(escalations) == 1
        esc = escalations[0]
        assert esc["alert_type"] == "brc_confirmation_timeout"
        assert esc["elapsed_seconds"] == 181

    def test_breadcrumb_logged_on_each_observation(self):
        """Per-iteration breadcrumb makes the check visible in container logs (#2079)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        with (
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
            patch("health_monitor.logger") as mock_logger,
        ):
            monitor.check_brc_progress()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "BRC progress check observed fully-acked producers" in c.args[0]
        ]
        assert len(info_calls) == 1, (
            "Expected one breadcrumb info log per check_brc_progress call "
            "with fully-acked producers"
        )
        # Breadcrumb must include producer + elapsed metadata for post-mortems.
        kwargs = info_calls[0].kwargs
        assert kwargs.get("pipeline_id") == PIPELINE_ID
        assert PRODUCER_ID in kwargs.get("producers", {})

    def test_no_breadcrumb_when_set_empty(self):
        """No breadcrumb log when no producers are fully-acked."""
        bus = _make_event_bus()
        monitor = _make_monitor(bus)

        mock_tracker = self._make_tracker_with_fully_acked(fully_acked={})

        with (
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
            patch("health_monitor.logger") as mock_logger,
        ):
            monitor.check_brc_progress()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "BRC progress check observed fully-acked producers" in c.args[0]
        ]
        assert info_calls == [], "Should not log breadcrumb on empty fully-acked set"

    def test_warns_when_skipping_producer_without_agent_state(self):
        """Warn loudly when a fully-acked producer past timeout has no agent_state (#2079)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        # Deliberately do NOT emit a heartbeat — producer has no AgentState.
        mock_tracker = self._make_tracker_with_fully_acked(
            fully_acked={PRODUCER_ID: time.time()},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
            patch("health_monitor.logger") as mock_logger,
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()  # records first-seen

            mock_time.time.return_value = base + 181
            actions = monitor.check_brc_progress()

        assert actions == [], "No action when producer has no agent_state"
        warn_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and "no agent_state" in c.args[0]
        ]
        assert len(warn_calls) == 1


class TestBRCProgressGlobalZeroProposalGate:
    """Regression coverage for #2187.

    ``check_brc_progress`` reads its candidate set from
    ``PeerConsensusTracker.get_fully_acked_producers``. Prior to #2187 the
    helper returned PROPOSED+ACKed producers regardless of whether
    ``mcp__brc__confirm`` would actually accept them, so a fast producer
    waiting on a slower peer's first proposal was nudged with
    ``brc_confirmation_timeout``. The agent then dutifully called confirm,
    got rejected with ``pending_acks`` (global zero-proposal guard, #1648),
    and went back to waiting — producing nothing but message-bus churn.

    The fix gates ``get_fully_acked_producers`` on ``check_confirm_guard``,
    so the detector never sees a producer that isn't actually ready to
    confirm. These tests exercise the detector through a real
    ``PeerConsensusTracker`` to verify the integration end-to-end.
    """

    def _build_tracker(self):
        """Build a real PeerConsensusTracker for the default implement graph."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        tracker = PeerConsensusTracker("issue-2187", graph, cooldown_seconds=0)
        for role in graph.all_roles():
            tracker.register_agent(role)
        return tracker

    def test_no_alert_while_peer_producer_has_zero_proposal(self):
        """No ``brc_confirmation_timeout`` while any peer producer has
        ``proposal_version == 0`` — the patient agent is correctly waiting,
        and ``mcp__brc__confirm`` would reject under the global zero-proposal
        guard. Mirrors the issue-2187 repro: documenter proposes + gets
        ACKed, coder/tester are still WORKING."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id="documenter")

        tracker = self._build_tracker()
        # Documenter is the easy case: no critical reviewers in the default
        # implement graph (reviewer_code reviews documenter ADVISORY-only),
        # so handle_propose alone leaves it fully-ACKed at the per-role
        # level. The advisory ACK below makes that explicit.
        tracker.handle_propose(
            "documenter",
            {"summary": "docs", "artifacts": ["docs/README.md"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "documenter",
            {"artifact_references": ["docs/README.md"]},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker,
            ),
        ):
            # First call records first-seen — but only if the helper
            # surfaces documenter, which it must NOT, since coder/tester
            # haven't proposed.
            mock_time.time.return_value = base
            monitor.check_brc_progress()

            # Well past the 180s timeout — still no alert.
            mock_time.time.return_value = base + 400
            actions = monitor.check_brc_progress()

        assert actions == [], (
            "Patient producer must not be escalated while peers have proposal_version == 0"
        )
        assert escalations == []
        assert "documenter" not in monitor._fully_acked_first_seen, (
            "Patient producer should not even enter the timeout-tracking dict"
        )

    def test_alert_fires_after_peers_finally_propose(self):
        """Once all producers have proposed, the global guard clears and the
        timeout window starts from genuine readiness — full 180s grace from
        the moment the patient agent is actually able to confirm."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id="documenter")

        tracker = self._build_tracker()
        tracker.handle_propose(
            "documenter",
            {"summary": "docs", "artifacts": ["docs/README.md"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "documenter",
            {"artifact_references": ["docs/README.md"]},
        )

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker,
            ),
        ):
            # Pre-clear: no alert at base+400.
            mock_time.time.return_value = base
            monitor.check_brc_progress()
            mock_time.time.return_value = base + 400
            monitor.check_brc_progress()

        assert escalations == []

        # Peers finally propose — global guard clears.
        tracker.handle_propose(
            "coder",
            {"summary": "code", "artifacts": ["src/m.py"], "commit_sha": "def"},
        )
        tracker.handle_propose(
            "tester",
            {"summary": "tests", "artifacts": ["tests/t.py"], "commit_sha": "ghi"},
        )
        # Critical reviewers ACK coder and tester (default implement graph).
        for reviewer in (
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_security",
            "reviewer_concurrency",
        ):
            tracker.handle_ack(reviewer, "coder", {"artifact_references": ["src/m.py"]})
            tracker.handle_ack(reviewer, "tester", {"artifact_references": ["tests/t.py"]})
        tracker.handle_ack("reviewer_contract", "coder", {"artifact_references": ["src/m.py"]})
        tracker.handle_ack("tester", "coder", {"artifact_references": ["src/m.py"]})

        cleared = base + 500
        _emit_heartbeat(bus, agent_id="coder")
        _emit_heartbeat(bus, agent_id="tester")
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker,
            ),
        ):
            # First post-clear tick records first-seen for newly-ready producers.
            mock_time.time.return_value = cleared
            monitor.check_brc_progress()
            assert escalations == [], "Within fresh timeout window"

            # Within the new 180s window — no alert.
            mock_time.time.return_value = cleared + 100
            monitor.check_brc_progress()
            assert escalations == []

            # Past the new window — alert fires for the producers that are
            # genuinely stuck post-clear.
            mock_time.time.return_value = cleared + 181
            monitor.check_brc_progress()

        assert any(e["alert_type"] == "brc_confirmation_timeout" for e in escalations), (
            "Genuine post-clear stall should still escalate"
        )

    def test_single_producer_phase_unaffected(self):
        """In a single-producer phase the global zero-proposal set is empty
        once that producer proposes, so the legitimate timeout still fires.
        Sanity check that the gate didn't accidentally suppress the
        single-producer case."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [ReviewEdge("reviewer_solo", "solo_producer", ReviewCriticality.CRITICAL)]
        )
        tracker = PeerConsensusTracker("issue-2187-solo", graph, cooldown_seconds=0)
        tracker.register_agent("solo_producer")
        tracker.register_agent("reviewer_solo")
        tracker.handle_propose(
            "solo_producer",
            {"summary": "solo", "artifacts": ["a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_solo",
            "solo_producer",
            {"artifact_references": ["a.py"]},
        )

        bus = _make_event_bus()
        config = _make_config(orchestrator_post_ack_confirmation_timeout_seconds=180)
        monitor = _make_monitor(bus, config)
        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))
        _emit_heartbeat(bus, agent_id="solo_producer")

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=tracker,
            ),
        ):
            mock_time.time.return_value = base
            monitor.check_brc_progress()

            mock_time.time.return_value = base + 181
            actions = monitor.check_brc_progress()

        assert len(actions) == 1, "Single-producer phase: legitimate timeout still escalates"
        assert actions[0]["agent_id"] == "solo_producer"
        assert actions[0]["alert_type"] == "brc_confirmation_timeout"
        assert len(escalations) == 1


# ---------------------------------------------------------------------------
# Tests: PipelineConfig new fields (#1613 — task-1-2)
# ---------------------------------------------------------------------------


class TestPipelineConfigBRCFields:
    """New config fields for post-propose grace and post-ACK timeout."""

    def test_post_propose_grace_default_300(self):
        """post_proposal_grace_seconds defaults to 300."""
        config = PipelineConfig()
        assert config.post_proposal_grace_seconds == 300

    def test_post_propose_grace_custom_value(self):
        """Custom value is accepted."""
        config = PipelineConfig(post_proposal_grace_seconds=60)
        assert config.post_proposal_grace_seconds == 60

    def test_post_propose_grace_rejects_below_30(self):
        """Value below 30 is rejected."""
        with pytest.raises(ValueError):
            PipelineConfig(post_proposal_grace_seconds=10)

    def test_post_propose_grace_accepts_30(self):
        """Value of exactly 30 is accepted."""
        config = PipelineConfig(post_proposal_grace_seconds=30)
        assert config.post_proposal_grace_seconds == 30

    def test_post_ack_timeout_default_180(self):
        """orchestrator_post_ack_confirmation_timeout_seconds defaults to 180."""
        config = PipelineConfig()
        assert config.orchestrator_post_ack_confirmation_timeout_seconds == 180

    def test_post_ack_timeout_custom_value(self):
        """Custom value is accepted."""
        config = PipelineConfig(orchestrator_post_ack_confirmation_timeout_seconds=60)
        assert config.orchestrator_post_ack_confirmation_timeout_seconds == 60

    def test_post_ack_timeout_rejects_below_30(self):
        """Value below 30 is rejected."""
        with pytest.raises(ValueError):
            PipelineConfig(orchestrator_post_ack_confirmation_timeout_seconds=10)

    def test_post_ack_timeout_accepts_30(self):
        """Value of exactly 30 is accepted."""
        config = PipelineConfig(orchestrator_post_ack_confirmation_timeout_seconds=30)
        assert config.orchestrator_post_ack_confirmation_timeout_seconds == 30


# ---------------------------------------------------------------------------
# Tests: Interaction between grace period and BRC progress (#1613)
# ---------------------------------------------------------------------------


class TestGraceAndBRCProgressInteraction:
    """Test interaction between post-propose grace and BRC progress checks."""

    def test_reviewer_suppressed_while_producer_past_ack_timeout(self):
        """Grace suppresses reviewer while BRC progress catches stuck producer."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            post_proposal_grace_seconds=300,
            orchestrator_post_ack_confirmation_timeout_seconds=180,
        )
        monitor = _make_monitor(bus, config)

        escalations: list[dict] = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=REVIEWER_ID)
        _emit_heartbeat(bus, agent_id=PRODUCER_ID)

        now = time.time()

        # Tracker: producer proposed 100s ago (within grace), fully-acked
        mock_tracker = _make_mock_tracker(
            reviewer_roles=[REVIEWER_ID],
            producer_roles=[PRODUCER_ID],
            producers_for_reviewer={REVIEWER_ID: [PRODUCER_ID]},
        )
        mock_tracker.get_earliest_proposal_time.return_value = now - 100
        mock_tracker.get_fully_acked_producers.return_value = {
            PRODUCER_ID: now - 100,
        }

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = now + 61
            actions = monitor.check_tripwires()

        # Reviewer should be suppressed (within grace)
        reviewer_actions = [a for a in actions if a.get("agent_id") == REVIEWER_ID]
        assert len(reviewer_actions) == 0, "Reviewer should be suppressed by grace"

        # Producer should NOT be escalated yet (within BRC timeout)
        brc_actions = [a for a in actions if a.get("alert_type") == "brc_confirmation_timeout"]
        assert len(brc_actions) == 0, "Producer within BRC timeout should not escalate"


# ---------------------------------------------------------------------------
# Tests: HEARTBEAT message wiring (issue #1897)
# ---------------------------------------------------------------------------


class TestHeartbeatMessageWiring:
    """Issue #1897 RISK-2: a HEARTBEAT message must reset ``last_heartbeat``
    and clear ``heartbeat_escalated`` so Tier-1 alarms do not falsely trip
    when an agent migrates off the legacy PROGRESS-heartbeat path.

    These tests exercise ``_on_message_sent`` — the new subscription that
    treats ``message_type=HEARTBEAT`` as a heartbeat signal.  See
    ``orchestrator/health_monitor.py``.
    """

    def test_heartbeat_message_resets_last_heartbeat(self):
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # Prime the agent state via a legacy heartbeat so the agent is
        # tracked.
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Fast-forward 61s — the agent should be escalatable.
        with patch("health_monitor.time") as mock_time:
            future = time.time() + 61
            mock_time.time.return_value = future
            # Emit a HEARTBEAT message — should reset last_heartbeat.
            bus.emit(
                EventType.MESSAGE_SENT,
                pipeline_id=PIPELINE_ID,
                data={
                    "agent_id": AGENT_ID,
                    "message_type": "HEARTBEAT",
                    "from_role": AGENT_ID,
                },
            )
            actions = monitor.check_heartbeats()
        # No escalation after the HEARTBEAT reset last_heartbeat.
        assert actions == []

    def test_non_heartbeat_message_does_not_reset(self):
        """A plain PROGRESS message should NOT reset the heartbeat clock
        (otherwise normal bus traffic would silently mask stalls)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        _emit_heartbeat(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            future = time.time() + 61
            mock_time.time.return_value = future
            # Emit a PROGRESS message — should NOT reset last_heartbeat.
            bus.emit(
                EventType.MESSAGE_SENT,
                pipeline_id=PIPELINE_ID,
                data={
                    "agent_id": AGENT_ID,
                    "message_type": "PROGRESS",
                    "from_role": AGENT_ID,
                },
            )
            actions = monitor.check_heartbeats()
        # Escalation must still fire because heartbeat is still stale.
        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

    def test_heartbeat_message_clears_escalation_flag(self):
        """After HEARTBEAT resets the clock, a new stall must re-escalate
        (i.e. ``heartbeat_escalated`` was cleared by the HEARTBEAT)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # 1) agent is tracked.
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # 2) first stall → escalate; sets heartbeat_escalated=True.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            first = monitor.check_heartbeats()
        assert len(first) == 1

        # 3) HEARTBEAT arrives — should clear escalation flag.
        t_after = time.time() + 70
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = t_after
            bus.emit(
                EventType.MESSAGE_SENT,
                pipeline_id=PIPELINE_ID,
                data={
                    "agent_id": AGENT_ID,
                    "message_type": "HEARTBEAT",
                    "from_role": AGENT_ID,
                },
            )

        # 4) stall again after another 61s — escalate again (flag was cleared).
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = t_after + 61
            second = monitor.check_heartbeats()
        assert len(second) == 1

    def test_heartbeat_event_accepts_from_role_alias(self):
        """routes/messages.py emits MESSAGE_SENT with ``from_role`` — the
        health monitor must accept it as an ``agent_id`` alias so the
        per-agent heartbeat state is keyed correctly regardless of
        which field the emitter populates (see
        orchestrator/health_monitor.py RISK-2 fallback)."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = _make_monitor(bus, config)

        # First tracking via legacy path so an entry exists.
        _emit_heartbeat(bus, agent_id="coder")

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            bus.emit(
                EventType.MESSAGE_SENT,
                pipeline_id=PIPELINE_ID,
                data={
                    # Only from_role populated — no agent_id — simulating
                    # the route's emit payload shape.
                    "from_role": "coder",
                    "message_type": "HEARTBEAT",
                },
            )
            # No escalation because HEARTBEAT reset the clock.
            actions = monitor.check_heartbeats()
        assert actions == []


# ---------------------------------------------------------------------------
# Tests: alive-signal progress gate (issue #2242)
# ---------------------------------------------------------------------------


class TestAlertProgressGate:
    """Issue #2242: heartbeat / progress alerts defer while the broader
    pipeline is still emitting peer or BRC-bus signals.

    Sibling of the consensus-failure progress gate added in #2243 / #2254;
    the same alive-signal vocabulary applied to per-agent tripwires so a
    single producer mid-Anthropic-completion does not trip an
    ``agent-heartbeat-stall`` while peers are clearly alive.
    """

    def test_heartbeat_alert_defers_when_peer_heartbeat_fresh(self):
        """A peer heartbeat within gate_seconds defers a stalled agent's alert."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()

        # Silent agent established at t=base.
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Peer heartbeats at t=base+200 — the broader pipeline is alive.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 200
            _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        # At t=base+250 the focal agent has been silent for 250s
        # (>60s threshold). Peer heartbeat is 50s old — within the
        # 300s gate. Defer.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 250
            actions = monitor.check_heartbeats()

        assert actions == [], "Heartbeat alert should defer while peer is heartbeating"

        # The escalated flag must NOT be set, so the next poll re-checks.
        agent_state = monitor._agents[AGENT_ID]
        assert not agent_state.heartbeat_escalated

    def test_heartbeat_alert_fires_when_gate_window_elapsed(self):
        """Once peer signal ages past gate_seconds, the alert proceeds."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)
        _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        # Both agents silent for 400s — past the 300s gate window.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 400
            actions = monitor.check_heartbeats()

        # Both agents fire (each has the other as a stale peer).
        assert len(actions) == 2

    def test_heartbeat_alert_excludes_self_from_peer_signal(self):
        """A solo agent's own heartbeat must not defer its own alert."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Only the focal agent exists. At t=base+61 it's silent for 61s.
        # Without self-exclusion, its own heartbeat (61s old, within
        # gate) would defer trivially.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Self-exclusion must let solo-agent alerts through"

    def test_gate_disabled_when_seconds_zero(self):
        """orchestrator_alert_progress_gate_seconds=0 reverts to pre-fix behavior."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=0,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Peer alive — but gate disabled, so still escalate.
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 100
            _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 150
            actions = monitor.check_heartbeats()

        # AGENT_ID has been silent 150s (>60s); gate disabled fires.
        assert any(a["agent_id"] == AGENT_ID for a in actions)

    def test_progress_alert_defers_when_peer_heartbeat_fresh(self):
        """progress_stall path also consults the gate."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_progress(bus, agent_id=AGENT_ID)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 200
            _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 250
            actions = monitor.check_progress()

        assert actions == [], "Progress-stall must defer while peers are alive"
        assert not monitor._agents[AGENT_ID].progress_escalated

    def test_gate_defers_when_brc_bus_active(self):
        """A fresh BRC-tracker progress timestamp defers the alert even if
        no peer heartbeat is fresh."""
        from datetime import UTC, datetime

        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # Mock tracker reporting a CONSENSUS_PROPOSE at base+200 (50s ago
        # relative to the mocked check time of base+250).
        mock_tracker = MagicMock()
        mock_tracker.get_latest_progress_timestamp.return_value = datetime.fromtimestamp(
            base + 200, tz=UTC
        )

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base + 250
            actions = monitor.check_heartbeats()

        assert actions == [], "Fresh BRC-bus signal should defer the alert"

    def test_gate_handles_missing_tracker_gracefully(self):
        """If no BRC tracker is registered, fall back to peer heartbeats only."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # No tracker, no peer heartbeat — alert proceeds.
        with (
            patch("health_monitor.time") as mock_time,
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
        ):
            mock_time.time.return_value = base + 100
            actions = monitor.check_heartbeats()

        assert len(actions) == 1

    def test_gate_filters_inactive_agent_heartbeats(self):
        """A heartbeat from a role not in the current-phase tracker graph
        does not defer alerts (mirrors ``_check_brc_progress_gate``'s
        ``active_role_names`` filter)."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        base = time.time()
        _emit_heartbeat(bus, agent_id=AGENT_ID)

        # AGENT_ID_2 has a real heartbeat 50s before check time — production
        # state shape after a phase transition that didn't ``reset_agent``
        # AGENT_ID_2's role (which lingers in ``_last_heartbeat`` and
        # ``_agents`` together; the prior ``_agents.keys()`` filter would
        # have been a no-op against this).
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = base + 200
            _emit_heartbeat(bus, agent_id=AGENT_ID_2)

        # Mock a tracker whose phase-scoped graph contains only AGENT_ID
        # — i.e. AGENT_ID_2 belonged to a prior phase. The gate must
        # drop AGENT_ID_2's heartbeat and proceed with AGENT_ID's alert.
        mock_tracker = MagicMock()
        mock_tracker.get_latest_progress_timestamp.return_value = None
        mock_tracker.graph.all_roles.return_value = {AGENT_ID}

        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            mock_time.time.return_value = base + 250
            actions = monitor.check_heartbeats()

        assert len(actions) == 1, "Inactive-role heartbeats must not defer alerts for active peers"


# ---------------------------------------------------------------------------
# Tests: phase-aware post-ACK confirmation timeout (issue #2242)
# ---------------------------------------------------------------------------


class TestPlanPhasePostAckTimeout:
    """Issue #2242: plan phase uses a higher post-ACK confirm threshold than
    refine/implement. Plan-phase reconciliation (resolved decisions, feedback
    bodies, slice-DAG sanity) legitimately exceeds the 180s default on heavy
    pipelines."""

    def test_plan_phase_post_ack_default_300(self):
        """orchestrator_plan_post_ack_confirmation_timeout_seconds defaults to 300."""
        config = PipelineConfig()
        assert config.orchestrator_plan_post_ack_confirmation_timeout_seconds == 300

    def test_plan_phase_post_ack_validation_minimum_30(self):
        """Value must be >= 30."""
        with pytest.raises(ValueError):
            PipelineConfig(orchestrator_plan_post_ack_confirmation_timeout_seconds=10)

    def test_get_post_ack_confirmation_timeout_phase_aware(self):
        """Helper returns plan-phase value during plan, default elsewhere."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_post_ack_confirmation_timeout_seconds=180,
            orchestrator_plan_post_ack_confirmation_timeout_seconds=300,
        )
        monitor = _make_monitor(bus, config)

        # No phase set — default.
        assert monitor._get_post_ack_confirmation_timeout() == 180

        monitor.set_current_phase("plan")
        assert monitor._get_post_ack_confirmation_timeout() == 300

        for phase in ["refine", "implement", "pr"]:
            monitor.set_current_phase(phase)
            assert monitor._get_post_ack_confirmation_timeout() == 180, (
                f"Phase {phase} should use the default 180s"
            )

    def test_check_brc_progress_uses_plan_phase_threshold(self):
        """During plan phase, check_brc_progress waits for the longer threshold."""
        bus = _make_event_bus()
        config = _make_config(
            orchestrator_post_ack_confirmation_timeout_seconds=180,
            orchestrator_plan_post_ack_confirmation_timeout_seconds=300,
        )
        monitor = _make_monitor(bus, config)
        monitor.set_current_phase("plan")

        # Register the producer so it has an AgentState.
        _emit_heartbeat(bus, agent_id="architect")

        mock_tracker = MagicMock()
        # Producer fully ACKed since base.
        mock_tracker.get_fully_acked_producers.return_value = {"architect": time.time()}

        base = time.time()
        with (
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # First call: records first-seen.
            mock_time.time.return_value = base
            monitor.check_brc_progress()

            # At base+200 — past 180s default but within 300s plan threshold.
            mock_time.time.return_value = base + 200
            actions = monitor.check_brc_progress()
            assert actions == [], "Plan phase should not fire at 200s (threshold=300s)"

            # At base+301 — past plan threshold.
            mock_time.time.return_value = base + 301
            actions = monitor.check_brc_progress()
            assert len(actions) == 1
            assert actions[0]["alert_type"] == "brc_confirmation_timeout"
