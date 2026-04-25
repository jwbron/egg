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
        # Must be OVERSEER_ALERT to wake the producer's post-confirm wait_loop,
        # whose filter is CONSENSUS_CONFIRMED,CONSENSUS_RE_REVIEW,OVERSEER_ALERT.
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
