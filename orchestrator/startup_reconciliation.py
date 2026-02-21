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

        if pipeline.status != PipelineStatus.RUNNING:
            continue

        changed = False

        for phase_key, phase_execution in pipeline.phases.items():
            if phase_execution.status != PipelineStatus.RUNNING:
                continue

            for container_info in phase_execution.containers:
                if container_info.status == ContainerStatus.RUNNING:
                    if container_info.container_id not in live_ids:
                        logger.warning(
                            "Startup reconciliation: container missing, marking FAILED",
                            pipeline_id=pipeline_id,
                            phase=phase_key,
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
                            phase=phase_key,
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

    return recovered
