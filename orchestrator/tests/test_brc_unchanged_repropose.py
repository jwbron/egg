"""Tests for the unchanged-tree re-propose guard (#3395).

A re-propose whose commit SHA equals the current proposal's SHA carries
zero new commits, so the NACK blockers cannot have been addressed — it
would only bump the version, re-invoke every reviewer, and refresh the
overseer's post-proposal grace window. Both explicit propose paths reject
it with a structured ``unchanged_tree_rejected`` envelope instead (the
push-triggered path was already short-circuited by
``check_auto_repropose``; the explicit path was unbounded and produced
the observed v3-v12 ten-cycle loop).

The guard lives in a shared ``_unchanged_tree_guard_response`` helper
called from **both** ``handle_re_propose`` and ``handle_propose`` (#3415).
A re-propose without ``--changed-artifacts`` routes to ``handle_propose``
(``routes/signals/_consensus_verdicts.py``), so guarding only
``handle_re_propose`` left the exact single-reviewer incident scenario —
where the open-NACK barrier self-skips — unguarded.
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


# --- handle_propose bypass path (#3415) -------------------------------------
#
# A re-propose that omits ``changed_artifacts`` routes to ``handle_propose``,
# NOT ``handle_re_propose`` (routes/signals/_consensus_verdicts.py). After a
# NACK the producer is back in WORKING, so ``check_propose_guard`` allows the
# call and — without the mirrored guard — ``_handle_propose_inner`` would bump
# the version and re-invoke every reviewer with zero new commits. The
# single-reviewer graph is exactly the incident scenario: the open-NACK
# barrier self-skips with <2 distinct NACKing reviewers, so ``handle_propose``
# had no other churn protection.


def _re_propose_via_propose(tracker: PeerConsensusTracker, commit_sha: str) -> dict:
    """Re-propose through the ``changed_artifacts``-absent route — i.e. the
    signals handler's ``tracker.handle_propose(...)`` branch."""
    return tracker.handle_propose(
        "tester",
        {
            "summary": (
                "Re-proposal with substantive content describing the fix, "
                "tests run, and tasks satisfied for re-review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": commit_sha,
        },
    )


def test_unchanged_sha_re_propose_via_handle_propose_rejected(tracker):
    """The bypass path is guarded: an unchanged-SHA re-propose that omits
    ``changed_artifacts`` (routing to ``handle_propose``) is rejected and the
    version does NOT advance."""
    _propose(tracker, "e29714a")
    _nack(tracker)

    result = _re_propose_via_propose(tracker, "e29714a")

    assert result["status"] == "unchanged_tree_rejected"
    assert result["current_version"] == 1
    assert result["commit_sha"] == "e29714a"
    assert result["attempts"] == 1
    # The version must NOT advance through the handle_propose bypass either.
    assert tracker.matrix.get_proposal_version("tester") == 1


def test_handle_propose_bypass_and_re_propose_share_counter(tracker):
    """The attempt counter is shared across both propose paths — an unchanged
    attempt via ``handle_propose`` accumulates on the same counter as one via
    ``handle_re_propose``, and a SHA-advancing propose resets it."""
    _propose(tracker, "e29714a")
    _nack(tracker)

    assert _re_propose_via_propose(tracker, "e29714a")["attempts"] == 1
    assert _re_propose(tracker, "e29714a")["attempts"] == 2

    # A real fix commit lands via the bypass path — it proceeds and resets.
    result = _re_propose_via_propose(tracker, "7bb0dbc")
    assert result["status"] == "proposed"
    assert result["version"] == 2

    _nack(tracker)
    assert _re_propose_via_propose(tracker, "7bb0dbc")["attempts"] == 1


def test_initial_propose_never_caught_by_guard(tracker):
    """The guard no-ops on the first proposal (``current_sha == ""``) so a
    version 0 → 1 propose is never mistaken for an unchanged-tree re-propose."""
    result = _re_propose_via_propose(tracker, "e29714a")
    assert result["status"] == "proposed"
    assert result["version"] == 1
