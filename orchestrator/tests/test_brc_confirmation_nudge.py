"""
Tests for the BRC confirmation-timeout nudge wired into HealthMonitor escalations.

Covers ``_send_brc_confirmation_nudge`` in ``orchestrator/routes/pipelines.py``,
which wakes producers stuck post-ACK by posting a directed OVERSEER_ALERT
when ``check_brc_progress`` fires the deterministic timeout (#2079).
"""

import sys
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

# Mock docker before importing route modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

try:
    from message_store import Message, MessageType
    from routes.pipelines import _send_brc_confirmation_nudge
except ImportError as exc:
    pytest.skip(
        f"Required orchestrator modules not available: {exc}",
        allow_module_level=True,
    )


PIPELINE_ID = "issue-2079"


def _make_escalation(
    *,
    alert_type: str = "brc_confirmation_timeout",
    agent_id: str = "documenter",
    elapsed_seconds: int = 240,
) -> dict:
    """Build a HealthMonitor escalation dict matching check_brc_progress."""
    return {
        "type": "overseer",
        "agent_id": agent_id,
        "alert_type": alert_type,
        "elapsed_seconds": elapsed_seconds,
        "reason": (
            f"Producer {agent_id} fully ACKed but not confirmed "
            f"for {elapsed_seconds}s (timeout: 180s)"
        ),
        "timestamp": "2026-04-25T18:07:22+00:00",
    }


class TestSendBRCConfirmationNudge:
    """The nudge posts a directed OVERSEER_ALERT to the stuck producer."""

    def test_posts_directed_overseer_alert(self):
        """A valid escalation results in an OVERSEER_ALERT addressed to the producer."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                _make_escalation(agent_id="documenter", elapsed_seconds=240),
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is True
        msg_store.add_message.assert_called_once()
        message = msg_store.add_message.call_args[0][0]
        assert isinstance(message, Message)
        assert message.pipeline_id == PIPELINE_ID
        assert message.from_role == "orchestrator"
        assert message.to_role == "documenter"
        # Must be OVERSEER_ALERT — it is the only message type in both the
        # producer's pre-confirm wait_loop filter (CONSENSUS_ACK,CONSENSUS_NACK,
        # CONSENSUS_RE_REVIEW,OVERSEER_ALERT) and post-confirm filter
        # (CONSENSUS_CONFIRMED,CONSENSUS_RE_REVIEW,OVERSEER_ALERT), so it wakes
        # the producer regardless of which wait they are blocked on.
        assert message.message_type == MessageType.OVERSEER_ALERT
        assert message.phase == "implement"
        assert "mcp__brc__confirm" in message.body
        assert "240s" in message.body
        assert message.metadata["alert_type"] == "brc_confirmation_timeout"
        assert message.metadata["elapsed_seconds"] == 240
        assert message.metadata["source"] == "health_monitor"

    def test_ignores_non_brc_escalations(self):
        """Escalations from other tripwires are ignored — no message posted."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                _make_escalation(alert_type="heartbeat_timeout"),
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False
        msg_store.add_message.assert_not_called()

    def test_ignores_escalation_with_no_alert_type(self):
        """Escalations missing alert_type (legacy tripwires) are ignored."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        escalation = _make_escalation()
        escalation.pop("alert_type")

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                escalation,
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False
        msg_store.add_message.assert_not_called()

    def test_ignores_escalation_missing_agent_id(self):
        """Defensive: escalation without agent_id is skipped, not crashed."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        escalation = _make_escalation()
        escalation.pop("agent_id")

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                escalation,
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False
        msg_store.add_message.assert_not_called()

    def test_returns_false_when_message_store_unavailable(self):
        """Returns False (does not raise) when the message store factory is None."""
        with patch(
            "routes.pipelines._get_message_store",
            return_value=None,
        ):
            posted = _send_brc_confirmation_nudge(
                _make_escalation(),
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False

    def test_returns_false_when_send_raises(self):
        """A send error is logged + swallowed; nudge call itself does not raise."""
        msg_store = MagicMock()
        msg_store.add_message.side_effect = RuntimeError("boom")
        store_factory = MagicMock(return_value=msg_store)

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                _make_escalation(),
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False

    def test_phase_none_passes_through(self):
        """phase=None is permitted (Message.phase is Optional)."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                _make_escalation(),
                PIPELINE_ID,
                phase=None,
            )

        assert posted is True
        message = msg_store.add_message.call_args[0][0]
        assert message.phase is None

    def test_missing_elapsed_seconds_rejected(self):
        """Escalation without elapsed_seconds is treated as malformed."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        escalation = _make_escalation()
        escalation.pop("elapsed_seconds")

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            posted = _send_brc_confirmation_nudge(
                escalation,
                PIPELINE_ID,
                phase="implement",
            )

        assert posted is False
        msg_store.add_message.assert_not_called()

    def test_zero_or_negative_elapsed_seconds_rejected(self):
        """elapsed_seconds <= 0 would render 'have not confirmed in 0s' — reject."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch(
            "routes.pipelines._get_message_store",
            return_value=store_factory,
        ):
            for bad_value in (0, -1):
                posted = _send_brc_confirmation_nudge(
                    _make_escalation(elapsed_seconds=bad_value),
                    PIPELINE_ID,
                    phase="implement",
                )
                assert posted is False, f"elapsed_seconds={bad_value} should be rejected"

        msg_store.add_message.assert_not_called()


class TestEscalationCallbackWiring:
    """End-to-end wiring: HealthMonitor escalation -> nudge with current phase.

    Mirrors the closure constructed in ``_run_pipeline``::

        def _on_health_escalation(escalation):
            phase = health_monitor_instance.get_current_phase()
            _send_brc_confirmation_nudge(escalation, pipeline_id, phase)

    The closure must read ``get_current_phase()`` at fire time, not at
    registration time, so a phase transition between ``init_health_monitor``
    and the first BRC escalation still records the correct phase on the
    OVERSEER_ALERT.  A refactor that, e.g., reorders ``init_health_monitor``
    and ``set_current_phase`` would otherwise leave ``get_current_phase()``
    returning ``None`` for the first escalation without any test catching it.
    """

    def _make_monitor_with_brc_escalation(self):
        """Build a HealthMonitor + tracker that will fire a brc_confirmation_timeout."""
        from events import EventBus, EventType
        from health_monitor import HealthMonitor
        from models import PipelineConfig

        bus = EventBus(async_delivery=False)
        config = PipelineConfig(
            orchestrator_post_ack_confirmation_timeout_seconds=180,
            overseer_enabled=True,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )

        # Register the producer so it has an AgentState.
        bus.emit(
            EventType.PROGRESS_EMITTED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": "documenter", "type": "heartbeat"},
        )

        return monitor

    def test_callback_records_current_phase_at_fire_time(self):
        """The closure reads phase at fire time — phase changes propagate."""
        import time as _time

        monitor = self._make_monitor_with_brc_escalation()
        # Initial phase recorded on the monitor (matches _run_pipeline ordering:
        # init_health_monitor -> set_current_phase).
        monitor.set_current_phase("plan")

        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        # Closure mirrors _run_pipeline's _on_health_escalation.
        def _on_health_escalation(escalation):
            phase = monitor.get_current_phase()
            _send_brc_confirmation_nudge(escalation, PIPELINE_ID, phase)

        monitor.on_escalation(_on_health_escalation)

        # Phase advances between registration and the first BRC escalation —
        # the closure must see the new value.
        monitor.set_current_phase("implement")

        mock_tracker = MagicMock()
        mock_tracker.get_fully_acked_producers.return_value = {
            "documenter": _time.time() - 200,
        }

        base = _time.time()
        with (
            patch(
                "routes.pipelines._get_message_store",
                return_value=store_factory,
            ),
            patch("health_monitor.time") as mock_time,
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                return_value=mock_tracker,
            ),
        ):
            # First call records first-seen at base.
            mock_time.time.return_value = base
            monitor.check_brc_progress()
            # Second call past timeout — fires escalation -> closure -> nudge.
            mock_time.time.return_value = base + 181
            monitor.check_brc_progress()

        msg_store.add_message.assert_called_once()
        message = msg_store.add_message.call_args[0][0]
        assert message.phase == "implement"
        assert message.to_role == "documenter"
        assert message.metadata["alert_type"] == "brc_confirmation_timeout"

    def test_callback_does_not_fire_on_non_brc_escalations(self):
        """Heartbeat escalations don't carry alert_type='brc_confirmation_timeout'."""
        import time as _time

        from events import EventBus, EventType
        from health_monitor import HealthMonitor
        from models import PipelineConfig

        bus = EventBus(async_delivery=False)
        config = PipelineConfig(
            orchestrator_heartbeat_timeout_seconds=60,
            overseer_enabled=True,
        )
        monitor = HealthMonitor(
            event_bus=bus,
            pipeline_id=PIPELINE_ID,
            config=config,
        )
        bus.emit(
            EventType.PROGRESS_EMITTED,
            pipeline_id=PIPELINE_ID,
            data={"agent_id": "coder", "type": "heartbeat"},
        )
        monitor.set_current_phase("implement")

        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        def _on_health_escalation(escalation):
            phase = monitor.get_current_phase()
            _send_brc_confirmation_nudge(escalation, PIPELINE_ID, phase)

        monitor.on_escalation(_on_health_escalation)

        with (
            patch(
                "routes.pipelines._get_message_store",
                return_value=store_factory,
            ),
            patch("health_monitor.time") as mock_time,
        ):
            mock_time.time.return_value = _time.time() + 61
            monitor.check_heartbeats()

        # Heartbeat escalations have no alert_type — the nudge filter
        # rejects them, so no OVERSEER_ALERT is posted.
        msg_store.add_message.assert_not_called()
