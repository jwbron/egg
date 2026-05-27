"""Tests for the producer-permanent-death ``OVERSEER_ALERT`` (#2806).

When a producer agent's consensus-wrapper exhausts its retry budget, the
slice state machine cannot replace the permanently dead producer.  The
orchestrator hard-fails the pipeline (Option A) and publishes a
high-priority ``OVERSEER_ALERT`` so the operator notices immediately
rather than waiting for the consensus-timeout alert 30+ minutes later.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from routes.pipelines import _emit_producer_death_alert


class TestEmitProducerDeathAlert:
    """Unit tests for ``_emit_producer_death_alert``."""

    def test_publishes_high_priority_overseer_alert(self):
        """High-priority OVERSEER_ALERT is published with role + exit code."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            _emit_producer_death_alert(
                pipeline_id="issue-2806",
                role="coder",
                phase="implement",
                slice_id=None,
                exit_code=1,
            )

        assert msg_store.add_message.call_count == 1
        msg = msg_store.add_message.call_args.args[0]
        assert msg.message_type == "OVERSEER_ALERT"
        # Subject includes the exit code for at-a-glance triage (#2811 review).
        assert msg.subject == "producer-permanent-death: coder exit=1 [high]"
        assert msg.from_role == "orchestrator"
        assert msg.to_role == "all"
        assert msg.metadata["anomaly_type"] == "producer-permanent-death"
        assert msg.metadata["role"] == "coder"
        assert msg.metadata["exit_code"] == 1
        assert msg.metadata["priority"] == "high"
        assert msg.metadata["phase"] == "implement"
        # Body conveys recovery guidance without prescribing the outcome.
        assert "permanently" in msg.body
        assert "restart_phase" in msg.body
        assert "cancel_task" in msg.body
        assert "FAILED" in msg.body

    def test_slice_id_propagates_into_metadata_and_body(self):
        """Slice-aware death surfaces the slice id so per-slice cascade is visible."""
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            _emit_producer_death_alert(
                pipeline_id="issue-2806",
                role="tester",
                phase="implement",
                slice_id="slice-2",
                exit_code=137,
            )

        msg = msg_store.add_message.call_args.args[0]
        assert msg.metadata["slice_id"] == "slice-2"
        assert "slice slice-2" in msg.body
        assert msg.metadata["exit_code"] == 137
        # Exit code in subject reflects the alternate kill (OOM = 137).
        assert msg.subject == "producer-permanent-death: tester exit=137 [high]"

    def test_swallows_message_store_unavailable(self):
        """Missing message store → log + return, no exception."""
        with patch("routes.pipelines._get_message_store", return_value=None):
            # Must not raise.
            _emit_producer_death_alert(
                pipeline_id="issue-2806",
                role="coder",
                phase="implement",
                slice_id=None,
                exit_code=1,
            )

    def test_swallows_message_store_exception(self):
        """add_message raising → log + return, no exception."""
        msg_store = MagicMock()
        msg_store.add_message.side_effect = RuntimeError("redis down")
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            # Must not raise — the polling loop has already decided to fail
            # the phase; a downstream alert failure must not derail that.
            _emit_producer_death_alert(
                pipeline_id="issue-2806",
                role="coder",
                phase="implement",
                slice_id=None,
                exit_code=1,
            )
