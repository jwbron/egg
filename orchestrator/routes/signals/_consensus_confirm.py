"""Consensus confirmation flow: confirmed / excuse-producer / resolve-obligation / producer-push + their helpers (#3312)."""

from pathlib import Path
from typing import Any

import routes.signals as _pkg
from flask import Response
from slice_id_validation import extract_slice_id as _extract_slice_id
from state_store import (
    StateStoreError,
)

from ._responses import make_error_response, make_success_response


def _write_consensus_confirmed_marker(pipeline_id: str, agent_role: str, repo_path: Path) -> None:
    """Write a marker file so auto-commit skips push after BRC confirmation (#1473)."""
    try:
        worktree_path = _pkg.resolve_worktree_path(pipeline_id, repo_path)
        marker_dir = worktree_path / ".egg-state" / "agent-outputs"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_file = marker_dir / "consensus-confirmed"
        marker_file.touch()
    except Exception as e:
        _pkg.logger.warning(
            "Failed to write consensus-confirmed marker (non-blocking)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )


def _existing_confirmed_for_role(
    pipeline_id: str,
    agent_role: str,
    phase: str | None,
    slice_id: str | None = None,
) -> tuple[bool, bool]:
    """Return (has_final, has_pending_acks) for prior CONFIRMED messages.

    Scans the message store for prior ``CONSENSUS_CONFIRMED`` messages
    from ``agent_role`` in ``phase``.  Used for idempotency so that
    repeated ``egg-orch consensus confirmed`` invocations don't pollute
    the bus with duplicate messages (see #1890).

    - ``has_final``: a non-pending_acks CONFIRMED message already exists.
    - ``has_pending_acks``: a pending_acks CONFIRMED message exists.

    ``slice_id`` scopes the check to a single slice. The message store
    keys messages by bare ``pipeline_id``, so without scoping a fresh
    slice-N coder would falsely appear "already confirmed" because
    slice-(N-1)'s coder wrote a CONFIRMED message under the same
    pipeline_id (#2535). Filtering on ``metadata["slice_id"]`` (written
    by the per-slice tracker path below) confines the lookup to the
    same slice. Pipeline-scoped (``slice_id is None``) callers continue
    to see only messages with no ``slice_id`` in metadata, preserving
    the legacy non-slice behaviour. Tracked under #2409 as part of
    end-to-end slice-scoped message routing.
    """
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        except ImportError:
            return (False, False)

    try:
        store = get_message_store()
        # Fetch a generous window of recent messages.  get_messages returns
        # the *newest* N, so an extremely old CONFIRMED in a >10k-message
        # pipeline could be missed — but that's the safe failure direction
        # (a duplicate write, not a lost write).  Don't lower this limit
        # without understanding that tradeoff.
        messages = store.get_messages(pipeline_id, limit=10000)
    except Exception:
        return (False, False)

    # Confirmed-producer reopen awareness (#3124): a CONSENSUS_REOPENED
    # targeting this role invalidates any *earlier* CONFIRMED message —
    # the role re-entered WORKING, so its eventual re-confirm must write
    # a fresh CONFIRMED message. Without this, the dedupe below would
    # swallow the re-confirm and message replay (which processes the
    # post-reopen re-propose *after* the stale CONFIRMED) would leave
    # the role unconfirmed forever after an orchestrator restart.
    latest_reopen_ts = None
    for m in messages:
        if str(getattr(m, "message_type", "")) != "CONSENSUS_REOPENED":
            continue
        if getattr(m, "to_role", None) != agent_role:
            continue
        msg_phase = getattr(m, "phase", None)
        if phase is not None and msg_phase is not None and msg_phase != phase:
            continue
        metadata = getattr(m, "metadata", None) or {}
        if metadata.get("slice_id") != slice_id:
            continue
        ts = getattr(m, "timestamp", None)
        if ts is not None and (latest_reopen_ts is None or ts > latest_reopen_ts):
            latest_reopen_ts = ts

    has_final = False
    has_pending = False
    for m in messages:
        if getattr(m, "from_role", None) != agent_role:
            continue
        if str(getattr(m, "message_type", "")) != "CONSENSUS_CONFIRMED":
            continue
        msg_phase = getattr(m, "phase", None)
        # A null msg_phase is treated as matching any phase.  In practice
        # all CONSENSUS_CONFIRMED writes set a phase, but if one somehow
        # doesn't, counting it as a match is the conservative choice
        # (prevents a duplicate rather than allowing one).
        if phase is not None and msg_phase is not None and msg_phase != phase:
            continue
        metadata = getattr(m, "metadata", None) or {}
        # Scope idempotency check to the same slice. A None slice_id on
        # either side (caller or message) only matches the same; this
        # cleanly separates slice-N from slice-M and from pipeline-level
        # confirms.
        if metadata.get("slice_id") != slice_id:
            continue
        # Stale: confirmed before the latest reopen (#3124). A missing
        # timestamp counts as stale — the safe failure direction here is
        # a duplicate CONFIRMED write, not a lost re-confirm.
        if latest_reopen_ts is not None:
            ts = getattr(m, "timestamp", None)
            if ts is None or ts <= latest_reopen_ts:
                continue
        if metadata.get("pending_acks"):
            has_pending = True
        else:
            has_final = True
    return (has_final, has_pending)


def handle_consensus_confirmed_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_CONFIRMED signal from an agent.

    Idempotent with respect to the message store: repeated invocations
    from the same agent in the same phase do not pollute the bus with
    duplicate CONFIRMED messages (see #1890).  The underlying consensus
    tracker still observes each call so its own state stays in sync.
    """
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        # Defaults must be outside the try block so the message-bus fallback
        # (second try block) can reference them even if reconstruction fails.
        _phase = "implement"
        _repo = None

        # Attempt reconstruction from message store before returning 404.
        # ``reconstruct_tracker_from_messages`` applies the strict-
        # equality filter ``_message_slice_id(m) == slice_id``
        # (peer_consensus.py near line 2003) so a slice-scoped
        # reconstruction populates the nested
        # ``{pipeline_id}/{slice_id}`` tracker key ONLY from
        # exactly-tagged messages. The store-level filter at
        # ``message_store.py:407-418`` is intentionally lenient
        # (``metadata.slice_id is None`` messages pass through any
        # slice filter so OVERSEER_ALERTs fan out) — the
        # peer_consensus filter is the actual isolation enforcer.
        # Slice-4 TASK-4-5 (closes #2409) removes the prior
        # ``slice_id is None`` skip: per-slice reconstruction is now
        # safe because the strict-equality filter prevents cross-
        # slice mingling, and startup_reconciliation has already had
        # its first crack at populating every slice tracker.
        try:
            from peer_consensus import reconstruct_tracker_from_messages
            from review_graph import get_review_graph_for_phase

            # Determine phase and repo from pipeline state if available
            try:
                _pip = _pkg.get_state_store(repo_path).load_pipeline(pipeline_id)
                _phase = _pip.current_phase.value
                _repo = _pip.repo
            except StateStoreError:
                pass

            graph = get_review_graph_for_phase(_phase, repo=_repo)
            tracker = reconstruct_tracker_from_messages(pipeline_id, graph, slice_id=slice_id)
        except Exception as recon_err:
            _pkg.logger.warning(
                "Tracker reconstruction failed in confirmed handler",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(recon_err),
            )

        if not tracker and slice_id is not None:
            # Slice-scoped: pipeline-wide message-bus fallback would mingle
            # other slices' CONFIRMs and reach false consensus the moment a
            # fresh slice spawns roles matching an already-confirmed prior
            # slice (#2535). Per-slice trackers are recreated by the slice
            # scheduler on the next iteration; surface the missing tracker
            # rather than guessing from sibling-slice state.
            scope = f"{pipeline_id}/{slice_id}"
            return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

        if not tracker:
            # Message-bus authoritative fallback: if all expected roles have
            # CONSENSUS_CONFIRMED messages, accept the confirmation directly.
            #
            # Contract-completeness gate carve-out (#3114): the
            # ``_contract_completeness_rejection`` CONFIRM check is on the
            # tracker path below and is BYPASSED here. The fallback only
            # fires when (a) the tracker is gone and (b) every role has
            # already emitted CONSENSUS_CONFIRMED via stored messages —
            # i.e. consensus is being replayed, not decided. The enforcer
            # CONFIRM that originally closed the slice has already passed
            # through the gate; this path simply re-acknowledges the
            # replayed state, so re-running the gate here would block a
            # legitimate idempotent recovery (a stale incomplete row that
            # has since been delivered would still hold consensus open).
            try:
                from message_store import Message, MessageType, get_message_store
                from review_graph import get_review_graph_for_phase

                store = get_message_store()
                messages = store.get_messages(pipeline_id, limit=10000)
                # Count ANY CONSENSUS_CONFIRMED message — when the
                # tracker is lost we can't cross-reference _confirmed,
                # so be lenient.  Matches the consensus_stall health
                # check which also doesn't filter pending_acks (#1671).
                confirmed_roles = {
                    m.from_role for m in messages if m.message_type == "CONSENSUS_CONFIRMED"
                }
                # Agent sending this signal is also confirming
                confirmed_roles.add(agent_role)

                graph = get_review_graph_for_phase(_phase, repo=_repo)
                all_roles = graph.all_roles()

                if all_roles and all_roles.issubset(confirmed_roles):
                    _pkg.logger.info(
                        "All roles confirmed via message bus (tracker lost)",
                        pipeline_id=pipeline_id,
                        confirmed_roles=sorted(confirmed_roles),
                    )
                    # Idempotency: only write the CONFIRMED message if this
                    # role hasn't already emitted a final one in this phase.
                    has_final, _ = _pkg._existing_confirmed_for_role(
                        pipeline_id, agent_role, _phase
                    )
                    if not has_final:
                        store.add_message(
                            Message(
                                pipeline_id=pipeline_id,
                                from_role=agent_role,
                                to_role="all",
                                message_type=MessageType.CONSENSUS_CONFIRMED,
                                subject=f"Confirmed by {agent_role}",
                                body="",
                                phase=_phase,
                                metadata={
                                    "consensus_reached": True,
                                    "fallback": "message_bus",
                                },
                            )
                        )
                        _pkg._write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)
                    return make_success_response(
                        f"Confirmation recorded for {agent_role} (message-bus fallback)",
                        data={
                            "status": "confirmed",
                            "consensus_reached": True,
                            "fallback": "message_bus",
                            "idempotent": has_final,
                        },
                    )
            except Exception as fallback_err:
                _pkg.logger.warning(
                    "Message-bus fallback failed in confirmed handler",
                    pipeline_id=pipeline_id,
                    error=str(fallback_err),
                )

            scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
            return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    # Contract-task completeness gate (#3114): an enforcer's CONFIRM is
    # the closing act of a slice consensus — reject it while ANY task row
    # in the slice is incomplete (role-scoped ACK gates cover owned rows;
    # this catches role-less rows and no-op-proposal bypasses).
    gate_rejection = _pkg._contract_completeness_rejection(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        slice_id=slice_id,
        check="confirm",
        enforcer_role=agent_role,
    )
    if gate_rejection is not None:
        return gate_rejection

    try:
        result = tracker.handle_confirmed(agent_role)

        # If the producer is waiting for reviewer re-ACKs (e.g. after a
        # re-proposal invalidated stale ACKs), return 202 so the agent
        # knows to retry later instead of treating it as an error.
        # We still write a CONSENSUS_CONFIRMED message to the store (with
        # pending_acks=True metadata) so the message-bus fallback in
        # check_consensus() can detect when all agents have *attempted*
        # confirmation even if the tracker rejected some (#1615).
        current_phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)
        # Pass slice_id so the idempotency probe doesn't see sibling-slice
        # CONFIRMs as "already confirmed for this role" (#2535).
        has_final, has_pending = _pkg._existing_confirmed_for_role(
            pipeline_id, agent_role, current_phase, slice_id=slice_id
        )

        # Common metadata tag so future _existing_confirmed_for_role probes
        # can scope by slice (None for pipeline-level callers, matching the
        # legacy behaviour exactly).
        _slice_meta = {"slice_id": slice_id} if slice_id is not None else {}

        if result.get("status") == "pending_acks":
            # Dedupe pending_acks writes once an agent has already emitted one
            # (or a final) in this phase — the fallback check only needs one
            # to detect "attempted confirmation" (#1615).
            if not has_pending and not has_final:
                from message_store import Message, MessageType, get_message_store

                store = get_message_store()
                store.add_message(
                    Message(
                        pipeline_id=pipeline_id,
                        from_role=agent_role,
                        to_role="all",
                        message_type=MessageType.CONSENSUS_CONFIRMED,
                        subject=f"Confirmed by {agent_role} (pending_acks)",
                        body=result.get("message", ""),
                        phase=current_phase,
                        metadata={"pending_acks": True, **_slice_meta},
                    )
                )
            return make_success_response(result["message"], data=result, status_code=202)

        # Final CONFIRMED: skip if this role has already emitted one in this
        # phase.  Prevents the ``egg-orch consensus confirmed`` retry-loop
        # from spraying the bus with duplicates (#1890).
        if not has_final:
            from message_store import Message, MessageType, get_message_store

            store = get_message_store()
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role=agent_role,
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject=f"Confirmed by {agent_role}",
                    body="",
                    phase=current_phase,
                    metadata={
                        "consensus_reached": result.get("consensus_reached", False),
                        **_slice_meta,
                    },
                )
            )

            # Write consensus-confirmed marker so auto-commit can detect that
            # BRC review is complete and skip pushing unreviewed WIP (#1473).
            _pkg._write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)

        payload = dict(result)
        if has_final:
            payload["idempotent"] = True
        return make_success_response(
            f"Confirmation recorded for {agent_role}",
            data=payload,
        )
    except ValueError as e:
        _pkg.logger.error(
            "Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e)
        )
        return make_error_response(str(e), 400)
    except Exception as e:
        _pkg.logger.error(
            "Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e)
        )
        return make_error_response(str(e), 500)


def handle_consensus_excuse_producer_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle EXCUSE_PRODUCER signal (HITL-gated).

    Removes a non-delivering producer from the review graph so that
    reviewers can proceed without its deliverable.  Requires a resolved
    HITL decision — the ``decision_id`` field must reference a RESOLVED
    decision to prevent unauthorized producer removal.

    Request data:
        producer_role: The producer role to excuse.
        reason: Why the producer is being excused.
        decision_id: ID of the resolved HITL decision authorizing this action.
    """
    producer_role = data.get("producer_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    reason = data.get("reason", "")

    # --- HITL gate: require a resolved decision ---
    decision_id = data.get("decision_id")
    if not decision_id:
        return make_error_response(
            "Missing decision_id. consensus_excuse_producer requires a "
            "resolved HITL decision. Create a decision via the decisions "
            "API and resolve it before calling this signal.",
            status_code=403,
        )

    try:
        from decision_queue import DecisionNotFoundError, get_decision_queue
    except ImportError:
        from ..decision_queue import (  # type: ignore[no-redef]
            DecisionNotFoundError,
            get_decision_queue,
        )

    try:
        from models import DecisionStatus
    except ImportError:
        from ..models import DecisionStatus  # type: ignore[no-redef]

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.get_decision(decision_id)
        if decision.status != DecisionStatus.RESOLVED:
            return make_error_response(
                f"Decision {decision_id} is not resolved "
                f"(status: {decision.status.value}). Only resolved HITL "
                f"decisions can authorize producer excusal.",
                status_code=403,
            )

        # Scope validation: the decision must be specifically about
        # excusing *this* producer, not just any resolved decision.
        # Mirrors the excuse_reviewer pattern in decisions.py.
        expected_context = f"failed_role:{producer_role}"
        if decision.context != expected_context:
            return make_error_response(
                f"Decision {decision_id} is not authorized for excusing "
                f"producer {producer_role} (expected context: "
                f"'{expected_context}', got: '{decision.context}').",
                status_code=403,
            )
    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found. A valid resolved HITL "
            f"decision is required to excuse a producer.",
            status_code=404,
        )

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.excuse_producer(producer_role, reason)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)
        # Tag with slice_id so the implement-phase BRC writer can
        # partition this STATUS into the correct per-slice transcript
        # (#2548). Pipeline-level (non-slice) callers leave the metadata
        # off entirely.
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}

        # Notify all agents that the producer has been excused
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.STATUS,
                subject=f"Producer {producer_role} excused from consensus",
                body=(
                    f"Producer {producer_role} has been excused from the consensus "
                    f"protocol (reason: {reason or 'HITL decision'}). Reviewers "
                    f"assigned to this producer are no longer blocked by it."
                ),
                phase=phase,
                metadata={
                    "excuse_producer": True,
                    "producer_role": producer_role,
                    "reason": reason,
                    "affected_reviewers": result.get("affected_reviewers", []),
                    **_slice_meta,
                },
            )
        )

        return make_success_response(
            f"Producer {producer_role} excused from consensus",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error(
            "Failed to excuse producer",
            pipeline_id=pipeline_id,
            producer_role=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_resolve_obligation_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_RESOLVE_OBLIGATION signal (#2338).

    The caller (typically the tester after cherry-picking the conditioning
    commit) marks a reviewer's conditional-ACK obligation as satisfied
    in-cycle. The matrix keeps the obligation text for audit but
    ``get_pre_merge_conditions`` filters out resolved entries, so the PR
    body and HITL gate stop surfacing the obligation.

    Resolution is per-version: any later ``record_ack`` / ``record_nack`` /
    ``invalidate_ack`` resets the resolved flag. If the same obligation
    re-appears on a later proposal, the satisfier must call this signal
    again (or the reviewer should drop it on re-ACK per the prompt
    guidance in ``code-review-criteria.md``).

    Request data:
        agent_role: Caller's role (the resolver — recorded for audit).
        reviewer_role: Reviewer whose conditional-ACK is being resolved.
        producer_role: Producer the conditional-ACK was attached to.
        commit_sha: Optional commit SHA that satisfies the obligation.
        note: Optional free-form note for the audit log.
    """
    resolver_role = data.get("agent_role")
    reviewer_role = data.get("reviewer_role")
    producer_role = data.get("producer_role")
    if not resolver_role:
        return make_error_response("Missing agent_role")
    if not reviewer_role:
        return make_error_response("Missing reviewer_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    commit_sha = (data.get("commit_sha") or "").strip()
    note = (data.get("note") or "").strip()

    # Validate the optional resolution note for parity with summary / reason
    # validation on other BRC verbs. Notes are short-form imperatives, not
    # rationale, so they share the relaxed minimum-length bucket with
    # pre-merge conditions (#2338).
    if note:
        note_error = _pkg._validate_brc_content(note, "Pre-merge condition")
        if note_error:
            return make_error_response(note_error, 400)

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.handle_resolve_obligation(
            resolver_role=resolver_role,
            reviewer_role=reviewer_role,
            producer_role=producer_role,
            commit_sha=commit_sha,
            note=note,
        )

        # Persist the resolution so ``reconstruct_tracker_from_messages``
        # can replay it after an orchestrator restart (#2338). Without
        # this, a satisfied obligation re-emerges from replay and the
        # HITL gate fires for work that was already done.
        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)
        # Tag with slice_id metadata for the implement-phase BRC writer's
        # per-slice partitioning (#2548). CONSENSUS_OBLIGATION_RESOLVED is
        # in BRC_HISTORY_TYPES and can fire during the implement phase
        # with slice scope (typical case: tester satisfies a coder's
        # conditional ACK on a per-slice review).
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=resolver_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_OBLIGATION_RESOLVED,
                subject=(
                    f"Obligation resolved: {reviewer_role} -> {producer_role} by {resolver_role}"
                ),
                body=note,
                phase=phase,
                metadata={
                    "reviewer_role": reviewer_role,
                    "producer_role": producer_role,
                    "resolver_role": resolver_role,
                    "commit_sha": commit_sha,
                    "note": note,
                    "version": result.get("version"),
                    "condition": result.get("condition", ""),
                    **_slice_meta,
                },
            )
        )

        return make_success_response(
            f"Obligation resolved: {reviewer_role} -> {producer_role} by {resolver_role}",
            data=result,
        )
    except ValueError as e:
        return make_error_response(str(e), 400)
    except Exception as e:
        _pkg.logger.error(
            "Failed to resolve obligation",
            pipeline_id=pipeline_id,
            resolver=resolver_role,
            reviewer=reviewer_role,
            producer=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 500)


def handle_consensus_producer_push_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle a producer push/commit that should trigger auto re-proposal.

    When a producer pushes new commits after having already proposed, this
    signal triggers an automatic re-proposal in the consensus tracker.
    Existing ACKs are invalidated and reviewers are notified to re-review.

    Request data:
        agent_role: The producer role that pushed.
        commit_sha: The new commit SHA.
        changed_files: Optional list of changed file paths for scoped
            re-evaluation.
    """
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    commit_sha = data.get("commit_sha", "")
    if not commit_sha:
        return make_error_response("Missing commit_sha")

    changed_files = data.get("changed_files")

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.handle_producer_push(agent_role, commit_sha, changed_files)

        # If auto re-propose happened, write a message and notify reviewers
        if result.get("auto_re_propose"):
            from message_store import Message, MessageType, get_message_store

            store = get_message_store()
            phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)
            # Tag with slice_id metadata for the implement-phase BRC
            # writer's per-slice partitioning (#2548). Same shape as the
            # manual re-propose path in handle_consensus_propose_signal.
            _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
            # Auto-push is a re-propose path: NACKing reviewers from the
            # prior version receive this CONSENSUS_PROPOSE (not the
            # per-reviewer CONSENSUS_RE_REVIEW — that's only for ACK'd
            # or stale-ACK reviewers whose state needs invalidation).
            # The CONSENSUS_PROPOSE body therefore needs the same
            # adversarial re-prime as the explicit re-propose path, or
            # the NACKing reviewer (the most-likely-to-find-new-issues
            # path per the #2724 post-mortem) never sees it.
            propose_body = (
                f"Producer {agent_role} pushed new commit {commit_sha}. "
                f"Existing ACKs invalidated; re-review required."
            ) + _pkg._get_re_review_priming_text(version=result.get("version"))
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role=agent_role,
                    to_role="all",
                    message_type=MessageType.CONSENSUS_PROPOSE,
                    subject=f"Auto re-proposal from {agent_role} (push)",
                    body=propose_body,
                    phase=phase,
                    metadata={
                        "auto_re_propose": True,
                        "trigger": "auto_push",
                        "commit_sha": commit_sha,
                        "version": result.get("version"),
                        "changed_files": changed_files,
                        **_slice_meta,
                    },
                )
            )

            # Notify invalidated reviewers (deduplicate in case a reviewer
            # appears in both lists)
            notified_reviewers = set(
                result.get("stale_reviewers", []) + result.get("invalidated_reviewers", [])
            )
            for reviewer in notified_reviewers:
                # Per-reviewer delta: scope mandate 2 to the commits since
                # this reviewer's own last verdict (#2887).
                delta_range = _pkg._resolve_reviewer_delta_range(
                    tracker, agent_role, reviewer, commit_sha
                )
                re_review_body = (
                    f"Producer {agent_role} has pushed new commits after "
                    f"proposing. Your previous review is invalidated. "
                    f"Please re-review and ACK/NACK the updated work."
                ) + _pkg._get_re_review_priming_text(
                    version=result.get("version"), delta_range=delta_range
                )
                store.add_message(
                    Message(
                        pipeline_id=pipeline_id,
                        from_role="orchestrator",
                        to_role=reviewer,
                        message_type=MessageType.CONSENSUS_RE_REVIEW,
                        subject=(
                            f"Re-review required: {agent_role} pushed new changes "
                            f"(v{result.get('version')})"
                        ),
                        body=re_review_body,
                        phase=phase,
                        metadata={
                            "producer_role": agent_role,
                            "version": result.get("version"),
                            "commit_sha": commit_sha,
                            **_slice_meta,
                        },
                    )
                )

            # Auto re-propose runs the same propose path that surfaces
            # newly-ready producers; emit nudges for symmetry with the
            # explicit propose/re-propose handlers.  Today no peer's
            # readiness depends on this producer's version bump (the
            # producer themselves cannot be newly ready since their own
            # ACKs were just invalidated), but skipping the call would
            # silently regress if a future guard depends on peer versions.
            _pkg._emit_ready_to_confirm_nudges(
                pipeline_id, phase, result.get("newly_ready", []), tracker, slice_id=slice_id
            )

        return make_success_response(
            f"Producer push processed for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error(
            "Failed to process consensus producer push",
            pipeline_id=pipeline_id,
            role=agent_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)
