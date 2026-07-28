"""live-pod guarding helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _get_spawner():
    """Get the appropriate spawner for the current runtime.

    Returns KubernetesSpawner when EGG_RUNTIME=kubernetes, otherwise
    ContainerSpawner (Docker).
    """
    if _pkg._RUNTIME == "kubernetes":
        return _pkg.get_kubernetes_spawner()
    return _pkg.get_container_spawner()


def _count_live_pods_for_pipeline(pipeline_id: str, *, quiet: bool = False) -> int | None:
    """Count live pods labeled to this pipeline (#2420).

    Live = ``ContainerStatus`` in :data:`_LIVE_POD_STATUSES` (Pending /
    Creating / Running). Pods in terminal phases (``Failed`` / ``Succeeded``
    → ``ContainerStatus.FAILED`` / ``EXITED``) are excluded — they have
    already exited and the start_pipeline reset orphans no work tied to
    them.

    Returns the number of live pods, or ``None`` if the label query failed —
    callers must distinguish "verified zero" from "unknown" because the
    start_pipeline reset would orphan any pods we couldn't see.

    ``quiet=True`` suppresses the helper-level warning when the label query
    fails. The guard's ``force=true`` branch passes this flag because it
    emits its own structured audit log on the ``live is None`` path; the
    helper's warning would just duplicate it.
    """
    try:
        spawner = _pkg._get_spawner()
        pods = spawner.backend.list_containers(
            labels={_pkg.LABEL_PIPELINE_ID: pipeline_id},
        )
        return sum(1 for p in pods if p.status in _pkg._LIVE_POD_STATUSES)
    except Exception as e:
        if not quiet:
            _pkg.logger.warning(
                "start_pipeline live-pod check failed",
                pipeline_id=pipeline_id,
                error=str(e),
            )
        return None


def _live_event_agents(pipeline_id: str, slice_id: str | None) -> list[dict[str, _pkg.Any]]:
    """Running-agent view reconstructed from live Job labels (#3230).

    Under the orchestrator-owned BRC event loop (#3164, now unconditional)
    each role's pod is an on-demand one-shot the loop deliberately does NOT
    persist into ``phase_exec.agents`` — ``event_loop.py`` treats the
    consensus tracker plus live-Job labels as the only sources of truth. So
    the persisted agent list is empty even while role pods are ``Running``,
    which the dashboard (``get_status.running_agents``) and the overseer
    (``concurrent.agents`` stall-duration math) both read as "0 running
    agents" — a blind dashboard and false ``phase stalled`` alerts.

    This reconstructs the running-pod cohort from the labels that ARE
    authoritative. Live = ``status`` in :data:`_LIVE_POD_STATUSES`
    (Pending / Creating / Running); terminal pods lingering in the
    ``ttlSecondsAfterFinished`` window are excluded so between-spawn
    quiescence reads as "no running agents" (the normal idle state, not a
    stall). Scoped to ``slice_id`` when supplied so a slice-DAG implement
    phase reports its own slice's pods rather than a cross-slice union;
    refine/plan phases are unsliced and query by pipeline label alone.

    Entry shape mirrors the persisted ``agents`` entries (``role`` /
    ``status`` / ``started_at`` / ``elapsed_seconds`` / ``container_id``)
    so consumers need no special-casing. ``status`` is reported as
    ``"running"`` for every live pod — Pending/Creating pods are agents
    spinning up, and the dashboard's running-agent filter keys on that
    literal.

    Best-effort: an absent/failed label query yields ``[]`` (callers treat
    that identically to "no persisted agents", so there is no regression
    versus the pre-fix behavior).
    """
    try:
        spawner = _pkg._get_spawner()
        labels = {_pkg.LABEL_PIPELINE_ID: pipeline_id}
        if slice_id:
            labels[_pkg.LABEL_SLICE_ID] = slice_id
        pods = spawner.backend.list_containers(labels=labels)
    except Exception as e:  # noqa: BLE001 — observability backfill is best-effort
        _pkg.logger.debug(
            "Live event-agent backfill query failed (#3230)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(e),
        )
        return []

    now = _pkg.datetime.now(_pkg.UTC)
    entries: list[dict[str, _pkg.Any]] = []
    for pod in pods:
        if pod.status not in _pkg._LIVE_POD_STATUSES:
            continue
        role = pod.agent_role.value if pod.agent_role is not None else None
        if not role:
            continue
        entry: dict[str, _pkg.Any] = {"role": role, "status": "running"}
        if isinstance(pod.container_id, str) and pod.container_id:
            entry["container_id"] = pod.container_id
        started_at = pod.started_at
        if isinstance(started_at, _pkg.datetime):
            started_dt = started_at if started_at.tzinfo else started_at.replace(tzinfo=_pkg.UTC)
            entry["started_at"] = started_dt.isoformat()
            entry["elapsed_seconds"] = max(0, int((now - started_dt).total_seconds()))
        entries.append(entry)
    return entries


def _reap_orphaned_slice_jobs(spawner: _pkg.Any, pipeline_id: str, slice_id: str) -> list[str]:
    """Tear down agent Jobs that outlived their event loop (#3685).

    Layer-C bootstrap re-drives every non-COMPLETE slice through the run
    loop, and the fresh cohort's one-shot event Jobs re-attach to the
    per-role worktrees. Any Job still live at that point belongs to a
    BRC event loop that no longer exists (the loop is process-local and
    died with the previous orchestrator process, or with the previous
    driver thread), so nothing will ever observe its termination or
    re-derive its next event. Left in place it holds the role's worktree
    while the new cohort's Job attaches to the same checkout (the #3337
    two-live-pods race), and the spawner's live-key adoption cannot
    collapse the duplicate because the orphan's dedupe key was derived
    against a tracker this process does not have.

    Reaping is foreground (``force=True``) so the pod is gone rather
    than merely terminating before the run loop admits the slice,
    matching ``restart_phase`` step 4's teardown contract. Per-agent
    worktrees are deliberately NOT deleted: the re-driven slice wants
    them warm, and the respawned agent's own dirty-state clean covers
    the handoff.

    Returns the container ids reaped (empty when the slice has no live
    Jobs, which is the common case after a full pod recycle). Entirely
    best-effort: a failed label query or a failed removal is logged and
    swallowed, because a reap failure must never block recovery of a
    slice that is otherwise ready to re-drive. The ``spawner`` is taken
    as a parameter (rather than fetched via ``_get_spawner``) so tests
    can inject a stub directly, paralleling how
    ``_classify_non_complete_slice`` receives ``gateway``.
    """
    try:
        pods = spawner.backend.list_containers(
            labels={
                _pkg.LABEL_PIPELINE_ID: pipeline_id,
                _pkg.LABEL_SLICE_ID: slice_id,
            },
        )
    except Exception as e:  # noqa: BLE001
        _pkg.logger.warning(
            "Orphaned-Job reap skipped: slice Job query failed (#3685)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(e),
        )
        return []

    reaped: list[str] = []
    for pod in pods:
        if pod.status not in _pkg._LIVE_POD_STATUSES:
            continue
        container_id = getattr(pod, "container_id", None)
        if not container_id:
            continue
        try:
            spawner.remove_agent_container(container_id, force=True, cleanup_session=True)
            reaped.append(container_id)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to reap orphaned slice Job during bootstrap re-drive (#3685)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                container_id=container_id,
                error=str(e),
            )
    if reaped:
        _pkg.logger.info(
            "Reaped orphaned slice Jobs whose BRC event loop died with the "
            "previous orchestrator process (#3685)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            reaped=reaped,
        )
    return reaped


def _guard_live_pods_or_force(
    pipeline_id: str,
    force: bool,
    force_reason: str | None,
) -> tuple[_pkg.Response, int] | None:
    """Refuse a phase reset that would orphan live pods (#2420).

    Returns ``None`` when the reset is safe to proceed (zero live pods, or
    ``force=true``). Returns a 409 ``(response, status)`` when live pods are
    present (or the label query failed) and the caller did not pass
    ``force=true``.
    """
    if force:
        # ``quiet=True`` because the ``live is None`` branch below emits
        # its own structured audit log; the helper-level warning would
        # just duplicate it on the override path.
        live = _pkg._count_live_pods_for_pipeline(pipeline_id, quiet=True)
        # Template the audit log so the static message reflects what the
        # override actually did. ``live == 0`` means the override was a
        # no-op — log at ``info`` so it doesn't read like a near-miss.
        if live is None:
            _pkg.logger.warning(
                "start_pipeline force=true override; live-pod check failed, "
                "phase reset will proceed regardless",
                pipeline_id=pipeline_id,
                live_pod_count=None,
                force_reason=force_reason,
            )
        elif live > 0:
            _pkg.logger.warning(
                "start_pipeline force=true override; phase reset will proceed "
                "and orphan live pods labeled to the pipeline",
                pipeline_id=pipeline_id,
                live_pod_count=live,
                force_reason=force_reason,
            )
        else:
            _pkg.logger.info(
                "start_pipeline force=true override applied (no live pods present)",
                pipeline_id=pipeline_id,
                live_pod_count=0,
                force_reason=force_reason,
            )
        return None

    live = _pkg._count_live_pods_for_pipeline(pipeline_id)
    if live is None:
        return _pkg.make_error_response(
            f"Could not verify live pod count for pipeline {pipeline_id}; "
            "the start_pipeline reset would orphan any pods labeled to it. "
            "Cancel them first via cancel_task(cleanup=true) or pass "
            "force=true to override.",
            status_code=409,
            reason="live_pod_check_failed",
        )
    if live > 0:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} has {live} live pod(s); the "
            "start_pipeline reset would orphan them. Cancel them first via "
            "cancel_task(cleanup=true) or pass force=true to override.",
            status_code=409,
            details={"live_pod_count": live},
            reason="live_pods_present",
        )
    return None
