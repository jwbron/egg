"""BRC edge-case coverage — issue #2635 follow-up.

Covers BRC paths that the unit tier has touched in isolation but
that weren't pinned at the integration boundary:

* Dual-role agents (tester is producer + reviewer) — both state
  machines must confirm before the agent is in the ``confirmed``
  set.
* Slice-aware tracker keys — per-slice trackers under
  ``{pipeline_id}/{slice_id}`` don't bleed into each other or
  into the bare ``pipeline_id`` tracker.
* Conditional ACK with ``pre_merge_condition`` + in-cycle
  resolution via ``handle_resolve_obligation`` (#2338).
* Stale-version ACK rejection — when a reviewer claims an ACK
  version that doesn't match the producer's current proposal,
  the guard rejects.

ScriptedProvider can't drive deployed agent pods (see #2474), so
the tests exercise the BRC Python API directly.
"""

from __future__ import annotations

import pytest
from _helpers import ack_payload, make_tracker, propose_payload
from events import EventType
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

pytestmark = pytest.mark.integration


class TestDualRoleAgent:
    """Tester is producer (writes tests) AND reviewer (runs tests on coder)."""

    PIPELINE_ID = "issue-2635-dual-role"

    def _graph(self) -> ReviewGraph:
        # coder produces; reviewer_code reviews coder.
        # tester produces (tests) AND reviews coder.
        return ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            ]
        )

    def test_tester_confirms_only_when_both_state_machines_confirm(self) -> None:
        """Dual-role tester needs producer-side AND reviewer-side confirmation."""
        graph = self._graph()
        assert graph.is_dual_role("tester") is True

        tracker = make_tracker(self.PIPELINE_ID, graph)

        # Both producers propose.
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        tracker.handle_propose("tester", propose_payload(commit_sha="def", artifacts=["test_x.py"]))

        # All cross-ACKs.
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        tracker.handle_ack("tester", "coder", {"ack_version": 1, **ack_payload()})
        tracker.handle_ack(
            "reviewer_code",
            "tester",
            {"ack_version": 1, **ack_payload(artifacts=["test_x.py"])},
        )

        # All three roles confirm — only after tester confirms BOTH
        # state machines (which happens in one ``handle_confirmed``
        # call since the same method walks both) does the global
        # consensus tip.
        tracker.handle_confirmed("reviewer_code")
        tracker.handle_confirmed("coder")
        result = tracker.handle_confirmed("tester")
        assert result["status"] == "confirmed"
        assert result["consensus_reached"] is True

        state = tracker.evaluate()
        assert state["is_complete"] is True
        # Tester appears in both producer and reviewer phase reports.
        tester_state = state["agents"]["tester"]
        # ``ConsensusPhase.CONFIRMED.value`` (the enum's serialised form)
        # is upper-cased — assert against the enum to insulate the test
        # from a future rename.
        from egg_orchestrator.types import ConsensusPhase

        assert tester_state["producer_phase"] == ConsensusPhase.CONFIRMED.value
        assert tester_state["reviewer_phase"] == ConsensusPhase.CONFIRMED.value
        assert tester_state["confirmed"] is True


class TestSliceAwareTrackerIsolation:
    """Per-slice trackers are keyed by ``{pipeline_id}/{slice_id}``."""

    PIPELINE_ID = "issue-2635-slice-aware"

    def test_two_slices_have_independent_trackers(self) -> None:
        """A NACK in slice-1 does not pollute slice-2's tracker state."""
        from peer_consensus import (
            create_peer_consensus_tracker,
            get_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )

        graph_1 = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        graph_2 = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])

        # Two slice trackers under the same pipeline id.
        remove_peer_consensus_tracker(self.PIPELINE_ID, slice_id="slice-1")
        remove_peer_consensus_tracker(self.PIPELINE_ID, slice_id="slice-2")
        tracker_1 = create_peer_consensus_tracker(
            self.PIPELINE_ID, graph_1, slice_id="slice-1", cooldown_seconds=0
        )
        tracker_2 = create_peer_consensus_tracker(
            self.PIPELINE_ID, graph_2, slice_id="slice-2", cooldown_seconds=0
        )
        for role in graph_1.all_roles():
            tracker_1.register_agent(role)
            tracker_2.register_agent(role)

        # slice-1: PROPOSE → NACK (consensus blocked).
        tracker_1.handle_propose("coder", propose_payload(commit_sha="aaa"))
        tracker_1.handle_nack(
            "reviewer_code",
            "coder",
            {"nack_version": 1, "artifact_references": ["a.py"], "reason": "bug"},
        )

        # slice-2: PROPOSE → ACK → CONFIRMED (consensus reached).
        tracker_2.handle_propose("coder", propose_payload(commit_sha="bbb"))
        tracker_2.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})
        tracker_2.handle_confirmed("coder")
        tracker_2.handle_confirmed("reviewer_code")

        # slice-1 still blocked; slice-2 done. The registry lookup
        # is what production routes use (signals, timeout handler).
        assert get_peer_consensus_tracker(self.PIPELINE_ID, "slice-1") is tracker_1
        assert get_peer_consensus_tracker(self.PIPELINE_ID, "slice-2") is tracker_2
        # The bare pipeline-level lookup is None — slice-aware trackers
        # don't pollute the umbrella key.
        assert get_peer_consensus_tracker(self.PIPELINE_ID) is None

        assert tracker_1.evaluate()["is_complete"] is False
        assert tracker_2.evaluate()["is_complete"] is True


class TestConditionalAckObligationResolution:
    """``pre_merge_condition`` lifecycle: conditional-ACK → resolve in-cycle (#2338)."""

    PIPELINE_ID = "issue-2635-conditional-ack"

    def test_conditional_ack_then_obligation_resolved_clears_merge_block(
        self, event_capture, filter_events
    ) -> None:
        """Conditional ACK creates a pre-merge obligation; resolution clears it.

        Before resolution: ``get_pre_merge_conditions`` returns the
        condition (PR-body builder surfaces it as a "do not merge until X"
        marker).  After ``handle_resolve_obligation``, the same query
        returns empty — the operator is no longer prompted to perform
        the obligation manually.

        The resolution emits ``CONSENSUS_OBLIGATION_RESOLVED`` so the
        SDLC skill's HITL gate can re-evaluate without polling the
        matrix.
        """
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = make_tracker(self.PIPELINE_ID, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        # Conditional ACK — reviewer says "I'd merge if you ran git mv X Y first".
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "ack_version": 1,
                "artifact_references": ["a.py"],
                "pre_merge_condition": "git mv old.py new.py before merge",
            },
        )
        # Plain ACK from the other reviewer (no condition).
        tracker.handle_ack("tester", "coder", {"ack_version": 1, **ack_payload()})

        # Condition is visible until resolved.
        before = tracker.get_pre_merge_conditions()
        assert len(before) == 1
        assert before[0]["reviewer"] == "reviewer_code"
        assert "git mv" in before[0]["condition"]

        # Tester resolves the obligation in-cycle (#2338 — the agent
        # that landed the conditioning commit reports it).
        result = tracker.handle_resolve_obligation(
            resolver_role="tester",
            reviewer_role="reviewer_code",
            producer_role="coder",
            commit_sha="resolved-by-tester-sha",
            note="git mv landed in this branch",
        )
        assert result["status"] == "resolved"
        assert result["remaining_pre_merge_conditions"] == []
        assert tracker.get_pre_merge_conditions() == []

        # Event emitted so the SDLC HITL gate can react without polling.
        resolved_events = filter_events(
            event_capture(),
            pipeline_id=self.PIPELINE_ID,
            event_type=EventType.CONSENSUS_OBLIGATION_RESOLVED,
        )
        assert len(resolved_events) == 1
        assert resolved_events[0].data["resolver"] == "tester"


class TestStaleVersionAckRejection:
    """``check_ack_guard`` rejects ACKs whose ``ack_version`` doesn't match current."""

    PIPELINE_ID = "issue-2635-stale-version"

    def test_acking_old_version_after_repropose_is_rejected(self, single_reviewer_graph) -> None:
        """A reviewer can't ACK v1 after the producer has re-proposed to v2."""
        tracker = make_tracker(self.PIPELINE_ID, single_reviewer_graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="v1"))
        # Reviewer NACKs v1.
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"nack_version": 1, "artifact_references": ["a.py"], "reason": "bug"},
        )
        # Producer re-proposes (v2).
        tracker.handle_re_propose(
            "coder",
            propose_payload(commit_sha="v2"),
            changed_artifacts=["a.py"],
        )
        # Reviewer mistakenly tries to ACK the old v1 — guard rejects.
        with pytest.raises(ValueError, match="version"):
            tracker.handle_ack(
                "reviewer_code",
                "coder",
                {"ack_version": 1, **ack_payload()},
            )
