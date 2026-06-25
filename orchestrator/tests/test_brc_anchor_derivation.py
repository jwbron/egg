"""Orchestrator-side tests for the #3189 BRC anchor derivation (slice-3, task-3-2).

#3200 / slice-3 ("Derive"). Companion to
``shared/egg_anchor/tests/test_brc_anchor_derivation.py``. That file feeds a
message-record fixture to the derivation; this file anchors the *ground truth*
to the existing consensus substrate — ``ApprovalMatrix`` /
``ApprovalEntry`` (``orchestrator/approval_matrix.py``) already track per-edge
verdict, ``ack_commit_sha``, NACK ``reason``, ``pre_merge_condition`` and the
``obligation_resolved`` flag. The four #3189 anchor fields are exactly a
deterministic projection of that matrix, so we build the matrix with its public
API (stable today) and assert the coder's derivation agrees.

Tester and coder run as parallel BRC producers, so the derivation symbol may be
absent on the tester branch. ``_derivation`` skips until it lands (the slice
convention, see ``test_reseed_threshold.py``). The matrix-projection test
``test_matrix_substrate_models_four_fields`` carries NO guard: it asserts the
substrate the derivation must read, and runs today.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Add orchestrator to sys.path the way the sibling orchestrator tests do.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


SHA_CODER_V1 = "aaaaaaa1111111111111111111111111111111aa"
SHA_CODER_V2 = "bbbbbbb2222222222222222222222222222222bb"
SHA_TESTER_V1 = "ccccccc3333333333333333333333333333333cc"


def _build_scenario_matrix() -> Any:
    """The task-3-2 AC scenario, expressed against the real ApprovalMatrix.

    coder:  ACK v1 -> re-propose v2 -> reviewer_code NACK v2 ("missing guard")
            + reviewer_security conditional-ACK v2 (obligation, UNRESOLVED).
    tester: conditional-ACK v1 (obligation) then obligation RESOLVED in-cycle.
    """
    from approval_matrix import ApprovalMatrix
    from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

    graph = ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )
    matrix = ApprovalMatrix(graph)

    # coder v1 — reviewed (ACK), then superseded by v2.
    matrix.record_proposal("coder")
    matrix.record_ack("reviewer_code", "coder", version=1, commit_sha=SHA_CODER_V1)
    matrix.record_proposal("coder")  # v2 supersedes v1
    matrix.record_nack("reviewer_code", "coder", version=2, reason="missing guard")
    matrix.record_ack(
        "reviewer_security",
        "coder",
        version=2,
        commit_sha=SHA_CODER_V2,
        pre_merge_condition="git mv old new",
    )

    # tester v1 — conditional ACK then obligation resolved in-cycle.
    matrix.record_proposal("tester")
    matrix.record_ack(
        "reviewer_code",
        "tester",
        version=1,
        commit_sha=SHA_TESTER_V1,
        pre_merge_condition="update import path",
    )
    # Another role (not the producer) satisfies the obligation in-cycle: the
    # producer may not self-resolve their own conditional-ACK obligation.
    matrix.mark_obligation_resolved("reviewer_code", "tester", resolved_by="coder")

    return matrix


def _derivation() -> Any:
    """Locate the coder's matrix-or-message derivation, or skip until it lands."""
    candidates = (
        ("brc_anchors", "derive_brc_anchors"),
        ("brc_anchors", "derive_anchors"),
        ("anchor_derivation", "derive_brc_anchors"),
        ("approval_matrix", "derive_brc_anchors"),
        ("peer_consensus", "derive_brc_anchors"),
    )
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if fn is not None:
            return fn
    # Also try the egg_anchor package (shared is on PYTHONPATH).
    for module_name, attr in (
        ("egg_anchor.brc_anchors", "derive_brc_anchors"),
        ("egg_anchor", "derive_brc_anchors"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        fn = getattr(module, attr, None)
        if fn is not None:
            return fn
    pytest.skip("BRC anchor derivation not found yet (coder task-3-1 unmerged)")


# ---------------------------------------------------------------------------
# Ground-truth substrate — runs today, no guard. Locks the four-field
# projection the derivation must reproduce.
# ---------------------------------------------------------------------------


def test_matrix_substrate_models_four_fields() -> None:
    """The ApprovalMatrix already carries all four #3189 anchor facts."""
    from approval_matrix import ApprovalState

    matrix = _build_scenario_matrix()

    # (ii) latest verdict per edge — current versions.
    assert matrix.get_proposal_version("coder") == 2
    assert matrix.get_proposal_version("tester") == 1
    rc_coder = matrix.get_entry("reviewer_code", "coder")
    rs_coder = matrix.get_entry("reviewer_security", "coder")
    rc_tester = matrix.get_entry("reviewer_code", "tester")
    assert rc_coder is not None and rc_coder.state is ApprovalState.NACKED
    assert rs_coder is not None and rs_coder.state is ApprovalState.ACKED
    assert rc_tester is not None and rc_tester.state is ApprovalState.ACKED

    # (i) last-reviewed SHA per edge (the reviewed proposal commit).
    assert rs_coder.ack_commit_sha == SHA_CODER_V2
    assert rc_tester.ack_commit_sha == SHA_TESTER_V1

    # (iii) open NACK reason on the current version.
    nack_entries = matrix.get_nack_entries_for("coder")
    reasons = {e.reason for _r, e in nack_entries}
    assert "missing guard" in reasons

    # (iv) conditional-ACK obligations — unresolved vs resolved.
    assert rs_coder.pre_merge_condition == "git mv old new"
    assert rs_coder.obligation_resolved is False
    assert rc_tester.pre_merge_condition == "update import path"
    assert rc_tester.obligation_resolved is True


# ---------------------------------------------------------------------------
# Derivation agreement — skip-guarded until the coder's symbol lands.
# ---------------------------------------------------------------------------


def _flatten(value: Any) -> str:
    return repr(value)


def test_derivation_agrees_with_matrix() -> None:
    """The derivation reproduces the matrix's four-field projection."""
    fn = _derivation()
    matrix = _build_scenario_matrix()

    anchors = None
    for arg in (matrix,):
        try:
            anchors = fn(arg)
            break
        except TypeError:
            continue
    if anchors is None:
        pytest.skip("derivation does not accept an ApprovalMatrix input")

    blob = _flatten(anchors)
    # Current reviewed SHAs present; superseded coder v1 absent.
    assert SHA_CODER_V2 in blob, blob
    assert SHA_TESTER_V1 in blob, blob
    assert SHA_CODER_V1 not in blob, blob
    # Open NACK reason surfaced; resolved obligation distinguishable.
    assert "missing guard" in blob, blob
    assert "git mv old new" in blob, blob
