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


def _scenario_messages() -> list[dict[str, Any]]:
    """The same AC scenario as a BRC message record (derivation input).

    Equivalent to ``_build_scenario_matrix`` but expressed as the serialized
    message dicts the derivation consumes — verdicts carry the producer in
    ``to_role`` and the version/commit/reason/condition in ``metadata``.
    """

    def m(mtype: str, frm: str, seq: int, to: str = "all", **meta: Any) -> dict[str, Any]:
        return {
            "message_type": mtype,
            "from_role": frm,
            "to_role": to,
            "id": f"msg-{seq:03d}",
            "metadata": meta,
        }

    return [
        m("CONSENSUS_PROPOSE", "coder", 1, version=1, commit_sha=SHA_CODER_V1),
        m("CONSENSUS_ACK", "reviewer_code", 2, "coder", version=1, commit_sha=SHA_CODER_V1),
        m("CONSENSUS_PROPOSE", "coder", 3, version=2, commit_sha=SHA_CODER_V2),
        m("CONSENSUS_NACK", "reviewer_code", 4, "coder", version=2, reason="missing guard"),
        m(
            "CONSENSUS_ACK",
            "reviewer_security",
            5,
            "coder",
            version=2,
            commit_sha=SHA_CODER_V2,
            pre_merge_condition="git mv old new",
        ),
        m("CONSENSUS_PROPOSE", "tester", 6, version=1, commit_sha=SHA_TESTER_V1),
        m(
            "CONSENSUS_ACK",
            "reviewer_code",
            7,
            "tester",
            version=1,
            commit_sha=SHA_TESTER_V1,
            pre_merge_condition="update import path",
        ),
        m(
            "CONSENSUS_OBLIGATION_RESOLVED",
            "coder",
            8,
            "tester",
            producer_role="tester",
            reviewer_role="reviewer_code",
        ),
    ]


def test_derivation_agrees_with_matrix() -> None:
    """Message-record derivation reproduces the ApprovalMatrix four-field projection."""
    from approval_matrix import ApprovalState

    fn = _derivation()
    matrix = _build_scenario_matrix()
    anchors = fn(_scenario_messages())

    # (i) last-reviewed SHA per producer == the matrix's reviewed proposal head.
    last_reviewed = dict(anchors.last_reviewed_sha)
    assert last_reviewed.get("coder") == SHA_CODER_V2, last_reviewed
    assert last_reviewed.get("tester") == SHA_TESTER_V1, last_reviewed
    assert SHA_CODER_V1 not in last_reviewed.values(), last_reviewed

    # (ii) latest verdict per edge agrees with matrix entry states.
    verdict_by_edge = {(v.reviewer, v.producer): v.verdict.value for v in anchors.latest_verdicts}
    assert verdict_by_edge[("reviewer_code", "coder")] == "nack"
    assert verdict_by_edge[("reviewer_security", "coder")] in ("ack", "conditional_ack")
    assert verdict_by_edge[("reviewer_code", "tester")] in ("ack", "conditional_ack")
    assert (
        matrix.get_entry("reviewer_code", "coder").state is ApprovalState.NACKED
    )  # ground-truth cross-check

    # (iii) open NACK reason on the current version.
    nack_reasons = {(n.reviewer, n.producer): n.reason for n in anchors.open_nacks}
    assert nack_reasons.get(("reviewer_code", "coder")) == "missing guard", nack_reasons

    # (iv) conditional-ACK obligations: coder's unresolved, tester's resolved.
    obligations = {(o.reviewer, o.producer): o for o in anchors.conditional_ack_obligations}
    coder_ob = obligations.get(("reviewer_security", "coder"))
    tester_ob = obligations.get(("reviewer_code", "tester"))
    assert coder_ob is not None and coder_ob.condition == "git mv old new"
    assert coder_ob.resolved is False
    assert tester_ob is not None and tester_ob.resolved is True
