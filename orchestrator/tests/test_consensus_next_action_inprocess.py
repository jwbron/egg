"""In-process vs HTTP-route parity test for ``derive_next_action``
(TASK-2-1 of issue #3023 slice-2).

The orchestrator's per-phase run loop will call
``derive_next_action(tracker, role)`` directly instead of taking a TCP
round-trip through the HTTP next-action route (see
``orchestrator/on_demand_spawner.py``, TASK-2-2). The byte-identity
guarantee in the task contract is: for any given
``(tracker, role)`` snapshot, the in-process callable and the HTTP
route must return the same ``(action, event_payload, reason)`` tuple.

This file exercises that parity for a representative set of BRC matrix
snapshots — at least one snapshot for each broad BRC verdict
(``wait``, ``propose``, ``ack``, ``confirm``, ``complete``). The
existing nine ``test_next_action_*`` cases in
``test_consensus_next_action.py`` already cover the per-verdict
correctness of the HTTP route; this file's value is the parity check
between the two call surfaces.

The acceptance criterion in the plan reads:

    New unit test asserts in-process and HTTP route return identical
    results for the same tracker snapshot.

We test the identity in both directions on the same tracker so a
future refactor that diverges the two paths (e.g. an HTTP-only
post-processing step) fails fast here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_consensus_next_action.py).
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

PIPELINE_ID = "issue-3023-slice-2"


# ---------------------------------------------------------------------------
# Fixtures — a single producer + two reviewers, the same shape used by
# the broader next-action route tests so any divergence between the two
# call paths surfaces on familiar ground.
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_graph():
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
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
# Helpers — reuse the patterns from test_consensus_next_action.py so the
# message bodies pass _validate_brc_content's ≥50-char gate.
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


def _route_call(client, tracker, role):
    """POST /consensus/next-action with the tracker patched in."""
    from unittest.mock import patch

    body = {"role": role}
    # The route resolves the tracker via _resolve_tracker, which in turn
    # calls get_peer_consensus_tracker. Patch that lookup so the route
    # sees the same in-memory tracker the in-process call uses.
    with patch(
        "routes.consensus.get_peer_consensus_tracker",
        return_value=tracker,
    ):
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
            data=json.dumps(body),
            content_type="application/json",
        )
    assert resp.status_code == 200, f"route returned {resp.status_code}: {resp.data!r}"
    return json.loads(resp.data)


def _assert_parity(client, tracker, role):
    """Call both surfaces and assert byte-identical verdict.

    The HTTP route adds an envelope (``success``, ``role``, ``slice_id``)
    around the raw tuple. We assert the action+event_payload+reason are
    identical to the in-process tuple — that's the byte-identity the
    on-demand spawner relies on.
    """
    from routes.consensus import derive_next_action  # type: ignore

    in_proc_action, in_proc_payload, in_proc_reason = derive_next_action(tracker, role)

    route_body = _route_call(client, tracker, role)
    assert route_body["success"] is True
    assert route_body["action"] == in_proc_action, (
        f"action mismatch: route={route_body['action']!r} "
        f"in_proc={in_proc_action!r}"
    )
    # event_payload absent on the route side means in-process must have
    # returned None. Symmetry both ways.
    route_payload = route_body.get("event_payload")
    assert route_payload == in_proc_payload, (
        f"event_payload mismatch: route={route_payload!r} "
        f"in_proc={in_proc_payload!r}"
    )
    # reason is included only when non-empty; in-process always returns
    # a non-empty string, so the route must echo it back verbatim.
    assert route_body.get("reason", "") == in_proc_reason, (
        f"reason mismatch: route={route_body.get('reason')!r} "
        f"in_proc={in_proc_reason!r}"
    )


# ---------------------------------------------------------------------------
# Parity cases — one per representative BRC verdict.
# ---------------------------------------------------------------------------


def test_parity_wait_after_propose(client, simple_tracker):
    """Producer just proposed → in-process and route both say ``wait``."""
    _propose(simple_tracker, "coder")
    _assert_parity(client, simple_tracker, "coder")


def test_parity_propose_working_state(client, simple_tracker):
    """Producer in WORKING (nothing proposed) → both say ``propose``."""
    _assert_parity(client, simple_tracker, "coder")


def test_parity_ack_pending_review(client, simple_tracker):
    """Reviewer with pending peer proposal → both say ``ack``."""
    _propose(simple_tracker, "coder")
    _assert_parity(client, simple_tracker, "reviewer_code")


def test_parity_confirm_after_all_acks(client, simple_tracker):
    """Producer with all reviewer ACKs → both say ``confirm``."""
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    _assert_parity(client, simple_tracker, "coder")


def test_parity_complete_after_full_convergence(client, simple_tracker):
    """All roles confirmed → in-process returns ``complete``; route
    must mirror.

    We drive both producers and reviewers through CONFIRMED so the
    matrix's ``is_complete`` flag flips. Then any role's next-action
    must report ``complete``.
    """
    _propose(simple_tracker, "coder")
    _ack(simple_tracker, "reviewer_code", "coder", version=1)
    _ack(simple_tracker, "reviewer_security", "coder", version=1)
    # Confirm each agent — this drives _confirmed_roles to all-three and
    # is_complete to True.
    simple_tracker.handle_confirmed("coder")
    simple_tracker.handle_confirmed("reviewer_code")
    simple_tracker.handle_confirmed("reviewer_security")
    _assert_parity(client, simple_tracker, "coder")
    _assert_parity(client, simple_tracker, "reviewer_code")
    _assert_parity(client, simple_tracker, "reviewer_security")
