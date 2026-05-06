"""
Startup reconciliation for orphaned container state.

On restart, persisted RUNNING agents whose containers are no longer alive
are detected and marked FAILED so operators (or CI) can retry via the
existing POST /pipelines/{id}/start endpoint.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

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
            # mask a genuinely orphaned pipeline.  Mirrors
            # ``routes/pipelines._count_live_pods_for_pipeline`` so both
            # label-scoped checks agree on what "live" means (#2420).
            _live_statuses = (
                ContainerStatus.PENDING,
                ContainerStatus.CREATING,
                ContainerStatus.RUNNING,
            )
            pipeline_live_ids: set[str] = {
                ci.container_id for ci in pipeline_live_containers if ci.status in _live_statuses
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
            if get_peer_consensus_tracker(pipeline_id) is not None:
                continue

            graph = get_review_graph_for_phase(pipeline.current_phase.value, repo=pipeline.repo)
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
                            # TODO(#2441): phase-level mutations are
                            # unconditional even though the agent walk above
                            # is slice-scoped (only pipeline-level agents
                            # flipped). Marks the whole phase COMPLETE even
                            # if per-slice trackers are still RUNNING; safe
                            # today because per-slice tracker reconstruction
                            # isn't wired in here yet.
                            phase_exec.status = PipelineStatus.COMPLETE
                            phase_exec.completed_at = datetime.now(UTC)
                            store.save_pipeline(pipeline)
                except Exception as eval_err:
                    logger.warning(
                        "Startup reconciliation: consensus evaluation failed",
                        pipeline_id=pipeline_id,
                        error=str(eval_err),
                    )
    except ImportError:
        logger.debug("Peer consensus module not available for startup reconstruction")
    except Exception as e:
        logger.warning(
            "Startup reconciliation: consensus reconstruction failed",
            error=str(e),
        )

    return recovered
