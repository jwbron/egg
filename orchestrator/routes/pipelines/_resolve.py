"""pipeline resolution + identifiers + event emit helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _ensure_pipeline_work_ref(branch: str | None) -> str | None:
    """Return the actual remote ref for an orchestrator-managed pipeline branch.

    The orchestrator pushes the pipeline tip to ``<branch>/work`` so the
    ``<branch>/`` namespace can hold slice integration branches as
    siblings (``<branch>/slice-N``) without git's ``directory file
    conflict`` rejection — see #2399. A leaf ref at ``<branch>`` and a
    child at ``<branch>/slice-N`` cannot coexist on origin, so the
    pipeline tip is moved one level deeper into the namespace.

    Idempotent and bounded to ``egg/<id>``-shaped branches:

    * ``None`` → ``None`` (prompt-driven; the caller generates a
      ``/work``-shaped branch later).
    * ``egg/<id>`` → ``egg/<id>/work`` (issue submissions).
    * ``egg/<id>/work`` → unchanged (resubmission, internal callers).
    * non-``egg/`` (passed unchanged) — a pipeline pointed at a foreign
      branch (e.g. ``feature/foo``). Slices on a non-``egg/`` branch are
      not a guaranteed-safe shape and are intentionally not normalised
      here — the conflict would resurface at the slice push and is
      tracked separately.

    The trailing-``/work`` check is structural rather than a plain
    suffix match (``branch.count("/") >= 2 and branch.rsplit("/", 1)[1]
    == "work"``) so a degenerate input like ``egg/work`` — a single
    segment that *happens* to end in ``/work`` — gets normalised to
    ``egg/work/work`` (siblings ``egg/work/slice-N``) rather than
    treated as already-normalised. Trailing slashes are stripped first
    so ``egg/`` does not collapse to a double-slash ``egg//work``.
    """
    if branch is None:
        return None
    branch = branch.rstrip("/")
    if not branch.startswith("egg/"):
        return branch
    # Structural check: only treat ``egg/<id>/work`` (≥2 slashes, last
    # segment is ``work``) as already-normalised. ``egg/work`` looks
    # like a suffix match but is a single-segment id and still needs the
    # ``/work`` namespace deepening.
    if branch.count("/") >= 2 and branch.rsplit("/", 1)[1] == "work":
        return branch
    return f"{branch}/work"


def _slice_namespace_root(pipeline_branch: str) -> str:
    """Return the slice-integration-branch namespace root for a pipeline branch.

    Slice integration branches live as siblings of the pipeline tip
    under ``egg/<id>/`` (see :func:`_ensure_pipeline_work_ref`). The
    namespace root is the pipeline branch with the trailing ``/work``
    stripped — that's the prefix slice paths (``<root>/slice-N``) are
    built from. For legacy / non-normalised branches that do not end in
    ``/work``, the branch itself is the root.

    The trailing-``/work`` check mirrors the structural check in
    :func:`_ensure_pipeline_work_ref` (≥2 slashes, last segment is
    ``work``) so a degenerate single-segment input like ``egg/work``
    is treated as the root itself rather than collapsing to ``egg``.
    """
    if pipeline_branch.count("/") >= 2 and pipeline_branch.rsplit("/", 1)[1] == "work":
        return pipeline_branch.rsplit("/", 1)[0]
    return pipeline_branch


def _pipeline_identifier(
    issue_number: int | None,
    pipeline_id: str,
) -> int | str:
    """Derive the pipeline identifier used for namespaced .egg-state filenames.

    Prefers ``issue_number`` when available, falling back to ``pipeline_id``.

    A pipeline whose id carries a qualifier beyond the bare ``issue-<N>``
    form (e.g. ``issue-1557-v2`` for a versioned re-run) keys by
    ``pipeline_id`` instead, so concurrent pipelines on the same issue
    don't collide on ``.egg-state/drafts/<N>-analysis.md``.
    """
    if pipeline_id and issue_number is not None:
        expected_issue_prefix = f"issue-{issue_number}"
        if pipeline_id.startswith(expected_issue_prefix + "-"):
            # A qualifier is present beyond the bare ``issue-<N>`` form;
            # key by pipeline_id so concurrent runs on the same issue do
            # not collide on draft files.
            return pipeline_id
    return issue_number if issue_number is not None else pipeline_id


def _brc_history_identifier(pipeline) -> int | str:
    """Return the identifier used to namespace BRC-history artifacts.

    Mirrors :func:`_pipeline_identifier` (favouring the issue number).
    """
    return _pkg._pipeline_identifier(
        getattr(pipeline, "issue_number", None),
        getattr(pipeline, "id", "") or "",
    )


def _emit_pipeline_event(
    pipeline: _pkg.Pipeline,
    event_type_str: str,
) -> None:
    """Emit a pipeline event to the EventBus for SSE streaming."""
    if _pkg._emit_event is None:
        return
    mapped = _pkg._EVENT_TYPE_MAP.get(event_type_str)
    if mapped is None:
        return
    _pkg._emit_event(
        mapped,
        pipeline.id,
        data={
            "status": pipeline.status.value,
            "phase": pipeline.current_phase.value,
        },
    )


def _resolve_pipeline(
    pipeline_id: str, base_path: _pkg.Path
) -> tuple[_pkg.StateStore, _pkg.Pipeline]:
    """Load a pipeline, resolving the correct repo subdirectory.

    Each repo has its own state store and worktree.  This function
    searches all repos under ``base_path`` to find the pipeline.

    Returns:
        (store, pipeline) tuple

    Raises:
        PipelineNotFoundError: if the pipeline cannot be found anywhere
        InvalidPipelineIdError: if the ID format is invalid
        GitOperationError: if the state-store worktree cannot be loaded
            (e.g. ``git worktree add`` contention).  Callers should
            surface this as 500, not 404 — it is recoverable
            infrastructure failure, not a missing pipeline.
    """
    from state_store import discover_repo_paths

    for repo_path in discover_repo_paths(base_path):
        try:
            store = _pkg.get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            return store, pipeline
        except _pkg.PipelineNotFoundError:
            continue
        # NOTE: do NOT broaden this to ``StateStoreError``.  Swallowing
        # ``GitOperationError`` here re-raised every state-store wedge
        # as ``Pipeline not found`` and surfaced to operators as 404,
        # masking a recoverable git contention as a missing pipeline
        # (#2167).  Let infrastructure failures propagate so the route
        # can return 500 with the actual error.

    raise _pkg.PipelineNotFoundError(f"Pipeline {pipeline_id} not found") from None


def _collect_all_pipelines(base_path: _pkg.Path) -> list:
    """Collect pipelines from all git repos under base_path.

    Each repo has its own state store and worktree. Pipelines are
    deduplicated by ID in case of overlapping stores.
    """
    from state_store import discover_repo_paths

    seen: set[str] = set()
    pipelines = []

    def _add_from_store(store):
        for pid in store.list_pipelines():
            if pid in seen:
                continue
            try:
                pipelines.append(store.load_pipeline(pid))
                seen.add(pid)
            except _pkg.StateStoreError:
                continue

    for repo_path in discover_repo_paths(base_path):
        try:
            _add_from_store(_pkg.get_state_store(repo_path))
        except _pkg.StateStoreError:
            continue

    return pipelines
