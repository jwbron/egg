"""
End-to-end integration test for the two-tier pipeline health monitoring flow.

Simulates the full chain:
1. Agent emits progress events
2. Orchestrator detects stall (no new progress within threshold)
3. Immediate escalation to overseer tier (Tier 1 no longer nudges — #1447)
4. Haiku classifier classifies anomaly as "stuck"
5. Sonnet decision maker decides "redirect" (or nudge)
6. Redirect/nudge message composed and sent by overseer

All LLM calls, containers, and message sending are mocked.

Related: issue #1059 - Phase 5 two-tier integration, #1447 - remove Tier 1 nudging
"""

import asyncio
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
# Conditional imports - skip if modules not yet available
# ---------------------------------------------------------------------------
try:
    from events import Event, EventBus, EventType
    from models import PipelineConfig, ProgressEvent, ProgressState
except ImportError:
    pytest.skip(
        "Core orchestrator modules not available",
        allow_module_level=True,
    )

# Try importing the health monitor and progress store; skip if not available.
try:
    from health_monitor import HealthMonitor
except ImportError:
    HealthMonitor = None

try:
    from progress_store import ProgressStore
except ImportError:
    ProgressStore = None

# Try importing overseer classifier and decision maker
try:
    from overseer.classifier import classify_stall
    from overseer.classifier import clear_cache as clear_classifier_cache
except ImportError, ModuleNotFoundError:
    classify_stall = None
    clear_classifier_cache = None

try:
    from overseer.decision_maker import compose_redirect_message, decide_corrective_action
except ImportError, ModuleNotFoundError:
    decide_corrective_action = None
    compose_redirect_message = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "issue-integration-1059"
AGENT_ID_CODER = "coder-abc123"
AGENT_ID_TESTER = "tester-def456"


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig with test-friendly thresholds."""
    defaults = {
        "orchestrator_heartbeat_timeout_seconds": 60,
        "orchestrator_error_repeat_threshold": 3,
        "orchestrator_message_rate_limit": 20,
        "overseer_enabled": True,
        "overseer_max_redirects_before_escalation": 2,
        "overseer_poll_interval_seconds": 10,
        "overseer_decision_maker_model": "sonnet",
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def _make_event_bus() -> EventBus:
    """Create a synchronous EventBus for testing."""
    return EventBus(async_delivery=False)


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _emit_progress(
    event_bus: EventBus,
    agent_id: str = AGENT_ID_CODER,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit a structured progress event."""
    return event_bus.emit(
        EventType.PROGRESS_EMITTED,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "type": "progress", "description": "working"},
    )


def _emit_heartbeat(
    event_bus: EventBus,
    agent_id: str = AGENT_ID_CODER,
    pipeline_id: str = PIPELINE_ID,
) -> Event:
    """Emit a heartbeat event for an agent."""
    return event_bus.emit(
        EventType.PROGRESS_EMITTED,
        pipeline_id=pipeline_id,
        data={"agent_id": agent_id, "type": "heartbeat"},
    )


# ---------------------------------------------------------------------------
# Test Scenario 1: Full escalation chain
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None or ProgressStore is None,
    reason="health_monitor or progress_store not yet implemented",
)
class TestFullEscalationChain:
    """Scenario 1: progress -> stall detection -> nudge -> escalation ->
    classification -> decision -> redirect.

    Covers the complete two-tier flow end-to-end with mocked LLM calls.
    """

    def test_full_flow_stall_to_nudge_to_escalation(self):
        """Simulate: agent stalls -> nudge -> stall persists -> escalate to overseer."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )
        store = ProgressStore()

        # Track escalations
        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Step 1: Agent emits initial progress
        initial_event = ProgressEvent(
            id="evt-001",
            pipeline_id=PIPELINE_ID,
            agent_role="coder",
            step="Starting implementation",
            state=ProgressState.WORKING,
        )
        store.add_event(initial_event)
        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # Step 2: Time passes beyond heartbeat threshold -> stall detected -> immediate escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        escalate_actions = [a for a in actions if a.get("action") == "escalate"]
        assert len(escalate_actions) >= 1, "Should have generated an escalate action"
        assert escalate_actions[0]["agent_id"] == AGENT_ID_CODER

        # Step 3: Stall persists but already escalated -> no re-escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 122
            actions2 = monitor.check_progress()

        assert len(actions2) == 0, "Should not re-escalate on subsequent cycle"

        assert len(escalations) == 1, "Should have escalated once on first detection"
        assert escalations[0]["type"] == "overseer"
        assert escalations[0]["agent_id"] == AGENT_ID_CODER

    @pytest.mark.skipif(
        classify_stall is None or decide_corrective_action is None,
        reason="overseer classifier or decision_maker not yet implemented",
    )
    @patch("overseer.decision_maker.run_agent_async", new_callable=AsyncMock)
    @patch("overseer.classifier.run_agent_async", new_callable=AsyncMock)
    def test_full_chain_classifier_to_decision_to_redirect(
        self, mock_classifier_agent, mock_decision_agent
    ):
        """Full chain: classifier returns stuck -> decision maker returns redirect ->
        compose_redirect_message produces a redirect message."""
        # Clear classifier cache so mocks are actually called
        if clear_classifier_cache:
            clear_classifier_cache()

        # Setup classifier mock to return 'stuck' classification
        mock_classifier_agent.return_value = MagicMock(
            success=True,
            stdout='{"classification": "stuck", "confidence": 0.9, "reasoning": "No output for 5 minutes"}',
            stderr="",
            returncode=0,
        )

        # Setup decision maker mock to return 'redirect' action
        mock_decision_agent.return_value = MagicMock(
            success=True,
            stdout='{"action": "redirect", "message": "Focus on the auth module", "priority": "high"}',
            stderr="",
            returncode=0,
        )

        # Step 1: Classifier classifies the stall
        classification = _run(
            classify_stall(
                logs=[{"msg": "no output for 5 min"}],
                progress=[{"step": "idle", "pct": 0}],
            )
        )
        assert classification["classification"] == "stuck"
        assert classification["confidence"] >= 0.8
        mock_classifier_agent.assert_awaited_once()

        # Verify Haiku model was used for classification
        classifier_call = mock_classifier_agent.call_args
        assert classifier_call.kwargs.get("model") == "haiku"

        # Step 2: Decision maker decides corrective action
        decision = _run(
            decide_corrective_action(
                classification=classification,
                context={"pipeline_id": PIPELINE_ID, "agent_role": "coder"},
            )
        )
        assert decision["action"] == "redirect"
        assert decision["priority"] == "high"
        mock_decision_agent.assert_awaited()

        # Verify Sonnet model was used for decision-making
        decision_call = mock_decision_agent.call_args
        assert decision_call.kwargs.get("model") == "sonnet"

    @pytest.mark.skipif(
        compose_redirect_message is None,
        reason="overseer decision_maker not yet implemented",
    )
    @patch("overseer.decision_maker.run_agent_async", new_callable=AsyncMock)
    def test_redirect_message_composed_and_sent(self, mock_agent):
        """After decision is 'redirect', a redirect message is composed."""
        mock_agent.return_value = MagicMock(
            success=True,
            stdout="You are stuck in the auth module. Please focus on test_login.py first.",
            stderr="",
            returncode=0,
        )

        message = _run(
            compose_redirect_message(
                agent_role="coder",
                issue="Agent stuck with no progress for 5 minutes",
                context={"contract_task": "Fix auth bug", "recent_files": ["auth/views.py"]},
            )
        )

        assert isinstance(message, str)
        assert len(message) > 10, "Redirect message should be substantive"
        mock_agent.assert_awaited_once()

    def test_progress_store_tracks_events_during_flow(self):
        """ProgressStore correctly tracks events throughout the flow."""
        store = ProgressStore()

        # Emit several progress events
        for i in range(5):
            store.add_event(
                ProgressEvent(
                    id=f"evt-{i:03d}",
                    pipeline_id=PIPELINE_ID,
                    agent_role="coder",
                    step=f"Step {i}",
                    state=ProgressState.WORKING,
                )
            )

        events = store.get_events(PIPELINE_ID)
        assert len(events) == 5

        latest = store.get_latest_per_agent(PIPELINE_ID)
        assert len(latest) == 1
        assert latest[0].step == "Step 4"


# ---------------------------------------------------------------------------
# Test Scenario 2: Deterministic resolution (nudge works, no overseer)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None,
    reason="health_monitor not yet implemented",
)
class TestDeterministicResolution:
    """Scenario 2: Stall detected, escalation sent, agent recovers."""

    def test_escalation_resolves_no_re_escalation(self):
        """If agent resumes after escalation, no second escalation occurs."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # Stall -> immediate escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

        # Agent resumes progress (resets progress_escalated flag)
        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # Check again within threshold - agent is active
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 30
            actions = monitor.check_progress()

        assert len(actions) == 0
        assert len(escalations) == 1, "Only one escalation total"

    def test_escalation_resolves_heartbeat_variant(self):
        """If agent sends heartbeat after escalation, no re-escalation occurs."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_heartbeat(bus, agent_id=AGENT_ID_CODER)

        # Stall -> immediate escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_heartbeats()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"

        # Agent resumes with heartbeat
        _emit_heartbeat(bus, agent_id=AGENT_ID_CODER)

        # Check again - should not re-escalate
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 30
            actions = monitor.check_heartbeats()

        assert len(actions) == 0
        assert len(escalations) == 1

    def test_recovery_then_second_stall_re_escalates(self):
        """Progress events after escalation reset the flag; second stall re-escalates."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_heartbeat_timeout_seconds=60)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # First stall -> escalation
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()
        assert actions[0]["action"] == "escalate"

        # Agent resumes
        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # Second stall -> fresh escalation (flag was reset by progress)
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        assert len(actions) == 1
        assert actions[0]["action"] == "escalate"
        assert len(escalations) == 2, "Second stall should produce fresh escalation"


# ---------------------------------------------------------------------------
# Test Scenario 3: Multiple agents stalling independently
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None,
    reason="health_monitor not yet implemented",
)
class TestMultipleAgentsStalling:
    """Scenario 3: Two agents stall at different times, each gets independent handling."""

    def test_two_agents_stall_at_different_times(self):
        """Two agents stall independently; each receives its own escalation."""
        bus = _make_event_bus()
        # Disable the alive-signal gate (#2242) so this test isolates the
        # per-agent escalation behavior from the peer-progress deferral.
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=0,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        # Register both agents
        _emit_progress(bus, agent_id=AGENT_ID_CODER)
        _emit_progress(bus, agent_id=AGENT_ID_TESTER)

        # Only coder stalls (update tester timestamp so it appears fresh)
        with patch("health_monitor.time") as mock_time:
            now = time.time() + 61
            mock_time.time.return_value = now
            # Keep tester fresh
            if hasattr(monitor, "_last_heartbeat"):
                monitor._last_heartbeat[AGENT_ID_TESTER] = now - 10
            if hasattr(monitor, "_agents") and AGENT_ID_TESTER in monitor._agents:
                monitor._agents[AGENT_ID_TESTER].last_progress = now - 10

            actions = monitor.check_progress()

        escalated = {a["agent_id"] for a in actions if a.get("action") == "escalate"}
        assert AGENT_ID_CODER in escalated, "Coder should have been escalated"
        assert AGENT_ID_TESTER not in escalated, "Tester should NOT have been escalated"

    def test_both_agents_stall_get_independent_escalations(self):
        """When both agents stall, each gets an independent escalation."""
        bus = _make_event_bus()
        # Disable the alive-signal gate (#2242) so this test isolates the
        # per-agent escalation behavior from the peer-progress deferral.
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            orchestrator_alert_progress_gate_seconds=0,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        # Register both agents
        _emit_progress(bus, agent_id=AGENT_ID_CODER)
        _emit_progress(bus, agent_id=AGENT_ID_TESTER)

        # Both stall
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            actions = monitor.check_progress()

        escalated_ids = {a["agent_id"] for a in actions if a.get("action") == "escalate"}
        assert AGENT_ID_CODER in escalated_ids, "Coder should be escalated"
        assert AGENT_ID_TESTER in escalated_ids, "Tester should be escalated"

    def test_one_agent_resolves_other_stays_escalated(self):
        """One agent recovers after escalation; the other remains escalated (no re-escalation)."""
        bus = _make_event_bus()
        # Disable the alive-signal gate (#2242) so this test isolates the
        # per-agent escalation behavior from the peer-progress deferral.
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            overseer_enabled=True,
            orchestrator_alert_progress_gate_seconds=0,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Register both agents
        _emit_progress(bus, agent_id=AGENT_ID_CODER)
        _emit_progress(bus, agent_id=AGENT_ID_TESTER)

        # Both stall -> both escalated immediately
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 2

        # Coder resumes progress (resets escalated flag)
        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # Check shortly after coder recovery — coder is within threshold, tester already escalated
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 30
            actions = monitor.check_progress()

        assert len(actions) == 0, (
            "No new escalations — tester already escalated, coder within threshold"
        )
        tester_escalations = [e for e in escalations if e["agent_id"] == AGENT_ID_TESTER]
        assert len(tester_escalations) == 1

    def test_independent_escalation_types(self):
        """Each agent's escalation is independent and carries correct metadata."""
        bus = _make_event_bus()
        # Disable the alive-signal gate (#2242) so this test isolates the
        # per-agent escalation behavior from the peer-progress deferral.
        config = _make_config(
            orchestrator_heartbeat_timeout_seconds=60,
            overseer_enabled=True,
            orchestrator_alert_progress_gate_seconds=0,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Register both agents
        _emit_progress(bus, agent_id=AGENT_ID_CODER)
        _emit_progress(bus, agent_id=AGENT_ID_TESTER)

        # Both stall -> both escalated immediately
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        assert len(escalations) == 2, "Both agents should have escalated"
        escalated_agents = {e["agent_id"] for e in escalations}
        assert AGENT_ID_CODER in escalated_agents
        assert AGENT_ID_TESTER in escalated_agents

        # Each escalation should have correct type
        for esc in escalations:
            assert esc["type"] == "overseer"
            assert "agent_id" in esc
            assert "reason" in esc
            assert "timestamp" in esc


# ---------------------------------------------------------------------------
# Test Scenario 4: Overseer disabled fallback
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None or ProgressStore is None,
    reason="health_monitor or progress_store not yet implemented",
)
class TestOverseerDisabledFallback:
    """Scenario 4: When overseer_enabled=False, deterministic tier escalates
    directly to HITL."""

    def test_escalation_goes_to_hitl_when_overseer_disabled(self):
        """With overseer_enabled=False, stall escalation goes directly to HITL."""
        bus = _make_event_bus()
        config = _make_config(
            overseer_enabled=False,
            orchestrator_heartbeat_timeout_seconds=60,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Initial progress
        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        # First stall -> nudge
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 61
            monitor.check_progress()

        # Second stall -> second nudge
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 122
            monitor.check_progress()

        # Third stall -> escalation (should be HITL, not overseer)
        with patch("health_monitor.time") as mock_time:
            mock_time.time.return_value = time.time() + 183
            monitor.check_progress()

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "hitl", (
            "With overseer disabled, escalation should go to HITL"
        )

    def test_repeated_errors_escalate_to_hitl_when_overseer_disabled(self):
        """With overseer_enabled=False, repeated errors escalate directly to HITL."""
        bus = _make_event_bus()
        config = _make_config(
            overseer_enabled=False,
            orchestrator_error_repeat_threshold=2,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Trigger repeated errors
        for _ in range(2):
            bus.emit(
                EventType.ERROR,
                pipeline_id=PIPELINE_ID,
                data={"agent_id": AGENT_ID_CODER, "error": "same error"},
            )

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "hitl"

    def test_overseer_enabled_escalates_to_overseer(self):
        """With overseer_enabled=True, repeated errors escalate to overseer."""
        bus = _make_event_bus()
        config = _make_config(
            overseer_enabled=True,
            orchestrator_error_repeat_threshold=2,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Trigger repeated errors
        for _ in range(2):
            bus.emit(
                EventType.ERROR,
                pipeline_id=PIPELINE_ID,
                data={"agent_id": AGENT_ID_CODER, "error": "same error"},
            )

        assert len(escalations) >= 1
        assert escalations[0]["type"] == "overseer"

    def test_container_exit_always_hitl_regardless_of_overseer_setting(self):
        """Container exit goes to HITL even when overseer is enabled."""
        for overseer_enabled in (True, False):
            bus = _make_event_bus()
            config = _make_config(overseer_enabled=overseer_enabled)
            monitor = HealthMonitor(
                event_bus=bus,
                pipeline_id=PIPELINE_ID,
                config=config,
            )

            escalations: list = []
            monitor.on_escalation(lambda e, _esc=escalations: _esc.append(e))

            bus.emit(
                EventType.CONTAINER_STOPPED,
                pipeline_id=PIPELINE_ID,
                data={"agent_id": AGENT_ID_CODER, "exit_code": 137},
            )

            assert len(escalations) >= 1, (
                f"Container exit should escalate (overseer_enabled={overseer_enabled})"
            )
            assert escalations[0]["type"] == "hitl", "Container exit should always go to HITL"


# ---------------------------------------------------------------------------
# Test: Event bus wiring
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    HealthMonitor is None,
    reason="health_monitor not yet implemented",
)
class TestTwoTierEventBusWiring:
    """Verify the event bus correctly routes events to the health monitor."""

    def test_event_bus_delivers_progress_events(self):
        """PROGRESS_EMITTED events are received by the health monitor."""
        bus = _make_event_bus()
        config = _make_config()
        HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        received_events = []
        bus.subscribe(EventType.PROGRESS_EMITTED, lambda e: received_events.append(e))

        _emit_progress(bus, agent_id=AGENT_ID_CODER)

        assert len(received_events) == 1
        assert received_events[0].pipeline_id == PIPELINE_ID

    def test_event_bus_delivers_error_events(self):
        """ERROR events are received and tracked by the health monitor."""
        bus = _make_event_bus()
        config = _make_config(orchestrator_error_repeat_threshold=2)
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        escalations = []
        monitor.on_escalation(lambda e: escalations.append(e))

        # Emit repeated errors to trigger escalation
        for _ in range(2):
            bus.emit(
                EventType.ERROR,
                pipeline_id=PIPELINE_ID,
                data={"agent_id": AGENT_ID_CODER, "error": "same error"},
            )

        assert len(escalations) >= 1
