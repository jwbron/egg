"""BRC consensus next-action derivation endpoint (#2908 slice-1).

This module owns ``POST /api/v1/pipelines/<pipeline_id>/consensus/next-action``.
The route inspects the in-memory :class:`PeerConsensusTracker` (with a
message-store replay fallback) and derives the *next* deterministic
BRC action a given role should take, returning one of::

    {"action": "wait" | "propose" | "ack" | "nack" | "confirm" | "complete",
     "event_payload": {...optional...}, "role": "...", "slice_id": "..."}

Slice-2 of #2908 wires this into the event-pump wrapper bash so the
wrapper drives the agent deterministically (one-shot per event) instead
of the agent re-entering a blocking ``wait_loop`` between events.

The derivation lives here — not in wrapper bash — so the sequencing
logic is unit-testable in Python (per architect od-3, slice-1
TASK-1-2). Pulling the rules into orchestrator code also keeps the
behaviour consistent across roles: a coder calling next-action and a
reviewer calling next-action see the same matrix state through the
same code path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from action_guards import check_confirm_guard
from egg_orchestrator.types import ConsensusPhase
from peer_consensus import (
    PeerConsensusTracker,
    get_peer_consensus_tracker,
    reconstruct_tracker_from_messages,
)
from slice_id_validation import extract_slice_id as _extract_slice_id

logger = get_logger("orchestrator.consensus")


consensus_bp = Blueprint("consensus", __name__, url_prefix="/api/v1/pipelines")


# Valid action values surfaced to the wrapper. Kept as a frozenset so a
# future contributor cannot accidentally widen the contract without
# updating the schema docs and the CLI's `--help` text in lockstep.
#
# Note on ``"nack"``: reserved in the schema for symmetry with ``"ack"``,
# but ``_derive_next_action`` never emits it directly — when a peer
# proposal needs review the route returns ``"ack"`` and the agent's
# ``brc_ack`` / ``brc_nack`` verb decides the actual verdict at the
# call site (the route does not pre-judge ACK vs NACK). Listing ``nack``
# here lets the slice-3 wrapper distinguish "schema is closed" from
# "agent has discretion" without a separate enum, but it is unreachable
# from the current derivation logic. A future refactor that wanted the
# route itself to surface a "must NACK" hint (e.g. for an automatic-NACK
# barrier) would emit ``"nack"`` and remain inside the schema.
_VALID_ACTIONS = frozenset({"wait", "propose", "ack", "nack", "confirm", "complete"})


def _error(message: str, status_code: int = 400) -> tuple[Response, int]:
    return jsonify({"success": False, "message": message}), status_code


def _success(
    *,
    action: str,
    role: str,
    slice_id: str | None,
    event_payload: dict[str, Any] | None = None,
    reason: str = "",
) -> tuple[Response, int]:
    """Return a 200 next-action response.

    The response is the document the CLI surfaces verbatim with ``--json``;
    keeping it structured (rather than free-form prose) makes it cheap for
    wrapper bash to parse with ``jq``.
    """
    body: dict[str, Any] = {
        "success": True,
        "action": action,
        "role": role,
        "slice_id": slice_id,
    }
    if event_payload is not None:
        body["event_payload"] = event_payload
    if reason:
        body["reason"] = reason
    return jsonify(body), 200


def _resolve_tracker(pipeline_id: str, slice_id: str | None) -> PeerConsensusTracker | None:
    """Return the in-memory tracker; reconstruct from messages if absent.

    Mirrors the pattern used by ``_get_concurrent_status`` in
    ``routes/pipelines.py`` so a route called immediately after an
    orchestrator restart sees the same state the wrapper saw before
    the restart. The reconstruct path is lazy and scoped to the
    requested slice + current phase so a slice-DAG pipeline doesn't
    spuriously mingle slices (#2761).
    """
    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if tracker is not None:
        return tracker

    # Reconstruct path: build a tracker from the message-store replay.
    # We need a ReviewGraph; pull it from the state store the same way
    # the pipelines status endpoint does (``routes/pipelines.py:4495``).
    # On any failure return None so the route degrades to "wait" rather
    # than 500-ing the wrapper.
    try:
        from review_graph import get_review_graph_for_phase
        from routes import get_state_store_for_pipeline

        _store, pipeline = get_state_store_for_pipeline(pipeline_id)
        if pipeline is None:
            return None
        phase = pipeline.current_phase.value
        graph = get_review_graph_for_phase(phase, repo=pipeline.repo)
        if graph is None:
            return None
        return reconstruct_tracker_from_messages(
            pipeline_id,
            graph,
            slice_id=slice_id,
            phase=phase,
        )
    except Exception:  # pragma: no cover - best-effort fallback
        logger.warning(
            "Failed to reconstruct tracker for next-action",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            exc_info=True,
        )
        return None


def _has_pending_peer_proposals(
    tracker: PeerConsensusTracker, reviewer: str
) -> tuple[bool, list[dict[str, Any]]]:
    """Return (has_pending, pending_descriptions) for a reviewer role.

    A "pending" review is one where the producer is currently PROPOSED
    AND this reviewer's matrix entry version is less than the producer's
    current proposal version (i.e. the reviewer has not yet weighed in
    on the current version). This covers both the first-review path and
    the stale-version re-review path (#2482) — in both cases the
    reviewer's verdict is needed.
    """
    producers = tracker.graph.producers_for(reviewer)
    if not producers:
        return False, []
    pending: list[dict[str, Any]] = []
    for producer in producers:
        # Skip self-reviews (a dual-role agent reviewing its own producer
        # role); BRC does not require self-review and including it would
        # let a dual-role agent block on itself.
        if producer == reviewer:
            continue
        # Skip a generic no-op proposal (#3027): the producer declared it
        # has no work in this slice, so there is nothing to review and the
        # reviewer must not NACK it (that was the empty-proposal deadlock).
        if tracker.matrix.is_no_changes_proposal(producer):
            continue
        producer_phase = tracker._producer_phases.get(producer, ConsensusPhase.WORKING)
        if producer_phase != ConsensusPhase.PROPOSED:
            continue
        current_version = tracker.matrix.get_proposal_version(producer)
        if current_version <= 0:
            continue
        entry = tracker.matrix.get_entry(reviewer, producer)
        # No verdict yet, or stale verdict on a prior version → review needed.
        if entry is None or entry.version < current_version:
            # Enrich with the producer's current proposal artifact list
            # so the wrapper's per-event prompt composer can render a
            # degraded changed_artifacts fallback when the reviewer has
            # no stored ``last_reviewed_commit_sha`` for this producer
            # (slice-3 reviewer_code_holistic v2 finding #1 — wire the
            # documented fallback through the live payload shape).
            # The snapshot is read-only and locked through the tracker's
            # public API; missing producers yield empty artifact lists.
            snapshot = tracker.get_current_proposal_snapshot(producer)
            artifact_refs = list(snapshot.get("artifacts") or [])
            pending.append(
                {
                    "producer": producer,
                    "current_version": current_version,
                    "prior_version": entry.version if entry else 0,
                    "prior_verdict": entry.state.value if entry else "pending",
                    "artifact_refs": artifact_refs,
                }
            )
    return bool(pending), pending


def _producer_has_open_barrier(
    tracker: PeerConsensusTracker, producer: str
) -> dict[str, Any] | None:
    """Return the open-NACK barrier payload if currently active, else None.

    Mirrors ``PeerConsensusTracker._open_nacks_barrier_response`` but is
    read-only: it does NOT update the ``_open_nack_notified_at``
    watermark (a next-action probe shouldn't acknowledge the NACKs on
    behalf of the producer). The matrix watermark continues to advance
    only when the producer actually calls ``re_propose`` and the
    barrier returns its own structured rejection (#2142).
    """
    current_version = tracker.matrix.get_proposal_version(producer)
    if current_version == 0:
        return None

    relevant: list[tuple[str, Any]] = []
    for reviewer, entry in tracker.matrix.get_nack_entries_for(producer):
        if entry.version == current_version and entry.timestamp is not None:
            relevant.append((reviewer, entry))
    if len(relevant) < 2:
        return None

    nacks_payload = [
        {
            "reviewer": reviewer,
            "version": entry.version,
            "reason": entry.reason,
            "artifact_refs": list(entry.nack_artifact_refs),
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
        }
        for reviewer, entry in relevant
    ]
    return {
        "status": "open_nacks_blocked",
        "producer": producer,
        "current_version": current_version,
        "nacking_reviewers": [r for r, _ in relevant],
        "nacks": nacks_payload,
    }


def _producer_has_unresolved_nacks_on_current_version(
    tracker: PeerConsensusTracker, producer: str
) -> list[dict[str, Any]]:
    """Return NACK entries against the producer's current proposal version.

    Distinct from the open-NACK *barrier* (which only fires for 2+
    reviewers): a single-reviewer NACK against the current version is
    still actionable feedback the producer must address with a
    re-propose. Used to flag "propose" (re-propose to fix) instead of
    "wait" when the producer is still in PROPOSED phase.
    """
    current_version = tracker.matrix.get_proposal_version(producer)
    if current_version == 0:
        return []
    out: list[dict[str, Any]] = []
    for reviewer, entry in tracker.matrix.get_nack_entries_for(producer):
        if entry.version == current_version:
            out.append(
                {
                    "reviewer": reviewer,
                    "version": entry.version,
                    "reason": entry.reason,
                    "artifact_refs": list(entry.nack_artifact_refs),
                }
            )
    return out


def _derive_next_action(
    tracker: PeerConsensusTracker, role: str
) -> tuple[str, dict[str, Any] | None, str]:
    """Compute (action, event_payload, reason) for a role.

    Caller MUST validate that ``role`` is a participant in the review
    graph (producer, reviewer, or both). This function assumes the
    membership check has been done so it can focus on the BRC state
    derivation.

    Dual-role ordering (#2749, risk_analyst R11):
      * sub-case a: dual-role in WORKING with peer proposals pending →
        the role's own ``propose`` takes priority, NOT peer review.
      * sub-case b: dual-role post-own-propose with peer reviews
        pending → review the peer (``ack``).
    """
    with tracker._lock:
        # ---- 1. role-complete short-circuit ----
        consensus = tracker.evaluate()
        is_complete = bool(consensus.get("is_complete", False))
        confirmed_roles = tracker.confirmed_roles  # frozenset snapshot
        if role in confirmed_roles:
            if is_complete:
                return "complete", None, "all roles confirmed"
            blocking = list(consensus.get("blocking_agents", []) or [])
            return (
                "wait",
                {"blocking_agents": blocking},
                "role confirmed; waiting for others to converge",
            )

        is_producer = tracker.graph.is_producer(role)
        is_reviewer = tracker.graph.is_reviewer(role)

        # ---- 2. producer-side logic (dual-role priority for own-work) ----
        #
        # Dual-role agents (e.g. tester is both producer of test files
        # and reviewer of coder's source files) follow the #2749 / R11
        # ordering:
        #   * sub-case a: WORKING with peer proposals pending → produce
        #     and propose own work first.
        #   * sub-case b: PROPOSED with peer proposals pending → review
        #     the peer.
        #
        # Below: WORKING short-circuits to "propose"; PROPOSED falls
        # through to the reviewer-side block when the producer cannot
        # confirm and has no NACKs of its own to address.
        if is_producer:
            producer_phase = tracker._producer_phases.get(role, ConsensusPhase.WORKING)

            if producer_phase == ConsensusPhase.WORKING:
                # 2a. WORKING — produce and propose. R11a applies:
                # propose OWN work even if peer proposals are pending.
                barrier = _producer_has_open_barrier(tracker, role)
                if barrier is not None:
                    return (
                        "propose",
                        barrier,
                        "open-NACK barrier requires aggregating fixes before re-propose",
                    )
                nacks = _producer_has_unresolved_nacks_on_current_version(tracker, role)
                payload: dict[str, Any] = {"producer": role}
                if nacks:
                    payload["unresolved_nacks"] = nacks
                return "propose", payload, "produce and propose"

            if producer_phase == ConsensusPhase.PROPOSED:
                # 2b. PROPOSED — own work submitted. First check
                # producer-side action (re-propose / confirm), then
                # fall through to reviewer-side for R11b.
                barrier = _producer_has_open_barrier(tracker, role)
                if barrier is not None:
                    return "propose", barrier, "open-NACK barrier requires re-propose"
                nacks = _producer_has_unresolved_nacks_on_current_version(tracker, role)
                if nacks:
                    return (
                        "propose",
                        {"unresolved_nacks": nacks, "producer": role},
                        "address NACK(s) and re-propose",
                    )
                guard = check_confirm_guard(
                    role, tracker.graph, tracker.matrix, set(confirmed_roles)
                )
                if guard.allowed:
                    return "confirm", None, "all preconditions for confirm satisfied"
                # Fall through to the reviewer block below if the role
                # has reviewer responsibilities (R11b) — otherwise wait.
                if not is_reviewer:
                    return (
                        "wait",
                        {"confirm_guard_reason": guard.reason},
                        "waiting for reviewers to ACK or NACK",
                    )
                # is_reviewer: continue to the reviewer block.

            elif producer_phase == ConsensusPhase.CONFIRMED:
                # 2c. Producer FSM CONFIRMED but role not in confirmed
                # set — inconsistent intermediate; wait.
                if not is_reviewer:
                    return (
                        "wait",
                        None,
                        "producer in CONFIRMED phase; awaiting global converge",
                    )
                # Continue to reviewer-side checks.

        # ---- 3. reviewer-side logic (pure-reviewer + dual-role fall-through) ----
        if is_reviewer:
            has_pending, pending = _has_pending_peer_proposals(tracker, role)
            if has_pending:
                return (
                    "ack",
                    {"pending_reviews": pending},
                    "peer proposal(s) need review",
                )
            guard = check_confirm_guard(role, tracker.graph, tracker.matrix, set(confirmed_roles))
            if guard.allowed:
                return "confirm", None, "all reviews complete; eligible to confirm"
            return (
                "wait",
                {"confirm_guard_reason": guard.reason},
                "no pending reviews; waiting for peer proposals or convergence",
            )

        # Should never reach here — _derive_next_action's contract
        # requires the caller to have already validated graph membership.
        return "wait", None, "role not in review graph"


@consensus_bp.route("/<pipeline_id>/consensus/next-action", methods=["POST"])
def handle_next_action(pipeline_id: str) -> tuple[Response, int]:
    """Derive the next BRC action for the given role.

    Request body::

        {"role": "coder",
         "slice_id": "slice-1"}  # optional; falls back to no-slice tracker

    Response 200::

        {"success": true,
         "action": "wait" | "propose" | "ack" | "nack" | "confirm" | "complete",
         "event_payload": {...optional, action-specific...},
         "role": "<role>",
         "slice_id": "<slice_id-or-null>",
         "reason": "<one-line human-readable explanation>"}

    Response 400 on missing/invalid role or pipeline_id; 200 with
    ``action="wait"`` when no tracker exists yet (the wrapper should
    fall back to blocking on the message bus until a proposal lands).

    Auth: agent-facing. Mirrors the existing ``/<pipeline_id>/signal``
    route — no ``require_lifecycle_secret`` decorator. Agents call
    this from inside the sandbox pod; the gateway-enforced
    NetworkPolicy is the trust boundary, identical to the rest of the
    consensus surface.
    """
    data = request.get_json(silent=True)
    if data is None:
        return _error("Missing request body")
    if not isinstance(data, dict):
        return _error("Request body must be a JSON object")

    role = (data.get("role") or "").strip()
    if not role:
        return _error("'role' is required")

    # Validate slice_id with the canonical helper so a malformed value
    # can never smuggle path separators into a tracker key.
    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return _error(f"Invalid slice_id: {exc}")

    tracker = _resolve_tracker(pipeline_id, slice_id)
    if tracker is None:
        # No tracker — pipeline hasn't started consensus yet (e.g. agent
        # pod came up before the orchestrator registered the graph). The
        # wrapper's contract on "wait" is to block on message wait_loop,
        # which is the right behaviour here.
        return _success(
            action="wait",
            role=role,
            slice_id=slice_id,
            reason="no consensus tracker yet; agent should block on message bus",
        )

    # Graph membership check — phantom agents return 400 so the wrapper
    # surfaces a configuration error instead of silently waiting forever.
    if not (tracker.graph.is_producer(role) or tracker.graph.is_reviewer(role)):
        return _error(
            f"Role {role!r} is not a participant in the review graph "
            f"(neither producer nor reviewer)",
            status_code=400,
        )

    action, event_payload, reason = _derive_next_action(tracker, role)
    if action not in _VALID_ACTIONS:  # pragma: no cover - defensive
        logger.error(
            "next-action produced invalid action; coercing to wait",
            pipeline_id=pipeline_id,
            role=role,
            action=action,
        )
        action = "wait"

    return _success(
        action=action,
        role=role,
        slice_id=slice_id,
        event_payload=event_payload,
        reason=reason,
    )
