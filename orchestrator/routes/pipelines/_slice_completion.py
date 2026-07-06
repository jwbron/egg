"""slice-completion basis validation helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


class SliceCompletionInvariantError(RuntimeError):
    """Raised when a slice would be persisted ``COMPLETE`` without a valid
    completion basis (#3214).

    The #3214 wedge traced to an interior forest node (``slice-3`` on
    pipeline ``issue-3200``) persisted as ``SliceStatus.COMPLETE`` while
    its only task was still ``pending``, it had no integration branch, and
    it carried its *parent's* commit SHA. ``_persist_slice_status_complete``
    wrote that contradictory state with no validation, so the slice-DAG
    driver skipped real work and the chain wedged with no successor — and
    it hung ~9h silently because nothing failed loud at the moment of the
    bad write.

    A slice has a valid completion basis when ANY of these execution
    signals is present:

    * a slice PR is recorded / supplied (``pr_number``); or
    * the caller declares a verified ``basis`` — ``"merged"`` (the
      integration branch was ancestry-verified merged into its parent) or
      ``"consensus_complete"`` (BRC consensus reached, PR not yet opened
      or its URL unparseable); or
    * the slice forked an integration branch (``integration_base_sha`` is
      set — #2871); or
    * every task is ``TaskStatus.COMPLETE``.

    The predicate accepts any one signal so it can only flag the slice-3
    state where *all* are absent — a slice marked COMPLETE with zero
    evidence it ran. We raise here so that corrupt write fails loud at its
    source instead of wedging the forest a phase later.

    #3253 refinement: ``basis="merged"`` is no longer an unconditional
    pass. A merged slice went through a PR and left commits its producers
    recorded; a ``basis="merged"`` write with **no PR and no produced task
    commit** is an empty / never-implemented branch that origin ancestry
    mis-detected as merged (the slice-10 case — producers exhausted before
    committing, so the integration branch's tip is still its fork base and
    is trivially an ancestor of the advanced parent). Such a write is
    rejected so the slice is re-run rather than false-completed.
    """


def _slice_produced_commits(slice_obj: _pkg.Any) -> bool:
    """Return True iff any of the slice's tasks recorded a commit SHA.

    This is the base-SHA-independent "a producer actually committed work"
    signal (#3253). It reads *task* commits only — a slice whose producers
    all failed before committing has every ``task.commit`` ``None`` (the
    AC-4 measurement in the issue-3200 slice-10 incident). It deliberately
    ignores ``Slice.commit``: that field can carry the *parent's* SHA on a
    false-complete (the #3214 slice-3 carryover), so it is not trustworthy
    evidence the slice itself produced anything.

    An empty integration branch (tip still at its fork base, so trivially
    an ancestor of an advanced parent) is indistinguishable from a merged
    one by origin ancestry alone once the recorded fork base is missing or
    stale (#3245). The contract's task-commit record is the durable signal
    that survives that ambiguity: no task commit + no slice PR ⇒ the slice
    never ran and must be re-run, not completed.

    A slice with *no tasks* returns ``False`` here (``any([])``). Paired with
    "origin-detected merged, no PR" that would force such a slice to re-run
    indefinitely — but a zero-task slice is unreachable in practice:
    plan-derived slices always carry at least one task. The safe direction is
    re-run over silently-dropped work, so the edge needs no special-casing
    (#3253).
    """
    tasks = getattr(slice_obj, "tasks", None) or []
    return any(getattr(t, "commit", None) for t in tasks)


def _validate_slice_completion_basis(
    slice_obj: _pkg.Any,
    *,
    pr_number: int | None = None,
    basis: str | None = None,
) -> str | None:
    """Return ``None`` when ``slice_obj`` may legitimately be marked
    ``SliceStatus.COMPLETE``, else a human-readable reason it may not.

    Shared by the write chokepoint (``_persist_slice_status_complete``,
    which raises :class:`SliceCompletionInvariantError` on a reason) and
    the Layer-A bootstrap read-trust point (which alerts and declines to
    trust a contradictory contract-recorded COMPLETE rather than
    propagating it into the scheduler). See
    :class:`SliceCompletionInvariantError` for the basis rules (#3214).
    """
    has_pr = pr_number is not None or getattr(slice_obj, "pr_number", None) is not None
    # #3253 — a ``basis="merged"`` slice with no PR and no produced task
    # commits is not a merged slice; it is an empty / never-implemented
    # integration branch (tip still at its fork base) that origin ancestry
    # mis-detected as merged. A genuine merge went through a PR and left
    # commits the producers recorded. Reject so the restart re-runs the
    # slice instead of false-completing the pipeline with its work missing.
    # This guard fires *before* the verified-basis / forked free-passes
    # below so a recorded (possibly stale) fork base cannot rescue it.
    if basis == "merged" and not has_pr and not _pkg._slice_produced_commits(slice_obj):
        return (
            f"slice {getattr(slice_obj, 'id', '?')} would be marked COMPLETE "
            f"basis='merged' with no slice PR and no produced task commits — an "
            f"empty / never-implemented integration branch is not a merged one "
            f"(#3253)"
        )
    verified_basis = basis in _pkg._VERIFIED_SLICE_COMPLETION_BASES
    # A slice that actually forked its integration branch recorded a base
    # SHA (#2871). Its absence — together with no PR, no verified basis,
    # and no completed tasks — is the slice-3 false-complete signature: a
    # slice marked COMPLETE with zero evidence it ever ran. The predicate
    # accepts ANY single execution signal so it can only flag that
    # genuinely-contradictory state, never a legitimately-completed slice
    # whose other signals happen to be absent (e.g. an unparseable PR URL
    # leaves ``pr_number`` None but the slice still forked and reached
    # consensus). ``tasks_all_complete`` is the canonical model-side
    # predicate so this can't drift from the contract's own notion of
    # "work finished".
    forked = getattr(slice_obj, "integration_base_sha", None) is not None
    if has_pr or verified_basis or forked or slice_obj.tasks_all_complete:
        return None
    return (
        f"slice {getattr(slice_obj, 'id', '?')} would be marked COMPLETE with no "
        f"evidence it ran: no slice PR, no verified merge/consensus basis "
        f"(basis={basis!r}), no integration-branch fork base, and tasks not all "
        f"complete"
    )
