"""
Startup reconciliation for orphaned container state.

On restart, persisted RUNNING agents whose containers are no longer alive
are detected and marked FAILED so operators (or CI) can retry via the
existing POST /pipelines/{id}/start endpoint.
"""

import sys
from datetime import datetime
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


def reconcile_stale_containers(store: object, docker_client: object) -> int:
    """Detect and recover pipelines whose running containers are gone.

    Called once at orchestrator startup before serving requests.  For each
    pipeline that shows status=RUNNING, any agent/container whose container_id
    is absent from the live Docker container set is marked FAILED.  If at
    least one such stale entry is found the pipeline itself is marked FAILED
    so that operators can restart it via POST /pipelines/{id}/start.

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
                    container_info.exited_at = datetime.utcnow()
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
                    agent.completed_at = datetime.utcnow()
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

                        phase_exec = pipeline.phases.get(
                            pipeline.current_phase.value
                            if hasattr(pipeline.current_phase, "value")
                            else pipeline.current_phase
                        )
                        if phase_exec is not None:
                            for agent in phase_exec.agents:
                                if agent.status == AgentExecutionStatus.RUNNING:
                                    agent.status = AgentExecutionStatus.COMPLETE
                            phase_exec.status = PipelineStatus.COMPLETE
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
