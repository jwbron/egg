"""BRC gap-audit invariants — issue #2635.

Covers the failure modes flagged in the issue's gap-audit section
that weren't already pinned at the integration boundary:

* NACK → re-propose → ACK round-trip restores consensus.
* Reviewer disagreement (one ACK + one NACK) blocks consensus and
  unresolved-NACK signal surfaces on the bus.
* Mid-cycle restart: ``reconstruct_tracker_from_messages`` replays
  PROPOSE/ACK/CONFIRMED into a fresh tracker without re-emitting
  message-bus events.  This is the orchestrator-restart recovery
  contract — without it, the consensus state is lost on every
  pod cycle.
* Phase-specific config inheritance: per-phase overrides do not
  bleed across phases (regression for the precedence order the
  resolver implements).

ScriptedProvider can't drive deployed agent pods (see #2474), so
these tests exercise the orchestrator's BRC Python API directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import ack_payload, make_tracker, nack_payload, propose_payload
from events import EventType
from message_store import Message, MessageStore, MessageType
from models import (
    PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN,
    PipelineConfig,
    resolve_consensus_timeout_minutes,
)
from peer_consensus import reconstruct_tracker_from_messages
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

pytestmark = pytest.mark.integration


class TestNackReproposeRoundTrip:
    """Producer NACK → re-propose → fresh ACKs restores consensus."""

    PIPELINE_ID = "issue-2635-nack-roundtrip"

    def test_nack_then_repropose_then_ack_reaches_consensus(
        self, single_reviewer_graph, event_capture, filter_events
    ) -> None:
        tracker = make_tracker(self.PIPELINE_ID, single_reviewer_graph)

        # v1: PROPOSE → NACK
        tracker.handle_propose("coder", propose_payload(commit_sha="aaa1111"))
        nack = tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"nack_version": 1, **nack_payload(reason="SQLi on a.py:42")},
        )
        assert nack["status"] == "nacked"
        assert nack["revision_count"] == 1
        assert nack["needs_escalation"] is False
        # Producer is back in WORKING, consensus blocked by unresolved NACK.
        state = tracker.evaluate()
        assert state["is_complete"] is False
        assert state["has_unresolved_nacks"] is True

        # v2: re-PROPOSE → ACK — fresh version invalidates the stale ACK/NACK.
        repropose = tracker.handle_re_propose(
            "coder",
            propose_payload(commit_sha="bbb2222"),
            changed_artifacts=["a.py"],
        )
        assert repropose["version"] == 2
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 2, **ack_payload()})
        tracker.handle_confirmed("coder")
        result = tracker.handle_confirmed("reviewer_code")
        assert result["consensus_reached"] is True

        state = tracker.evaluate()
        assert state["is_complete"] is True
        assert state["has_unresolved_nacks"] is False

        # Event-count contract for the round-trip path.
        events = event_capture()
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_PROPOSE_RECEIVED,
                )
            )
            == 2
        )
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_NACK_RECEIVED,
                )
            )
            == 1
        )
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_REACHED,
                )
            )
            == 1
        )


class TestReviewerDisagreement:
    """One ACK + one NACK from critical reviewers blocks consensus."""

    PIPELINE_ID = "issue-2635-disagreement"

    def test_split_critical_reviewers_block_consensus(
        self, two_reviewer_graph, event_capture, filter_events
    ) -> None:
        tracker = make_tracker(self.PIPELINE_ID, two_reviewer_graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="ccc3333"))

        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        tracker.handle_nack(
            "reviewer_contract",
            "coder",
            {"nack_version": 1, **nack_payload(reason="contract drift in models.py")},
        )

        state = tracker.evaluate()
        assert state["is_complete"] is False
        assert state["has_unresolved_nacks"] is True
        # The NACKing reviewer surfaces in the unresolved set.
        nacking = [n["reviewer"] for n in state["unresolved_nacks"]]
        assert "reviewer_contract" in nacking

        # The producer cannot confirm — pending NACK from reviewer_contract.
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"

        # Event payload reflects the disagreement.
        nack_events = filter_events(
            event_capture(),
            pipeline_id=self.PIPELINE_ID,
            event_type=EventType.CONSENSUS_NACK_RECEIVED,
        )
        assert len(nack_events) == 1
        assert nack_events[0].data["reviewer"] == "reviewer_contract"
        assert nack_events[0].data["producer"] == "coder"


class TestMidCycleRestartReplay:
    """``reconstruct_tracker_from_messages`` rebuilds state from persisted msgs."""

    PIPELINE_ID = "issue-2635-restart-replay"

    def _record_message(
        self,
        store: MessageStore,
        *,
        from_role: str,
        to_role: str,
        message_type: str,
        timestamp: datetime,
        metadata: dict | None = None,
        body: str = "",
    ) -> Message:
        msg = Message(
            pipeline_id=self.PIPELINE_ID,
            from_role=from_role,
            to_role=to_role,
            message_type=message_type,
            subject=message_type,
            body=body,
            metadata=metadata or {},
            timestamp=timestamp,
        )
        store.add_message(msg)
        return msg

    def test_replay_rebuilds_consensus_state(self) -> None:
        """Persisted PROPOSE+ACK+CONFIRMED messages reconstruct a confirmed tracker.

        Drops a real ``MessageStore`` and ``reconstruct_tracker_from_messages``
        with it, then asserts the rebuilt tracker reports consensus reached.
        This is the regression for a pod-cycle that loses the in-memory
        tracker mid-cycle (#2429-style scenarios).
        """
        store = MessageStore()
        now = datetime.now(UTC)

        # Producer proposed at t-30s.
        self._record_message(
            store,
            from_role="coder",
            to_role="reviewer_code",
            message_type=MessageType.CONSENSUS_PROPOSE,
            timestamp=now - timedelta(seconds=30),
            metadata={
                "payload": {
                    "summary": "v1",
                    "artifacts": ["a.py"],
                    "commit_sha": "abc1234",
                }
            },
        )
        # Reviewer ACKed at t-20s.
        self._record_message(
            store,
            from_role="reviewer_code",
            to_role="coder",
            message_type=MessageType.CONSENSUS_ACK,
            timestamp=now - timedelta(seconds=20),
            metadata={
                "payload": {
                    "artifact_references": ["a.py"],
                    "ack_version": 1,
                }
            },
        )
        # Both roles confirmed.
        self._record_message(
            store,
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_CONFIRMED,
            timestamp=now - timedelta(seconds=10),
        )
        self._record_message(
            store,
            from_role="reviewer_code",
            to_role="all",
            message_type=MessageType.CONSENSUS_CONFIRMED,
            timestamp=now - timedelta(seconds=5),
        )

        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = reconstruct_tracker_from_messages(self.PIPELINE_ID, graph, message_store=store)
        assert tracker is not None
        state = tracker.evaluate()
        # The rebuilt tracker reports the same consensus shape the live one
        # would have — that's the contract that lets a restarted orchestrator
        # resume without re-running the BRC cycle.
        assert state["is_complete"] is True
        assert state["blocking_agents"] == []
        # ``coder``'s commit SHA must round-trip — the ``_update_agents_complete``
        # path reads this back to populate ``agent.commit`` (#1691).
        assert tracker.get_proposal_commit_sha("coder") == "abc1234"

    def test_replay_with_no_messages_returns_none(self) -> None:
        """An empty message store yields ``None`` rather than a phantom tracker."""
        store = MessageStore()
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = reconstruct_tracker_from_messages(
            self.PIPELINE_ID + "-empty", graph, message_store=store
        )
        assert tracker is None


class TestPhaseConfigInheritance:
    """Per-phase timeout overrides don't bleed across phases.

    Complements the unit-tier resolver tests by asserting the
    precedence holds when multiple per-phase overrides are set
    simultaneously — the production case where ``refine`` and
    ``implement`` have explicit overrides but ``plan`` falls back
    to the calibrated default.
    """

    def test_three_phase_overrides_isolate_correctly(self) -> None:
        config = PipelineConfig(
            consensus_timeout_minutes_refine=20,
            consensus_timeout_minutes_implement=120,
        )
        assert resolve_consensus_timeout_minutes(config, "refine") == 20
        # plan has no override and no legacy global → calibrated default.
        assert (
            resolve_consensus_timeout_minutes(config, "plan")
            == PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["plan"]
        )
        assert resolve_consensus_timeout_minutes(config, "implement") == 120

    def test_legacy_global_with_one_phase_override_yields_two_buckets(self) -> None:
        """Legacy global wins where no per-phase override is set, override wins where one is."""
        config = PipelineConfig(
            consensus_timeout_minutes=45,
            consensus_timeout_minutes_implement=120,
        )
        assert resolve_consensus_timeout_minutes(config, "refine") == 45
        assert resolve_consensus_timeout_minutes(config, "plan") == 45
        assert resolve_consensus_timeout_minutes(config, "implement") == 120
