"""BRC happy-path consensus — issue #2635 starting point 1.

Verifies the single-cycle PROPOSE → ACK → CONFIRMED protocol drives
to consensus with exact message counts on the event bus.

Why this lives in ``integration_tests/regression/``:

The unit tier under ``orchestrator/tests/`` covers the
``PeerConsensusTracker`` internals exhaustively (``test_brc_*.py``),
but no test asserts the end-to-end event-counts contract that
external consumers — the SSE bridge, the SDLC skill, the
``babysit_pr`` BRC bridge — depend on.  When a future refactor
re-routes one event type through a wrapper or skips emission on a
fast path, the unit tier won't catch it; this regression will.

ScriptedProvider can't drive deployed agent pods (see #2474), so
the test exercises the orchestrator's BRC Python API directly.
"""

from __future__ import annotations

import pytest
from _helpers import ack_payload, make_tracker, propose_payload
from events import EventType

pytestmark = pytest.mark.integration


class TestBRCSingleCycleHappyPath:
    """Producer PROPOSE → reviewer ACK → both CONFIRMED, no NACKs, no retries."""

    PIPELINE_ID = "issue-2635-single-cycle"

    def test_minimal_topology_reaches_consensus(
        self, single_reviewer_graph, event_capture, filter_events
    ) -> None:
        """1 producer + 1 critical reviewer is the smallest valid BRC graph."""
        tracker = make_tracker(self.PIPELINE_ID, single_reviewer_graph)

        # ---- PROPOSE ----
        result = tracker.handle_propose("coder", propose_payload(commit_sha="abc1234"))
        assert result["status"] == "proposed"
        assert result["version"] == 1
        assert result["reviewers"] == ["reviewer_code"]

        # ---- ACK ----
        result = tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        assert result["status"] == "acked"
        assert result["fully_acked"] is True

        # ---- CONFIRMED (producer) ----
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "confirmed"
        # coder has confirmed but reviewer hasn't yet → no global consensus
        assert result["consensus_reached"] is False

        # ---- CONFIRMED (reviewer) ----
        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] == "confirmed"
        assert result["consensus_reached"] is True

        # Tracker-level state is consistent with the event stream.
        state = tracker.evaluate()
        assert state["is_complete"] is True
        assert state["blocking_agents"] == []
        assert state["has_unresolved_nacks"] is False

        # Exact event counts — the regression invariant.
        events = event_capture()
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_PROPOSE_RECEIVED,
                )
            )
            == 1
        )
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_ACK_RECEIVED,
                )
            )
            == 1
        )
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_NACK_RECEIVED,
                )
            )
            == 0
        )
        assert (
            len(
                filter_events(
                    events,
                    pipeline_id=self.PIPELINE_ID,
                    event_type=EventType.CONSENSUS_CONFIRMED_RECEIVED,
                )
            )
            == 2
        )
        reached = filter_events(
            events, pipeline_id=self.PIPELINE_ID, event_type=EventType.CONSENSUS_REACHED
        )
        assert len(reached) == 1
        assert reached[0].data["protocol"] == "brc"
        assert sorted(reached[0].data["confirmed_roles"]) == ["coder", "reviewer_code"]

    def test_two_reviewer_topology_reaches_consensus(
        self, two_reviewer_graph, event_capture, filter_events
    ) -> None:
        """Single PROPOSE drives 2 ACK + 3 CONFIRMED events.

        Two critical reviewers each ACK the single producer's proposal at
        the same version.  Then all three roles confirm — the producer
        last because it cannot confirm until both reviewers have ACKed
        (``check_confirm_guard.producer_not_fully_acked``).
        """
        pipeline_id = self.PIPELINE_ID + "-two-rev"
        tracker = make_tracker(pipeline_id, two_reviewer_graph)

        tracker.handle_propose("coder", propose_payload(commit_sha="def5678"))
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        tracker.handle_ack("reviewer_contract", "coder", {"ack_version": 1, **ack_payload()})
        # Reviewers confirm first, then producer (matches production order).
        tracker.handle_confirmed("reviewer_code")
        tracker.handle_confirmed("reviewer_contract")
        result = tracker.handle_confirmed("coder")
        assert result["consensus_reached"] is True

        events = event_capture()
        counts = {
            EventType.CONSENSUS_PROPOSE_RECEIVED: 1,
            EventType.CONSENSUS_ACK_RECEIVED: 2,
            EventType.CONSENSUS_NACK_RECEIVED: 0,
            EventType.CONSENSUS_CONFIRMED_RECEIVED: 3,
            EventType.CONSENSUS_REACHED: 1,
        }
        for event_type, expected in counts.items():
            actual = filter_events(events, pipeline_id=pipeline_id, event_type=event_type)
            assert len(actual) == expected, f"{event_type}: expected {expected}, got {len(actual)}"


class TestBRCSingleCycleGuardsHonored:
    """Out-of-order calls fail rather than silently advancing state."""

    PIPELINE_ID = "issue-2635-single-cycle-guards"

    def test_pre_proposal_ack_is_recorded_then_invalidated(self, single_reviewer_graph) -> None:
        """A pre-proposal ACK is *recorded at version 0* and invalidated on PROPOSE.

        ``check_ack_guard``'s version-match clause only fires when the
        producer's current proposal version is ``> 0`` — a reviewer
        racing the producer with ``ack_version=1`` therefore lands a
        version-0 ACK in the matrix.  The producer's first PROPOSE
        bumps the version and ``_invalidate_pre_proposal_acks`` clears
        the stale entry, surfacing the invalidated reviewer in
        ``stale_reviewers`` so the wait-loop knows to re-review.

        This test pins the *actual* behavior so a future refactor of
        the guard ordering doesn't silently regress the invalidation
        rescue (see #2635 gap-audit note in the PR body).
        """
        tracker = make_tracker(self.PIPELINE_ID + "-ack", single_reviewer_graph)
        # Reviewer ACKs before producer proposes — guard accepts because
        # current proposal version is 0.  Omitting ``ack_version`` so the
        # version-match clause skips (test-path comment in handle_ack).
        result = tracker.handle_ack("reviewer_code", "coder", ack_payload())
        assert result["status"] == "acked"
        assert result["version"] == 0
        # Now the producer proposes — invalidation kicks in.
        result = tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        assert "reviewer_code" in result["stale_reviewers"]

    def test_producer_confirm_before_full_acks_is_pending(self, two_reviewer_graph) -> None:
        """A producer must wait for every critical reviewer to ACK.

        The guard returns ``pending_acks`` (not a raise) so the
        producer's wait-loop is informed to keep polling.
        """
        pipeline_id = self.PIPELINE_ID + "-prod"
        tracker = make_tracker(pipeline_id, two_reviewer_graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        # Only reviewer_code has ACKed; reviewer_contract has not.
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"
        assert tracker.evaluate()["is_complete"] is False
