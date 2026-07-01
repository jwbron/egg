"""Tests for the unchanged-tree re-propose guard (#3395).

A re-propose whose commit SHA equals the current proposal's SHA carries
zero new commits, so the NACK blockers cannot have been addressed — it
would only bump the version, re-invoke every reviewer, and refresh the
overseer's post-proposal grace window. ``handle_re_propose`` rejects it
with a structured ``unchanged_tree_rejected`` envelope instead (the
push-triggered path was already short-circuited by
``check_auto_repropose``; the explicit path was unbounded and produced
the observed v3-v12 ten-cycle loop).
"""

import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph


@pytest.fixture
def single_reviewer_graph():
    """One producer, one reviewer — the open-NACK barrier (#2142) needs
    two or more distinct NACKing reviewers to fire, so a single-reviewer
    graph isolates the unchanged-tree guard (and mirrors the observed
    tester ↔ reviewer_code loop, where the barrier never engaged)."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def tracker(single_reviewer_graph):
    t = PeerConsensusTracker("test-pipeline-3395", single_reviewer_graph, cooldown_seconds=0)
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    return t


def _propose(tracker: PeerConsensusTracker, commit_sha: str) -> None:
    tracker.handle_propose(
        "tester",
        {
            "summary": (
                "Proposal with substantive content describing the work, "
                "tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": commit_sha,
        },
    )


def _nack(tracker: PeerConsensusTracker) -> None:
    tracker.handle_nack(
        "reviewer_code",
        "tester",
        {
            "artifact_references": ["a.py"],
            "reason": (
                "Named blocker on a.py — full reason text long enough to "
                "satisfy the ≥50 char content gate enforced upstream."
            ),
        },
    )


def _re_propose(tracker: PeerConsensusTracker, commit_sha: str) -> dict:
    return tracker.handle_re_propose(
        "tester",
        {
            "summary": (
                "Re-proposal with substantive content describing the fix, "
                "tests run, and tasks satisfied for re-review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": commit_sha,
        },
        ["a.py"],
    )


def test_unchanged_sha_re_propose_rejected(tracker):
    """Re-proposing the current proposal's SHA is rejected without bumping
    the version or invalidating any reviewer state."""
    _propose(tracker, "e29714a")
    _nack(tracker)

    result = _re_propose(tracker, "e29714a")

    assert result["status"] == "unchanged_tree_rejected"
    assert result["current_version"] == 1
    assert result["commit_sha"] == "e29714a"
    assert result["attempts"] == 1
    assert "zero new commits" in result["message"]
    # The version must NOT advance — a rejected re-propose re-invokes
    # nobody and leaves the reviewer's NACK as the live verdict.
    assert tracker.matrix.get_proposal_version("tester") == 1


def test_unchanged_sha_attempts_accumulate_until_sha_advances(tracker):
    """Consecutive unchanged-tree rejections count up; a re-propose that
    actually lands new commits proceeds and resets the counter."""
    _propose(tracker, "e29714a")
    _nack(tracker)

    assert _re_propose(tracker, "e29714a")["attempts"] == 1
    assert _re_propose(tracker, "e29714a")["attempts"] == 2

    # The real fix commit lands — the re-propose goes through.
    result = _re_propose(tracker, "7bb0dbc")
    assert result["status"] == "proposed"
    assert result["version"] == 2

    # A later unchanged attempt starts counting from 1 again.
    _nack(tracker)
    assert _re_propose(tracker, "7bb0dbc")["attempts"] == 1


def test_changed_sha_re_propose_unaffected(tracker):
    """The guard never fires on a commit-advancing re-propose."""
    _propose(tracker, "e29714a")
    _nack(tracker)

    result = _re_propose(tracker, "7bb0dbc")
    assert result["status"] == "proposed"
    assert result["version"] == 2
    assert tracker.get_proposal_commit_sha("tester") == "7bb0dbc"
