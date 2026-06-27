"""Conditional-ACK consensus-graph mutations (#3312 decomposition).

The 3-way conditional-ACK HITL gate (``_handle_conditional_ack_gate`` in
``_handlers``) dispatches into these helpers to persist deferred actions or to
drive the approval matrix / producer phase directly. ``get_peer_consensus_tracker``
is resolved through the package barrel (``_pkg``) to preserve the test seam.
"""

from pathlib import Path
from typing import Any

import routes.decisions as _pkg

from . import logger


def _persist_deferred_actions(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
    repo_path: Path,
) -> None:
    """Write conditions to ``contract.pr.deferred_actions`` (#2004).

    Loads the contract from the pipeline's worktree, appends one formatted
    line per condition, and saves. Writes are deduplicated against any
    existing entries so resolving the same gate twice (or re-queuing after
    tracker-state changes) doesn't duplicate bullets on the PR.
    """
    try:
        from egg_contracts.loader import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
            save_contract,
        )
        from egg_contracts.models import DeferredAction, PRMetadata
    except ImportError:
        logger.warning(
            "egg_contracts unavailable; cannot persist deferred_actions",
            pipeline_id=pipeline_id,
        )
        return

    try:
        from routes import resolve_worktree_path
        from routes.pipelines import _pipeline_identifier
        from state_store import get_state_store

        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        worktree_path = resolve_worktree_path(pipeline_id, repo_path)
        contract_id = _pipeline_identifier(pipeline.issue_number, pipeline.id)
        contract = load_contract(contract_id, worktree_path)
    except ContractNotFoundError, OSError, ValueError, ContractValidationError:
        logger.warning(
            "Cannot load contract to persist deferred_actions",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return

    new_actions: list[DeferredAction] = []
    for c in conditions:
        reviewer = str(c.get("reviewer", "")).strip() or "unknown"
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        resolved_in_diff = str(c.get("resolved_in_diff", "")).strip()
        new_actions.append(
            DeferredAction(
                reviewer=reviewer,
                condition=condition,
                resolved_in_diff=resolved_in_diff,
            )
        )
    if not new_actions:
        return

    # PR metadata may be absent on ISSUE-mode pipelines that haven't yet hit
    # the PR-finalization writeback (unlikely here since the gate requires a
    # tracker, but defensive). Create a minimal stub using the issue title if so.
    if contract.pr is None:
        contract.pr = PRMetadata(
            title=(contract.issue.title if contract.issue else "Pipeline deferred actions"),
        )

    # Dedupe by (reviewer, condition) so re-resolving the same gate doesn't
    # double-list. A later call that adds a ``resolved_in_diff`` for an
    # already-persisted obligation upgrades the existing entry in place.
    #
    # Dedup is intentionally one-way (#2336 review):
    #   - SHA-replaces-SHA: an existing resolution is *not* overwritten
    #     by a later resolution for the same ``(reviewer, condition)``.
    #     Once a reviewer marks an obligation resolved, the recorded SHA
    #     is sticky.
    #   - Resolved → open downgrade: a later open-only entry does *not*
    #     clear ``resolved_in_diff``. This matches the pre-existing
    #     append-only design of contract-persisted obligations (#2004).
    # Both cases are edge cases driven by NACK / re-propose / reviewer
    # re-ACK cycles; the live tracker remains the source of truth for
    # in-flight state and is preferred by the renderer at Tier 2.
    merged: list[DeferredAction] = list(contract.pr.deferred_actions)
    by_key: dict[tuple[str, str], DeferredAction] = {(a.reviewer, a.condition): a for a in merged}
    for action in new_actions:
        key = (action.reviewer, action.condition)
        existing = by_key.get(key)
        if existing is None:
            merged.append(action)
            by_key[key] = action
        elif action.resolved_in_diff and not existing.resolved_in_diff:
            # Upgrade open → resolved; preserve list ordering.
            existing.resolved_in_diff = action.resolved_in_diff
    contract.pr.deferred_actions = merged

    try:
        save_contract(contract, worktree_path)
    except OSError, ValueError:
        # The gate decision is already resolved, so the human's intent is
        # recorded. Recovery depends on the tracker surviving until the
        # next complete_phase call, where _ensure_conditional_ack_gate
        # re-queues a new gate. If the tracker is torn down before that
        # (e.g. orchestrator restart), the obligations are silently lost.
        logger.warning(
            "Failed to save contract with deferred_actions",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return

    logger.info(
        "Persisted pre-merge obligations to contract",
        pipeline_id=pipeline_id,
        deferred_action_count=len(merged),
    )


def _force_nack_conditional_edges(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
) -> None:
    """Force-NACK each (reviewer, producer) edge carrying a condition (#2004).

    The human has rejected the obligation. This is not a reviewer-
    authored NACK — there's no proposal artifact to cite, and the
    ReviewPayload schema rightly rejects empty artifact_references.
    Instead, drive the approval matrix + producer-phase state directly
    so the end state matches a normal NACK: edge NACKED at current
    version, condition cleared, producer back in WORKING.
    """
    from peer_consensus import ConsensusPhase

    tracker = _pkg.get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        logger.warning(
            "No active tracker to force-NACK conditional ACK",
            pipeline_id=pipeline_id,
        )
        return

    synthetic_reason = "human rejected conditional ACK"
    nacked: list[tuple[str, str]] = []
    with tracker._lock:
        for c in conditions:
            reviewer = str(c.get("reviewer", "")).strip()
            producer = str(c.get("producer", "")).strip()
            if not reviewer or not producer:
                continue
            try:
                version = tracker.matrix.get_proposal_version(producer)
                tracker.matrix.record_nack(
                    reviewer,
                    producer,
                    version,
                    reason=synthetic_reason,
                    artifact_refs=[],
                )
                tracker._producer_phases[producer] = ConsensusPhase.WORKING
                nacked.append((reviewer, producer))
            except Exception:
                logger.warning(
                    "Failed to force-NACK conditional edge",
                    pipeline_id=pipeline_id,
                    reviewer=reviewer,
                    producer=producer,
                    exc_info=True,
                )
    if nacked:
        logger.info(
            "Force-NACKed conditional ACK edges",
            pipeline_id=pipeline_id,
            edges=nacked,
        )


def _invalidate_conditional_acks(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
) -> None:
    """Invalidate each conditioning ACK edge so the producer re-proposes (#2004).

    Unlike NACK, invalidation doesn't bump the revision count — the ACK
    just drops back to PENDING. The producer phase state is reset to
    WORKING so it can re-propose with the condition folded into its
    next proposal's scope.
    """
    from peer_consensus import ConsensusPhase

    tracker = _pkg.get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        logger.warning(
            "No active tracker to invalidate conditional ACK",
            pipeline_id=pipeline_id,
        )
        return

    invalidated: list[tuple[str, str]] = []
    with tracker._lock:
        for c in conditions:
            reviewer = str(c.get("reviewer", "")).strip()
            producer = str(c.get("producer", "")).strip()
            if not reviewer or not producer:
                continue
            try:
                did_invalidate = tracker.matrix.invalidate_ack(reviewer, producer)
            except Exception:
                logger.warning(
                    "Failed to invalidate conditional ACK",
                    pipeline_id=pipeline_id,
                    reviewer=reviewer,
                    producer=producer,
                    exc_info=True,
                )
                continue
            if did_invalidate:
                tracker._producer_phases[producer] = ConsensusPhase.WORKING
                invalidated.append((reviewer, producer))
    if invalidated:
        logger.info(
            "Invalidated conditional ACK edges for in-pipeline address",
            pipeline_id=pipeline_id,
            edges=invalidated,
        )
