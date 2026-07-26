"""pipeline-driver lifecycle helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _spawn_pipeline_run_thread(
    pipeline_id: str,
    repo_path: _pkg.Path,
    run_epoch: _pkg.datetime,
) -> _pkg.threading.Thread:
    """Spawn a fresh ``_run_pipeline`` driver thread.

    Callers (all use the ``pipeline-{id}-{epoch}`` naming scheme):

    - ``advance_phase`` (manual phase advance via REST)
    - ``restart_phase`` (manual phase restart via REST)
    - the auto-advance block in ``_run_pipeline`` (#2165)

    The other ``_run_pipeline`` thread spawn sites — ``start_pipeline``'s
    initial-spawn and AWAITING_HUMAN-recovery paths, plus the spurious-PNFE
    respawn inside ``_run_pipeline`` — use different naming or take extra
    kwargs (e.g. ``_respawn_attempt``) and are deliberately left inline.

    Without a fresh thread per phase, a mid-execution exception in the new
    phase's first iteration takes down the whole pipeline (#2165).
    """
    thread = _pkg.threading.Thread(
        target=_pkg._run_pipeline,
        args=(pipeline_id, repo_path),
        daemon=True,
        name=f"pipeline-{pipeline_id}-{int(run_epoch.timestamp())}",
    )
    thread.start()
    return thread


def has_live_pipeline_driver(pipeline_id: str) -> bool:
    """Return True if a live ``_run_pipeline`` driver thread owns this pipeline.

    Driver threads are named ``pipeline-{id}`` (``start_pipeline``'s initial
    and AWAITING_HUMAN-recovery spawns), ``pipeline-{id}-{epoch}``
    (``_spawn_pipeline_run_thread``), or ``pipeline-{id}-respawn-...`` (the
    spurious-PNFE recovery).  Every variant is either exactly ``pipeline-{id}``
    or carries a ``pipeline-{id}-`` prefix, so the literal-hyphen boundary
    keeps a pipeline whose id is a prefix of another (``issue-3`` vs
    ``issue-32``) from matching.

    After an orchestrator restart the process holds no driver threads, which
    is precisely the orphaned-parked condition behind #3233: a pipeline left
    AWAITING_HUMAN with a pending decision has no thread polling
    ``wait_for_decision``, so a later resolution is recorded with no consumer
    and the pipeline hangs silently.
    """
    exact = f"pipeline-{pipeline_id}"
    prefix = exact + "-"
    for t in _pkg.threading.enumerate():
        if not t.is_alive():
            continue
        if t.name == exact or t.name.startswith(prefix):
            return True
    return False


def relaunch_driverless_running_pipelines(store) -> int:
    """Relaunch drivers for RUNNING pipelines orphaned by a restart (#3469).

    Called once per repo store at orchestrator startup, after
    ``startup_reconciliation.reconcile_stale_containers`` has settled each
    pipeline's status.  A pipeline still RUNNING at that point was mid-flight
    when the previous orchestrator process died: its consensus state is fully
    reconciled at boot, but its ``_run_pipeline`` driver thread — and the BRC
    event loop the driver owns — died with the old process, and no other code
    path revives it.  ``restart_agent`` delegates the respawn to the (dead)
    event loop and returns success, while ``start_pipeline`` rejects
    status=RUNNING with a 409, so without this sweep the pipeline is
    permanently driverless and never spawns another pod (#3469).

    Relaunching reuses the proven resume path (the same one
    ``restart_agent``'s inactive-pipeline branch relies on, #3244):
    ``_run_pipeline`` re-enters ``pipeline.current_phase``, re-syncs the
    worktree with the remote, and restarts the event loop, which respawns
    one-shot agent Jobs within one poll.  The persisted ``run_epoch`` is
    deliberately NOT bumped: the old process is gone so no stale thread can
    contend for the epoch, and the relaunched thread derives its own epoch
    from persisted state exactly as the original did.

    AWAITING_HUMAN pipelines are out of scope — their drivers are revived on
    decision resolution by ``maybe_revive_orphaned_awaiting_human_driver``
    (#3233).

    The sweep iterates ``store.get_active_pipelines()`` rather than the full
    ``list_pipelines()`` so terminal/historical records (COMPLETE, FAILED,
    CANCELLED) are skipped without a redundant load — reconciliation already
    walked every pipeline immediately before this, and re-scanning the whole
    store would double the boot-time git reads on repos with many historical
    pipelines.

    Returns the number of drivers relaunched.  Failures are isolated at two
    layers: a record that fails to load with ``StateStoreError`` (corruption)
    is skipped inside ``get_active_pipelines()``, and a per-pipeline failure
    during the relaunch itself (driver probe or thread spawn) is logged and
    skipped so one bad pipeline cannot strand the rest.  The one case that is
    *not* isolated: a load failure other than ``StateStoreError`` propagates out
    of ``get_active_pipelines()`` and the outer ``except`` aborts the sweep
    (returns 0) — an accepted trade for using the canonical active-pipeline
    accessor, matching how ``get_active_pipelines()`` behaves for its other
    callers.
    """
    try:
        active_pipelines = store.get_active_pipelines()
    except Exception as e:  # noqa: BLE001 - startup sweep must not raise
        _pkg.logger.warning(
            "Driver relaunch sweep skipped: could not list active pipelines",
            error=str(e),
        )
        return 0

    relaunched = 0
    for pipeline in active_pipelines:
        try:
            if pipeline.status != _pkg.PipelineStatus.RUNNING:
                continue
            if _pkg.has_live_pipeline_driver(pipeline.id):
                continue
            run_epoch = pipeline.run_epoch or pipeline.created_at
            _pkg._spawn_pipeline_run_thread(pipeline.id, store.repo_path, run_epoch)
            relaunched += 1
            _pkg.logger.warning(
                "Relaunched _run_pipeline driver for RUNNING pipeline with no "
                "live driver thread (orchestrator restart recovery, #3469)",
                pipeline_id=pipeline.id,
                phase=pipeline.current_phase.value,
                run_epoch=run_epoch.isoformat(),
            )
        except Exception as e:  # noqa: BLE001 - per-pipeline isolation
            _pkg.logger.warning(
                "Failed to relaunch driver for RUNNING pipeline (continuing sweep)",
                pipeline_id=getattr(pipeline, "id", "unknown"),
                error=str(e),
            )
    return relaunched


def _broadcast_orphaned_driver_alert(pipeline_id: str, pipeline: _pkg.Pipeline) -> None:
    """Surface an orphaned-driver revival as an overseer alert (#3233).

    A resolved decision on a driver-less pipeline used to return ``success``
    and hang invisibly.  Emit an OVERSEER_ALERT alongside the WARNING log so
    the recovery is visible on the bus, not just in orchestrator logs.
    Best-effort: a broadcast failure never blocks the revival itself.
    """
    try:
        from message_store import Message, MessageType

        store_fn = _pkg._get_message_store()
        if store_fn is None:
            return
        msg_store = store_fn()
        phase = pipeline.current_phase.value if pipeline.current_phase else None
        _run_epoch_str = pipeline.run_epoch.isoformat() if pipeline.run_epoch else None
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject="orphaned_driver_revived: orchestrator [medium]",
                body=(
                    "A HITL decision was resolved on a pipeline whose "
                    "_run_pipeline driver thread did not survive an "
                    "orchestrator restart. The driver is being re-launched so "
                    "the resolution is acted on (no manual start_pipeline "
                    "needed). See #3233."
                ),
                metadata={"reason": "restart_orphaned_awaiting_human"},
                phase=phase,
            ),
            run_epoch=_run_epoch_str,
        )
    except Exception as alert_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to broadcast orphaned-driver revival alert (non-fatal)",
            pipeline_id=pipeline_id,
            error=str(alert_err),
        )


def maybe_revive_orphaned_awaiting_human_driver(pipeline_id: str, repo_path: _pkg.Path) -> bool:
    """Re-launch the driver for an AWAITING_HUMAN pipeline orphaned by a restart.

    Called from the decision-resolve path (#3233).  When the orchestrator
    restarts while a pipeline is parked AWAITING_HUMAN at a phase gate, the
    in-memory ``_run_pipeline`` driver blocked on ``wait_for_decision`` is
    gone and startup reconciliation deliberately leaves the still-pending
    decision as-is (``startup_reconciliation.py``).  Resolving the decision
    then flips it to RESOLVED with no consumer and the pipeline hangs
    silently — the operator sees ``success`` and nothing happens.

    This detects that no live driver owns the pipeline and, once the queue
    has no remaining pending decisions, routes through ``start_pipeline``'s
    proven AWAITING_HUMAN recovery branch (advance-or-rerun + driver respawn)
    so the resolution self-heals without a manual ``start_pipeline``.

    No-ops (returns ``False``) when a live driver is already polling — the
    normal in-process path consumes the resolution — or when the pipeline
    isn't in the orphaned-parked state.  Must be called from a Flask request
    context (it reuses the lifecycle-secret-guarded ``start_pipeline`` route),
    which the resolve-decision handler satisfies.
    """
    store = _pkg.get_state_store(repo_path)
    try:
        pipeline = store.load_pipeline(pipeline_id)
    except Exception:
        return False

    if pipeline.status != _pkg.PipelineStatus.AWAITING_HUMAN:
        return False
    # A multi-decision batch (e.g. the contract-decision bridge) is only
    # ready to resume once every decision is resolved; leave it parked while
    # siblings are still pending.
    if pipeline.get_pending_decisions():
        return False
    if _pkg.has_live_pipeline_driver(pipeline_id):
        return False

    _pkg.logger.warning(
        "Decision resolved on AWAITING_HUMAN pipeline with no live driver "
        "thread (orphaned by an orchestrator restart); reviving via "
        "start_pipeline recovery so the resolution is acted on (#3233)",
        pipeline_id=pipeline_id,
        current_phase=pipeline.current_phase.value if pipeline.current_phase else None,
    )
    _pkg._broadcast_orphaned_driver_alert(pipeline_id, pipeline)

    try:
        _resp, status_code = _pkg.start_pipeline(pipeline_id)
    except Exception as revive_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Orphaned-driver revival raised (decision is still resolved; an "
            "operator can recover manually via start_pipeline) (#3233)",
            pipeline_id=pipeline_id,
            error=str(revive_err),
        )
        return False

    if status_code != 200:
        _pkg.logger.warning(
            "Orphaned-driver revival did not start the pipeline (#3233)",
            pipeline_id=pipeline_id,
            status_code=status_code,
        )
        return False

    _pkg.logger.info(
        "Orphaned AWAITING_HUMAN pipeline revived after decision resolution (#3233)",
        pipeline_id=pipeline_id,
    )
    return True
