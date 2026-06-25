"""Tests for the per-iteration operator kickback delivery on the
orchestrator-owned event-loop ``next-action`` path (#3231).

The re-spawned producer's prompt is composed from the
``event_payload`` the ``next-action`` route
returns. Without the ``iteration_feedback`` block the producer re-reads
its own prior on-disk draft and re-proposes it byte-for-byte — the
operator's ``request_changes`` / ``change_approach`` silently no-ops
(the #1283 / #1915 fake-cycle class). These tests pin the orchestrator-
side attachment of ``PhaseExecution.operator_directives`` /
``iteration_history`` (#2795) onto the ``propose`` event_payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import IterationSummary, OperatorDirective, PhaseExecution  # noqa: E402
from peer_consensus import PeerConsensusTracker  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402

PIPELINE_ID = "issue-3231-impl"


def _phase_execution_with_directives(phase: str = "refine") -> PhaseExecution:
    pe = PhaseExecution(phase=phase)  # type: ignore[arg-type]
    pe.operator_directives = [
        OperatorDirective(
            iteration_n=0,
            feedback_text="Build for ALL roles, not a single-role prototype.",
        ),
        OperatorDirective(
            iteration_n=1,
            feedback_text="Drop cq-2; reframe cq-1 around measurement tooling.",
        ),
    ]
    pe.iteration_history = [
        IterationSummary(
            iteration_n=0,
            verdict_matrix={"reviewer_code->coder": "nacked"},
            nack_reasons=["reviewer_code->coder: prototype is single-role"],
            final_proposal_commit={"coder": "abc123"},
        ),
    ]
    return pe


# ---------------------------------------------------------------------------
# _build_iteration_feedback (pure function of pipeline state)
# ---------------------------------------------------------------------------


def _pipeline_state(phase_execution: PhaseExecution | None = None) -> MagicMock:
    """Build a fake pipeline state object exposing ``current_phase`` and a
    real ``phases`` dict.

    ``_build_iteration_feedback`` reads the phase execution directly off the
    persisted ``phases`` map (not the get-or-create ``get_phase_execution``
    accessor — see #3231 re-review note 3), so the fake exposes a plain
    dict keyed by the phase value. ``current_phase`` is the string the real
    code's ``getattr(phase_enum, "value", phase_enum)`` collapses to.
    """
    pipeline = MagicMock()
    pipeline.current_phase = "refine"
    pipeline.phases = {"refine": phase_execution} if phase_execution is not None else {}
    return pipeline


def test_build_iteration_feedback_surfaces_directives_and_history() -> None:
    """Directives (chronological) + latest iteration summary land in the
    serializable block the route attaches to the event_payload.
    """
    from routes.consensus import _build_iteration_feedback

    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(_phase_execution_with_directives())
    with patch("routes.consensus.get_state_store", return_value=store):
        block = _build_iteration_feedback(PIPELINE_ID, Path("/repo"))
    assert block is not None
    assert [d["iteration_n"] for d in block["directives"]] == [0, 1]
    assert "Build for ALL roles" in block["directives"][0]["feedback_text"]
    assert block["prior_iteration"]["iteration_n"] == 0
    assert block["prior_iteration"]["verdict_matrix"] == {"reviewer_code->coder": "nacked"}
    assert "prototype is single-role" in block["prior_iteration"]["nack_reasons"][0]


def test_build_iteration_feedback_directive_created_at_round_trips_to_render() -> None:
    """End-to-end ``datetime`` → ``.isoformat()`` → rendered string for a
    directive's ``created_at`` (#3231 re-review note 2).

    The renderer tests in ``test_compose_event_prompt.py`` feed hand-built
    dicts with string timestamps, and the builder test above constructs
    ``OperatorDirective``s without ``created_at`` — so neither half locks
    the full round-trip. This drives a real ``datetime`` through
    ``_build_iteration_feedback`` (which calls ``.isoformat()``) and on into
    ``compose_event_prompt``, asserting the wall-clock signal survives both
    hops and lands in the rendered prompt.
    """
    from datetime import UTC, datetime

    from routes.consensus import _build_iteration_feedback

    from orchestrator.routes.event_prompt import compose_event_prompt

    issued_at = datetime(2026, 6, 24, 22, 40, 0, tzinfo=UTC)
    pe = PhaseExecution(phase="refine")  # type: ignore[arg-type]
    pe.operator_directives = [
        OperatorDirective(
            iteration_n=1,
            feedback_text="Drop cq-2; reframe cq-1 around measurement tooling.",
            created_at=issued_at,
        ),
    ]
    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(pe)
    with patch("routes.consensus.get_state_store", return_value=store):
        block = _build_iteration_feedback(PIPELINE_ID, Path("/repo"))

    assert block is not None
    # The builder serialised the datetime via .isoformat().
    assert block["directives"][0]["created_at"] == issued_at.isoformat()

    prompt = compose_event_prompt(
        "coder", {"action": "propose"}, "", [], [], "main", iteration_feedback=block
    )
    # The same timestamp the datetime produced lands in the rendered header.
    assert f"iteration 1, {issued_at.isoformat()}" in prompt


def test_build_iteration_feedback_none_when_no_kickback() -> None:
    """No directives and no iteration history → None (section omitted)."""
    from routes.consensus import _build_iteration_feedback

    empty_pe = PhaseExecution(phase="refine")  # type: ignore[arg-type]
    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(empty_pe)
    with patch("routes.consensus.get_state_store", return_value=store):
        assert _build_iteration_feedback(PIPELINE_ID, Path("/repo")) is None


def test_build_iteration_feedback_none_when_repo_unresolvable() -> None:
    """A None repo_path (resolution failed) → None, never raises."""
    from routes.consensus import _build_iteration_feedback

    assert _build_iteration_feedback(PIPELINE_ID, None) is None


def test_build_iteration_feedback_best_effort_on_store_failure() -> None:
    """A store/parse failure degrades to None rather than 500-ing the route."""
    from routes.consensus import _build_iteration_feedback

    store = MagicMock()
    store.load_pipeline.side_effect = RuntimeError("state unreadable")
    with patch("routes.consensus.get_state_store", return_value=store):
        assert _build_iteration_feedback(PIPELINE_ID, Path("/repo")) is None


# ---------------------------------------------------------------------------
# Route-level: handle_next_action attaches iteration_feedback on propose
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_graph():
    return ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])


@pytest.fixture
def simple_tracker(simple_graph):
    return PeerConsensusTracker(PIPELINE_ID, simple_graph)


@pytest.fixture
def app():
    from flask import Flask
    from routes.consensus import consensus_bp

    app = Flask(__name__)
    app.register_blueprint(consensus_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _post(client, role):
    return client.post(
        f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
        data=json.dumps({"role": role}),
        content_type="application/json",
    )


def test_next_action_attaches_iteration_feedback_on_propose(client, simple_tracker) -> None:
    """A producer's ``propose`` event_payload carries ``iteration_feedback``
    when the current phase execution has operator directives.
    """

    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(_phase_execution_with_directives())
    with (
        patch("routes.consensus.get_peer_consensus_tracker", return_value=simple_tracker),
        patch("routes.consensus.get_state_store", return_value=store),
        patch(
            "routes.consensus._resolve_repo_path_for_next_action",
            return_value=Path("/repo"),
        ),
    ):
        resp = _post(client, "coder")
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["action"] == "propose"
    payload = data["event_payload"]
    assert "iteration_feedback" in payload
    assert "Drop cq-2" in payload["iteration_feedback"]["directives"][-1]["feedback_text"]


def test_next_action_omits_iteration_feedback_when_no_kickback(client, simple_tracker) -> None:
    """No kickback → the event_payload carries no ``iteration_feedback``
    key (golden-stable for the no-kickback path).
    """

    empty_pe = PhaseExecution(phase="refine")  # type: ignore[arg-type]
    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(empty_pe)
    with (
        patch("routes.consensus.get_peer_consensus_tracker", return_value=simple_tracker),
        patch("routes.consensus.get_state_store", return_value=store),
        patch(
            "routes.consensus._resolve_repo_path_for_next_action",
            return_value=Path("/repo"),
        ),
    ):
        resp = _post(client, "coder")
    assert resp.status_code == 200
    payload = resp.get_json().get("event_payload") or {}
    assert "iteration_feedback" not in payload


def _propose(tracker, role: str = "coder") -> None:
    tracker.handle_propose(
        role,
        {
            "summary": (
                "Proposal with substantive content describing the work, "
                "tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc1234",
        },
    )


def test_build_iteration_feedback_reviewer_audience_drops_prior_iteration() -> None:
    """``audience="reviewer"`` surfaces directives only — no producer
    scorecard — and tags the block for review framing (#3231 review item 1).
    """
    from routes.consensus import _build_iteration_feedback

    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(_phase_execution_with_directives())
    with patch("routes.consensus.get_state_store", return_value=store):
        block = _build_iteration_feedback(PIPELINE_ID, Path("/repo"), audience="reviewer")
    assert block is not None
    assert block["audience"] == "reviewer"
    assert [d["iteration_n"] for d in block["directives"]] == [0, 1]
    assert "prior_iteration" not in block


def test_next_action_attaches_directives_only_on_reviewer_ack(client, simple_tracker) -> None:
    """A reviewer's ``ack`` event_payload carries directives-only
    ``iteration_feedback`` so it evaluates the producer's directive-driven
    change against the operator's steering, not a stale rubric (#3231
    review item 1).
    """
    _propose(simple_tracker, "coder")
    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(_phase_execution_with_directives())
    with (
        patch("routes.consensus.get_peer_consensus_tracker", return_value=simple_tracker),
        patch("routes.consensus.get_state_store", return_value=store),
        patch(
            "routes.consensus._resolve_repo_path_for_next_action",
            return_value=Path("/repo"),
        ),
    ):
        resp = _post(client, "reviewer_code")
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["action"] == "ack"
    fb = data["event_payload"]["iteration_feedback"]
    assert fb["audience"] == "reviewer"
    assert "Drop cq-2" in fb["directives"][-1]["feedback_text"]
    # Reviewer side gets directives only — no producer scorecard.
    assert "prior_iteration" not in fb


def test_next_action_omits_iteration_feedback_on_reviewer_ack_when_no_kickback(
    client, simple_tracker
) -> None:
    """No kickback → a reviewer's ``ack`` event_payload carries no
    ``iteration_feedback`` key either (locks the reviewer golden path,
    symmetric with the producer omission test — #3231 re-review note 4).
    """
    _propose(simple_tracker, "coder")
    empty_pe = PhaseExecution(phase="refine")  # type: ignore[arg-type]
    store = MagicMock()
    store.load_pipeline.return_value = _pipeline_state(empty_pe)
    with (
        patch("routes.consensus.get_peer_consensus_tracker", return_value=simple_tracker),
        patch("routes.consensus.get_state_store", return_value=store),
        patch(
            "routes.consensus._resolve_repo_path_for_next_action",
            return_value=Path("/repo"),
        ),
    ):
        resp = _post(client, "reviewer_code")
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["action"] == "ack"
    payload = data.get("event_payload") or {}
    assert "iteration_feedback" not in payload
