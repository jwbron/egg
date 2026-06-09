"""
Startup reconciliation for orphaned container state.

On restart, persisted RUNNING agents whose containers are no longer alive
are detected and marked FAILED so operators (or CI) can retry via the
existing POST /pipelines/{id}/start endpoint.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.startup_reconciliation")

# K8s label that scopes a pod to a specific pipeline.  Imported from
# ``kubernetes_client`` so the literal lives in exactly one place.  Safe at
# import time because ``kubernetes_client`` only imports the ``kubernetes``
# pip package inside method bodies, so this module stays importable even
# in test environments that don't install it.
from kubernetes_client import LABEL_PIPELINE_ID as _LABEL_PIPELINE_ID
from models import LIVE_POD_STATUSES as _LIVE_POD_STATUSES


def reconcile_stale_containers(store: object, docker_client: object) -> int:
    """Detect and recover pipelines whose running containers are gone.

    Called once at orchestrator startup before serving requests.  For each
    pipeline that shows status=RUNNING, the reconciler queries k8s for live
    pods labeled ``egg.pipeline.id=<id>``.  If any pods are alive for the
    pipeline, the pipeline is left RUNNING — record drift between the
    persisted in-memory state and the new orch process's view of the pods is
    expected after a restart and is reconciled by the running orchestrator,
    not at startup (#2411).  Only when the pipeline has zero live pods do we
    fall back to the older "any stale record fails the pipeline" behavior so
    that genuinely orphaned pipelines still surface as FAILED.

    Args:
        store: StateStore instance (already bound to the correct repo path).
        docker_client: DockerClient instance.

    Returns:
        Number of pipelines that were recovered (marked FAILED).
    """
    try:
        from models import AgentExecutionStatus, ContainerStatus, PipelineStatus
    except ImportError:
        from models import AgentExecutionStatus, ContainerStatus, PipelineStatus  # type: ignore

    # Collect IDs of containers that are currently running in Docker.
    try:
        live_containers = docker_client.list_containers(all=False)  # type: ignore[attr-defined]
        live_ids: set[str] = {ci.container_id for ci in live_containers}
    except Exception as e:
        logger.warning(
            "Startup reconciliation skipped: could not list live containers",
            error=str(e),
        )
        return 0

    try:
        pipeline_ids: list[str] = store.list_pipelines()  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning(
            "Startup reconciliation skipped: could not list pipelines",
            error=str(e),
        )
        return 0

    recovered = 0

    for pipeline_id in pipeline_ids:
        try:
            pipeline = store.load_pipeline(pipeline_id)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(
                "Startup reconciliation: could not load pipeline",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            continue

        # Handle AWAITING_HUMAN pipelines orphaned by a restart.
        # If all decisions are resolved (0 pending) the polling thread that
        # would have picked up the resolution is gone.  Mark FAILED so that
        # start_pipeline can recover it with the correct phase transition.
        if pipeline.status == PipelineStatus.AWAITING_HUMAN:
            pending = pipeline.get_pending_decisions()
            if len(pending) == 0:
                pipeline.status = PipelineStatus.FAILED
                pipeline.error = (
                    "Pipeline marked FAILED at orchestrator startup: "
                    "AWAITING_HUMAN with no pending decisions (likely orphaned "
                    "after a restart). Restart via POST /pipelines/{id}/start."
                )
                try:
                    store.save_pipeline(pipeline)  # type: ignore[attr-defined]
                    recovered += 1
                    logger.warning(
                        "Startup reconciliation: AWAITING_HUMAN pipeline with "
                        "0 pending decisions marked FAILED",
                        pipeline_id=pipeline_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Startup reconciliation: could not save pipeline",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
            else:
                logger.info(
                    "Startup reconciliation: AWAITING_HUMAN pipeline has "
                    "pending decisions, leaving as-is",
                    pipeline_id=pipeline_id,
                    pending_count=len(pending),
                )
            continue

        if pipeline.status != PipelineStatus.RUNNING:
            continue

        changed = False

        # Only check the current phase — containers from prior phases are
        # intentionally terminated and their absence is expected.  This
        # prevents completed phases (e.g. refine) from falsely marking the
        # pipeline as FAILED when the orchestrator restarts.
        current_phase_key = pipeline.current_phase.value
        phase_execution = pipeline.phases.get(current_phase_key)
        if phase_execution is None:
            continue

        # Crash-between-submit-and-spawn: pipeline RUNNING but the current
        # phase never reached `executor.spawn_all`.  `_run_pipeline` is
        # fire-and-forget from the submit handler, so a crash before the
        # spawn loop leaves the row PENDING with `started_at=null` and
        # nothing to resume it (#2009).  Mark FAILED so operators see
        # something actionable instead of an indefinitely frozen pipeline.
        #
        # Note: a PENDING phase with agents but no containers is intentionally
        # left to the container loop below — if agents were created, some
        # spawn work started even if containers weren't registered yet.
        if (
            phase_execution.status == PipelineStatus.PENDING
            and phase_execution.started_at is None
            and not phase_execution.containers
            and not phase_execution.agents
        ):
            pipeline.status = PipelineStatus.FAILED
            pipeline.error = (
                "Pipeline marked FAILED at orchestrator startup: current phase "
                f"{current_phase_key!r} never spawned (likely a crash between "
                "submit and phase start). Restart via POST /pipelines/{id}/start."
            )
            try:
                store.save_pipeline(pipeline)  # type: ignore[attr-defined]
                recovered += 1
                logger.warning(
                    "Startup reconciliation: RUNNING pipeline with un-spawned "
                    "PENDING phase marked FAILED",
                    pipeline_id=pipeline_id,
                    phase=current_phase_key,
                )
            except Exception as e:
                logger.warning(
                    "Startup reconciliation: could not save pipeline",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
            continue

        # Query k8s for pods labeled to this pipeline.  When any are alive
        # the pipeline is not dead — even if individual ``container_id``s in
        # the persisted state don't match the new orch process's view of
        # the pods (e.g. a pod was recreated and its uid changed, or the
        # record was written before the latest pod uid was observed),
        # treat that drift as a problem the running orchestrator will
        # reconcile naturally instead of terminating the pipeline at
        # startup (#2411).
        #
        # On failure we fail-safe (leave the pipeline RUNNING and skip).
        # In practice both queries route through the same
        # ``KubernetesClient.list_containers`` → ``list_namespaced_pod`` —
        # if the global query at line 63 already succeeded, a per-pipeline
        # failure is rare enough that the safe choice is to defer to the
        # running orchestrator's reconciliation rather than risk repeating
        # the #2411 false-positive on misbehaving clusters.  The genuinely
        # orphaned case (zero live pods) is rare and surfaceable elsewhere
        # — drift cases are the active concern here.
        try:
            pipeline_live_containers = docker_client.list_containers(  # type: ignore[attr-defined]
                labels={_LABEL_PIPELINE_ID: pipeline_id},
            )
            # Filter to genuinely live pods (Pending / Creating / Running).
            # ``list_containers`` returns pods regardless of phase, so a
            # ``Failed`` / ``Succeeded`` pod still inside its Job's
            # ``ttlSecondsAfterFinished`` window (default 600s) would otherwise
            # mask a genuinely orphaned pipeline.  Shares the
            # ``models.LIVE_POD_STATUSES`` constant with
            # ``routes/pipelines._count_live_pods_for_pipeline`` so both
            # label-scoped checks can't drift on what "live" means
            # (#2420, #2650).
            pipeline_live_ids: set[str] = {
                ci.container_id
                for ci in pipeline_live_containers
                if ci.status in _LIVE_POD_STATUSES
            }
        except Exception as e:
            logger.warning(
                "Startup reconciliation: pipeline-scoped container query failed, "
                "leaving pipeline RUNNING (deferring to running orchestrator's "
                "reconciliation rather than risk a #2411-style false positive)",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            continue

        if pipeline_live_ids:
            logger.info(
                "Startup reconciliation: pipeline has live pods, leaving RUNNING",
                pipeline_id=pipeline_id,
                live_pod_count=len(pipeline_live_ids),
            )
            continue

        # ----------------------------------------------------------------
        # On-demand in-flight fall-through (#3023 slice-3 TASK-3-5)
        # ----------------------------------------------------------------
        # Cross-version revert tolerance: after #3023's PR merges, in-
        # flight pipelines may have on-demand pods (or NO pod at all
        # between events) because the orchestrator now spawns a one-shot
        # pod per actionable BRC event rather than holding a long-lived
        # wrapper pod per role. A reverted orchestrator's reader (this
        # function) lands on a pipeline state where ``pipeline_live_ids``
        # is empty but the BRC tracker has a non-empty event history for
        # at least one role of the current phase — that is the
        # ``on-demand in-flight`` signature, NOT a crashed pipeline.
        #
        # Without this fall-through the reverted reader would treat the
        # missing wrapper pod as a #2009-style "crashed between submit
        # and spawn" and mark the pipeline FAILED, which would force
        # operators into a manual restart for every in-flight pipeline
        # during a revert window. With this branch the reader leaves the
        # pipeline RUNNING and the running orchestrator's per-phase tick
        # re-derives ``next-action`` per role (see
        # ``routes/consensus.py::_derive_next_action``) and spawns on
        # demand for any role whose action is not ``wait``.
        #
        # The "non-empty event history" check is intentionally permissive
        # — any role whose tracker has registered a CONSENSUS_PROPOSE /
        # CONSENSUS_ACK / CONSENSUS_NACK message is enough to identify the
        # pipeline as having genuinely moved through the on-demand path.
        # An entirely fresh pipeline with no events (a spawn-failed-on-
        # first-event scenario) still falls through to the legacy
        # marking-FAILED path below because the tracker carries nothing
        # to anchor the in-flight verdict on.
        if _is_on_demand_in_flight(pipeline, pipeline_id):
            logger.info(
                "Startup reconciliation: pipeline has no live pods but BRC "
                "tracker shows on-demand in-flight events — leaving RUNNING "
                "(cross-version revert tolerance, #3023 TASK-3-5). The "
                "running orchestrator's tick will re-derive next-action and "
                "spawn on demand.",
                pipeline_id=pipeline_id,
                phase=current_phase_key,
            )
            continue

        for container_info in phase_execution.containers:
            if container_info.status == ContainerStatus.RUNNING:
                if container_info.container_id not in live_ids:
                    logger.warning(
                        "Startup reconciliation: container missing, marking FAILED",
                        pipeline_id=pipeline_id,
                        phase=current_phase_key,
                        container_id=container_info.container_id,
                    )
                    container_info.status = ContainerStatus.FAILED
                    container_info.exit_code = -1
                    container_info.exited_at = datetime.now(UTC)
                    changed = True

        for agent in phase_execution.agents:
            if agent.status == AgentExecutionStatus.RUNNING:
                if agent.container_id and agent.container_id not in live_ids:
                    logger.warning(
                        "Startup reconciliation: agent container missing, marking FAILED",
                        pipeline_id=pipeline_id,
                        phase=current_phase_key,
                        agent_role=str(agent.role),
                        container_id=agent.container_id,
                    )
                    agent.status = AgentExecutionStatus.FAILED
                    agent.completed_at = datetime.now(UTC)
                    agent.error = (
                        "Container not found at orchestrator startup — "
                        "likely lost during a previous crash"
                    )
                    changed = True

        if changed:
            pipeline.status = PipelineStatus.FAILED
            pipeline.error = (
                "Pipeline marked FAILED at orchestrator startup: one or more agent "
                "containers were not found. Restart via POST /pipelines/{id}/start."
            )
            try:
                store.save_pipeline(pipeline)  # type: ignore[attr-defined]
                recovered += 1
                logger.warning(
                    "Startup reconciliation: pipeline marked FAILED",
                    pipeline_id=pipeline_id,
                )
            except Exception as e:
                logger.warning(
                    "Startup reconciliation: could not save pipeline",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

    # Reconstruct consensus trackers for RUNNING concurrent pipelines
    # whose in-memory tracker was lost during the restart.
    try:
        from concurrent_executor import is_concurrent_execution
        from peer_consensus import get_peer_consensus_tracker, reconstruct_tracker_from_messages
        from review_graph import get_review_graph_for_phase

        for pipeline_id in pipeline_ids:
            try:
                pipeline = store.load_pipeline(pipeline_id)
            except Exception:
                continue

            if pipeline.status != PipelineStatus.RUNNING:
                continue
            if not is_concurrent_execution(pipeline, pipeline.current_phase):
                continue

            graph = get_review_graph_for_phase(pipeline.current_phase.value, repo=pipeline.repo)

            # Pipeline-level reconstruction is guarded by a tracker-
            # existence check so a second invocation of
            # ``reconcile_stale_containers`` (mid-run hook, future test)
            # does not redo the work — but the per-slice loop below is
            # NOT short-circuited by the pipeline-level tracker
            # existing, because slice trackers are registered under
            # nested ``{pipeline_id}/{slice_id}`` keys and may be
            # missing even when the pipeline-level tracker is present.
            # Reviewer feedback on PR #2895: hardening against silent
            # data gaps if this function is ever re-invoked.
            if get_peer_consensus_tracker(pipeline_id) is None:
                tracker = reconstruct_tracker_from_messages(pipeline_id, graph)
                if tracker:
                    logger.info(
                        "Startup reconciliation: reconstructed consensus tracker",
                        pipeline_id=pipeline_id,
                    )

                    # If consensus was already complete before the restart,
                    # mark agents and phase COMPLETE so the pipeline can advance.
                    try:
                        evaluation = tracker.evaluate()
                        if evaluation.get("is_complete"):
                            logger.warning(
                                "Startup reconciliation: consensus already complete, "
                                "marking phase complete for recovery",
                                pipeline_id=pipeline_id,
                            )
                            from models import AgentExecutionStatus

                            phase_exec = pipeline.phases.get(pipeline.current_phase.value)
                            if phase_exec is not None:
                                # The reconstructed tracker is the pipeline-level
                                # one (``get_peer_consensus_tracker(pipeline_id)``
                                # — no slice arg), so only mark pipeline-level
                                # agents COMPLETE. Per-slice tracker
                                # reconstruction would have to evaluate each
                                # slice's tracker separately; flipping every
                                # agent regardless of slice would prematurely
                                # complete agents whose slice-scoped consensus
                                # hadn't actually reached terminal state (#2422).
                                for agent in phase_exec.agents:
                                    if getattr(agent, "slice_id", None) is not None:
                                        continue
                                    if agent.status == AgentExecutionStatus.RUNNING:
                                        agent.status = AgentExecutionStatus.COMPLETE
                                        agent.completed_at = datetime.now(UTC)
                                # Phase-level mutations are scoped to "no
                                # other slice still active": the
                                # reconstructed tracker is the pipeline-level
                                # one, so only ``slice_id is None`` agents
                                # were flipped above. If sibling slice-scoped
                                # agents are still non-terminal, their
                                # per-slice trackers haven't been
                                # reconstructed here yet — leave the phase
                                # RUNNING so they aren't prematurely marked
                                # COMPLETE (#2441).
                                other_slice_active = any(
                                    getattr(agent, "slice_id", None) is not None
                                    and agent.status
                                    not in (
                                        AgentExecutionStatus.COMPLETE,
                                        AgentExecutionStatus.FAILED,
                                    )
                                    for agent in phase_exec.agents
                                )
                                if not other_slice_active:
                                    phase_exec.status = PipelineStatus.COMPLETE
                                    phase_exec.completed_at = datetime.now(UTC)
                                store.save_pipeline(pipeline)
                    except Exception as eval_err:
                        logger.warning(
                            "Startup reconciliation: consensus evaluation failed",
                            pipeline_id=pipeline_id,
                            error=str(eval_err),
                        )

            # Slice-4 TASK-4-5 (closes #2409): per-slice consensus
            # tracker reconstruction. Iterate the pipeline's contract
            # slices and reconstruct each slice's tracker from the
            # message store's slice_id-tagged history.
            # ``reconstruct_tracker_from_messages`` accepts the
            # ``slice_id`` kwarg (peer_consensus.py:1955-2046) and
            # registers each tracker under the nested
            # ``{pipeline_id}/{slice_id}`` key. The cross-slice
            # isolation invariant (#2409 / #2761) holds by
            # construction: the strict-equality filter
            # ``_message_slice_id(m) == slice_id`` inside
            # ``reconstruct_tracker_from_messages`` (peer_consensus.py
            # near line 2003) excludes any message whose metadata
            # ``slice_id`` does not exactly match. The store-level
            # filter at ``message_store.py:407-418`` (#2725) is
            # intentionally lenient — it passes through
            # ``metadata.slice_id is None`` messages so OVERSEER_ALERTs
            # fan out across slices — so the peer_consensus filter is
            # the actual isolation enforcer for reconstruction.
            # Sibling loop to the pipeline-level reconstruction above:
            # NOT nested under the pipeline-level tracker-existence
            # guard, because slice trackers live under their own
            # ``{pipeline_id}/{slice_id}`` keys and a present
            # pipeline-level tracker does not imply slice trackers
            # exist (reviewer feedback on PR #2895).
            _slice_ids = _enumerate_contract_slices(pipeline, store)
            for _slice_id in _slice_ids:
                try:
                    if get_peer_consensus_tracker(pipeline.id, slice_id=_slice_id) is not None:
                        continue
                    _slice_tracker = reconstruct_tracker_from_messages(
                        pipeline.id, graph, slice_id=_slice_id
                    )
                    if _slice_tracker is not None:
                        logger.info(
                            "Startup reconciliation: reconstructed per-slice "
                            "consensus tracker (slice-4 TASK-4-5 / #2409)",
                            pipeline_id=pipeline.id,
                            slice_id=_slice_id,
                        )
                except Exception as slice_recon_err:
                    logger.warning(
                        "Startup reconciliation: per-slice tracker "
                        "reconstruction failed (slice-4 TASK-4-5)",
                        pipeline_id=pipeline.id,
                        slice_id=_slice_id,
                        error=str(slice_recon_err),
                    )
    except ImportError:
        logger.debug("Peer consensus module not available for startup reconstruction")
    except Exception as e:
        logger.warning(
            "Startup reconciliation: consensus reconstruction failed",
            error=str(e),
        )

    return recovered


def _is_on_demand_in_flight(pipeline: Any, pipeline_id: str) -> bool:
    """Return ``True`` when a no-live-pods pipeline is an on-demand in-flight
    pipeline rather than a crashed one.

    See the ``On-demand in-flight fall-through`` block above
    (``reconcile_stale_containers``) for the cross-version revert
    scenario that motivates this branch (#3023 slice-3 TASK-3-5). The
    heuristic is intentionally permissive — any role whose BRC tracker
    has at least one non-trivial event (a CONSENSUS_PROPOSE / ACK /
    NACK in the message history, or a reconstructed-from-messages
    tracker that already carries a proposal version > 0) is enough to
    flag the pipeline as having genuinely moved through the on-demand
    path. A consequence: a pipeline that has already reached CONFIRMED
    for every role but has not yet been cleaned up by the run loop
    (e.g. orchestrator crashed in the brief window between phase
    completion and the tick that closes out the phase) also falls
    through to RUNNING here. That is intentional — the running
    orchestrator's tick will re-derive next-action, see ``complete``
    for every role, and either advance the phase cleanly or surface a
    ``stuck-phase-transition`` alert if it cannot. The expensive shape
    (asking the message store for the role's history) is wrapped in a
    defensive try/except so a reconstruction failure here cannot crash
    the whole startup-reconciliation loop; on any unexpected exception
    the function returns ``False`` and the pipeline falls through to
    the legacy marking-FAILED path, which is the strictly safer
    behaviour for an undecidable cross-version state.

    Args:
        pipeline: Pipeline object whose current-phase role set we
            inspect.
        pipeline_id: Identifier used to look up the BRC tracker / message
            store entries.

    Returns:
        ``True`` if at least one role of the pipeline's current phase
        has a non-empty BRC event history, signalling on-demand in-flight;
        ``False`` otherwise (either no events recorded, or the lookup
        machinery is unavailable).
    """
    # Lazy imports inside the function so a missing peer_consensus /
    # message_store dependency at startup-reconciliation import time
    # cannot brick the whole reconciler — we just fall through to the
    # legacy behaviour. ``peer_consensus`` is also the module that
    # ``reconcile_stale_containers`` already imports below, so the soft
    # ``ImportError`` swallow here mirrors that pattern.
    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        return False

    try:
        from message_store import MessageType, get_message_store
    except ImportError:
        get_message_store = None  # type: ignore[assignment]
        MessageType = None  # type: ignore[assignment]

    # Tracker check first — cheaper than a message-store query, and
    # covers the common case where the pipeline-level tracker has been
    # reconstructed (or never lost) and already carries the in-flight
    # state.
    try:
        tracker = get_peer_consensus_tracker(pipeline_id)
    except Exception:
        tracker = None
    if tracker is not None:
        try:
            agents = tracker.evaluate().get("agents") or {}
        except Exception:
            agents = {}
        for _role, state in agents.items():
            # ``state.producer_phase`` is ``WORKING`` / ``PROPOSED`` /
            # ``CONFIRMED``; ``reviewer_phase`` is ``REVIEWING`` /
            # ``ACKED`` / ``NACKED``. Any non-``WORKING`` /
            # non-``REVIEWING`` phase implies at least one BRC event has
            # already been recorded for the role, i.e. the pipeline has
            # genuinely moved past the initial spawn boundary.
            producer_phase = (state or {}).get("producer_phase")
            reviewer_phase = (state or {}).get("reviewer_phase")
            if producer_phase and producer_phase != "WORKING":
                return True
            if reviewer_phase and reviewer_phase != "REVIEWING":
                return True

    # Message-store fallback: a tracker that hasn't been reconstructed
    # yet (the typical state at startup before
    # ``reconcile_stale_containers`` runs its reconstruction loop) won't
    # answer ``evaluate`` usefully, but the persisted message history
    # still names the events that flowed through the on-demand path.
    if get_message_store is None or MessageType is None:
        return False
    try:
        store = get_message_store()
        messages = store.get_messages(pipeline_id=pipeline_id)
    except Exception:
        return False
    for msg in messages or []:
        mtype = getattr(msg, "message_type", None)
        if mtype in (
            MessageType.CONSENSUS_PROPOSE,
            MessageType.CONSENSUS_ACK,
            MessageType.CONSENSUS_NACK,
            MessageType.CONSENSUS_CONFIRMED,
        ):
            return True
    return False


def _enumerate_contract_slices(pipeline: Any, store: Any) -> list[str]:
    """Return the slice IDs from the pipeline's contract, if any.

    Slice-4 TASK-4-5 helper. Reads ``contract.slices`` for the pipeline
    so the consensus-reconstruction loop can iterate per-slice tracker
    reconstruction without coupling startup_reconciliation to the
    contract loader.

    **Worktree-path resolution (reviewer_code v1 blocker 1)**: an
    active pipeline's contract lives in the per-pipeline worktree
    at
    ``/home/egg/.egg-worktrees/<pipeline_id>/<repo>/.egg-state/contracts/<pipeline_id>.json``,
    NOT under ``store.repo_path`` (the main orchestrator repo;
    e.g. ``/home/egg/repos/egg``). Without
    ``resolve_worktree_path`` ``load_contract`` raises
    ``ContractNotFoundError`` for every active pipeline, the
    function returns ``[]``, the reconstruction loop never
    iterates, and per-slice trackers are NEVER reconstructed —
    the whole #2409 closure deliverable. Pattern mirrors
    ``orchestrator/routes/signals.py:709`` and the dozen-plus
    other production call sites that load contracts at runtime.
    Failures are logged so operators see the degradation rather
    than silently losing per-slice tracker reconstruction.
    """
    repo_path = getattr(store, "repo_path", None)
    if not repo_path:
        return []
    try:
        from egg_contracts.loader import load_contract
    except ImportError:
        logger.warning(
            "Per-slice tracker reconstruction skipped: egg_contracts.loader "
            "not importable (slice-4 TASK-4-5 / #2409)",
            pipeline_id=pipeline.id,
        )
        return []
    try:
        from routes import resolve_worktree_path
    except ImportError:
        try:
            from orchestrator.routes import (  # type: ignore[no-redef]
                resolve_worktree_path,
            )
        except ImportError:
            resolve_worktree_path = None  # type: ignore[assignment]
    try:
        if resolve_worktree_path is not None:
            contract_repo_path = resolve_worktree_path(pipeline.id, Path(repo_path))
        else:
            contract_repo_path = Path(repo_path)
        contract = load_contract(pipeline.id, contract_repo_path)
    except Exception as load_err:
        logger.warning(
            "Per-slice tracker reconstruction failed to load contract "
            "(slice-4 TASK-4-5 / #2409); pipeline-level tracker is still "
            "reconstructed above",
            pipeline_id=pipeline.id,
            error=str(load_err),
        )
        return []
    slices = getattr(contract, "slices", None) or []
    return [s.id for s in slices]
