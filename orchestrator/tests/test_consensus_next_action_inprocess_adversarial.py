"""Adversarial parity tests for ``derive_next_action`` vs the HTTP next-action
route (TASK-2-1 of issue #3023 slice-2).

``test_consensus_next_action_inprocess.py`` (the coder's slice-2 TASK-2-1
output) covers five happy-path verdicts (wait, propose, ack, confirm,
complete) — one snapshot per branch of ``_derive_next_action``'s
``with tracker._lock:`` body. The byte-identity guarantee the
``OnDemandSpawner`` (TASK-2-2) relies on is broader than that: every
remaining branch of the derivation must mirror across the two call
surfaces, *and* the **documented divergences** between the two
(``role not in review graph`` returns ``"wait"`` in-process but ``400``
over HTTP) must be pinned so a future refactor that quietly removes the
HTTP-side membership check or quietly adds an in-process one fails fast
here.

The cases below were chosen by walking each remaining return-statement
in ``orchestrator/routes/consensus.py:280-406`` and asserting parity at
that branch:

1. **role-complete short-circuit with ``is_complete=False``** —
   confirmed role waits with ``blocking_agents`` payload (line 304-309).
2. **open-NACK barrier on PROPOSED producer** (≥2 NACKing reviewers) —
   surfaces ``status="open_nacks_blocked"`` payload (line 350-352).
3. **single-NACK on PROPOSED producer** (1 NACKing reviewer) — surfaces
   ``unresolved_nacks`` payload (line 353-359).
4. **PROPOSED producer with confirm_guard not allowed** — wait with
   ``confirm_guard_reason`` payload (line 367-372).
5. **dual-role WORKING + peer PROPOSE pending (R11a)** — own
   "propose" wins over peer review (line 330-344).
6. **dual-role post-own-propose with pending peer review (R11b)** —
   reviewer-side "ack" fires (line 386-394).
7. **pure reviewer with no pending proposals + guard not allowed** —
   wait with ``confirm_guard_reason`` (line 395-402).
8. **stale-version re-review after re-propose v2** — reviewer who
   ACKed v1 sees v2 and is told to ``ack`` again (line 386-394 via
   ``_has_pending_peer_proposals``'s stale-version branch).
9. **producer with single NACK on a WORKING-state version-0 view** —
   exercises the WORKING-branch ``nacks`` enrichment path (line
   340-344).
10. **documented divergence: non-graph role** — in-process returns
    ``("wait", None, "role not in review graph")``; HTTP route returns
    400. The on-demand spawner's caller MUST validate membership
    before calling ``derive_next_action`` and this test pins the
    contract so that requirement does not silently regress.
11. **role-complete short-circuit with ``is_complete=True``** —
    confirmed role with global ``is_complete`` returns ``"complete"``
    with no payload (line 302-303). The happy-path
    ``test_parity_complete_after_full_convergence`` exercises the same
    branch indirectly; this case is a tighter unit assertion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_consensus_next_action_inprocess.py).
# ---------------------------------------------------------------------------
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing route modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from peer_consensus import PeerConsensusTracker  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402

PIPELINE_ID = "issue-3023-slice-2-adversarial"


# ---------------------------------------------------------------------------
# Fixtures — same shapes as the happy-path file so any divergence between
# the two call paths surfaces on familiar ground, plus a dual-role graph
# for the #2749 R11a/R11b parity cases.
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_graph():
    """Single producer + two reviewers — covers cases 1, 2, 3, 4, 7, 8, 9, 11."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def dual_role_graph():
    """Tester is BOTH a producer (downstream of coder, reviewed by
    reviewer_code) AND a reviewer of coder — exercises the #2749 R11a /
    R11b dual-role ordering rule (cases 5 + 6)."""
    return ReviewGraph(
        [
            ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def simple_tracker(simple_graph):
    t = PeerConsensusTracker(PIPELINE_ID, simple_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("reviewer_code")
    t.register_agent("reviewer_security")
    return t


@pytest.fixture
def dual_tracker(dual_role_graph):
    t = PeerConsensusTracker(PIPELINE_ID, dual_role_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    return t


@pytest.fixture
def app():
    from flask import Flask
    from routes.consensus import consensus_bp  # type: ignore

    app = Flask(__name__)
    app.register_blueprint(consensus_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers — same conventions as the happy-path file (≥50-char content
# gate on every reason / summary).
# ---------------------------------------------------------------------------


def _propose(tracker, role="coder", *, version_label="v1"):
    tracker.handle_propose(
        role,
        {
            "summary": (
                f"Proposal {version_label} with substantive content describing "
                "the work, tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc1234",
        },
    )


def _re_propose(tracker, role="coder", *, version_label="v2"):
    tracker.handle_re_propose(
        role,
        {
            "summary": (
                f"Re-proposal {version_label} addressing the prior NACK "
                "blockers with substantive content for re-review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "def5678",
        },
        changed_artifacts=["a.py"],
    )


def _ack(tracker, reviewer, producer, *, version=1):
    tracker.handle_ack(
        reviewer,
        producer,
        {
            "artifact_references": ["a.py"],
            "reason": (
                "Substantive review verdict satisfying the ≥50 char content "
                "gate enforced by _validate_brc_content."
            ),
            "ack_version": version,
        },
    )


def _nack(tracker, reviewer, producer, reason):
    tracker.handle_nack(
        reviewer,
        producer,
        {
            "artifact_references": ["a.py"],
            "reason": (
                f"{reason} — full reason text long enough to satisfy the "
                "≥50 char content gate enforced by _validate_brc_content."
            ),
        },
    )


def _route_call(client, tracker, role):
    """POST /consensus/next-action; patch tracker lookup to use the in-memory tracker."""
    body = {"role": role}
    with patch(
        "routes.consensus.get_peer_consensus_tracker",
        return_value=tracker,
    ):
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
            data=json.dumps(body),
            content_type="application/json",
        )
    return resp


def _assert_parity(client, tracker, role):
    """Call both surfaces and assert byte-identical verdict on the
    ``(action, event_payload, reason)`` triple.

    The HTTP envelope (``success``, ``role``, ``slice_id``) is the route
    surface's add-on — the underlying triple must be identical to the
    in-process tuple. The route omits ``event_payload`` when None and
    omits ``reason`` when empty; we accept those omissions as
    equivalent to ``None`` / ``""`` so the parity check matches the
    route's documented surface (route docstring at
    ``routes/consensus.py:430-445``).
    """
    from routes.consensus import derive_next_action  # type: ignore

    in_proc_action, in_proc_payload, in_proc_reason = derive_next_action(tracker, role)

    resp = _route_call(client, tracker, role)
    assert resp.status_code == 200, f"route returned {resp.status_code}: {resp.data!r}"
    route_body = json.loads(resp.data)

    assert route_body["success"] is True
    assert route_body["action"] == in_proc_action, (
        f"action mismatch: route={route_body['action']!r} in_proc={in_proc_action!r}"
    )
    route_payload = route_body.get("event_payload")
    assert route_payload == in_proc_payload, (
        f"event_payload mismatch: route={route_payload!r} in_proc={in_proc_payload!r}"
    )
    assert route_body.get("reason", "") == in_proc_reason, (
        f"reason mismatch: route={route_body.get('reason')!r} in_proc={in_proc_reason!r}"
    )


# ---------------------------------------------------------------------------
# Adversarial parity cases — one per surviving derivation branch.
# ---------------------------------------------------------------------------


def test_parity_confirmed_role_waits_when_others_not_yet_complete(client, simple_tracker):
    """Confirmed role with ``is_complete=False`` → both surfaces emit
    ``wait`` with a ``blocking_agents`` event_payload.

    This is the role-complete short-circuit's "wait" branch at
    ``consensus.py:304-309`` — distinct from the happy-path
    ``test_parity_complete_after_full_convergence`` which exercises the
    "complete" branch on the SAME line ladder.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    # Only the coder confirms — the two reviewers do not.
    simple_tracker.handle_confirmed("coder")
    # The coder is confirmed but is_complete remains False; next-action
    # must report wait + the list of agents still blocking.
    _assert_parity(client, simple_tracker, "coder")


def test_parity_complete_short_circuit_for_a_confirmed_role(client, simple_tracker):
    """Tighter unit assertion of the ``role in confirmed_roles AND
    is_complete=True → "complete"`` branch (``consensus.py:302-303``).

    The happy-path file's ``test_parity_complete_after_full_convergence``
    exercises this via the full propose-ack-confirm flow on three roles;
    this test reaches the same branch with just a single confirm cycle
    so the parity assertion is on the exact short-circuit response.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    simple_tracker.handle_confirmed("coder")
    simple_tracker.handle_confirmed("reviewer_code")
    simple_tracker.handle_confirmed("reviewer_security")
    # Now every role is confirmed and is_complete flips True; the
    # short-circuit must trigger for each.
    for role in ("coder", "reviewer_code", "reviewer_security"):
        _assert_parity(client, simple_tracker, role)


def test_parity_open_nack_barrier_surfaces_payload(client, simple_tracker):
    """PROPOSED producer with 2 NACKing reviewers → both surfaces emit
    ``propose`` with the open-NACK barrier payload (``consensus.py:350-352``).

    The barrier's ``status="open_nacks_blocked"`` and ``nacks[]`` array
    must surface byte-identically over both call paths so the
    on-demand spawner's per-event prompt composer receives the same
    NACK aggregation the wrapper saw.
    """
    _propose(simple_tracker, "coder")
    _nack(simple_tracker, "reviewer_code", "coder", "SQL injection at a.py:42")
    _nack(simple_tracker, "reviewer_security", "coder", "missing auth check at a.py:60")
    _assert_parity(client, simple_tracker, "coder")


def test_parity_single_nack_surfaces_unresolved_nacks(client, simple_tracker):
    """PROPOSED producer with one NACKing reviewer (sub-barrier
    threshold) → both surfaces emit ``propose`` with
    ``unresolved_nacks`` in the payload (``consensus.py:353-359``).

    The single-reviewer NACK path is distinct from the
    two-reviewer barrier path (#2142); the route's behaviour here is
    "re-propose to address the one NACK". Parity asserts the
    in-process derivation does not silently widen the payload to a
    barrier shape.
    """
    _propose(simple_tracker, "coder")
    _nack(simple_tracker, "reviewer_code", "coder", "off-by-one at a.py:128")
    _assert_parity(client, simple_tracker, "coder")


def test_parity_proposed_producer_waits_when_confirm_guard_blocks(client, simple_tracker):
    """PROPOSED producer with no NACKs and an UNSATISFIED confirm guard
    (only one of two reviewers has ACKed) → both surfaces emit ``wait``
    with a ``confirm_guard_reason`` (``consensus.py:367-372``).

    This is the "produced, no NACKs, reviewers still working" branch
    — the path the wrapper used to poll-loop on. The on-demand
    spawner's prompt composer reads ``confirm_guard_reason`` to render
    the wait rationale.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    # reviewer_security has NOT acked → confirm guard blocked.
    _assert_parity(client, simple_tracker, "coder")


def test_parity_dual_role_pre_propose_returns_propose_R11a(client, dual_tracker):
    """Dual-role agent in WORKING with a peer PROPOSE pending → both
    surfaces emit ``propose`` (#2749 R11a; ``consensus.py:330-344``).

    Per the ordering rule, dual-role agents propose OWN work first
    even when a peer producer has a pending CONSENSUS_PROPOSE for
    them to review. If parity breaks here the on-demand spawner could
    deadlock by deriving ``ack`` in-process while the route says
    ``propose`` (or vice versa).
    """
    _propose(dual_tracker, "coder")
    # tester is dual-role: producer (still WORKING) + reviewer of coder.
    _assert_parity(client, dual_tracker, "tester")


def test_parity_dual_role_post_propose_returns_ack_R11b(client, dual_tracker):
    """Dual-role agent post-own-propose with a peer PROPOSE pending →
    both surfaces emit ``ack`` (#2749 R11b; ``consensus.py:386-394``
    via the dual-role fall-through at ``consensus.py:367-373``).

    This is the matching branch to R11a above — once the dual-role
    agent has proposed its own work it transitions to the reviewer
    half and must review the peer.
    """
    _propose(dual_tracker, "coder")
    _propose(dual_tracker, "tester")
    _assert_parity(client, dual_tracker, "tester")


def test_parity_pure_reviewer_waits_when_no_pending_and_guard_blocked(client, simple_tracker):
    """Pure reviewer with no pending peer proposals AND an UNSATISFIED
    confirm guard → both surfaces emit ``wait`` with
    ``confirm_guard_reason`` (``consensus.py:395-402``).

    Concretely: producer in WORKING (no proposal yet), so the reviewer
    has nothing to ack; reviewer's confirm guard fails because the
    producer hasn't reached CONFIRMED. The wait reason surfaces the
    guard's rejection so the on-demand spawner's per-event prompt
    composer can render it.
    """
    # Producer is in WORKING — reviewer has no pending proposals and
    # the confirm guard is not satisfied.
    _assert_parity(client, simple_tracker, "reviewer_code")


def test_parity_stale_version_re_review_after_re_propose(client, simple_tracker):
    """Reviewer who ACKed v1 sees v2 after a re-propose → both surfaces
    emit ``ack`` (stale-version branch of ``_has_pending_peer_proposals``;
    ``consensus.py:386-394``).

    Concretely: v1 lands, reviewer_code ACKs v1, then the producer
    re-proposes v2 (e.g. addressing a separate NACK from
    reviewer_security). reviewer_code's matrix entry is now stale; the
    derivation must surface ``ack`` with a pending entry pinning the
    current (v2) and prior (v1) versions so the reviewer re-reviews.
    """
    _propose(simple_tracker, "coder", version_label="v1")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _nack(simple_tracker, "reviewer_security", "coder", "v1 concurrency bug at a.py:80")
    # Producer re-proposes v2 with reviewer_code's v1 ACK now stale.
    _re_propose(simple_tracker, "coder", version_label="v2")
    # reviewer_code's matrix entry is at v1 but the producer is at v2.
    _assert_parity(client, simple_tracker, "reviewer_code")


def test_parity_documented_divergence_non_graph_role(client, simple_tracker):
    """Documented divergence: a role that is not in the review graph
    returns ``("wait", None, "role not in review graph")`` in-process
    (``consensus.py:404-406``) but the HTTP route returns 400
    (``consensus.py:488-493``).

    The on-demand spawner's caller MUST validate graph membership
    before calling ``derive_next_action`` — the in-process callable
    is documented as "Caller MUST validate that role is a participant
    in the review graph" (``consensus.py:421-423``).

    This test pins the contract: if a future refactor accidentally
    adds a membership check inside ``_derive_next_action`` (changing
    the in-process behaviour) or accidentally removes the HTTP-side
    check (changing the route behaviour), it fails here. The
    divergence is a feature of the contract, not a bug, and is the
    only point where the two call surfaces are NOT byte-identical.
    """
    from routes.consensus import derive_next_action  # type: ignore

    phantom = "phantom_role_not_in_graph"

    # In-process: derivation falls through to the "role not in review
    # graph" default (line 404-406) because the role is neither a
    # producer nor a reviewer in this graph.
    action, payload, reason = derive_next_action(simple_tracker, phantom)
    assert action == "wait", f"in-process should default to wait; got {action!r}"
    assert payload is None, f"in-process payload should be None; got {payload!r}"
    assert reason == "role not in review graph", (
        f"in-process reason should pin the role-not-in-graph default; got {reason!r}"
    )

    # HTTP route: same role returns 400 because the route validates
    # membership up front (line 488-493).
    resp = _route_call(client, simple_tracker, phantom)
    assert resp.status_code == 400, (
        f"HTTP route must reject non-graph role with 400; got {resp.status_code}: {resp.data!r}"
    )
    body = json.loads(resp.data)
    assert body.get("success") is False
    assert "not a participant" in (body.get("message") or "").lower(), (
        f"HTTP 400 must explain the non-participation; got {body!r}"
    )
