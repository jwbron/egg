"""
Kubernetes Job health monitoring and cleanup.

Monitors agent Job health and removes orphaned Jobs.
Detects unhealthy/exited Jobs and triggers appropriate actions.
Equivalent to ContainerMonitor but uses KubernetesClient.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from container_backend import (
    KubernetesClientError,
    PodNotFoundError,
)
from container_monitor import (
    ContainerEvent,
    EventHandler,
    _reconcile_container_state,
)
from kubernetes_client import KubernetesClient, get_kubernetes_client
from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.kubernetes_monitor")


class KubernetesMonitor:
    """Monitors Kubernetes Job health and lifecycle.

    Periodically checks Job status and invokes handlers
    for state changes. Automatically cleans up orphaned Jobs.
    """

    def __init__(
        self,
        k8s_client: KubernetesClient | None = None,
        check_interval: int = 10,
        orphan_age_hours: int = 24,
    ):
        """Initialize monitor.

        Args:
            k8s_client: Kubernetes client (default: singleton).
            check_interval: Seconds between health checks.
            orphan_age_hours: Hours before Job is considered orphaned.
        """
        self.k8s_client = k8s_client or get_kubernetes_client()
        self.check_interval = check_interval
        self.orphan_age_hours = orphan_age_hours

        self._handlers: list[EventHandler] = []
        self._job_states: dict[str, ContainerStatus] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Periodic reconciliation state
        self._reconciliation_running = False
        self._reconciliation_thread: threading.Thread | None = None
        self._reconciliation_stores: list[Any] = []
        self._reconciliation_interval: int = 30
        self._clean_exit_skipped: set[str] = set()

    def add_handler(self, handler: EventHandler) -> None:
        """Add an event handler.

        Args:
            handler: Function to call on Job events.
        """
        with self._lock:
            self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        """Remove an event handler.

        Args:
            handler: Handler to remove.
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _emit_event(self, event: ContainerEvent) -> None:
        """Emit an event to all handlers.

        Args:
            event: Event to emit.
        """
        with self._lock:
            handlers = self._handlers.copy()

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    "Event handler error",
                    event_type=event.event_type,
                    container_id=event.container_info.container_id[:12],
                    error=str(e),
                )

    def _check_job(self, job_info: ContainerInfo) -> None:
        """Check a single Job and emit events for state changes.

        Args:
            job_info: Job info to check.
        """
        job_id = job_info.container_id
        old_status = self._job_states.get(job_id)
        new_status = job_info.status

        if old_status != new_status:
            self._job_states[job_id] = new_status

            if new_status == ContainerStatus.RUNNING:
                if old_status is None:
                    self._emit_event(
                        ContainerEvent(ContainerEvent.STARTED, job_info)
                    )
            elif new_status == ContainerStatus.EXITED:
                if job_info.exit_code == 0:
                    self._emit_event(
                        ContainerEvent(
                            ContainerEvent.STOPPED, job_info
                        )
                    )
                else:
                    self._emit_event(
                        ContainerEvent(
                            ContainerEvent.FAILED,
                            job_info,
                            data={"exit_code": job_info.exit_code},
                        )
                    )
            elif new_status == ContainerStatus.FAILED:
                self._emit_event(
                    ContainerEvent(ContainerEvent.FAILED, job_info)
                )

    def _check_all_jobs(self) -> None:
        """Check all orchestrator Jobs."""
        try:
            jobs = self.k8s_client.list_containers(all=True)
            current_ids = set()

            for job_info in jobs:
                current_ids.add(job_info.container_id)
                self._check_job(job_info)

            # Check for removed Jobs
            removed_ids = set(self._job_states.keys()) - current_ids
            for job_id in removed_ids:
                del self._job_states[job_id]
                logger.info("Job removed", job_name=job_id)

        except Exception as e:
            logger.error("Job check failed", error=str(e))

    def _cleanup_orphaned(self) -> int:
        """Remove orphaned Jobs.

        Returns:
            Number of Jobs removed.
        """
        try:
            return self.k8s_client.cleanup_orphaned_containers(
                max_age_hours=self.orphan_age_hours
            )
        except Exception as e:
            logger.error("Orphan cleanup failed", error=str(e))
            return 0

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        cleanup_counter = 0
        cleanup_interval = 60

        while self._running:
            self._check_all_jobs()

            cleanup_counter += 1
            if cleanup_counter >= cleanup_interval:
                self._cleanup_orphaned()
                cleanup_counter = 0

            time.sleep(self.check_interval)

    def start(self) -> None:
        """Start the monitor in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._thread.start()

        logger.info(
            "Kubernetes monitor started",
            check_interval=self.check_interval,
        )

    def start_periodic_reconciliation(
        self,
        stores: Any,
        interval: int = 30,
    ) -> None:
        """Start periodic reconciliation of stale Jobs.

        Every *interval* seconds, lists all RUNNING pipelines and
        checks whether Jobs marked RUNNING still exist. Missing
        Jobs are reconciled via _reconcile_container_state.

        Args:
            stores: StateStore instance or list of StateStore instances.
            interval: Seconds between reconciliation sweeps.
        """
        if self._reconciliation_running:
            return

        if isinstance(stores, list):
            self._reconciliation_stores = stores
        else:
            self._reconciliation_stores = [stores]
        self._reconciliation_interval = interval
        self._reconciliation_running = True
        self._reconciliation_thread = threading.Thread(
            target=self._reconciliation_loop, daemon=True
        )
        self._reconciliation_thread.start()
        logger.info(
            "Periodic Job reconciliation started",
            interval=interval,
        )

    def _reconciliation_loop(self) -> None:
        """Background loop for periodic Job reconciliation."""
        from models import AgentExecutionStatus, PipelineStatus

        time.sleep(self._reconciliation_interval)

        while self._reconciliation_running:
            try:
                live_jobs = self.k8s_client.list_containers(all=False)
                live_ids: set[str] = {
                    ci.container_id for ci in live_jobs
                }

                for store in self._reconciliation_stores:
                    try:
                        pipeline_ids: list[str] = store.list_pipelines()
                    except Exception as e:
                        logger.warning(
                            "Periodic reconciliation: could not "
                            "list pipelines",
                            error=str(e),
                        )
                        continue

                    for pipeline_id in pipeline_ids:
                        try:
                            pipeline = store.load_pipeline(pipeline_id)
                        except Exception:
                            continue

                        if pipeline.status != PipelineStatus.RUNNING:
                            continue

                        current_phase_key = (
                            pipeline.current_phase.value
                        )
                        phase_execution = pipeline.phases.get(
                            current_phase_key
                        )
                        if phase_execution is None:
                            continue

                        for agent in phase_execution.agents:
                            if (
                                agent.status
                                == AgentExecutionStatus.RUNNING
                                and agent.container_id
                                and agent.container_id not in live_ids
                            ):
                                actual_exit_code = (
                                    self._get_job_exit_code(
                                        agent.container_id
                                    )
                                )
                                if actual_exit_code == 0:
                                    cid = agent.container_id
                                    if cid not in self._clean_exit_skipped:
                                        logger.info(
                                            "Job exited cleanly "
                                            "(code 0), skipping "
                                            "FAILED reconciliation",
                                            pipeline_id=pipeline_id,
                                            job_name=cid,
                                            agent_role=str(agent.role),
                                        )
                                        self._clean_exit_skipped.add(cid)
                                    continue

                                if actual_exit_code == 143 and (
                                    phase_execution.status
                                    != PipelineStatus.RUNNING
                                ):
                                    cid = agent.container_id
                                    if cid not in self._clean_exit_skipped:
                                        logger.info(
                                            "Job received SIGTERM "
                                            "during phase transition "
                                            "(exit 143), skipping "
                                            "FAILED reconciliation",
                                            pipeline_id=pipeline_id,
                                            job_name=cid,
                                            agent_role=str(agent.role),
                                        )
                                        self._clean_exit_skipped.add(cid)
                                    continue

                                matching_ci = None
                                for ci in phase_execution.containers:
                                    if (
                                        ci.container_id
                                        == agent.container_id
                                    ):
                                        matching_ci = ci
                                        break

                                if matching_ci is not None:
                                    _reconcile_container_state(
                                        store, matching_ci
                                    )
                                else:
                                    logger.debug(
                                        "Stale agent has no matching "
                                        "ContainerInfo",
                                        pipeline_id=pipeline_id,
                                        job_name=agent.container_id,
                                        agent_role=str(agent.role),
                                    )

            except Exception as e:
                logger.warning(
                    "Periodic reconciliation sweep failed",
                    error=str(e),
                )

            time.sleep(self._reconciliation_interval)

    def stop(self) -> None:
        """Stop the monitor and periodic reconciliation."""
        stopped_any = False

        if self._running:
            self._running = False
            if self._thread:
                self._thread.join(timeout=self.check_interval + 1)
                self._thread = None
            stopped_any = True

        if self._reconciliation_running:
            self._reconciliation_running = False
            if self._reconciliation_thread:
                self._reconciliation_thread.join(
                    timeout=self._reconciliation_interval + 1
                )
                self._reconciliation_thread = None
            self._clean_exit_skipped.clear()
            stopped_any = True

        if stopped_any:
            logger.info("Kubernetes monitor stopped")

    def is_running(self) -> bool:
        """Check if monitor is running.

        Returns:
            True if monitor or periodic reconciliation is active.
        """
        return self._running or self._reconciliation_running

    def get_container_status(
        self, container_id: str
    ) -> ContainerStatus | None:
        """Get cached Job status.

        Args:
            container_id: Job name.

        Returns:
            Cached status or None if not tracked.
        """
        return self._job_states.get(container_id)

    def _get_job_exit_code(self, job_name: str) -> int | None:
        """Get the exit code of a Job that is no longer in the live list.

        Queries the Kubernetes API for the Job's actual state. Returns
        the exit code if available, or None if the Job cannot be
        inspected (already deleted, API error, etc.).
        """
        try:
            info = self.k8s_client.get_container_info(job_name)
            return info.exit_code
        except KubernetesClientError:
            return None

    def check_container_health(
        self, container_id: str
    ) -> dict[str, Any]:
        """Check health of a specific Job.

        Args:
            container_id: Job name.

        Returns:
            Health status dictionary.
        """
        try:
            info = self.k8s_client.get_container_info(container_id)
            return {
                "healthy": info.status == ContainerStatus.RUNNING,
                "status": info.status.value,
                "exit_code": info.exit_code,
                "started_at": (
                    info.started_at.isoformat()
                    if info.started_at
                    else None
                ),
                "exited_at": (
                    info.exited_at.isoformat()
                    if info.exited_at
                    else None
                ),
            }
        except PodNotFoundError:
            return {
                "healthy": False,
                "status": "not_found",
                "error": "Job not found",
            }


# Singleton monitor instance
_kubernetes_monitor: KubernetesMonitor | None = None


def get_kubernetes_monitor() -> KubernetesMonitor:
    """Get the singleton Kubernetes monitor.

    Returns:
        KubernetesMonitor instance.
    """
    global _kubernetes_monitor
    if _kubernetes_monitor is None:
        _kubernetes_monitor = KubernetesMonitor()
    return _kubernetes_monitor
