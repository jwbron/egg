"""Tests for the BRC PeerConsensusTracker — the primary consensus mechanism.

The legacy ``ConsensusEvaluator`` and its ``READY``-tallying protocol were
hard-removed in #2777 (cq-5 / TASK-2-6 of the slice-2 cleanup). The
deprecated class lived in ``orchestrator/consensus.py``; the file is
deleted and tests that asserted on its surface (TestConsensusEvaluatorDeprecated
and TestBRCPeerConsensusIsPreferred.test_consensus_module_has_deprecation_comment)
are removed lock-step. This module retains the BRC-protocol smoke tests
that still apply.
"""

import sys
from pathlib import Path

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


class TestBRCPeerConsensusIsPreferred:
    """Verify that PeerConsensusTracker is the consensus mechanism."""

    def test_peer_consensus_tracker_exists(self):
        from peer_consensus import PeerConsensusTracker

        assert PeerConsensusTracker is not None

    def test_legacy_consensus_module_is_removed(self):
        """orchestrator/consensus.py was deleted in #2777 (cq-5 / TASK-2-6).

        This is the negative-assert counterpart to the deletion: the
        legacy ``consensus`` module must NOT be importable, so future
        callers cannot resurrect the deprecated ``ConsensusEvaluator``
        surface by accident.
        """
        # Drop any stale ``consensus`` module that a prior test in the
        # same pytest session may have monkey-patched into ``sys.modules``
        # (a few tests in this repo use ``patch.dict(sys.modules, ...)``
        # to shim the name). Without this guard, the import below would
        # silently succeed against the stub instead of failing closed.
        sys.modules.pop("consensus", None)
        try:
            import consensus  # noqa: F401
        except ModuleNotFoundError:
            return
        else:
            raise AssertionError(
                "orchestrator/consensus.py should be removed (#2777 cq-5 / TASK-2-6) "
                "but the ``consensus`` module is still importable. The deprecated "
                "ConsensusEvaluator surface must not be resurrected."
            )

    def test_brc_evaluate_returns_protocol_field(self):
        """BRC tracker evaluate() should identify protocol as 'brc'."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        state = tracker.evaluate()
        assert state["protocol"] == "brc"
