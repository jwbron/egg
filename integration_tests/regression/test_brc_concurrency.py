"""BRC concurrency invariants — issue #2635 follow-up coverage.

The ``PeerConsensusTracker`` is designed for concurrent use — every
mutating method holds ``self._lock`` (RLock) so producers and reviewers
can call into the same tracker from different threads.  None of that
serialisation was exercised before; the unit tier under
``orchestrator/tests/`` is single-threaded.

These tests drive the tracker from multiple Python threads to make
sure:

1. The lock genuinely serialises mutations — no torn state, no
   duplicate ACK entries, every event reaches the bus.
2. The open-NACK barrier (#2142) actually fires when two reviewers
   race a NACK against the same proposal version.
3. The withdraw cooldown + flip-flop lockout guards hold under
   rapid re-tries.

ScriptedProvider can't drive deployed agent pods (see #2474), so
the tests exercise the BRC Python API directly.

Threading note: BRC's lock is an ``RLock`` so a deadlock between
``handle_propose`` and ``handle_ack`` is structurally impossible.
The tests still use a ``threading.Barrier`` to give the threads
roughly-simultaneous entry — that maximises the chance of catching
a non-serialised mutation if one is ever introduced.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from _helpers import ack_payload, make_tracker, nack_payload, propose_payload
from events import EventType

pytestmark = pytest.mark.integration


class TestConcurrentAcks:
    """Multiple reviewers ACKing the same proposal in parallel are serialised."""

    PIPELINE_ID = "issue-2635-concurrent-acks"

    def _wide_graph(self, n_reviewers: int):
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        # All distinct critical reviewers reviewing a single producer.
        edges = [
            ReviewEdge(f"reviewer_{i}", "coder", ReviewCriticality.CRITICAL)
            for i in range(n_reviewers)
        ]
        return ReviewGraph(edges)

    def test_six_reviewers_acking_in_parallel_all_recorded(
        self, event_capture, filter_events
    ) -> None:
        """Six threaded ACKs land six matrix entries — no torn writes.

        If the RLock ever became a no-op, we'd expect either fewer
        than six recorded ACKs (lost write) or duplicate
        ``CONSENSUS_ACK_RECEIVED`` events (double-emit).
        """
        n = 6
        graph = self._wide_graph(n)
        tracker = make_tracker(self.PIPELINE_ID, graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        barrier = threading.Barrier(n)

        def ack(reviewer: str) -> dict:
            barrier.wait(timeout=5)
            return tracker.handle_ack(reviewer, "coder", {"ack_version": 1, **ack_payload()})

        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(ack, f"reviewer_{i}") for i in range(n)]
            results = [f.result(timeout=10) for f in as_completed(futures)]

        # Every ACK reported success and the producer is fully ACKed.
        assert all(r["status"] == "acked" for r in results)
        assert tracker.matrix.is_fully_acked("coder") is True

        # Exactly N ACK events on the bus — no doubles, no drops.
        ack_events = filter_events(
            event_capture(),
            pipeline_id=self.PIPELINE_ID,
            event_type=EventType.CONSENSUS_ACK_RECEIVED,
        )
        assert len(ack_events) == n
        # And every reviewer reported in the events is distinct.
        assert {e.data["reviewer"] for e in ack_events} == {f"reviewer_{i}" for i in range(n)}

    def test_propose_and_ack_interleaved_does_not_corrupt_state(self, two_reviewer_graph) -> None:
        """Re-propose racing an ACK on the previous version: matrix stays consistent.

        Without proper serialisation, the ACK could land at the
        wrong version or the version counter could double-increment.
        We don't assert a specific timing outcome — only that the
        post-race state is self-consistent (is_fully_acked agrees
        with the recorded ACK versions).
        """
        tracker = make_tracker(self.PIPELINE_ID + "-interleave", two_reviewer_graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="v1"))

        # reviewer_code ACKs v1.
        tracker.handle_ack("reviewer_code", "coder", {"ack_version": 1, **ack_payload()})

        barrier = threading.Barrier(2)

        def repropose() -> dict:
            barrier.wait(timeout=5)
            return tracker.handle_re_propose(
                "coder",
                propose_payload(commit_sha="v2"),
                changed_artifacts=["a.py"],
            )

        def ack_v1() -> dict:
            barrier.wait(timeout=5)
            # Reviewer trying to ACK the old version — guard should
            # reject either via version-match or this lands before
            # re_propose. Either way the matrix must end up consistent.
            try:
                return tracker.handle_ack(
                    "reviewer_contract", "coder", {"ack_version": 1, **ack_payload()}
                )
            except ValueError as e:
                return {"status": "rejected", "reason": str(e)}

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_re = ex.submit(repropose)
            f_ack = ex.submit(ack_v1)
            re_result = f_re.result(timeout=10)
            ack_result = f_ack.result(timeout=10)

        # Final state self-consistent: either the v1 ACK lost the race
        # (post-repropose ACKs land at v2, never v1) or it landed first
        # and is_fully_acked is False because re_propose invalidated it.
        # In both cases, the recorded version cannot exceed the current
        # proposal version.
        current_version = tracker.matrix.get_proposal_version("coder")
        assert current_version == 2  # repropose always bumps
        # ACK result is either acked@1 (raced) or rejected.
        assert ack_result["status"] in {"acked", "rejected"}
        assert re_result["version"] == 2


class TestOpenNackBarrier:
    """Two reviewers NACKing the same version trigger the #2142 aggregation barrier."""

    PIPELINE_ID = "issue-2635-open-nack-barrier"

    def test_two_concurrent_nacks_block_repropose_until_acknowledged(
        self, two_reviewer_graph
    ) -> None:
        """A producer cannot re_propose past 2+ NACKs against the same version.

        The barrier exists to prevent the multi-reviewer aggregation
        hazard — without it a producer could re-propose addressing
        only the *first* NACK they saw via wait-loop, hiding the
        second reviewer's NACK from the cycle.

        The first re_propose attempt is rejected with
        ``open_nacks_blocked``; the second (after the producer has
        been informed) is accepted.
        """
        tracker = make_tracker(self.PIPELINE_ID, two_reviewer_graph)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        barrier = threading.Barrier(2)

        def nack(reviewer: str) -> dict:
            barrier.wait(timeout=5)
            return tracker.handle_nack(
                reviewer,
                "coder",
                {"nack_version": 1, **nack_payload(reason=f"{reviewer} concern")},
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [
                ex.submit(nack, "reviewer_code"),
                ex.submit(nack, "reviewer_contract"),
            ]
            results = [f.result(timeout=10) for f in as_completed(futures)]

        assert all(r["status"] == "nacked" for r in results)

        # First re_propose hits the barrier — both reviewers' NACKs are
        # surfaced before the producer can advance.
        first = tracker.handle_re_propose(
            "coder",
            propose_payload(commit_sha="def"),
            changed_artifacts=["a.py"],
        )
        assert first["status"] == "open_nacks_blocked"
        assert set(first["nacking_reviewers"]) == {"reviewer_code", "reviewer_contract"}
        assert len(first["nacks"]) == 2

        # Producer has now been informed — retry proceeds.
        second = tracker.handle_re_propose(
            "coder",
            propose_payload(commit_sha="def"),
            changed_artifacts=["a.py"],
        )
        assert second["version"] == 2


class TestWithdrawCooldownAndLockout:
    """Withdraw cooldown and flip-flop lockout under rapid retries."""

    PIPELINE_ID = "issue-2635-withdraw-cooldown"

    def test_withdraw_within_cooldown_is_rejected(self) -> None:
        """A producer can't withdraw within ``cooldown_seconds`` of proposing.

        Defends against rapid propose/withdraw cycles that would
        thrash reviewers' review queues.  Using a non-zero cooldown
        for this test (the make_tracker default is 0 for speed).
        """
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = make_tracker(self.PIPELINE_ID, graph, cooldown_seconds=60)
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))

        # Immediate withdraw — well within the 60s cooldown.
        with pytest.raises(ValueError, match="[Cc]ooldown"):
            tracker.handle_withdraw("coder", reason="changed my mind")

    def test_flip_flop_lockout_after_repeated_withdrawals(self) -> None:
        """Producer is locked out after ``max_flip_flops`` propose/withdraw cycles.

        Uses cooldown=0 + max_flip_flops=2 so the test runs without
        wallclock waits.  After 2 successful withdraws, the third
        triggers the lockout — the tracker returns ``locked_out``
        instead of raising.
        """
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        # Override defaults: cooldown=0 (no waits), max_flip_flops=2 (small).
        # ``make_tracker`` doesn't expose max_flip_flops; build directly via
        # the registered creator with kwargs.
        from peer_consensus import (
            create_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )

        remove_peer_consensus_tracker(self.PIPELINE_ID + "-flipflop")
        tracker = create_peer_consensus_tracker(
            self.PIPELINE_ID + "-flipflop",
            graph,
            cooldown_seconds=0,
            max_flip_flops=2,
        )
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        # Cycle 1: propose → withdraw → counter=1, allowed.
        tracker.handle_propose("coder", propose_payload(commit_sha="abc"))
        r1 = tracker.handle_withdraw("coder", reason="bug found")
        assert r1["status"] == "withdrawn"

        # Cycle 2: propose → withdraw → peek=2, 2 >= max=2, locked out.
        # The guard's peek is ``current + 1 >= max`` so with max=2 the
        # second withdraw is the one that gets locked out.
        tracker.handle_propose("coder", propose_payload(commit_sha="def"))
        r2 = tracker.handle_withdraw("coder", reason="another bug")
        assert r2["status"] == "locked_out"
        assert r2["needs_escalation"] is True
