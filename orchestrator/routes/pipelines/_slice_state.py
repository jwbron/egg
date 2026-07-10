"""slice-DAG state helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401


def _resolve_pipeline_worktree_path(pipeline: _pkg.Pipeline, fallback: _pkg.Path) -> _pkg.Path:
    """Resolve the on-disk worktree path for *pipeline*.

    Prefers ``WORKTREE_BASE_DIR / pipeline.id / <repo_short>`` when it
    exists (the same layout _run_pipeline materialises at spawn time;
    see pipelines.py spawn block).  Falls back to *fallback* — typically
    the state store's ``repo_path`` — when no worktree is materialised.
    """
    repo_short = pipeline.repo.split("/")[-1] if pipeline.repo else None
    if repo_short:
        candidate = _pkg.WORKTREE_BASE_DIR / pipeline.id / repo_short
        if candidate.exists():
            return candidate
    pipeline_wt_dir = _pkg.WORKTREE_BASE_DIR / pipeline.id
    if pipeline_wt_dir.exists():
        # sorted() for deterministic selection when multiple subdirs exist
        for sub in sorted(pipeline_wt_dir.iterdir()):
            if sub.is_dir() and (sub / ".git").exists():
                return sub
    return fallback


def _resolve_slice_gate_repo(slice_obj, pipeline: _pkg.Pipeline) -> str | None:
    """The repo every implement-phase gate for *slice_obj* is scoped to (#3393).

    Single source of truth for slice → gate-repo resolution (task-6-1): the
    test gate, the reviewer diff base, the per-repo check/lint commands, and
    the slice agent's cwd all key off this one accessor. It is exactly
    :func:`models.resolve_slice_repo` — the slice's own ``repo`` when set,
    else the pipeline's primary repo (so a repoless slice, or any slice in an
    N=1 pipeline, scopes to the single/primary repo). Returns ``None`` only
    for a genuinely repoless pipeline (test scaffolds with no repo at all).
    """
    try:
        from models import resolve_slice_repo  # type: ignore[no-redef]
    except ImportError:
        from ..models import resolve_slice_repo  # type: ignore[no-redef]
    return resolve_slice_repo(slice_obj, pipeline)


def _resolve_slice_worktree_path(
    pipeline: _pkg.Pipeline, slice_repo: str | None, fallback: _pkg.Path
) -> _pkg.Path:
    """Resolve the on-disk worktree path for a slice's repo (#3393 task-6-1).

    A multi-repo pipeline materialises one worktree per participating repo
    under ``WORKTREE_BASE_DIR / pipeline.id / <repo_short>`` — the same
    owner/repo-keyed layout as :func:`_resolve_pipeline_worktree_path`, one
    directory per repo. Given a slice's resolved repo (``owner/name``), this
    returns that repo's worktree when it exists on disk, else *fallback*
    (the pipeline-primary worktree). For an N=1 pipeline the slice's repo IS
    the primary, so ``slice_repo`` matches ``pipeline.repo`` and the answer
    is byte-identical to the pipeline-primary worktree — callers therefore
    only reach here for a genuine secondary-repo slice.
    """
    repo_short = slice_repo.split("/")[-1] if slice_repo else None
    if repo_short:
        candidate = _pkg.WORKTREE_BASE_DIR / pipeline.id / repo_short
        if candidate.exists():
            return candidate
    return fallback


def _is_slice_dag_mode(contract) -> bool:
    """Return True when the contract represents a multi-slice DAG (#2777, cq-10).

    Dedupes the bare ``len(contract.slices) > 1`` recompute that
    appears at the ``_run_implement_phase_slices`` entry and inside the
    run loop's per-slice handling. A single helper means future changes
    to "what counts as DAG mode" — e.g. treating a single slice with
    explicit dependencies as DAG — only need to land in one place.
    The third site under the deleted ``_should_skip_pr_phase_auto_pr``
    is gone since slice-2 of #2777 removed the PR phase.

    Returns False for ``None`` or a contract without a populated
    ``slices`` list (monolithic / pre-populate phase pipelines).
    """
    if contract is None:
        return False
    slices = getattr(contract, "slices", None) or []
    return len(slices) > 1


def _slice_linear_parent_id(
    slice_record,
    *,
    issue_branch: str,
    known_ids: set[str],
) -> str | None:
    """Return the slice id this slice's branch was actually forked from.

    Prefers ``parent_branch_at_creation`` — under #3541 root
    linearization a root slice's branch can fork from a chain outside
    its declared ``dependencies`` — and maps the recorded branch name
    (``{issue_branch}/{slice_id}``) back to a slice id. Falls back to
    ``dependencies[0]`` (the declared DAG parent) when nothing usable
    is recorded. Returns ``None`` for a true root forked from the
    pipeline work branch.

    ``known_ids`` bounds the branch-name mapping: a recorded parent
    that does not correspond to a known slice (e.g. the pipeline work
    branch itself, or a foreign branch) yields the declared-dependency
    fallback rather than a phantom id.
    """
    recorded = getattr(slice_record, "parent_branch_at_creation", None) or ""
    prefix = f"{issue_branch}/"
    if recorded.startswith(prefix):
        candidate = recorded[len(prefix) :]
        if candidate in known_ids:
            return candidate
        if recorded == f"{issue_branch}/work":
            # Forked from the pipeline work branch — a true root.
            return None
    deps = getattr(slice_record, "dependencies", None) or []
    return deps[0] if deps else None


def _latest_completed_chain_tip(
    slices,
    *,
    slice_id: str,
    issue_branch: str,
    pipeline_id: str,
    branch_exists: _pkg.Callable[[str], bool] | None = None,
) -> str | None:
    """Return the integration branch of the deepest COMPLETE slice chain (#3541).

    Root-slice linearization support: during a run the pipeline work
    branch only ever advances with bookkeeping commits (slice PRs are
    human-merged after the pipeline finishes), so a root slice forked
    from ``work`` after a sibling chain completed would exclude that
    chain's reviewed code from itself and every descendant — the #3541
    orphaning. The base resolver calls this to fork new roots from the
    latest completed chain tip instead.

    Selection: among slices whose contract status is COMPLETE
    (excluding ``slice_id`` itself), a *tip* is a completed slice that
    no other completed slice forked from (via
    ``parent_branch_at_creation``, falling back to declared
    ``dependencies``). Tips are ranked by completed-chain length, then
    by contract declaration order, descending — under the default
    serialized execution there is exactly one tip and its chain
    contains every completed slice.

    Liveness: each candidate tip's integration branch is probed via
    ``branch_exists`` (same contract as the resolver's #2928 gate — a
    raised probe is treated conservatively as "exists", failing loud at
    branch creation rather than silently mis-basing). A definitively
    absent branch means the chain's PR was merged into ``work`` and
    cascade-deleted, so its content is already on the work branch; the
    next tip is tried. Returns ``None`` when no completed tip with a
    live branch remains — the caller falls back to the pipeline branch.
    """
    from egg_contracts.models import SliceStatus

    completed = {
        s.id: s
        for s in slices
        if s.id != slice_id and getattr(s, "status", None) == SliceStatus.COMPLETE
    }
    if not completed:
        return None

    completed_ids = set(completed)
    referenced: set[str] = set()
    for s in completed.values():
        parent_id = _slice_linear_parent_id(s, issue_branch=issue_branch, known_ids=completed_ids)
        if parent_id is not None:
            referenced.add(parent_id)
    tips = [s for sid, s in completed.items() if sid not in referenced]
    if not tips:
        # Unreachable for a validated forest (no cycles); bail rather
        # than guess.
        return None

    def _chain_len(tip) -> int:
        seen: set[str] = set()
        cursor = tip
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            parent_id = _slice_linear_parent_id(
                cursor, issue_branch=issue_branch, known_ids=completed_ids
            )
            cursor = completed.get(parent_id) if parent_id else None
        return len(seen)

    declared_index = {s.id: i for i, s in enumerate(slices)}
    tips.sort(
        key=lambda s: (_chain_len(s), declared_index.get(s.id, -1)),
        reverse=True,
    )

    for tip in tips:
        candidate = f"{issue_branch}/{tip.id}"
        if branch_exists is None:
            return candidate
        try:
            exists = branch_exists(candidate)
        except Exception as probe_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Completed-tip branch probe raised; assuming tip exists "
                "and chaining onto it (#3541)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                tip_branch=candidate,
                error=str(probe_err),
            )
            return candidate
        if exists:
            return candidate
        _pkg.logger.info(
            "Completed-tip branch absent on origin (merged and "
            "cascade-deleted); trying next tip (#3541)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            tip_branch=candidate,
        )
    return None


def _resolve_slice_base_branch(
    contract,
    slice_id: str,
    *,
    pipeline_id: str,
    pipeline_branch: str,
    extant_branches: set[str] | None = None,
    parent_branch_exists: _pkg.Callable[[str], bool] | None = None,
) -> str:
    """Return the parent branch for a slice's integration branch (#2777, cq-9).

    Replaces the deleted slice-1 resolver helper (removed by slice-2
    TASK-2-1) with a single resolver that handles both root and
    non-root slices.

    Three-tier resolution (default — ``extant_branches is None``):

    1. **Eager-persisted parent** (post-slice-4 TASK-4-2). If
       ``parent_branch_at_creation`` is set on the slice record,
       return it. This is the primary path post-slice-4 — slices
       created after the eager persist landed always go through
       this arm.
    2. **Dependency-derived parent, gated on parent existence
       (#2928)**. For a non-root slice whose
       ``parent_branch_at_creation`` is empty (the normal first-run
       case), the stack target is its dependency parent's
       integration branch ``{issue_branch}/{dependencies[0]}``. When
       a ``parent_branch_exists`` callback is provided, the resolver
       probes whether that parent branch is still present on origin:

       * parent branch **exists** → return the dependency-derived
         parent. This is the correct target for both fresh slices
         (whose own integration branch does not exist yet) and
         legacy slices.
       * parent branch **absent** → the parent slice's PR was merged
         into ``work`` and its branch deleted by the cascade, so
         ``work`` already contains the parent's commits. Fall back
         to ``pipeline_branch``.
       * probe **raises** → conservative default: assume the parent
         exists and return the derived parent. Never silently swap a
         real slice onto ``work`` because of a flaky gateway.

       This replaces the pre-#2928 merge-base check, which probed the
       *slice's own* integration branch for a fork point and routed a
       ``None`` result (no fork point) to ``pipeline_branch``. That
       conflated a FRESH slice (integration branch not yet created —
       the common first-run case) with a genuinely orphaned slice,
       silently mis-basing fresh slices onto ``work`` whenever
       ``work`` had advanced ahead of the parent (the wedge in
       #2928).
    3. **Root linearization (#3541)**. A root slice (no dependencies,
       no recorded parent) forks from the latest COMPLETE chain tip
       (see :func:`_latest_completed_chain_tip`) rather than the work
       branch: during a run the work branch only advances with
       bookkeeping commits, so basing a root there after a sibling
       chain completed would orphan that chain's reviewed code from
       every downstream slice.
    4. **Final fallback** to ``pipeline_branch`` (``egg/<id>/work``)
       when (a) no eager-persisted parent and the slice is a root
       with no completed chain tip to linearize onto, OR (b) the
       slice's dependency parent branch is absent from origin.
       Root-targeted branches are never deleted by the cascade so
       this is always a safe terminal candidate.

    **Orphan-reconciler mode (``extant_branches`` non-None)**: the
    stacked-PR reconciler at ``orchestrator/stacked_pr_reconciler.py``
    needs the resolver to SKIP ancestors whose branches are no longer
    on origin (the primary trigger for orphan reconciliation is "parent
    branch was deleted by the cascade merge"). When ``extant_branches``
    is supplied, each candidate (including ``parent_branch_at_creation``
    and any walked ancestor) is filtered against the set; if no extant
    candidate is found the resolver falls back to ``pipeline_branch``
    (which is always extant — root-targeted branches are never deleted
    by the stacked-PR flow).

    Args:
        contract: The pipeline contract (must carry ``slices``).
        slice_id: The slice whose base branch to resolve.
        pipeline_id: Used only for log diagnostics; the resolver does
            NOT consult the state store.
        pipeline_branch: The pipeline's work branch (``egg/<id>/work``).
            Returned for root slices when no
            ``parent_branch_at_creation`` is recorded, and as the
            final fallback in orphan-reconciler mode and the
            merge-base "no fork point" arm.
        extant_branches: Optional set of branch names known to exist
            on origin. When supplied, the resolver filters every
            candidate (recorded parent + walked ancestors) against
            this set and skips any that are absent. The reconciler
            uses this to escape from the deleted parent branch up the
            DAG until an extant ancestor is reached.
        parent_branch_exists: Optional callback (#2928) used to
            decide whether a non-root slice's dependency parent
            branch is still on origin. When provided, the resolver
            invokes ``parent_branch_exists(parent_branch)`` with the
            dependency-derived parent branch name. ``True`` returns
            the derived parent; ``False`` routes to
            ``pipeline_branch`` (parent merged + cascade-deleted); a
            raised exception is treated conservatively as ``True``.
            The default ``_run_one_slice_inner`` caller wires this
            against ``spawner.gateway.ls_remote_branch_strict`` — the
            strict variant is required so a gateway / network /
            policy failure RAISES into this resolver's ``try/except``
            instead of being collapsed to ``False`` (which would
            silently route a real slice onto ``pipeline_branch`` on
            any gateway flake — re-creating the #2928 wedge). The
            stacked-PR reconciler leaves it ``None`` (it has already
            verified extant branches via the ``extant_branches``
            set).

            Mutually exclusive with ``extant_branches`` in practice:
            the production caller (``_run_one_slice_inner``) passes
            only this gate, and the stacked-PR reconciler passes only
            ``extant_branches``. If a future caller passed both, this
            gate would short-circuit to ``pipeline_branch`` on a
            ``False`` return BEFORE the ``extant_branches`` walk
            could find an extant ancestor; callers that have already
            built the extant set should leave this ``None``.

    Returns:
        The branch name to use as the slice integration branch's
        parent. Never an empty string.

    Raises:
        ValueError: When the requested slice id is absent from the
            contract — a structural bug that the slice loop's earlier
            forest-validation step should have caught.
    """
    slices = getattr(contract, "slices", None) or []
    slice_record = next((s for s in slices if s.id == slice_id), None)
    if slice_record is None:
        raise ValueError(
            f"slice {slice_id!r} not present in contract for pipeline "
            f"{pipeline_id!r}; available slices: "
            f"{[s.id for s in slices]}"
        )

    def _extant(candidate: str) -> bool:
        """True when ``candidate`` passes the orphan-reconciler filter.

        When ``extant_branches`` is None, every non-empty candidate
        passes (the default resolver doesn't validate liveness).
        """
        if not candidate:
            return False
        if extant_branches is None:
            return True
        return candidate in extant_branches

    # (1) Eager-persisted parent (post-slice-4 TASK-4-2). Treated as
    # authoritative regardless of root-status: if the persist landed,
    # it's the resolved parent — UNLESS the orphan-reconciler caller
    # told us this branch was deleted on origin (extant_branches
    # filter).
    parent_recorded = getattr(slice_record, "parent_branch_at_creation", None) or ""
    if parent_recorded and _extant(parent_recorded):
        return parent_recorded

    # Build the slice-id → slice-record lookup once for the DAG walk
    # below (used in both the default and orphan-reconciler modes).
    slices_by_id = {s.id: s for s in slices}

    deps = getattr(slice_record, "dependencies", None) or []
    parent_slice_id = deps[0] if deps else None
    issue_branch = _pkg._slice_namespace_root(pipeline_branch)

    # (2) Root slice — under the new topology (cq-4), the context PR
    # is ``egg/<id>/work → main``, and root slices used to stack
    # directly on the work branch. #3541: during a run the work branch
    # only ever advances with bookkeeping commits (contract persists;
    # slice PRs are human-merged after the pipeline finishes), so a
    # root admitted after a sibling chain completed would fork a base
    # that silently excludes that chain's reviewed, consensus-approved
    # code — and every descendant inherits the gap. Chain the root
    # onto the latest completed chain tip instead; the work branch
    # remains the base for the first root (nothing completed yet) and
    # the fallback when every completed tip's branch was merged and
    # cascade-deleted (its content then already lives on ``work``).
    #
    # Orphan-reconciler mode keeps the old behaviour: a root whose
    # recorded parent was filtered out by ``extant_branches`` was
    # forked from a branch that has since been merged into ``work``,
    # so ``pipeline_branch`` is the correct retarget — re-linearizing
    # onto an unrelated live chain would rewrite the PR's diff.
    if parent_slice_id is None:
        if extant_branches is not None:
            return pipeline_branch
        tip_branch = _latest_completed_chain_tip(
            slices,
            slice_id=slice_id,
            issue_branch=issue_branch,
            pipeline_id=pipeline_id,
            branch_exists=parent_branch_exists,
        )
        if tip_branch is not None:
            _pkg.logger.info(
                "Root slice linearized onto completed chain tip (#3541)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                tip_branch=tip_branch,
            )
            return tip_branch
        return pipeline_branch

    # (3) Non-root slice — derive from the first dependency. Mirrors
    # the existing ``f"{issue_branch}/{parent_slice_id}"`` convention
    # at the legacy slice-loop call site.
    derived_parent = f"{issue_branch}/{parent_slice_id}"

    # #2928: parent-existence gate. When eager-persist did not land
    # (``parent_recorded`` empty above) AND a ``parent_branch_exists``
    # callback is provided, decide between the dependency-derived
    # parent and ``pipeline_branch`` by probing whether the parent
    # slice's integration branch is still on origin — NOT by probing
    # the slice's own branch for a fork point.
    #
    # The pre-#2928 implementation computed
    # ``merge_base(integration_branch, derived_parent)`` and routed a
    # ``None`` result to ``pipeline_branch``. That conflated a FRESH
    # slice (its integration branch is created *after* this resolver
    # runs, so it has no fork point on the first run — the common
    # case) with a genuinely orphaned slice, silently mis-basing
    # fresh slices onto ``work`` whenever ``work`` had advanced ahead
    # of the parent (e.g. a stray contract-state commit on ``work``).
    # The correct discriminator is parent-branch existence:
    #
    #   * parent exists  → stack on it (fresh OR legacy slice).
    #   * parent absent  → the parent PR merged into ``work`` and its
    #     branch was cascade-deleted, so ``work`` already contains the
    #     parent's commits → ``pipeline_branch`` is the right base.
    #   * probe raises    → conservative: assume the parent exists and
    #     return the derived parent; never silently swap a real slice
    #     onto ``work`` because the gateway was flaky.
    if parent_branch_exists is not None:
        try:
            exists = parent_branch_exists(derived_parent)
        except Exception as probe_err:  # noqa: BLE001
            _pkg.logger.warning(
                "parent_branch_exists probe raised; assuming parent "
                "exists and returning dependency-derived parent (#2928)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                derived_parent=derived_parent,
                error=str(probe_err),
            )
            exists = True
        if not exists:
            _pkg.logger.warning(
                "Dependency-parent branch absent on origin; parent "
                "appears merged into work — basing slice on pipeline "
                "branch (#2928)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                derived_parent=derived_parent,
                pipeline_branch=pipeline_branch,
            )
            return pipeline_branch

    # Default mode (no extant filter): return the immediate parent
    # branch synthesised from the slice DAG. This is the unchanged
    # pre-extant-kwarg behaviour.
    if extant_branches is None:
        return f"{issue_branch}/{parent_slice_id}"

    # Orphan-reconciler mode: walk up the DAG via ``dependencies[0]``
    # until an extant ancestor branch is found. The forest constraint
    # at ``shared/egg_contracts/models.py:341`` guarantees ≤1 parent
    # per slice, so a single traversal pointer suffices.
    cursor: str | None = parent_slice_id
    while cursor:
        candidate = f"{issue_branch}/{cursor}"
        if _extant(candidate):
            return candidate
        cursor_slice = slices_by_id.get(cursor)
        if cursor_slice is None:
            break
        next_deps = getattr(cursor_slice, "dependencies", None) or []
        cursor = next_deps[0] if next_deps else None

    # Every ancestor's branch has been deleted (cascading merge). Fall
    # back to the pipeline branch — stable across the stacked-PR flow
    # because root-targeted branches are never deleted by the cascade.
    return pipeline_branch


def _lookup_peer_consensus_tracker_or_none(
    pipeline_id: str, slice_id: str | None
) -> _pkg.Any | None:
    """Look up a per-slice PeerConsensusTracker; return None on import failure.

    Slice-4 TASK-4-4 helper. The bootstrap classifier needs to inspect
    consensus state (``tracker.evaluate()['is_complete']``) for
    IN_PROGRESS slices with commits on origin to differentiate case (2)
    (consensus not reached → mark spawned) from case (3) (consensus
    reached → mark COMPLETE so the slice-PR opener fires). This thin
    wrapper centralises the lazy import + None-on-import-failure dance
    so the classifier itself stays declarative and easily unit-tested.
    """
    try:
        from orchestrator.peer_consensus import (
            get_peer_consensus_tracker as _gpct,
        )
    except ImportError:
        try:
            from peer_consensus import (  # type: ignore[no-redef]
                get_peer_consensus_tracker as _gpct,
            )
        except ImportError:
            return None
    try:
        return _gpct(pipeline_id, slice_id=slice_id)
    except Exception:  # noqa: BLE001
        return None


def _slice_has_pending_decision(slice_id: str, decisions: list[_pkg.Any]) -> bool:
    """Return True iff the contract has any unresolved HITL decision.

    Slice-4 TASK-4-4 case (4) helper. The classifier treats a BLOCKED
    slice with no pending decision as a state-machine anomaly (the
    slice was waiting on a HITL that has since been resolved without
    flipping the slice status forward). Surface that to the operator
    via OVERSEER_ALERT.

    The contract's :class:`egg_contracts.models.Decision` does NOT
    carry a structured ``slice_id`` tag — decisions are scoped by
    phase (``decision.phase``) rather than by slice. The conservative
    interpretation: any unresolved decision is *potentially* the
    reason this slice is BLOCKED, so we return ``True`` (suppress the
    "missing-HITL" alert) whenever the contract carries ANY unresolved
    decision. The function only returns ``False`` when ZERO unresolved
    decisions exist on the contract — at which point a BLOCKED slice
    is provably unexplained and the overseer alert is warranted.

    Practically: this errs on the side of NOT alerting (suppressing
    a real cross-slice mismatch in favour of skipping a spurious
    alert), because alert noise during normal multi-slice HITL flows
    is worse than a missed anomaly that the next bootstrap pass will
    re-check anyway.

    ``slice_id`` is currently unused; kept in the signature so a
    future contract schema bump that adds a structured slice tag can
    use the existing call sites verbatim.
    """
    del slice_id  # contract decisions are not tagged by slice yet
    for d in decisions:
        if not getattr(d, "resolved", False):
            return True
    return False


def _classify_non_complete_slice(
    *,
    pipeline_id: str,
    slice_obj: _pkg.Any,
    issue_branch: str,
    pipeline_repo: _pkg.Any,
    worktree_repo_path: _pkg.Path,
    gateway: _pkg.Any,
    gateway_mode: Literal["public", "private"],
    consensus_tracker_lookup: _pkg.Callable[[str, str | None], _pkg.Any | None],
) -> str:
    """Classify a non-COMPLETE slice for Layer-C bootstrap reconciliation.

    Slice-4 TASK-4-4. Returns one of the five classification labels:

    * ``"fresh"`` — case (1) IN_PROGRESS/PENDING with no commits on
      origin. No Layer-C action; the scheduler re-yields READY and
      the run loop spawns fresh agents.
    * ``"resume"`` — case (2) IN_PROGRESS with commits on origin and
      consensus NOT reached. Caller calls
      ``scheduler.mark_spawned(slice_id)`` so the run loop does NOT
      respawn.
    * ``"consensus_complete"`` — case (3) IN_PROGRESS with commits
      and ``tracker.evaluate()['is_complete']`` True. Caller marks
      the slice COMPLETE so the next loop iteration runs the slice-PR
      opener via its idempotent pre-flight.
    * ``"blocked"`` — case (4) BLOCKED slice (HITL pending). Caller
      preserves status. If no pending HITL is found on the contract,
      caller escalates via ``_escalate_blocked_slice_to_hitl``
      which writes a new ``Decision`` to the contract.
    * ``"corrupt"`` — case (5) impossible status enum or
      contradictory state combination (PENDING with commits, etc.).
      Caller escalates via ``_escalate_corrupt_slice_to_hitl``
      which writes a new ``Decision`` to the contract.

    The classifier is intentionally a pure function modulo the
    injected ``gateway`` probe + ``consensus_tracker_lookup`` —
    unit tests in TASK-4-6 fake both.
    """
    try:
        from egg_contracts.models import SliceStatus
    except ImportError:
        return "corrupt"

    status = getattr(slice_obj, "status", None)
    if status == SliceStatus.BLOCKED:
        # Case 4 — caller (Layer-C loop) validates the HITL via
        # ``_slice_has_pending_decision`` and escalates if absent.
        # The classifier itself just reports the BLOCKED state.
        return "blocked"

    if status not in (SliceStatus.PENDING, SliceStatus.IN_PROGRESS):
        # Case 5 — unknown / corrupt status enum value. The
        # SliceStatus StrEnum has exactly four members; any other
        # value (None, a string that didn't deserialise to the enum,
        # a future enum addition we don't recognise yet) is treated
        # as corrupt rather than silently re-yielded as READY.
        return "corrupt"

    # Probe the slice's integration branch for commits on origin.
    integration_branch = f"{issue_branch}/{slice_obj.id}"
    has_commits: bool
    if pipeline_repo is None:
        # Repoless pipelines (test scaffolds) — no origin to consult.
        # Treat as no-commits → fresh, which mirrors the default
        # scheduler behaviour.
        has_commits = False
    else:
        try:
            sha = gateway.get_remote_branch_sha(
                pipeline_id,
                str(worktree_repo_path),
                f"refs/heads/{integration_branch}",
                mode=gateway_mode,
            )
            has_commits = sha is not None
        except Exception as probe_err:  # noqa: BLE001
            # Probe failure (gateway down, transient HTTP). Conservative
            # default: treat as has_commits=False so the slice is
            # re-yielded READY rather than silently mark-spawned with
            # no agents alive.
            #
            # NOTE on asymmetry vs. ``_resolve_slice_base_branch``
            # (slice-4 TASK-4-3, ~line 10510): the resolver defaults
            # the *opposite* direction — probe failure → "has fork
            # point → derived parent" — because mis-routing onto
            # ``pipeline_branch`` on a transient probe error would
            # silently change a slice's stack target. Here in Layer C,
            # a "fresh" mis-classification just causes the scheduler
            # to re-yield the slice as READY (fresh-agent spawn, which
            # then sync-then-fetches and continues correctly). The
            # asymmetry is deliberate: each direction picks the safer
            # default for its own caller.
            _pkg.logger.warning(
                "Layer-C bootstrap probe raised; treating slice as fresh (slice-4 TASK-4-4)",
                pipeline_id=pipeline_id,
                slice_id=slice_obj.id,
                error=str(probe_err),
            )
            has_commits = False

    if not has_commits:
        # PENDING-without-commits is the normal fresh slice case.
        # IN_PROGRESS-without-commits means a crash between the
        # eager-persist (TASK-4-2) and ``create_slice_integration_branch``
        # — also fresh from the scheduler's perspective.
        return "fresh"

    if status == SliceStatus.PENDING and has_commits:
        # Case 5 — PENDING with commits on origin is a state-machine
        # impossibility (the eager-persist (TASK-4-2) flips PENDING →
        # IN_PROGRESS in the same contract write that records the
        # parent branch BEFORE any commits could land). Treat as
        # corrupt.
        return "corrupt"

    # IN_PROGRESS with commits — distinguish (2) vs (3) via the
    # consensus tracker reconstructed by startup_reconciliation.py
    # (slice-4 TASK-4-5).
    tracker = consensus_tracker_lookup(pipeline_id, slice_obj.id)
    consensus_complete = False
    if tracker is not None:
        try:
            evaluation = tracker.evaluate()
            consensus_complete = bool(evaluation.get("is_complete"))
        except Exception as eval_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Layer-C bootstrap tracker.evaluate() raised; treating slice "
                "as consensus-incomplete (slice-4 TASK-4-4)",
                pipeline_id=pipeline_id,
                slice_id=slice_obj.id,
                error=str(eval_err),
            )

    return "consensus_complete" if consensus_complete else "resume"


def _escalate_layer_c_hitl(
    *,
    pipeline_id: str,
    slice_id: str,
    worktree_repo_path: _pkg.Path,
    current_phase: _pkg.PipelinePhase | None,
    question: str,
) -> None:
    """Create an HITL Decision on the contract for a Layer-C anomaly (slice-4 TASK-4-4).

    Shared transport for case (4) blocked-without-HITL and case (5)
    corrupt-status escalations. Per the plan task body — "escalate
    via ``mcp__sdlc__register_open_question`` (do NOT silently
    re-yield as READY — silent classification error is worse than
    an operator pause)" — Layer C must create an unresolved
    ``Decision`` on the contract that pauses the slice until the
    operator picks an option, not just a message-bus broadcast.

    The caller supplies ``worktree_repo_path`` (the per-pipeline
    worktree where the live contract lives — Layer C runs inside
    ``_run_implement_phase_slices`` which already has it in scope)
    and ``current_phase`` (the live pipeline phase, so a Decision
    surfaces under the phase the operator is debugging rather than
    a hard-coded literal).

    **Lock-nesting invariant (reviewer_code v2 blocker 3)**: the
    caller MUST NOT already hold ``get_pipeline_state_lock`` for
    this pipeline. Today the Layer-C dispatch loop in
    ``_run_implement_phase_slices`` calls this helper at the
    top-level slice-loop scope BEFORE any per-slice lock
    acquisition (the eager-persist site at
    ``_run_one_slice_inner`` is the only nested-lock contract
    write today). The current lock IS an ``threading.RLock`` so
    re-entry would not deadlock, but if a future refactor narrows
    the lock to a plain ``Lock`` (e.g. for monitor visibility),
    a Layer-C call from inside another lock-holding scope would
    deadlock the entire bootstrap.

    Pattern mirrors ``_persist_hitl_decision`` (above) but loads the
    contract from the per-pipeline worktree directly (the caller
    has it in scope) so the decision lands on the live contract
    that ``/sdlc`` reads. Best-effort: contract-load / save
    failures are logged and swallowed (consistent with the rest of
    Layer C). The decision is tagged with a ``context`` prefix so a
    dispatch handler in
    ``routes/decisions.py`` can route on a stable discriminator if
    one is added in a follow-up.
    """
    try:
        from egg_contracts.decisions import (
            find_duplicate_open_question,
            find_resolved_question,
            next_cq_id,
        )
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType
    except ImportError:
        try:
            from orchestrator.egg_contracts.decisions import (  # type: ignore[no-redef]
                find_duplicate_open_question,
                find_resolved_question,
                next_cq_id,
            )
            from orchestrator.egg_contracts.loader import (  # type: ignore[no-redef]
                load_contract,
                save_contract,
            )
            from orchestrator.egg_contracts.models import (  # type: ignore[no-redef]
                Decision,
                DecisionOption,
                DecisionType,
            )
        except ImportError:
            _pkg.logger.warning(
                "Layer-C HITL escalation skipped: egg_contracts not importable (slice-4 TASK-4-4)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
            return
    decision_id: str = ""
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            contract_local = load_contract(pipeline_id, worktree_repo_path)
            existing_decisions = contract_local.decisions or []
            decision_phase = current_phase or _pkg.PipelinePhase.IMPLEMENT
            # Dedupe/carry-forward — parity with ``register_open_question``
            # (#3374/#3392). The Layer-C question text is deterministic per
            # (case, slice, pipeline), so every bootstrap re-run after a
            # ``restart_phase`` re-derives the identical question. Without
            # this guard each re-run minted a fresh ``cq-N`` (or, against a
            # reset-stale contract, re-minted an existing one), making the
            # operator re-answer questions they had already answered (#3427).
            duplicate = find_duplicate_open_question(existing_decisions, question, decision_phase)
            if duplicate is not None:
                _pkg.logger.info(
                    "Layer-C HITL escalation adopted existing open decision (slice-4 TASK-4-4)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    decision_id=getattr(duplicate, "id", None),
                )
                return
            carried = find_resolved_question(existing_decisions, question, decision_phase)
            if carried is not None:
                _pkg.logger.info(
                    "Layer-C HITL escalation skipped: identical question "
                    "already resolved by the operator (slice-4 TASK-4-4)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    decision_id=getattr(carried, "id", None),
                    resolution=str(getattr(carried, "resolution", None))[:200],
                )
                return
            # Use the canonical ``cq-N`` allocator from
            # ``shared/egg_contracts/decisions.py``. Orchestrator-side
            # HITL escalations write to the ``cq-N`` namespace; the
            # pipeline-side bridge owns ``decision-N``. The split was
            # introduced by #2616 to prevent the
            # ``len(decisions)+1`` collision between the two
            # allocators (see the docstring at
            # ``shared/egg_contracts/decisions.py``).
            decision_id = next_cq_id(contract_local.decisions)
            options = [
                DecisionOption(id="opt-1", label="Mark slice complete and continue"),
                DecisionOption(id="opt-2", label="Restart slice from scratch"),
                DecisionOption(id="opt-3", label="Cancel pipeline for manual investigation"),
            ]
            # Use the live pipeline phase rather than a hard-coded
            # ``PipelinePhase.IMPLEMENT`` — Layer C fires during
            # bootstrap which can run before any phase walk, and
            # future slice-DAG topologies may span phases.
            #
            # The ``or PipelinePhase.IMPLEMENT`` arm (folded into
            # ``decision_phase`` above) is defensive: the
            # ``Pipeline.current_phase`` field is non-Optional with a
            # default at the schema layer (``models.py:1032``), so
            # in-tree callers should always populate it. The fallback
            # exists for future non-``Pipeline``-shaped callers (e.g.
            # contract-only loads during cold-start reconciliation
            # that may construct a lighter object) — *not* a known-bug
            # papering exercise for current shapes.
            contract_local.decisions.append(
                Decision(
                    id=decision_id,
                    question=question,
                    type=DecisionType.HITL,
                    phase=decision_phase,
                    options=options,
                )
            )
            save_contract(contract_local, worktree_repo_path)
        _pkg.logger.info(
            "Layer-C HITL escalation persisted on contract (slice-4 TASK-4-4)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            decision_id=decision_id,
        )
        # Durably land the new decision on the work branch so the next
        # phase-(re)start worktree reset cannot revert it (#3427).
        _pkg.persist_contract_statefiles(
            pipeline_id,
            worktree_repo_path,
            f"Persist Layer-C HITL escalation {decision_id} (#3427)",
        )
    except Exception as escalate_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Layer-C HITL escalation failed (slice-4 TASK-4-4); slice will "
            "remain in its current contract status",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(escalate_err),
        )


def _escalate_corrupt_slice_to_hitl(
    *,
    pipeline_id: str,
    slice_id: str,
    worktree_repo_path: _pkg.Path,
    current_phase: _pkg.PipelinePhase | None,
) -> None:
    """Escalate a Layer-C case-5 corrupt-state slice to HITL (slice-4 TASK-4-4).

    Question text is prefixed with ``[#2777 slice-4 TASK-4-4 case 5]``
    so a future dispatch handler in ``routes/decisions.py`` can route
    on the literal substring without a separate context field on the
    contract-level ``Decision`` model.
    """
    _pkg._escalate_layer_c_hitl(
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        worktree_repo_path=worktree_repo_path,
        current_phase=current_phase,
        question=(
            f"[#2777 slice-4 TASK-4-4 case 5] Slice {slice_id} of pipeline "
            f"{pipeline_id} has an impossible status enum value or state "
            f"combination (e.g. status not in PENDING/IN_PROGRESS/COMPLETE/"
            f"BLOCKED, or PENDING with commits on the integration branch). "
            f"Bootstrap reconciliation cannot classify the slice safely. "
            f"How should the orchestrator proceed?"
        ),
    )


def _escalate_blocked_slice_to_hitl(
    *,
    pipeline_id: str,
    slice_id: str,
    reason: str,
    worktree_repo_path: _pkg.Path,
    current_phase: _pkg.PipelinePhase | None,
) -> None:
    """Escalate a Layer-C case-4 blocked-without-HITL slice to HITL (slice-4 TASK-4-4).

    Question text is prefixed with ``[#2777 slice-4 TASK-4-4 case 4]``
    so a future dispatch handler in ``routes/decisions.py`` can route
    on the literal substring without a separate context field on the
    contract-level ``Decision`` model.
    """
    _pkg._escalate_layer_c_hitl(
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        worktree_repo_path=worktree_repo_path,
        current_phase=current_phase,
        question=(
            f"[#2777 slice-4 TASK-4-4 case 4] Slice {slice_id} of pipeline "
            f"{pipeline_id} is in BLOCKED status, but no PENDING HITL "
            f"decision was found on the contract that matches the slice. "
            f"{reason}. How should the orchestrator proceed?"
        ),
    )


def _cross_repo_hold_marker(slice_id: str) -> str:
    """Return the stable per-gate discriminator embedded in the hold question."""
    return f"{_pkg._CROSS_REPO_HOLD_MARKER_PREFIX} slice={slice_id}]"


def _cross_repo_hold_resolution(contract: _pkg.Any, slice_id: str) -> str | None:
    """Return the human's verdict on the cross-repo hold Decision for a slice.

    Scans the (freshly-loaded) contract for the Decision carrying this gate's
    :func:`_cross_repo_hold_marker` and, when it is resolved, maps the
    operator's SELECTED option to a gate verdict:

    * :data:`cross_repo_merge_gate.RELEASE` — the release option was chosen
      (mark the PR ready), else
    * :data:`cross_repo_merge_gate.KEEP` — the keep-held option was chosen, OR
      the resolution is present but unrecognized (fail-safe: an ambiguous
      resolution must NOT auto-ready — cq-1 "human owns the release").

    Returns ``None`` when the Decision is absent or not yet resolved (keep
    waiting). The stored ``Decision.resolution`` may be the option label, the
    option id, or a ``{"action":"select","selected":<label>}`` envelope (the
    SDLC HITL CLI shape), so we unwrap the envelope and match on both id and a
    distinctive keyword. This is the release path that honours the operator's
    choice rather than readying on the bare resolved-boolean
    (reviewer_code_holistic v1 NACK).
    """
    try:
        from cross_repo_merge_gate import KEEP, RELEASE
    except ImportError:
        from ..cross_repo_merge_gate import KEEP, RELEASE  # type: ignore[no-redef]

    marker = _pkg._cross_repo_hold_marker(slice_id)
    decision = None
    for d in getattr(contract, "decisions", None) or []:
        if marker in (getattr(d, "question", "") or ""):
            decision = d
            break
    if decision is None or not getattr(decision, "resolved", False):
        return None

    raw = getattr(decision, "resolution", None) or ""
    # Unwrap the ``{"action":"select","selected":<label>}`` envelope the SDLC
    # HITL CLI sends (mirrors routes.decisions._normalize_choice_resolution),
    # tolerating a bare string / non-JSON resolution unchanged.
    selected = raw
    try:
        import json as _json

        payload = _json.loads(raw)
        if isinstance(payload, dict) and payload.get("action") == "select":
            sel = payload.get("selected")
            if isinstance(sel, str):
                selected = sel
    except ValueError, TypeError:
        pass

    text = selected.strip().lower()
    # #3393 task-5-1/gap-2 (defends the operator's cq-1 fail-safe ruling):
    # release ONLY on an EXACT match against the release option's id or
    # label.  The prior ``"release" in text`` substring check failed OPEN
    # — a freeform "Other" resolution that merely CONTAINS the word
    # "release" in a negating sense (e.g. "do NOT release yet") would have
    # auto-readied a PR the human meant to keep held, a narrower
    # reintroduction of the "keep-held is a lie" class reviewer_code_holistic
    # NACK'd.  Exact equality (after envelope-unwrap + strip + lower) keeps
    # the designed path (selecting opt-release / its label) working while
    # every ambiguous or negated value falls through to the KEEP fail-safe.
    if text in (
        _pkg._CROSS_REPO_HOLD_RELEASE_OPTION_ID.lower(),
        _pkg._CROSS_REPO_HOLD_RELEASE_OPTION_LABEL.lower(),
    ):
        return RELEASE
    # Any other resolved value (the keep option, or an unrecognized/freeform
    # string) keeps the PR held — never ready on an ambiguous selection.
    return KEEP


def _register_cross_repo_hold(
    *,
    pipeline_id: str,
    slice_id: str,
    repo: str,
    pr_number: int,
    reason: str,
    worktree_repo_path: _pkg.Path,
    current_phase: _pkg.PipelinePhase | None,
) -> bool:
    """Ensure a cross-repo merge-sequencing HITL hold exists on the contract.

    Idempotent: if a Decision carrying this gate's marker already exists
    (pending OR resolved), no new Decision is created. Returns ``True``
    when a hold now exists for the gate (freshly registered or already
    present), ``False`` only when registration could not be persisted —
    the poll uses the return to decide whether the gate has been handed
    off to the HITL release path. Modelled on :func:`_escalate_layer_c_hitl`
    (loads the live contract from the per-pipeline worktree, allocates a
    ``cq-N`` id, appends an unresolved HITL Decision, saves). The hold
    surfaces on ``/status`` via the existing pending-decision collector.
    """
    try:
        from egg_contracts.decisions import next_cq_id
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType
    except ImportError:
        try:
            from orchestrator.egg_contracts.decisions import (  # type: ignore[no-redef]
                next_cq_id,
            )
            from orchestrator.egg_contracts.loader import (  # type: ignore[no-redef]
                load_contract,
                save_contract,
            )
            from orchestrator.egg_contracts.models import (  # type: ignore[no-redef]
                Decision,
                DecisionOption,
                DecisionType,
            )
        except ImportError:
            _pkg.logger.warning(
                "Cross-repo hold skipped: egg_contracts not importable (#3393)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
            return False

    marker = _pkg._cross_repo_hold_marker(slice_id)
    reason_text = _pkg._CROSS_REPO_HOLD_REASON_TEXT.get(reason, reason)
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            contract_local = load_contract(pipeline_id, worktree_repo_path)
            # Idempotent: a hold Decision for this gate already exists.
            for d in contract_local.decisions or []:
                if marker in (getattr(d, "question", "") or ""):
                    return True
            decision_id = next_cq_id(contract_local.decisions)
            question = (
                f"{marker} Slice {slice_id} of pipeline {pipeline_id} opened PR "
                f"{repo}#{pr_number} as a draft behind a cross-repo dependency, "
                f"but {reason_text}. Choose how the orchestrator should proceed: "
                f"selecting '{_pkg._CROSS_REPO_HOLD_RELEASE_OPTION_LABEL}' marks the PR "
                f"ready; selecting '{_pkg._CROSS_REPO_HOLD_KEEP_OPTION_LABEL}' leaves it "
                f"draft for you to handle manually."
            )
            options = [
                DecisionOption(
                    id=_pkg._CROSS_REPO_HOLD_RELEASE_OPTION_ID,
                    label=_pkg._CROSS_REPO_HOLD_RELEASE_OPTION_LABEL,
                ),
                DecisionOption(
                    id=_pkg._CROSS_REPO_HOLD_KEEP_OPTION_ID,
                    label=_pkg._CROSS_REPO_HOLD_KEEP_OPTION_LABEL,
                ),
            ]
            contract_local.decisions.append(
                Decision(
                    id=decision_id,
                    question=question,
                    type=DecisionType.HITL,
                    phase=current_phase or _pkg.PipelinePhase.IMPLEMENT,
                    options=options,
                )
            )
            save_contract(contract_local, worktree_repo_path)
        _pkg.logger.info(
            "Registered cross-repo merge-sequencing HITL hold (#3393)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            repo=repo,
            pr_number=pr_number,
            reason=reason,
            decision_id=decision_id,
        )
        return True
    except Exception as hold_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Cross-repo hold registration failed (#3393); PR stays draft, "
            "poll will retry next tick",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            reason=reason,
            error=str(hold_err),
        )
        return False


def _check_slice_evidence_reachability(
    pipeline_id: str,
    spawner: "ContainerSpawner",  # noqa: UP037
    worktree_repo_path: _pkg.Path,
    slice_id: str,
    integration_branch: str,
    *,
    gateway_mode: Literal["public", "private"] = "public",
    contract: _pkg.Any | None = None,
) -> str | None:
    """Verify the slice's cited evidence commits reached the integration branch (#3125).

    The integration branch only advances when a producer pushes
    (``consensus_push`` at propose time). A commit recorded by
    ``egg-contract complete-task --commit <sha>`` *after* that producer
    confirmed — the prescribed HITL unblock flow for a post-confirmation
    task reassignment (#3124) — lives only on the agent's local worktree
    branch, so the slice would otherwise close and open its PR without
    the deliverable while the contract task record points at a commit
    nothing retains.

    Runs after slice consensus and before any close side effects (BRC
    transcript commit, slice PR). Returns ``None`` when the slice may
    close, or a human-readable failure string listing every task row
    whose cited commit is not an ancestor of the integration branch tip
    — the caller records the slice failure with it, which routes
    through the existing cascade + HITL escalation machinery instead of
    closing silently.

    Only role-bound task rows are gated (#3339): the check exists for
    the producer-scoped #3124 flow, so a ``role=unassigned`` row's
    orphan commit is bookkeeping, not a gated deliverable, and must not
    fail a consensus-reached slice. See ``cc.evidence_commits``.

    Failure posture mirrors the other completeness checks (#3081 /
    #3114): the gate degrades to ``None`` (close proceeds, warning
    logged) when the contract cannot be read, the slice id does not
    resolve, or the gateway reachability probe cannot be evaluated.
    Only a definitive "this cited commit is not on the branch" verdict
    fails the close. ``EGG_EVIDENCE_REACHABILITY_GATE`` is the operator
    kill switch.

    ``contract`` is an optional pre-loaded contract: the close path
    already needs the contract one stretch later for the slice PR data
    snapshot, so threading the same load through saves one file read
    and one ``get_pipeline_state_lock`` acquisition. When ``None``
    (the default — keeps the gate self-contained for tests), the gate
    loads the contract itself under the lock.
    """
    try:
        import contract_completeness as cc
    except ImportError:
        from .. import contract_completeness as cc  # type: ignore[no-redef]

    if not cc.evidence_gate_enabled():
        _pkg.logger.info(
            "Evidence-reachability gate disabled by kill switch (#3125)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )
        return None

    if contract is None:
        from egg_contracts.loader import load_contract as _load_contract

        try:
            with _pkg.get_pipeline_state_lock(pipeline_id):
                contract = _load_contract(pipeline_id, worktree_repo_path)
        except Exception as load_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Evidence-reachability gate skipped: contract load failed (#3125)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(load_err),
            )
            return None

    rows = cc.evidence_commits(contract, slice_id)
    if rows is None:
        _pkg.logger.warning(
            "Evidence-reachability gate skipped: slice not found in contract (#3125)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )
        return None
    if not rows:
        return None

    # De-duplicate while preserving first-seen order: multiple task rows
    # can cite the same commit (the prescribed unblock flow #3124 often
    # links one commit across two adjacent rows). Each duplicate would
    # otherwise burn one merge-base round-trip per dupe. The membership
    # join below re-attaches the verdict to every row that cites it.
    probe_shas = list(dict.fromkeys(r["commit"] for r in rows))
    unreachable_shas = spawner.gateway.find_unreachable_evidence_commits(
        pipeline_id,
        str(worktree_repo_path),
        commit_shas=probe_shas,
        integration_branch=integration_branch,
        mode=gateway_mode,
    )
    if unreachable_shas is None:
        # The probe itself could not be evaluated (gateway/network).
        # find_unreachable_evidence_commits already logged the cause.
        return None
    if not unreachable_shas:
        return None

    lost = [r for r in rows if r["commit"] in set(unreachable_shas)]
    summary = cc.format_evidence_rows(lost)
    _pkg.logger.error(
        "Slice close blocked: task records cite commits unreachable from "
        "the integration branch (#3125)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        integration_branch=integration_branch,
        unreachable=summary,
    )
    return (
        f"slice {slice_id}: evidence-reachability gate failed — contract task "
        f"records cite commits that are not on integration branch "
        f"{integration_branch}: {summary}. Cherry-pick (or push) the cited "
        f"commits onto {integration_branch}, then re-run the slice close; "
        f"set {cc.EVIDENCE_GATE_ENV_VAR}=off to bypass."
    )


def _check_slice_base_ancestry(
    pipeline_id: str,
    spawner: "ContainerSpawner",  # noqa: UP037
    worktree_repo_path: _pkg.Path,
    slice_id: str,
    integration_branch: str,
    *,
    issue_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
    max_parallel_slices: int = 1,
    contract: _pkg.Any | None = None,
) -> str | None:
    """Verify the new slice's base contains completed predecessors' commits (#3541).

    Slice-admission counterpart of the slice-close evidence gate
    (#3125). Runs right after ``create_slice_integration_branch`` — the
    branch tip still equals the fork base, so probing the branch probes
    the base — and before any agent is spawned. It asserts the
    invariant the #3541 orphaning violated: every commit SHA the
    contract records as evidence on already-COMPLETE slices that this
    slice is supposed to build on must be an ancestor of the new
    slice's base. A base that silently excludes reviewed,
    consensus-approved work must fail loudly here, not surface as a
    missing deliverable slices later (in pipeline issue-3523 it was
    only caught by the slice-8 documenter, seven slices downstream).

    Scope: under serialized execution (``max_parallel_slices == 1``,
    the default) root linearization guarantees the base covers EVERY
    completed slice, so all of them are gated. With genuine slice
    concurrency (cap > 1) sibling chains may legitimately complete
    without being ancestors of this slice's base, so the gate narrows
    to the slices on this slice's own fork chain (walked via
    ``parent_branch_at_creation`` / ``dependencies``) — the set the
    topology actually promises.

    Returns ``None`` when the slice may spawn, or a human-readable
    failure string; the caller records the slice failure with it,
    routing through the existing cascade + HITL escalation machinery.

    Failure posture mirrors #3125: the gate degrades to ``None``
    (spawn proceeds, warning logged) when the contract cannot be read
    or the gateway reachability probe cannot be evaluated. Only a
    definitive "this completed commit is not on the base" verdict
    fails the admission. ``EGG_SLICE_BASE_ANCESTRY_GATE`` is the
    operator kill switch (needed e.g. when an operator squash-merge
    legitimately rewrote a completed slice's SHAs).
    """
    try:
        import contract_completeness as cc
    except ImportError:
        from .. import contract_completeness as cc  # type: ignore[no-redef]

    if not cc.base_ancestry_gate_enabled():
        _pkg.logger.info(
            "Base-ancestry gate disabled by kill switch (#3541)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )
        return None

    if contract is None:
        from egg_contracts.loader import load_contract as _load_contract

        try:
            with _pkg.get_pipeline_state_lock(pipeline_id):
                contract = _load_contract(pipeline_id, worktree_repo_path)
        except Exception as load_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Base-ancestry gate skipped: contract load failed (#3541)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(load_err),
            )
            return None

    from egg_contracts.models import SliceStatus

    slices = list(getattr(contract, "slices", []) or [])
    all_ids = {s.id for s in slices}
    completed = {
        s.id: s
        for s in slices
        if s.id != slice_id and getattr(s, "status", None) == SliceStatus.COMPLETE
    }
    if not completed:
        return None

    if max_parallel_slices <= 1:
        predecessors = list(completed.values())
    else:
        by_id = {s.id: s for s in slices}
        predecessors = []
        cursor = by_id.get(slice_id)
        seen: set[str] = set()
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            parent_id = _slice_linear_parent_id(
                cursor, issue_branch=issue_branch, known_ids=all_ids
            )
            cursor = by_id.get(parent_id) if parent_id else None
            if cursor is not None and cursor.id in completed:
                predecessors.append(cursor)
    if not predecessors:
        return None

    rows: list[dict[str, _pkg.Any]] = []
    for predecessor in predecessors:
        rows.extend(cc.evidence_commits(contract, predecessor.id) or [])
    if not rows:
        return None

    probe_shas = list(dict.fromkeys(r["commit"] for r in rows))
    unreachable_shas = spawner.gateway.find_unreachable_evidence_commits(
        pipeline_id,
        str(worktree_repo_path),
        commit_shas=probe_shas,
        integration_branch=integration_branch,
        mode=gateway_mode,
    )
    if unreachable_shas is None:
        # The probe itself could not be evaluated (gateway/network).
        # find_unreachable_evidence_commits already logged the cause.
        return None
    if not unreachable_shas:
        return None

    lost = [r for r in rows if r["commit"] in set(unreachable_shas)]
    summary = cc.format_evidence_rows(lost)
    _pkg.logger.error(
        "Slice admission blocked: base excludes commits recorded complete "
        "on predecessor slices (#3541)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        integration_branch=integration_branch,
        unreachable=summary,
    )
    return (
        f"slice {slice_id}: base-ancestry gate failed (#3541) — the slice's "
        f"integration branch {integration_branch} was forked from a base "
        f"that does not contain commits the contract records as complete "
        f"on predecessor slices: {summary}. The completed work would be "
        f"orphaned from this slice and every descendant. Re-point the "
        f"slice's base (or merge/cherry-pick the missing commits onto it), "
        f"then respawn; set {cc.BASE_ANCESTRY_GATE_ENV_VAR}=off to bypass."
    )
