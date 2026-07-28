"""live-pod guarding helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401

# Budget for observing reaped slice Jobs actually gone (#3685). Job
# deletion is asynchronous even with ``force=True``, so the reap must
# wait for the teardown it requested before the run loop admits the
# slice — otherwise the fresh cohort's Job attaches to a worktree the
# orphan still holds. Mirrors ``_routes_restart._JOB_TEARDOWN_WAIT_SECONDS``
# (20.0) and ``kubernetes_spawner._EVENT_JOB_TERMINATION_WAIT_S`` (15.0);
# the deadline is shared across every Job reaped for one slice so a
# pathological cohort cannot stall the bootstrap pass unboundedly.
_REAP_TEARDOWN_WAIT_SECONDS = 20.0


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


def _await_reaped_jobs_gone(
    spawner: _pkg.Any,
    pipeline_id: str,
    slice_id: str,
    job_names: list[str],
) -> bool:
    """Block until the reaped Jobs are observed gone, bounded (#3685).

    ``remove_agent_container(force=True)`` only orders a foreground
    delete: ``KubernetesClient.remove_container`` says so in its own
    docstring — "the GC deletes the pods asynchronously — they are not
    guaranteed gone by the time the call returns". Returning at that
    point and immediately making the slice admissible re-opens the very
    window the reap exists to close: the orphan is still ``Running``
    inside its termination grace period with the role worktree mounted
    when the fresh cohort's Job attaches to the same checkout and
    ``_clean_reused_worktree`` runs ``git reset --hard && git clean -fd``
    under it (#3337).

    The spawn-side pre-spawn wait (``kubernetes_spawner._events``) does
    not cover this: it selects on ``LABEL_EVENT_DEDUPE={dedupe_key}``,
    and the orphan's key is not necessarily the fresh loop's first key
    (see ``_reap_orphaned_slice_jobs``). So the wait has to happen here,
    on the same "wait for the teardown you requested to be OBSERVED"
    contract ``restart_agent`` uses (#3597).

    Returns ``True`` only when every named Job was observed gone.
    ``False`` means "not observed", never "the teardown failed" — it
    under-claims by design, and each unobserved Job is logged so an
    operator can see that the slice was admitted with the window
    potentially still open.
    """
    k8s = getattr(spawner, "k8s", None)
    waiter = getattr(k8s, "wait_for_job_gone", None)
    namespace = getattr(k8s, "namespace", None)
    if waiter is None or not namespace:
        # A backend without the wait helper (or without a namespace) is
        # a single backend-capability fact, not N per-Job failures, so
        # it earns one log line. Defensive: ``KubernetesClient``
        # implements ``wait_for_job_gone``, and ``ContainerSpawner`` is
        # an alias of ``KubernetesSpawner``, so this is unreachable
        # against the production spawner.
        _pkg.logger.warning(
            "Orphaned-Job reap could not observe teardown; slice admitted "
            "with the worktree-handoff window potentially open (#3685)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            jobs=len(job_names),
            reason="no_wait_helper",
        )
        return False

    confirmed = True
    deadline = _pkg.time.monotonic() + _REAP_TEARDOWN_WAIT_SECONDS
    for name in job_names:
        remaining = deadline - _pkg.time.monotonic()
        if remaining <= 0:
            confirmed = False
            _pkg.logger.warning(
                "Orphaned-Job reap could not observe teardown; slice admitted "
                "with the worktree-handoff window potentially open (#3685)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                job_name=name,
                reason="budget_exhausted",
            )
            continue
        try:
            gone = bool(waiter(name, namespace, timeout_s=remaining))
        except Exception as wait_err:  # noqa: BLE001 — the wait is best-effort
            confirmed = False
            _pkg.logger.warning(
                "Orphaned-Job reap teardown wait raised; treating the teardown "
                "as unconfirmed (#3685)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                job_name=name,
                error=str(wait_err),
            )
            continue
        if not gone:
            confirmed = False
            _pkg.logger.warning(
                "Orphaned slice Job still terminating after the teardown wait; "
                "the re-driven cohort may attach to a worktree the orphan "
                "still holds (#3685)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                job_name=name,
            )
    return confirmed


def _reap_orphaned_slice_jobs(spawner: _pkg.Any, pipeline_id: str, slice_id: str) -> list[str]:
    """Tear down agent Jobs that outlived their event loop (#3685).

    Layer-C bootstrap re-drives every non-COMPLETE slice through the run
    loop, and the fresh cohort's one-shot event Jobs re-attach to the
    per-role worktrees. Any Job still live at that point belongs to a
    BRC event loop that no longer exists (the loop is process-local and
    died with the previous orchestrator process, or with the previous
    driver thread), so nothing will ever observe its termination or
    re-derive its next event. Left in place it holds the role's worktree
    while the new cohort's Job attaches to the same checkout — the #3337
    two-live-pods race.

    Why the spawner's live-key adoption is not enough to collapse that
    duplicate: ``compute_dedupe_key`` IS deterministic across
    orchestrator restarts, so a matching key is possible — but only when
    the fresh loop happens to derive the same ``event_identity``, and it
    derives that from the tracker ``spawn_all`` registers, which is a
    fresh zeroed one superseding the reconstructed one (#2409). Where
    the orphan was mid-round its key differs, adoption cannot see it,
    and two pods run against one worktree. This process cannot tell the
    two cases apart — it has no view of the dead loop's session or
    round state — so it reaps unconditionally and accepts that a
    matching-key orphan is killed slightly early, costing one replayed
    round. That is the strictly safer direction: a redundant delete is
    recoverable, a clobbered worktree is not.

    Reaping is foreground (``force=True``) and then WAITED ON: the
    delete is asynchronous, so ``_await_reaped_jobs_gone`` observes the
    teardown (bounded by :data:`_REAP_TEARDOWN_WAIT_SECONDS`) before the
    caller makes the slice admissible. This is deliberately stricter
    than ``restart_phase`` step 4, whose own teardown is unwaited — step
    4 goes on to DELETE the per-agent worktrees (step 4b, with salvage),
    so nothing is left for a lingering pod to corrupt. Here the
    worktrees are deliberately kept warm for the re-driven slice, which
    is exactly what makes the observed teardown load-bearing.

    Returns the handles reaped — the Job name where the listing supplied
    one, else the container id — empty when the slice has no live Jobs
    (the common case after a full pod recycle). Entirely best-effort: a
    failed label query or a failed removal is logged and swallowed,
    because a reap failure must never block recovery of a slice that is
    otherwise ready to re-drive. The ``spawner`` is taken as a parameter
    (rather than fetched via ``_get_spawner``) so tests can inject a
    stub directly, paralleling how ``_classify_non_complete_slice``
    receives ``gateway``.
    """
    try:
        # ``list_slice_jobs`` applies the same
        # ``{LABEL_PIPELINE_ID, LABEL_SLICE_ID}`` pair, so the label
        # scoping lives in one place rather than being re-derived here.
        pods = spawner.list_slice_jobs(pipeline_id, slice_id)
    except Exception as e:  # noqa: BLE001
        _pkg.logger.warning(
            "Orphaned-Job reap skipped: slice Job query failed (#3685)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(e),
        )
        return []

    reaped: list[str] = []
    pending_waits: list[str] = []
    unaddressable = 0
    for pod in pods:
        # Status-only liveness, unlike ``_job_is_live``, which also
        # excludes terminating Jobs via ``deletion_timestamp`` (#3597).
        # The asymmetry is deliberate, not an oversight:
        # ``KubernetesClient.list_containers`` never populates
        # ``deletion_timestamp``, and a Terminating pod still reports
        # phase ``Running``, so this filter cannot see the distinction.
        # It does not need to — the consequence is a redundant delete on
        # a Job already on its way out (plus an entry in the ``reaped=``
        # audit list), whereas on the adoption path treating a corpse as
        # live silently swallows a respawn. Waiting on it is correct
        # either way: an already-terminating Job is precisely one whose
        # worktree we must see released.
        if pod.status not in _pkg._LIVE_POD_STATUSES:
            continue
        job_name = getattr(pod, "job_name", None)
        handle = job_name or getattr(pod, "container_id", None)
        if not handle:
            continue
        try:
            # ``job_name`` first, matching ``cleanup_pipeline`` and
            # ``restart_agent``. ``remove_agent_container`` resolves a
            # Job name, Job UID or Pod UID equally well for the delete,
            # but it also forwards the handle to
            # ``delete_session_by_container``: event-mode gateway
            # sessions are keyed by the stable base ``container_id``
            # (not the per-event Job name), so that call only ever
            # reaches a legacy per-Job session here — the event-mode
            # session is released by the phase/pipeline-end
            # ``cleanup_pipeline`` → ``_teardown_session`` path.
            spawner.remove_agent_container(handle, force=True, cleanup_session=True)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to reap orphaned slice Job during bootstrap re-drive (#3685)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                container_id=handle,
                error=str(e),
            )
            continue
        reaped.append(handle)
        if isinstance(job_name, str) and job_name:
            pending_waits.append(job_name)
        else:
            # Without a Job name the only handle is a Pod UID, which
            # ``wait_for_job_gone`` would normalize into a Job name that
            # never existed — a 404 on the first read, reported as
            # "gone" without observing anything. Count it as unobserved
            # rather than claiming an observation we never made.
            unaddressable += 1

    if not reaped:
        return []

    teardown_confirmed = _await_reaped_jobs_gone(spawner, pipeline_id, slice_id, pending_waits)
    if unaddressable:
        teardown_confirmed = False
        _pkg.logger.warning(
            "Orphaned-Job reap could not observe teardown; slice admitted "
            "with the worktree-handoff window potentially open (#3685)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            jobs=unaddressable,
            reason="unaddressable",
        )
    _pkg.logger.info(
        "Reaped orphaned slice Jobs whose BRC event loop died with the "
        "previous orchestrator process (#3685)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        reaped=reaped,
        teardown_confirmed=teardown_confirmed,
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
