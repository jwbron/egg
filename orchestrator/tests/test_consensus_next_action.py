"""Tests for the ``POST /api/v1/pipelines/{pid}/consensus/next-action``
orchestrator route (TASK-1-2, slice-1 of issue #2908).

The route derives one of ``{wait, propose, ack, nack, confirm, complete}``
from the in-process ``PeerConsensusTracker`` matrix, the
``_open_nacks_barrier_response`` ``nacks[]`` payload (#2142), and the
``changed_artifacts`` delta.  It is the **server-side sequencing
primitive** that the slice-2 event-pump wrapper queries each iteration,
moving the dispatch logic out of bash and into testable Python.

Per the acceptance criteria there are nine (role, BRC-state)
combinations the handler must enumerate.  Each lives in its own
``test_next_action_*`` named test (per reviewer_plan v2 non-blocker —
the two dual-role sub-cases stay distinct, not collapsed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (orchestrator package + shared) — mirrors the established
# pattern in test_brc_open_nacks_barrier.py / test_brc_confirmation_nudge.py.
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

PIPELINE_ID = "issue-2908-impl2"


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_graph():
    """Single producer + two reviewers — covers cases 1, 2, 5, 6, 7, 8, 9."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def dual_role_graph():
    """Tester is BOTH a producer (downstream of coder) AND a reviewer
    of coder — exercises the #2749 dual-role ordering rule (cases 3 + 4).

    Edges:
      * tester reviews coder (tester is_reviewer of coder)
      * reviewer_code reviews tester (tester is_producer)
      * reviewer_code reviews coder (so coder has > 1 reviewer)
    """
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
def advisory_holdout_graph():
    """#3043 shape: coder's only CRITICAL reviewer is reviewer_security, with
    reviewer_code ADVISORY on coder (e.g. a stall-demoted edge). reviewer_code
    also CRITICALLY reviews tester. coder can therefore confirm on
    reviewer_security's ACK alone, leaving reviewer_code as a lagging holdout
    that never reviewed coder.
    """
    return ReviewGraph(
        [
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.ADVISORY),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def advisory_holdout_tracker(advisory_holdout_graph):
    t = PeerConsensusTracker(PIPELINE_ID, advisory_holdout_graph, cooldown_seconds=0)
    for role in ("coder", "tester", "reviewer_security", "reviewer_code"):
        t.register_agent(role)
    return t


@pytest.fixture
def app():
    """Flask app with the next-action route blueprint.

    The route lands in ``orchestrator/routes/consensus.py`` per
    TASK-1-2; the coder may alternatively extend an existing BRC
    routes module.  We accept either import path.
    """
    from flask import Flask

    consensus_bp = None
    # Primary expected location.
    try:
        from routes.consensus import consensus_bp  # type: ignore
    except ImportError:
        # Fallback: the coder placed the route in an existing module.
        for candidate in ("routes.brc", "routes.pipelines", "routes.signals"):
            try:
                mod = __import__(candidate, fromlist=["*"])
                # Look for any blueprint that registers the endpoint.
                for name in dir(mod):
                    if name.endswith("_bp"):
                        consensus_bp = getattr(mod, name)
                        break
                if consensus_bp is not None:
                    break
            except ImportError:
                continue
    if consensus_bp is None:
        pytest.skip(
            "consensus next-action blueprint not found — TASK-1-2 has not "
            "landed yet. Coder must register a blueprint exporting POST "
            "/api/v1/pipelines/{pid}/consensus/next-action."
        )
    app = Flask(__name__)
    app.register_blueprint(consensus_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _post_next_action(client, role, *, tracker=None, slice_id=None):
    """Hit the route, threading a registered tracker into whatever
    module-level accessor the route uses.

    The coder will resolve the tracker via one of a small set of
    patterns; we patch the most likely accessor and fall back to
    others if the test environment exposes them differently.
    """
    body = {"role": role}
    if slice_id is not None:
        body["slice_id"] = slice_id

    # Try to patch the most likely tracker-resolution function.
    patches = []
    candidate_targets = [
        "routes.consensus._get_peer_consensus_tracker",
        "routes.consensus.get_peer_consensus_tracker",
        "routes.consensus._tracker_for",
        "routes.brc._get_peer_consensus_tracker",
        "routes.brc.get_peer_consensus_tracker",
        "routes.pipelines._get_peer_consensus_tracker",
    ]
    if tracker is not None:
        for target in candidate_targets:
            try:
                p = patch(target, return_value=tracker)
                p.start()
                patches.append(p)
            except AttributeError, ModuleNotFoundError:
                continue
    try:
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
            data=json.dumps(body),
            content_type="application/json",
        )
    finally:
        for p in patches:
            p.stop()
    return resp


def _propose(tracker, role="coder", *, summary=None, version_label="v1"):
    tracker.handle_propose(
        role,
        {
            "summary": summary
            or (
                f"Proposal {version_label} with substantive content describing "
                "the work, tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc1234",
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


def _assert_action(resp, expected_action):
    """Decode the response, assert 200, and return the action."""
    assert resp.status_code == 200, f"next-action returned {resp.status_code}: {resp.data!r}"
    data = json.loads(resp.data)
    # The route may envelope under {success, data: {...}} or return the
    # action directly.  Accept either.
    if "data" in data and isinstance(data["data"], dict) and "action" in data["data"]:
        action = data["data"]["action"]
        payload = data["data"]
    elif "action" in data:
        action = data["action"]
        payload = data
    else:
        pytest.fail(f"next-action response missing 'action' field: {data!r}")
    assert action == expected_action, (
        f"Expected action={expected_action!r}, got {action!r}; payload={payload!r}"
    )
    return payload


# ---------------------------------------------------------------------------
# (1) Producer just PROPOSED -> next action = wait (for reviews)
# ---------------------------------------------------------------------------


def test_next_action_producer_after_propose_returns_wait(client, simple_tracker):
    """A producer that just PROPOSED waits for reviewer verdicts."""
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    _assert_action(resp, "wait")


# ---------------------------------------------------------------------------
# (2) Reviewer with pending proposal -> next action = ack or nack
# ---------------------------------------------------------------------------


def test_next_action_reviewer_with_pending_proposal_returns_review(client, simple_tracker):
    """A reviewer whose upstream producer has PROPOSED must review.

    The route returns 'ack' (the canonical 'review' verb) — the actual
    verdict is up to the agent; the next-action simply says "review now".
    Acceptable values: "ack" or "nack" — both indicate the reviewer
    must form a verdict.
    """
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "reviewer_code", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    action = data.get("data", data).get("action") if isinstance(data, dict) else None
    assert action in ("ack", "nack", "review"), (
        f"Reviewer with pending proposal must be told to review — got action={action!r}"
    )


# ---------------------------------------------------------------------------
# (3) Dual-role WORKING + peer PROPOSE pending -> action = propose (#2749 R11a)
# ---------------------------------------------------------------------------


def test_next_action_dual_role_pre_propose_returns_propose(client, dual_tracker):
    """Per #2749 ordering rule (risk_analyst R11 sub-case a): a dual-role
    agent that has NOT yet proposed its own work must propose first, even
    if a peer producer has a pending CONSENSUS_PROPOSE for it to review.

    This is the rule that closes the dual-role BRC deadlock — if every
    dual-role agent reviews peers before proposing, peer producers wait
    forever on each other's verdicts and the BRC round never closes.
    """
    # coder has proposed (peer producer for tester); tester has NOT.
    _propose(dual_tracker, "coder")
    resp = _post_next_action(client, "tester", tracker=dual_tracker)
    _assert_action(resp, "propose")


# ---------------------------------------------------------------------------
# (4) Dual-role post-own-propose with pending peer review -> ack/nack (#2749 R11b)
# ---------------------------------------------------------------------------


def test_next_action_dual_role_post_propose_returns_review(client, dual_tracker):
    """Per #2749 ordering rule (risk_analyst R11 sub-case b): once a
    dual-role agent has PROPOSED its own work, it transitions into the
    reviewer half of its lifecycle and starts reviewing peer producers.
    """
    _propose(dual_tracker, "coder")
    _propose(dual_tracker, "tester")
    # Now tester is post-own-propose; coder is still pending review.
    resp = _post_next_action(client, "tester", tracker=dual_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    action = data.get("data", data).get("action") if isinstance(data, dict) else None
    assert action in ("ack", "nack", "review"), (
        "Dual-role post-own-propose must be told to review the peer's "
        f"pending proposal — got action={action!r}"
    )


# ---------------------------------------------------------------------------
# (5) Open-NACK barrier (#2142) blocks re-propose
# ---------------------------------------------------------------------------


def test_next_action_open_nack_barrier_surfaces_nacks(client, simple_tracker):
    """The #2142 multi-reviewer NACK barrier must surface in next-action
    so the producer fixes both findings before re-proposing.

    Action should be 'propose' (the producer's next step IS to
    re-propose with fixes) but the ``event_payload`` must carry the
    inlined NACKs so the agent gets them in one round-trip.
    """
    _propose(simple_tracker, "coder")
    _nack(simple_tracker, "reviewer_code", "coder", "SQL injection at a.py:42")
    _nack(simple_tracker, "reviewer_security", "coder", "missing auth check at a.py:60")
    # Now the producer queries next-action — must surface NACKs.
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    payload = data.get("data", data)
    # Either nacks[] surfaces in event_payload, or open_nacks_blocked
    # status is exposed.  Both indicate the producer must address the
    # blockers.
    event_payload = payload.get("event_payload") or {}
    nacks_seen = (
        event_payload.get("nacks")
        or payload.get("nacks")
        or event_payload.get("status") == "open_nacks_blocked"
    )
    assert nacks_seen, (
        f"Open-NACK barrier must surface inlined NACKs in next-action response — got {payload!r}"
    )


# ---------------------------------------------------------------------------
# (6) Conditional ACK in effect — producer next-action = wait/confirm
# ---------------------------------------------------------------------------


def test_next_action_conditional_ack_in_effect(client, simple_tracker):
    """A conditional ACK (#1998) is still an ACK — the producer can
    confirm. The pre-merge obligation lives on the approval matrix and
    surfaces via ``brc_get_state`` / the PR body, NOT in next-action
    (which is a sequencing primitive, not a state snapshot).

    The 9-case acceptance criterion #6 is: "conditional ACK still in
    effect" — the test verifies the action plumbing handles a
    conditional ACK on the current version the same as an unconditional
    ACK on the routing side, and that the obligation persists on the
    matrix for downstream retrieval.
    """
    _propose(simple_tracker, "coder")
    # reviewer_code issues a conditional ACK.
    simple_tracker.handle_ack(
        "reviewer_code",
        "coder",
        {
            "artifact_references": ["a.py"],
            "reason": (
                "Code is correct but requires a manual git mv before "
                "merging — conditional approval satisfying the 50-char gate."
            ),
            "ack_version": 1,
            "pre_merge_condition": "git mv legacy/x new/x before merging",
        },
    )
    # reviewer_security issues a plain ACK.
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    # Producer should now be told to confirm.
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    payload = data.get("data", data)
    action = payload.get("action")
    assert action in ("confirm", "wait"), (
        f"All-ACKed producer with conditional ACK must be eligible to "
        f"confirm (or wait if other reviewers pending) — got {action!r}"
    )
    # The conditional obligation persists on the matrix and is reachable
    # via the consensus state (asserted via tracker.evaluate to keep this
    # test independent of get-state's exact serialization).
    consensus = simple_tracker.evaluate()
    conditions = consensus.get("pre_merge_conditions") or []
    assert any("git mv legacy/x" in (c.get("condition") or "") for c in conditions), (
        f"Conditional ACK obligation must persist on the consensus state "
        f"for downstream retrieval (PR body / brc_get_state) — "
        f"got conditions={conditions!r}"
    )


# ---------------------------------------------------------------------------
# (7) Stale-version (#2482) reviewer must re-review
# ---------------------------------------------------------------------------


def test_next_action_stale_version_triggers_re_review(client, simple_tracker):
    """A reviewer that ACKed v1 then a re-propose lifted the version to
    v2 should be told to re-review (next-action = ack/nack with version
    in event_payload), per #2482.
    """
    _propose(simple_tracker, "coder", version_label="v1")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _nack(simple_tracker, "reviewer_security", "coder", "blocker for v1")
    # NACK barrier blocks first re-propose; pay the toll then advance.
    first = simple_tracker.handle_re_propose(
        "coder",
        {
            "summary": (
                "Re-propose v2: addressed reviewer_security NACK — added auth check at a.py:60."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc5678",
        },
        changed_artifacts=["a.py"],
    )
    # If the single-reviewer NACK case advances directly (no barrier
    # toll on a single-reviewer NACK), we should already be at v2.
    if first.get("status") == "open_nacks_blocked":
        simple_tracker.handle_re_propose(
            "coder",
            {
                "summary": ("Re-propose v2 retry: addressed all reviewer NACKs."),
                "artifacts": ["a.py"],
                "commit_sha": "abc5678",
            },
            changed_artifacts=["a.py"],
        )
    # Now reviewer_code is at version 1 but producer is at v2 — stale.
    resp = _post_next_action(client, "reviewer_code", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    payload = data.get("data", data)
    action = payload.get("action")
    assert action in ("ack", "nack", "review"), (
        f"Stale-version reviewer must re-review the new proposal — got action={action!r}"
    )
    # The event_payload threads the producer's current version under
    # ``pending_reviews[]`` so the reviewer's next ACK / NACK carries
    # the right version-int (avoids the orchestrator 409 stale_version
    # rejection on submit).
    event = payload.get("event_payload") or {}
    pending = event.get("pending_reviews") or []
    matching = [p for p in pending if p.get("producer") == "coder"]
    assert matching, (
        f"Stale-version re-review must list coder under pending_reviews "
        f"with the current version — got {event!r}"
    )
    assert matching[0].get("current_version", 0) >= 2, (
        f"Stale-version re-review must carry the producer's current "
        f"version (>=2) in pending_reviews[0].current_version — "
        f"got {matching[0]!r}"
    )


# ---------------------------------------------------------------------------
# (8) Confirmation eligible
# ---------------------------------------------------------------------------


def test_next_action_confirm_eligible(client, simple_tracker):
    """All critical reviewers ACKed the current version — producer
    is eligible to confirm.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    _assert_action(resp, "confirm")


# ---------------------------------------------------------------------------
# (9) Role complete (post-CONFIRMED)
# ---------------------------------------------------------------------------


def test_next_action_role_complete(client, simple_tracker):
    """Once **every** role has confirmed (global ``is_complete=True``),
    next-action returns ``complete`` so the wrapper loop exits cleanly.

    A role that has confirmed but is still waiting on peers gets
    ``wait`` with ``blocking_agents`` in event_payload — that's the
    intermediate state and is distinct from the terminal ``complete``.
    Both paths are covered.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    simple_tracker.handle_confirmed("coder")

    # Intermediate: coder confirmed but reviewers haven't → wait, NOT complete.
    resp_intermediate = _post_next_action(client, "coder", tracker=simple_tracker)
    payload_intermediate = json.loads(resp_intermediate.data)
    action_intermediate = payload_intermediate.get("data", payload_intermediate).get("action")
    assert action_intermediate == "wait", (
        f"Confirmed-but-not-globally-complete role must wait — got {action_intermediate!r}"
    )

    # Terminal: every role confirms → next-action returns complete.
    simple_tracker.handle_confirmed("reviewer_code")
    simple_tracker.handle_confirmed("reviewer_security")
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    _assert_action(resp, "complete")


def test_advisory_reviewer_can_confirm_after_producer_confirmed(client, advisory_holdout_tracker):
    """#3043 regression: a lagging advisory reviewer must be routed to
    ``confirm`` once the producer it never reviewed has CONFIRMED.

    Field repro (issue-3023 slice-3): coder confirmed via its critical
    reviewers while reviewer_code (advisory on coder) was the lone holdout.
    Because ``_has_pending_peer_proposals`` skips a CONFIRMED producer,
    reviewer_code was never routed back to ``ack`` coder; and ``check_confirm_guard``
    blocked its ``confirm`` on the must_have_reviewed guard. Result: a permanent
    ``wait`` → ``is_complete`` (all-confirmed) never trips → the slice never
    closes. The fix excludes already-CONFIRMED producers from the reviewer
    guards, so the holdout can confirm and consensus completes.
    """
    t = advisory_holdout_tracker
    _propose(t, "coder")
    _propose(t, "tester")
    # coder's CRITICAL reviewer ACKs; reviewer_code (advisory) never reviews coder.
    _ack(t, "reviewer_security", "coder", version=1)
    # reviewer_code reviews its CRITICAL producer (tester).
    _ack(t, "reviewer_code", "tester", version=1)
    # Producers + the fully-reviewed pure reviewer confirm, leaving reviewer_code.
    t.handle_confirmed("coder")
    t.handle_confirmed("tester")
    t.handle_confirmed("reviewer_security")

    # The lone holdout: its next action must be ``confirm``, not a terminal ``wait``.
    resp = _post_next_action(client, "reviewer_code", tracker=t)
    _assert_action(resp, "confirm")

    # Confirming it completes global consensus — the slice can now close.
    t.handle_confirmed("reviewer_code")
    assert t.evaluate()["is_complete"] is True


# ---------------------------------------------------------------------------
# Edge cases — these are NOT in the 9-case list but cover regressions
# the route would otherwise allow.
# ---------------------------------------------------------------------------


def test_next_action_returns_json_envelope(client, simple_tracker):
    """The route must return JSON, not text/html or empty."""
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "coder", tracker=simple_tracker)
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("Content-Type", ""), (
        f"next-action must return JSON — got {resp.headers!r}"
    )


def test_next_action_unknown_role_returns_4xx(client, simple_tracker):
    """A request for an unregistered role surfaces as a clear 4xx so
    the wrapper bash can diagnose its misconfigured ``--role``.

    Returning 200 with action=wait on an unknown role would silently
    mask wrapper bugs.
    """
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "fictional_role_does_not_exist", tracker=simple_tracker)
    assert resp.status_code in (400, 404, 422), (
        f"Unknown role must surface as 4xx — got {resp.status_code}: {resp.data!r}"
    )


# ---------------------------------------------------------------------------
# reviewer_code_holistic v2 finding #1: pending_reviews entries must
# carry artifact_refs so the slice-3 event-pump composer's
# changed_artifacts fallback can render a degraded baseline when the
# reviewer has no stored last_reviewed_commit_sha for the producer.
# ---------------------------------------------------------------------------


def test_next_action_reviewer_pending_reviews_includes_artifact_refs(client, simple_tracker):
    """Reviewer-side pending_reviews entries must surface the producer's
    current proposal artifact list as ``artifact_refs`` so the slice-3
    composer can render a per-producer changed_artifacts fallback on
    first-ever ACK (when no last_reviewed_commit_sha is yet stored).

    Reviewer_code_holistic v2 finding #1: the documented fallback was
    dead code in production because next-action emitted ``pending_reviews``
    entries without artifact_refs and the composer's fallback path
    looked for a top-level ``changed_artifacts`` key the route never
    set. This test pins the new artifact_refs enrichment.
    """
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "reviewer_code", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    payload = data.get("data", data)
    pending = (payload.get("event_payload") or {}).get("pending_reviews") or []
    assert len(pending) >= 1, (
        f"reviewer with pending proposal must surface pending_reviews — payload={payload!r}"
    )
    entry = pending[0]
    assert entry.get("producer") == "coder"
    assert "artifact_refs" in entry, (
        f"pending_reviews entry must include artifact_refs (slice-3 holistic "
        f"finding #1) — entry={entry!r}"
    )
    # _propose() above seeds artifacts=['a.py'] — the enriched payload
    # must carry that through verbatim so the composer's fallback can
    # render it as the degraded baseline.
    assert entry["artifact_refs"] == ["a.py"], (
        f"artifact_refs must mirror the producer's current proposal "
        f"artifacts (a.py from _propose) — got {entry['artifact_refs']!r}"
    )


def test_next_action_reviewer_pending_reviews_includes_proposal_commit_sha(client, simple_tracker):
    """Reviewer-side pending_reviews entries must surface the producer's
    proposed commit SHA (#3076).

    The composer scopes the re-review delta to
    ``{last_reviewed}..{proposal_commit_sha}`` and renders
    ``git show <sha>:<path>`` reads from it — ``{sha}..HEAD`` against
    the REVIEWER's own HEAD never contains the producer's commits
    (per-role worktrees), which produced the "re-review delta is empty"
    phantom NACK on pipeline-2b3d8b0b.
    """
    _propose(simple_tracker, "coder")
    resp = _post_next_action(client, "reviewer_code", tracker=simple_tracker)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    payload = data.get("data", data)
    pending = (payload.get("event_payload") or {}).get("pending_reviews") or []
    assert len(pending) >= 1
    entry = pending[0]
    assert entry.get("proposal_commit_sha") == "abc1234", (
        f"pending_reviews entry must carry the producer's proposed commit "
        f"SHA (abc1234 from _propose) — entry={entry!r}"
    )
