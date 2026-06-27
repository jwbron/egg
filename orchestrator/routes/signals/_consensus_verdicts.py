"""Consensus verdict intake: propose/ack/nack/withdraw + their helpers (#3312)."""

from pathlib import Path
from typing import Any

import routes.signals as _pkg
from flask import Response
from slice_id_validation import extract_slice_id as _extract_slice_id

from ._responses import make_error_response, make_success_response


def _get_re_review_priming_text(
    version: int | None = None,
    delta_range: str | None = None,
) -> str:
    """Return the adversarial re-review priming block, or "" if unavailable.

    Centralizes the lazy import of ``_re_review_priming_block`` from
    ``routes.pipelines`` (which would otherwise be triple-duplicated
    across the propose/re-propose and auto-push handlers). On import
    failure the helper logs a warning and returns "" so the caller
    falls back to the un-primed message body — a regression that would
    silently drop the re-prime surfaces in logs instead of degrading
    the feature invisibly (see #2724 post-mortem).

    Args:
        version: Current (re-proposed) proposal version, so the block
            anchors to ``vN`` / ``v(N-1)`` rather than a hardcoded
            v1→v2 transition (#2887).
        delta_range: Per-reviewer ``<sha>..HEAD`` range scoping mandate
            2 to the commits since that reviewer's own last verdict.
            Only passed on the per-reviewer ``CONSENSUS_RE_REVIEW``
            path; omitted on the broadcast ``CONSENSUS_PROPOSE`` body.
    """
    try:
        from routes.pipelines import _re_review_priming_block
    except ImportError:
        try:
            from .pipelines import _re_review_priming_block  # type: ignore[no-redef]
        except ImportError:
            _pkg.logger.warning(
                "Failed to import _re_review_priming_block from routes.pipelines; "
                "re-review priming will not be appended to message bodies"
            )
            return ""
    return _re_review_priming_block(version=version, delta_range=delta_range)


def _resolve_reviewer_delta_range(
    tracker: Any,
    producer: str,
    reviewer: str,
    head_sha: str,
) -> str | None:
    """Return a ``<last_sha>..<head_sha>`` range for a reviewer's re-review.

    Scopes the reviewer's mandate-2 audit to exactly the commits landed
    since *their own* last verdict (#2887): the reviewer's last-verdicted
    proposal version (``entry.version``) resolves, via the tracker's
    per-version commit history, to the commit they actually reviewed, and
    the range runs from there to the new proposal commit.

    Returns ``None`` when the prior-reviewed commit can't be resolved — no
    prior verdict, a version-0 pre-proposal ACK, missing commit history,
    or an empty/unchanged head — so the caller falls back to the priming
    block's generic, reviewer-self-tracked range from REVIEWER-SYNC.md.
    """
    if not head_sha:
        return None
    # Both ``tracker.matrix.get_entry`` and ``tracker.get_commit_sha_for_version``
    # are real on ``PeerConsensusTracker``, but several call sites pass a
    # ``MagicMock`` tracker (the pre-#2887 propagation tests). We catch
    # ``AttributeError`` across both reads so a stub missing either surface
    # degrades to the REVIEWER-SYNC fallback rather than 500-ing — the
    # asymmetry of catching one but not the other was a foot-gun flagged
    # in PR review.
    try:
        entry = tracker.matrix.get_entry(reviewer, producer)
        if entry is None or not entry.version:
            return None
        last_sha = tracker.get_commit_sha_for_version(producer, entry.version)
    except AttributeError:
        return None
    if not last_sha or last_sha == head_sha:
        return None
    return f"{last_sha}..{head_sha}"


def _resolve_pipeline_phase(pipeline_id: str, repo_path: Path) -> str:
    """Resolve the current phase for a pipeline, with graceful fallback.

    Loads the pipeline from the state store and returns the current phase
    name as a string.  Falls back to ``"implement"`` (the most common BRC
    phase) if loading fails for any reason — this keeps Message creation
    from silently dropping the phase field.
    """
    try:
        store = _pkg.get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        return pipeline.current_phase.value
    except Exception:
        return "implement"


def _emit_ready_to_confirm_nudges(
    pipeline_id: str,
    phase: str,
    newly_ready: list[dict[str, Any]],
    tracker: Any = None,
    slice_id: str | None = None,
) -> None:
    """Emit a STATUS to each producer that newly became ready to confirm.

    The tracker returns producers whose ``check_confirm_guard`` now passes —
    not just those that are fully ACKed by their critical reviewers.  This
    closes the gap where a documenter (advisory-only) was nudged the moment
    its single ADVISORY ACK arrived, even though the global zero-proposal
    guard still rejected confirm (#2078).

    If ``add_message`` raises for a given producer and a ``tracker`` is
    supplied, the per-version memo entry is rolled back so the producer
    can be re-nudged on the next state change.  Other producers in the
    batch are still attempted.

    ``slice_id`` is forwarded into the STATUS metadata so the
    implement-phase BRC writer (#2548) routes the nudge into the
    producer's per-slice transcript.  Pipeline-level (non-slice) callers
    leave it as ``None``.
    """
    if not newly_ready:
        return
    from message_store import Message, MessageType, get_message_store

    store = get_message_store()
    _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
    for entry in newly_ready:
        producer = entry["role"]
        version = entry["version"]
        try:
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="orchestrator",
                    to_role=producer,
                    message_type=MessageType.STATUS,
                    subject="Ready to confirm — all confirm preconditions satisfied",
                    body=(
                        f"Your proposal (version {version}) is ready to confirm — "
                        f"all blocking reviews are clear and global confirm "
                        f"preconditions are met. Run "
                        f"`egg-orch consensus confirmed` to confirm."
                    ),
                    phase=phase,
                    metadata={"ready_to_confirm": True, "version": version, **_slice_meta},
                )
            )
        except Exception as exc:
            if tracker is not None:
                tracker.release_nudge(producer, version)
            _pkg.logger.error(
                "Failed to emit ready-to-confirm nudge",
                pipeline_id=pipeline_id,
                role=producer,
                version=version,
                error=str(exc),
            )


def _stale_version_rejection(
    tracker: Any,
    producer_role: str,
    err_message: str,
    reviewer_role: str,
    verdict: str,
) -> tuple[Response, int] | None:
    """Build a structured 409 for stale-version ACK / NACK rejections (#2142).

    Returns the (response, status) tuple when ``err_message`` came from the
    version-match guard, or ``None`` to let the caller raise normally.  The
    rejection inlines the producer's current proposal snapshot so the
    reviewer can re-review the latest version without a separate fetch.
    """
    if "version mismatch" not in err_message.lower():
        return None
    snapshot = tracker.get_current_proposal_snapshot(producer_role)
    _pkg.logger.warning(
        f"{verdict} rejected: stale proposal version",
        reviewer=reviewer_role,
        producer=producer_role,
        current_version=snapshot.get("version"),
    )
    return make_error_response(
        err_message,
        status_code=409,
        details={
            "status": "stale_version",
            "reviewer": reviewer_role,
            "verdict": verdict,
            "current_proposal": snapshot,
        },
    )


def _contract_completeness_rejection(
    *,
    pipeline_id: str,
    repo_path: Path,
    slice_id: str | None,
    check: str,
    enforcer_role: str | None = None,
    producer_role: str | None = None,
    payload: dict[str, Any] | None = None,
    current_phase: str | None = None,
) -> tuple[Response, int] | None:
    """Contract-task completeness gate for consensus signals (#3114).

    Returns a structured rejection response, or ``None`` when the signal
    may proceed. Three checks share the same contract read:

    * ``check="ack"`` — an enforcer reviewer (see
      ``CONTRACT_ENFORCER_ROLE_NAMES``) may not ACK ``producer_role``
      while that producer owns non-``complete`` task rows in the active
      slice; when the rows are complete, the ACK must carry an
      ``attestation.tasks_verified`` list covering every owned row.
    * ``check="confirm"`` — an enforcer may not CONFIRM while *any* row
      in the active slice is incomplete (covers role-less rows and
      producers whose no-op proposal made ``is_fully_acked`` vacuous).
    * ``check="noop_propose"`` — ``producer_role`` may not submit a
      ``no_changes_needed`` proposal while owning incomplete rows in the
      active slice (a no-op needs no review, so it would bypass the
      per-producer ACK gate entirely).

    Scope: implement phase only. Plan/refine consensus runs against
    contracts whose rows are expected to be pending, and the apply phase
    (#1557) tracks lifecycle in ``jira_action_status``.

    Failure posture: fail-OPEN on orchestrator-side read failures (state
    store, worktree, contract load, unknown slice id) — an
    infrastructure glitch must not deadlock every consensus in flight;
    the gate logs and skips, and the existing reviewers remain the
    backstop (#3081 posture). The deliberate exception is the no-op
    phase guard upstream of the ``noop_propose`` call, which stays
    fail-closed for the reasons documented there. The
    ``EGG_CONTRACT_ACK_GATE`` env var is the operator kill switch.
    """
    try:
        import contract_completeness as cc
    except ImportError:
        from .. import contract_completeness as cc  # type: ignore[no-redef]
    from egg_contracts.agent_roles import CONTRACT_ENFORCER_ROLE_NAMES

    if not cc.gate_enabled():
        return None
    if check in ("ack", "confirm") and enforcer_role not in CONTRACT_ENFORCER_ROLE_NAMES:
        return None

    issue_number: int | None = None
    if current_phase is None:
        try:
            pipeline_state = _pkg.get_state_store(repo_path).load_pipeline(pipeline_id)
            phase_attr = getattr(pipeline_state, "current_phase", None)
            current_phase = phase_attr.value if phase_attr is not None else None
            issue_number = getattr(pipeline_state, "issue_number", None)
        except Exception as exc:
            _pkg.logger.warning(
                "Contract completeness gate skipped: pipeline state unreadable",
                pipeline_id=pipeline_id,
                check=check,
                error=str(exc),
            )
            return None
    if current_phase != "implement":
        return None

    try:
        worktree = _pkg.resolve_worktree_path(pipeline_id, repo_path)
    except Exception as exc:
        _pkg.logger.warning(
            "Contract completeness gate skipped: worktree unresolvable",
            pipeline_id=pipeline_id,
            check=check,
            error=str(exc),
        )
        return None

    identifiers: list[int | str] = [pipeline_id]
    if issue_number:
        identifiers.append(issue_number)
    contract = cc.load_live_contract(worktree, identifiers)
    if contract is None:
        _pkg.logger.warning(
            "Contract completeness gate skipped: contract not loadable",
            pipeline_id=pipeline_id,
            check=check,
            slice_id=slice_id,
        )
        return None

    row_role = None if check == "confirm" else producer_role
    rows = cc.incomplete_tasks(contract, slice_id, role=row_role)
    if rows is None:
        _pkg.logger.warning(
            "Contract completeness gate skipped: slice not found in contract",
            pipeline_id=pipeline_id,
            check=check,
            slice_id=slice_id,
        )
        return None

    scope = f"slice {slice_id}" if slice_id else "the contract"
    if rows:
        summary = cc.format_incomplete_rows(rows)
        if check == "ack":
            return make_error_response(
                f"ACK rejected: {producer_role} owns {len(rows)} incomplete "
                f"contract task(s) in {scope}: {summary}. The contract is not "
                f"satisfied until every owned row is status=complete. NACK "
                f"{producer_role} citing these task ids so it delivers the "
                f"work (or marks finished work complete via mcp__task__complete).",
                status_code=409,
                details={
                    "status": "contract_incomplete",
                    "producer": producer_role,
                    "slice_id": slice_id,
                    "incomplete_tasks": rows,
                },
            )
        if check == "confirm":
            return make_error_response(
                f"CONFIRM rejected: {len(rows)} contract task(s) in {scope} "
                f"are incomplete: {summary}. Consensus cannot close over an "
                f"undelivered contract. NACK the owning producer(s), or — if "
                f"a row genuinely cannot be delivered in this slice — "
                f"escalate for a human decision instead of confirming.",
                status_code=409,
                details={
                    "status": "contract_incomplete",
                    "slice_id": slice_id,
                    "incomplete_tasks": rows,
                },
            )
        # noop_propose
        return make_error_response(
            f"No-op propose rejected: {producer_role} owns {len(rows)} "
            f"incomplete contract task(s) in {scope}: {summary}. A "
            f"no_changes_needed proposal asserts you have no work in this "
            f"slice, but these rows are assigned to you. Deliver them and "
            f"mark them complete (mcp__task__complete), or escalate for a "
            f"human decision if they cannot be done in this slice.",
            status_code=400,
            details={
                "status": "contract_incomplete",
                "producer": producer_role,
                "slice_id": slice_id,
                "incomplete_tasks": rows,
            },
        )

    if check == "ack":
        # Defensive: a missing/empty producer_role at this point would
        # silently no-op the attestation check (``task_ids_for_role``
        # filters on ``task.role == ""`` and matches nothing). Signal-handler
        # validation prevents this upstream; log and skip so an upstream
        # regression is visible rather than degrading the gate.
        if not producer_role:
            _pkg.logger.warning(
                "Contract completeness gate: empty producer_role on ACK check; "
                "attestation check skipped",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
            return None
        owned = cc.task_ids_for_role(contract, slice_id, producer_role)
        if owned:
            # ``attestation`` is structured input; if a caller (operator
            # probe, future internal signal, dev/test traffic) sends a
            # truthy non-dict, ``.get(...)`` would raise AttributeError →
            # 500. Coerce non-dicts to {} and non-lists to [] so the gate
            # surfaces a clean ``attestation_required`` instead.
            raw_attestation = (payload or {}).get("attestation")
            attestation = raw_attestation if isinstance(raw_attestation, dict) else {}
            raw_verified = attestation.get("tasks_verified")
            if not isinstance(raw_verified, list):
                raw_verified = []
            verified = {v for v in raw_verified if isinstance(v, str) and v}
            if not verified:
                return make_error_response(
                    f"ACK rejected: contract-enforcer ACKs must carry an "
                    f"attestation with tasks_verified listing every task id "
                    f"you verified for {producer_role} in {scope}: "
                    f"{', '.join(sorted(owned))}. Re-send the ACK with "
                    f'attestation={{"tasks_verified": [...]}}.',
                    status_code=409,
                    details={
                        "status": "attestation_required",
                        "producer": producer_role,
                        "slice_id": slice_id,
                        "expected_tasks": sorted(owned),
                    },
                )
            missing = owned - verified
            known = cc.all_task_ids(contract, slice_id) or set()
            unknown = verified - known
            if missing or unknown:
                return make_error_response(
                    f"ACK rejected: attestation.tasks_verified does not match "
                    f"{producer_role}'s contract rows in {scope}. "
                    f"Missing: {sorted(missing) or 'none'}; unknown ids: "
                    f"{sorted(unknown) or 'none'}. Verify each owned row "
                    f"against the implementation and re-send.",
                    status_code=409,
                    details={
                        "status": "attestation_mismatch",
                        "producer": producer_role,
                        "slice_id": slice_id,
                        "missing_tasks": sorted(missing),
                        "unknown_tasks": sorted(unknown),
                    },
                )
    return None


def handle_consensus_propose_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_PROPOSE signal from a producer agent."""
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    payload = data.get("payload", {})
    if not payload:
        return make_error_response("Missing payload")

    # Validate proposal summary content (#1716)
    summary_error = _pkg._validate_brc_content(payload.get("summary", ""), "Proposal summary")
    if summary_error:
        return make_error_response(summary_error, 400)

    # Generic no-op propose (#3027): producer has no work in this slice.
    # A no-op carries no commit_sha and bypasses the producer pre-flight
    # checks that assume real work (commit-on-branch, tester check coverage)
    # — but it is NOT valid in refine/plan, where the producer's entire job
    # is to author the analysis/plan draft (rejected below).
    no_changes = bool(payload.get("no_changes_needed"))

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

    # Verify commit SHA exists on the expected branch before accepting
    # the proposal (#1473).  Reuses _verify_commit_on_branch() from the
    # completion handler — graceful degradation on network errors (None).
    #
    # ``pipeline_state`` and ``worktree_path`` are loaded once here and
    # threaded into the producer validator below
    # (``_validate_producer_artifacts``) so its dependency on this block is
    # explicit and the state-store + worktree lookups aren't duplicated.
    commit_sha = payload.get("commit_sha", "")
    pipeline_state = None
    worktree_path = None
    # Tri-state mirroring ``_verify_commit_on_branch``: True (commit on
    # branch), False (commit NOT on branch — 409 short-circuit below), or
    # None (verification inconclusive — fetch or branch-contains errored).
    # Threaded into the producer validators so that a glitch in our fetch
    # doesn't get blamed on the producer as a missing draft (a non-zero
    # ``git show`` after a failed fetch could be "commit not in local object
    # cache" rather than "path absent at commit").
    branch_verified: bool | None = True
    if commit_sha:
        try:
            store_mod = _pkg.get_state_store(repo_path)
            pipeline_state = store_mod.load_pipeline(pipeline_id)
            if pipeline_state.branch:
                worktree_path = _pkg.resolve_worktree_path(pipeline_id, repo_path)
                branch_verified = _pkg._verify_commit_on_branch(
                    commit_sha,
                    pipeline_state.branch,
                    worktree_path,
                    pipeline_id,
                    pipeline_state=pipeline_state,
                )
                if branch_verified is False:
                    return make_error_response(
                        f"Proposal rejected: commit {commit_sha} not found on "
                        f"expected branch {pipeline_state.branch}. Push your "
                        f"work before proposing consensus.",
                        status_code=409,
                        details={
                            "commit_sha": commit_sha,
                            "expected_branch": pipeline_state.branch,
                            "pipeline_id": pipeline_id,
                        },
                    )
        except Exception as e:
            _pkg.logger.warning(
                "Could not verify commit on branch (non-blocking)",
                pipeline_id=pipeline_id,
                commit_sha=commit_sha,
                error=str(e),
            )
            # Verification raised — same posture as ``_verify_commit_on_branch``
            # returning None: skip the downstream presence check rather than
            # blame the producer for an orchestrator-side glitch.
            branch_verified = None

    try:
        # A no-op propose (#3027) is only meaningful in the implement phase,
        # where a producer (e.g. documenter) may have no work in a given
        # slice. In every other phase the producer's job IS to author the
        # phase's deterministic input (analysis draft in refine; plan draft,
        # architecture artifact, or risk register in plan), so a no-op there
        # would silently skip the input the gate reads. Reject on phase, not
        # on a hand-maintained role list — a future plan/refine producer added
        # to the review graph picks up the guard automatically and the prose
        # in ``_build_brc_preamble`` stays in lockstep (it conditions the
        # no-op invitation on ``phase == "implement"`` for the same reason).
        if no_changes:
            # Resolve phase fail-CLOSED for the no-op gate.
            # ``_resolve_pipeline_phase`` returns ``"implement"`` on any
            # state-load exception — a sensible default for stamping
            # ``Message.phase`` (drop a field rather than the message), but
            # unsafe here: a transient FS error or partial state-store
            # corruption during a plan-phase no-op would silently pass the
            # guard and let an architect / risk_analyst skip authoring.
            # Refuse with 503 instead and let the producer retry once state
            # is readable.
            current_phase: str | None = None
            phase_resolution_error: Exception | None = None
            if pipeline_state is not None:
                # The commit-on-branch block already loaded state for a
                # propose that carries both ``commit_sha`` and
                # ``no_changes_needed=true`` (Pydantic permits it;
                # ``validate_commit_sha_present`` skips on no-op).  Reuse
                # the cached state, but still fail closed if the loaded
                # ``current_phase`` is None — same posture as the
                # explicit-load branch below: never fall through to a
                # default-implement assumption when phase is unreadable.
                phase_attr = getattr(pipeline_state, "current_phase", None)
                if phase_attr is not None:
                    current_phase = phase_attr.value
            else:
                try:
                    store_for_phase = _pkg.get_state_store(repo_path)
                    loaded = store_for_phase.load_pipeline(pipeline_id)
                    phase_attr = getattr(loaded, "current_phase", None)
                    if phase_attr is not None:
                        current_phase = phase_attr.value
                except Exception as exc:
                    phase_resolution_error = exc
            if current_phase is None:
                # Both branches converge here: pipeline state is loaded but
                # ``current_phase`` is None, or state-load itself raised.
                # Either way the no-op guard cannot prove ``implement`` and
                # must refuse rather than trust a default.  Log the raw
                # error for ops; the response body deliberately omits it so
                # file paths / DB connection strings / traceback fragments
                # from arbitrary state-store backends don't leak through
                # this internal API.
                _pkg.logger.error(
                    "No-op propose rejected: pipeline phase resolution failed",
                    pipeline_id=pipeline_id,
                    role=agent_role,
                    error=str(phase_resolution_error)
                    if phase_resolution_error
                    else "current_phase is None",
                )
                return make_error_response(
                    "Cannot resolve pipeline phase for no-op propose. "
                    "The phase guard fails closed rather than trust the "
                    "default-implement fallback. Retry after pipeline "
                    "state is readable.",
                    503,
                )
            if current_phase != "implement":
                return make_error_response(
                    f"{agent_role} cannot submit a no-op propose in phase "
                    f"'{current_phase}': the producer's draft is required for "
                    f"this phase. Author and commit the draft, then propose.",
                    400,
                )
            # Contract-task completeness gate (#3114): a no-op proposal
            # needs no review (``is_fully_acked`` is vacuously true), so a
            # producer that still owns incomplete contract rows in this
            # slice would bypass the enforcer's per-producer ACK gate by
            # no-op proposing. Reject it here; the producer either delivers
            # the rows or escalates for a human decision.
            gate_rejection = _pkg._contract_completeness_rejection(
                pipeline_id=pipeline_id,
                repo_path=repo_path,
                slice_id=slice_id,
                check="noop_propose",
                producer_role=agent_role,
                current_phase=current_phase,
            )
            if gate_rejection is not None:
                return gate_rejection
        # Validate tester proposals cover all configured repo checks (#1459).
        # Must run BEFORE handle_propose to avoid mutating tracker state on
        # rejected proposals. Skipped for a no-op — the tester ran nothing.
        if agent_role == "tester" and not no_changes:
            _pkg._validate_tester_check_coverage(pipeline_id, payload, repo_path)
        # Spec-driven producer-artifact presence validation (#3077 slice-3).
        # Generalises the per-role refine / plan dispatch this block used to
        # carry: ``_validate_producer_artifacts`` resolves
        # ``specs_for(phase, agent_role)`` against the artifact registry and
        # runs a single ``git show`` presence check per registered artifact,
        # then layers the plan-draft parseability (#3026) and role↔files
        # alignment (#2527/#2528) extensions on the ``plan-draft`` row only.
        # Roles with no registered artifact (every reviewer, ``coder``,
        # ``tester``, ``documenter``) fall through cleanly via an empty
        # ``specs_for`` tuple. Runs BEFORE handle_propose so the tracker
        # isn't mutated on a rejected proposal, and is skipped for
        # ``no_changes_needed`` proposes (the producer asserted no work in
        # this slice; the no-op phase gate above already rejected the case
        # where refine/plan attempt this).
        if not no_changes:
            _pkg._validate_producer_artifacts(
                pipeline_id,
                payload,
                repo_path,
                agent_role=agent_role,
                pipeline_state=pipeline_state,
                worktree_path=worktree_path,
                branch_verified=branch_verified,
            )

        # Check if this is a re-proposal
        changed_artifacts = data.get("changed_artifacts")
        if changed_artifacts:
            result = tracker.handle_re_propose(agent_role, payload, changed_artifacts)
        else:
            result = tracker.handle_propose(agent_role, payload)

        # Open-NACK barrier rejection (#2142): re_propose returned a
        # structured rejection because NACKs against the current version
        # had not yet been delivered to the producer.  Surface every NACK
        # inline (full reason text + artifact refs) so the producer can
        # aggregate them into one re-propose without a separate fetch.
        if isinstance(result, dict) and result.get("status") == "open_nacks_blocked":
            _pkg.logger.warning(
                "re_propose blocked by open NACKs",
                pipeline_id=pipeline_id,
                role=agent_role,
                version=result.get("current_version"),
                nacking_reviewers=result.get("nacking_reviewers"),
            )
            return make_error_response(
                result.get("message", "Re-propose blocked: unresolved NACKs"),
                status_code=409,
                details=result,
            )

        # Write consensus message to message bus.
        # Tag every CONSENSUS_* message with slice_id metadata when the
        # producer is slice-scoped so the implement-phase BRC writer
        # (#2548) can partition messages into per-slice transcript
        # files. Pipeline-level (non-slice) callers leave the metadata
        # off entirely — matches the legacy non-slice shape and signals
        # the writer to fall back to its aggregate filename.
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)

        # When this is a re-propose (changed_artifacts set), append the
        # adversarial re-prime to the CONSENSUS_PROPOSE body. Reviewers
        # who NACK'd the prior version receive CONSENSUS_PROPOSE (not
        # CONSENSUS_RE_REVIEW — that's only for ACK'd-prior reviewers
        # whose state needs invalidation), so without this both paths
        # don't reach every reviewer. See #2724 post-mortem.
        propose_body = payload.get("summary", "")
        if changed_artifacts:
            # Broadcast body (to_role="all") — one text shared across
            # reviewers who may sit at different last-reviewed versions,
            # so no per-reviewer delta_range; the block points each
            # reviewer at the REVIEWER-SYNC self-tracked range (#2887).
            propose_body = propose_body + _pkg._get_re_review_priming_text(
                version=result.get("version")
            )

        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject=f"Proposal from {agent_role}",
                body=propose_body,
                phase=phase,
                metadata={
                    "payload": payload,
                    "version": result.get("version"),
                    "commit_sha": commit_sha,
                    **_slice_meta,
                },
            )
        )

        # Notify stale reviewers that they need to re-review.  Includes
        # both reviewers who confirmed on a prior version and reviewers
        # whose pre-proposal ACKs (version 0) were invalidated.
        for stale_reviewer in result.get("stale_reviewers", []):
            re_review_body = (
                f"Producer {agent_role} has submitted a new proposal "
                f"(version {result.get('version')}) after withdrawal. "
                f"Your previous confirmation was on an earlier version. "
                f"Please re-review and ACK/NACK the new proposal."
            )
            # Per-reviewer delta: scope mandate 2 to the commits since
            # this reviewer's own last verdict, resolved authoritatively
            # from their last-reviewed version's commit (#2887).
            delta_range = _pkg._resolve_reviewer_delta_range(
                tracker, agent_role, stale_reviewer, commit_sha
            )
            re_review_body = re_review_body + _pkg._get_re_review_priming_text(
                version=result.get("version"), delta_range=delta_range
            )

            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="orchestrator",
                    to_role=stale_reviewer,
                    message_type=MessageType.CONSENSUS_RE_REVIEW,
                    subject=f"Re-review required: {agent_role} submitted new proposal v{result.get('version')}",
                    body=re_review_body,
                    phase=phase,
                    metadata={
                        "producer_role": agent_role,
                        "version": result.get("version"),
                        **_slice_meta,
                    },
                )
            )

        # A new proposal can unblock the global zero-proposal guard for
        # producers that were previously fully ACKed but unable to confirm.
        _pkg._emit_ready_to_confirm_nudges(
            pipeline_id, phase, result.get("newly_ready", []), tracker, slice_id=slice_id
        )

        return make_success_response(
            f"Proposal recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error(
            "Failed to process consensus propose",
            pipeline_id=pipeline_id,
            role=agent_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_ack_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_ACK signal from a reviewer agent."""
    reviewer_role = data.get("agent_role")
    producer_role = data.get("producer_role")
    if not reviewer_role:
        return make_error_response("Missing agent_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    payload = data.get("payload", {})

    # Forward ack_version from signal data into the payload so the
    # version-match guard can detect stale ACKs.
    if "ack_version" in data and "ack_version" not in payload:
        payload["ack_version"] = data["ack_version"]

    # Require ack_version >= 1 at the route boundary so the HTTP surface
    # matches the MCP handler's contract (`_require_version_int` in
    # sandbox/egg_agent_tools/handlers/brc.py). Without this, a client that
    # omits ack_version bypasses the version-match guard in
    # check_ack_guard (#2674).
    version_error = _pkg._require_route_version(payload, "ack_version")
    if version_error is not None:
        return version_error

    # Validate ACK reason content (#1716)
    reason_error = _pkg._validate_brc_content(payload.get("reason", ""), "ACK reason")
    if reason_error:
        return make_error_response(reason_error, 400)

    # Validate pre-merge condition content when present (#2005). An empty
    # or whitespace-only condition is a plain ACK, not a conditional ACK,
    # and must pass through unaffected.
    pre_merge_condition = (payload.get("pre_merge_condition") or "").strip()
    if pre_merge_condition:
        condition_error = _pkg._validate_brc_content(pre_merge_condition, "Pre-merge condition")
        if condition_error:
            return make_error_response(condition_error, 400)

    # A resolution SHA without an obligation has nothing to resolve (#2336);
    # reject at the boundary so downstream code can assume the invariant.
    pre_merge_condition_resolved_in_diff = (
        payload.get("pre_merge_condition_resolved_in_diff") or ""
    ).strip()
    if pre_merge_condition_resolved_in_diff and not pre_merge_condition:
        return make_error_response(
            "pre_merge_condition_resolved_in_diff requires a non-empty "
            "pre_merge_condition; a resolution SHA has nothing to resolve "
            "on a plain ACK",
            400,
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

    # Contract-task completeness gate (#3114): an enforcer reviewer may
    # not ACK a producer whose contract rows in this slice are incomplete,
    # and a passing ACK must attest the rows it verified. Runs BEFORE
    # handle_ack so a rejected ACK never lands in the approval matrix.
    gate_rejection = _pkg._contract_completeness_rejection(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        slice_id=slice_id,
        check="ack",
        enforcer_role=reviewer_role,
        producer_role=producer_role,
        payload=payload,
    )
    if gate_rejection is not None:
        return gate_rejection

    try:
        try:
            result = tracker.handle_ack(reviewer_role, producer_role, payload)
        except ValueError as ack_err:
            stale_response = _pkg._stale_version_rejection(
                tracker, producer_role, str(ack_err), reviewer_role, "ACK"
            )
            if stale_response is not None:
                return stale_response
            raise

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _pkg._resolve_pipeline_phase(pipeline_id, repo_path)
        # Tag with slice_id metadata for the implement-phase BRC writer's
        # per-slice partitioning (#2548). Pipeline-level callers leave it
        # off, matching the legacy non-slice shape.
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=reviewer_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_ACK,
                subject=f"ACK from {reviewer_role} for {producer_role}",
                body=payload.get("reason", ""),
                phase=phase,
                metadata={
                    "payload": payload,
                    "version": result.get("version"),
                    **_slice_meta,
                },
            )
        )

        # Nudge any producer that the tracker says is now ready to confirm —
        # i.e. ``check_confirm_guard`` actually passes, not just the
        # critical-reviewer ACK predicate.  Replaces the prior ``fully_acked``
        # gate which fired before global guards (e.g. zero-proposal) cleared
        # and could mislead an advisory-only producer like documenter (#2078).
        _pkg._emit_ready_to_confirm_nudges(
            pipeline_id, phase, result.get("newly_ready", []), tracker, slice_id=slice_id
        )

        return make_success_response(
            f"ACK recorded: {reviewer_role} -> {producer_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error(
            "Failed to process consensus ACK",
            pipeline_id=pipeline_id,
            reviewer=reviewer_role,
            producer=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_nack_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_NACK signal from a reviewer agent."""
    reviewer_role = data.get("agent_role")
    producer_role = data.get("producer_role")
    if not reviewer_role:
        return make_error_response("Missing agent_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    payload = data.get("payload", {})

    # Forward nack_version from signal data into the payload so the
    # version-match guard can detect stale NACKs (#2142).
    if "nack_version" in data and "nack_version" not in payload:
        payload["nack_version"] = data["nack_version"]

    # Require nack_version >= 1 at the route boundary so the HTTP surface
    # matches the MCP handler's contract (`_require_version_int` in
    # sandbox/egg_agent_tools/handlers/brc.py). Without this, a client that
    # omits nack_version bypasses the version-match guard in
    # check_nack_guard (#2674).
    version_error = _pkg._require_route_version(payload, "nack_version")
    if version_error is not None:
        return version_error

    # Validate NACK reason content (#1716)
    reason_error = _pkg._validate_brc_content(payload.get("reason", ""), "NACK reason")
    if reason_error:
        return make_error_response(reason_error, 400)

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
        try:
            result = tracker.handle_nack(reviewer_role, producer_role, payload)
        except ValueError as nack_err:
            stale_response = _pkg._stale_version_rejection(
                tracker, producer_role, str(nack_err), reviewer_role, "NACK"
            )
            if stale_response is not None:
                return stale_response
            raise

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        # Tag with slice_id metadata for the implement-phase BRC writer's
        # per-slice partitioning (#2548).
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=reviewer_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_NACK,
                subject=f"NACK from {reviewer_role} for {producer_role}",
                body=payload.get("reason", ""),
                phase=_pkg._resolve_pipeline_phase(pipeline_id, repo_path),
                metadata={
                    "payload": payload,
                    "reason": result.get("reason"),
                    "revision_count": result.get("revision_count"),
                    **_slice_meta,
                },
            )
        )

        return make_success_response(
            f"NACK recorded: {reviewer_role} -> {producer_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error("Failed to process consensus NACK", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_withdraw_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_WITHDRAW signal from a producer agent."""
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    reason = data.get("reason", "")

    # Validate withdrawal reason content (#1716)
    reason_error = _pkg._validate_brc_content(reason, "Withdrawal reason")
    if reason_error:
        return make_error_response(reason_error, 400)

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
        result = tracker.handle_withdraw(agent_role, reason)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        # Tag with slice_id metadata for the implement-phase BRC writer's
        # per-slice partitioning (#2548).
        _slice_meta: dict[str, Any] = {"slice_id": slice_id} if slice_id is not None else {}
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_WITHDRAW,
                subject=f"Withdrawal by {agent_role}",
                body=reason,
                phase=_pkg._resolve_pipeline_phase(pipeline_id, repo_path),
                metadata=_slice_meta,
            )
        )

        return make_success_response(
            f"Withdrawal recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        _pkg.logger.error(
            "Failed to process consensus withdraw", pipeline_id=pipeline_id, error=str(e)
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)
