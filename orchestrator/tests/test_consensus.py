"""Tests for consensus module — BRC protocol and deprecated READY-tallying.

Verifies that:
- The deprecated ConsensusEvaluator still functions for backwards compatibility
- The BRC PeerConsensusTracker is the primary consensus mechanism
- ConsensusEvaluator.evaluate() returns correct state for READY/BLOCKED/OBJECTING
- Deprecated readiness states are properly handled
"""

import sys
from pathlib import Path

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from consensus import ConsensusEvaluator, ReadinessState


class TestConsensusEvaluatorDeprecated:
    """Tests for the deprecated ConsensusEvaluator READY-tallying.

    This module is superseded by PeerConsensusTracker (BRC protocol)
    but kept for backwards compatibility during transition.
    """

    def test_register_agent(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")

        state = evaluator.evaluate("pipeline-1")
        assert not state["is_complete"]
        assert "coder" in state["blocking_agents"]

    def test_all_ready_is_complete(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.register_agent("pipeline-1", "tester")

        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)
        evaluator.update_readiness("pipeline-1", "tester", ReadinessState.READY)

        state = evaluator.evaluate("pipeline-1")
        assert state["is_complete"] is True
        assert state["blocking_agents"] == []

    def test_objection_blocks_consensus(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.register_agent("pipeline-1", "reviewer_code")

        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)
        evaluator.update_readiness(
            "pipeline-1", "reviewer_code", ReadinessState.OBJECTING, reason="Bug found"
        )

        state = evaluator.evaluate("pipeline-1")
        assert state["is_complete"] is False
        assert state["has_objections"] is True
        assert "reviewer_code" in state["blocking_agents"]

    def test_blocked_prevents_consensus(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.register_agent("pipeline-1", "tester")

        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)
        evaluator.update_readiness(
            "pipeline-1", "tester", ReadinessState.BLOCKED, reason="Waiting for coder"
        )

        state = evaluator.evaluate("pipeline-1")
        assert state["is_complete"] is False
        assert "tester" in state["blocking_agents"]

    def test_empty_pipeline_not_complete(self):
        evaluator = ConsensusEvaluator()
        state = evaluator.evaluate("nonexistent")
        assert state["is_complete"] is False
        assert state["blocking_agents"] == []

    def test_remove_agent(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.register_agent("pipeline-1", "tester")

        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)
        evaluator.remove_agent("pipeline-1", "tester")

        state = evaluator.evaluate("pipeline-1")
        # Only coder remains and is READY
        assert state["is_complete"] is True

    def test_clear_pipeline(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)
        evaluator.clear("pipeline-1")

        state = evaluator.evaluate("pipeline-1")
        assert state["is_complete"] is False
        assert state["agents"] == {}

    def test_auto_register_on_update(self):
        evaluator = ConsensusEvaluator()
        # update_readiness should auto-register if not registered
        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)

        state = evaluator.evaluate("pipeline-1")
        assert state["is_complete"] is True

    def test_pipeline_isolation(self):
        evaluator = ConsensusEvaluator()
        evaluator.register_agent("pipeline-1", "coder")
        evaluator.register_agent("pipeline-2", "tester")

        evaluator.update_readiness("pipeline-1", "coder", ReadinessState.READY)

        state1 = evaluator.evaluate("pipeline-1")
        state2 = evaluator.evaluate("pipeline-2")

        assert state1["is_complete"] is True
        assert state2["is_complete"] is False


class TestBRCPeerConsensusIsPreferred:
    """Verify that PeerConsensusTracker is the preferred consensus mechanism."""

    def test_peer_consensus_tracker_exists(self):
        from peer_consensus import PeerConsensusTracker

        assert PeerConsensusTracker is not None

    def test_consensus_module_has_deprecation_comment(self):
        """consensus.py should have a deprecation note."""
        import consensus

        source_file = Path(consensus.__file__)
        content = source_file.read_text()
        assert "DEPRECATED" in content or "deprecated" in content

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
