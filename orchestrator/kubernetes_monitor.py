"""
Kubernetes pod health monitoring and cleanup.

Monitors agent pod health via periodic polling, detects state transitions,
and fires callbacks. Replaces ContainerMonitor for Kubernetes deployments.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models import Pipeline
    from state_store import StateStore

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


from kubernetes_client import (
    LABEL_ORCHESTRATOR,
    LABEL_PIPELINE_ID,
    JobOperationError,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
    get_kubernetes_client,
)
from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.kubernetes_monitor")


class ContainerEvent:
    """Event representing a pod/container state change.

    Uses the same event type constants as container_monitor.ContainerEvent
    for compatibility with existing event handlers.
    """

    STARTED = "started"
    STOPPED = "stopped"
    EXITED = "exited"
    FAILED = "failed"
    REMOVED = "removed"
    UNHEALTHY = "unhealthy"

    def __init__(
        self,
        event_type: str,
        container_info: ContainerInfo,
        timestamp: datetime | None = None,
        data: dict[str, Any] | None = None,
    ):
        self.event_type = event_type
        self.container_info = container_info
        self.timestamp = timestamp or datetime.now(UTC)
        self.data = data or {}


EventHandler = Callable[[ContainerEvent], None]


class KubernetesMonitor:
    """Monitors Kubernetes pod health and lifecycle.

    Periodically polls pod status via the Kubernetes API and invokes
    handlers for state changes. Automatically cleans up orphaned Jobs.
    """

    def __init__(
        self,
        k8s_client: KubernetesClient | None = None,
        check_interval: int = 10,
        orphan_age_hours: int = 24,
    ):
        """Initialize monitor.

        Args:
            k8s_client: Kubernetes client (default: singleton)
            check_interval: Seconds between health checks
            orphan_age_hours: Hours before a Job is considered orphaned
        """
        self.k8s_client = k8s_client or get_kubernetes_client()
        self.check_interval = check_interval
        self.orphan_age_hours = orphan_age_hours

        self._handlers: list[EventHandler] = []
        self._pod_states: dict[str, ContainerStatus] = {}
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
            handler: Function to call on pod events
        """
        with self._lock:
            self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        """Remove an event handler.

        Args:
            handler: Handler to remove
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _emit_event(self, event: ContainerEvent) -> None:
        """Emit an event to all handlers.

        Args:
            event: Event to emit
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
                    pod_name=event.container_info.pod_name,
                    error=str(e),
                )

    def _check_pod(self, pod_info: ContainerInfo) -> None:
        """Check a single pod and emit events for state changes.

        Args:
            pod_info: Pod information from k8s API
        """
        pod_id = pod_info.pod_name or pod_info.container_id
        old_status = self._pod_states.get(pod_id)
        new_status = pod_info.status

        if old_status != new_status:
            self._pod_states[pod_id] = new_status

            # Emit appropriate event based on state transition
            if new_status == ContainerStatus.RUNNING:
                if old_status is None or old_status == ContainerStatus.PENDING:
                    self._emit_event(ContainerEvent(ContainerEvent.STARTED, pod_info))

            elif new_status == ContainerStatus.EXITED:
                # Succeeded — clean exit
                if pod_info.exit_code == 0 or pod_info.exit_code is None:
                    self._emit_event(ContainerEvent(ContainerEvent.STOPPED, pod_info))
                else:
                    self._emit_event(
                        ContainerEvent(
                            ContainerEvent.FAILED,
                            pod_info,
                            data={"exit_code": pod_info.exit_code},
                        )
                    )

            elif new_status == ContainerStatus.FAILED:
                self._emit_event(
                    ContainerEvent(
                        ContainerEvent.FAILED,
                        pod_info,
                        data={"exit_code": pod_info.exit_code},
                    )
                )

    def _check_all_pods(self) -> None:
        """Check all orchestrator-managed pods."""
        try:
            pods = self.k8s_client.list_containers(all=True)
            current_ids = set()

            for pod in pods:
                pod_id = pod.pod_name or pod.container_id
                current_ids.add(pod_id)
                self._check_pod(pod)

            # Check for removed pods
            removed_ids = set(self._pod_states.keys()) - current_ids
            for pod_id in removed_ids:
                del self._pod_states[pod_id]
                logger.info("Pod removed", pod_id=pod_id)

        except KubernetesClientError as e:
            logger.error("Pod check failed", error=str(e))

    def _cleanup_orphaned(self) -> int:
        """Remove orphaned Jobs.

        Returns:
            Number of Jobs removed
        """
        try:
            return self.k8s_client.cleanup_orphaned_containers(
                max_age_hours=self.orphan_age_hours,
            )
        except KubernetesClientError as e:
            logger.error("Orphan cleanup failed", error=str(e))
            return 0

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        cleanup_counter = 0
        cleanup_interval = 60  # Check for orphans every 60 iterations

        while self._running:
            self._check_all_pods()

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
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

        logger.info(
            "Kubernetes monitor started",
            check_interval=self.check_interval,
        )

    def start_periodic_reconciliation(self, stores: Any, interval: int = 30) -> None:
        """Start a background thread that periodically reconciles stale pods.

        Every *interval* seconds, lists all RUNNING pipelines across all
        stores and checks whether pods marked RUNNING in the current
        phase still exist in Kubernetes. Missing pods are reconciled.

        Args:
            stores: StateStore instance or list of StateStore instances.
            interval: Seconds between reconciliation sweeps (default 30).
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
            "Periodic pod reconciliation started",
            interval=interval,
        )

    def _reconciliation_loop(self) -> None:
        """Background loop for periodic pod reconciliation."""
        from models import AgentExecutionStatus, PipelineStatus

        # Sleep before the first sweep
        time.sleep(self._reconciliation_interval)

        while self._reconciliation_running:
            try:
                live_pods = self.k8s_client.list_containers(all=False)
                live_ids: set[str] = set()
                for pod in live_pods:
                    live_ids.add(pod.container_id)
                    if pod.pod_name:
                        live_ids.add(pod.pod_name)
                    if pod.job_name:
                        live_ids.add(pod.job_name)

                for store in self._reconciliation_stores:
                    try:
                        pipeline_ids: list[str] = store.list_pipelines()
                    except Exception as e:
                        logger.warning(
                            "Periodic reconciliation: could not list pipelines",
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

                        current_phase_key = pipeline.current_phase.value
                        phase_execution = pipeline.phases.get(current_phase_key)
                        if phase_execution is None:
                            continue

                        for agent in phase_execution.agents:
                            if (
                                agent.status == AgentExecutionStatus.RUNNING
                                and agent.container_id
                                and agent.container_id not in live_ids
                            ):
                                # Check actual exit code
                                actual_exit_code = self._get_pod_exit_code(
                                    agent.container_id
                                )
                                if actual_exit_code == 0:
                                    if agent.container_id not in self._clean_exit_skipped:
                                        logger.info(
                                            "Pod exited cleanly (code 0), "
                                            "skipping FAILED reconciliation",
                                            pipeline_id=pipeline_id,
                                            container_id=agent.container_id,
                                            agent_role=str(agent.role),
                                        )
                                        self._clean_exit_skipped.add(agent.container_id)
                                    continue

                                if actual_exit_code == 143 and (
                                    phase_execution.status != PipelineStatus.RUNNING
                                ):
                                    if agent.container_id not in self._clean_exit_skipped:
                                        logger.info(
                                            "Pod received SIGTERM during phase "
                                            "transition (exit 143), skipping FAILED "
                                            "reconciliation",
                                            pipeline_id=pipeline_id,
                                            container_id=agent.container_id,
                                            agent_role=str(agent.role),
                                        )
                                        self._clean_exit_skipped.add(agent.container_id)
                                    continue

                                # Find matching ContainerInfo
                                matching_ci = None
                                for ci in phase_execution.containers:
                                    if ci.container_id == agent.container_id:
                                        matching_ci = ci
                                        break

                                if matching_ci is not None:
                                    _reconcile_pod_state(store, matching_ci)
                                else:
                                    logger.debug(
                                        "Stale agent has no matching ContainerInfo",
                                        pipeline_id=pipeline_id,
                                        container_id=agent.container_id,
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
                self._reconciliation_thread.join(timeout=self._reconciliation_interval + 1)
                self._reconciliation_thread = None
            self._clean_exit_skipped.clear()
            stopped_any = True

        if stopped_any:
            logger.info("Kubernetes monitor stopped")

    def is_running(self) -> bool:
        """Check if monitor is running.

        Returns:
            True if monitor or periodic reconciliation is active
        """
        return self._running or self._reconciliation_running

    def get_pod_status(self, pod_id: str) -> ContainerStatus | None:
        """Get cached pod status.

        Args:
            pod_id: Pod name or container ID

        Returns:
            Cached status or None if not tracked
        """
        return self._pod_states.get(pod_id)

    def _get_pod_exit_code(self, container_id: str) -> int | None:
        """Get the exit code of a pod that is no longer in the live list.

        Args:
            container_id: Container/Job identifier

        Returns:
            Exit code if available, None otherwise.
        """
        try:
            info = self.k8s_client.get_container_info(container_id)
            return info.exit_code
        except KubernetesClientError:
            return None

    def check_container_health(self, container_id: str) -> dict[str, Any]:
        """Check health of a specific pod/Job.

        Args:
            container_id: Container/Job identifier

        Returns:
            Health status dictionary
        """
        try:
            info = self.k8s_client.get_container_info(container_id)
            return {
                "healthy": info.status == ContainerStatus.RUNNING,
                "status": info.status.value,
                "exit_code": info.exit_code,
                "started_at": info.started_at.isoformat() if info.started_at else None,
                "exited_at": info.exited_at.isoformat() if info.exited_at else None,
                "pod_name": info.pod_name,
                "job_name": info.job_name,
            }
        except PodNotFoundError:
            return {
                "healthy": False,
                "status": "not_found",
                "error": "Pod/Job not found",
            }
        except KubernetesClientError as e:
            return {
                "healthy": False,
                "status": "error",
                "error": str(e),
            }


# Singleton monitor instance
_kubernetes_monitor: KubernetesMonitor | None = None


def get_kubernetes_monitor() -> KubernetesMonitor:
    """Get the singleton Kubernetes monitor.

    Returns:
        KubernetesMonitor instance
    """
    global _kubernetes_monitor
    if _kubernetes_monitor is None:
        _kubernetes_monitor = KubernetesMonitor()
    return _kubernetes_monitor


def _reconcile_pod_state(store: Any, container_info: ContainerInfo) -> bool:
    """Update pipeline state for a single pod that has exited.

    Scans all RUNNING pipelines for a container matching the given
    container_info and marks the container and its agent as FAILED.

    Args:
        store: StateStore instance
        container_info: Info about the exited/failed pod

    Returns:
        True if any pipeline state was updated
    """
    from models import AgentExecutionStatus, PipelineStatus
    from state_store import VersionConflictError, get_pipeline_state_lock

    try:
        pipeline_ids: list[str] = store.list_pipelines()
    except Exception as e:
        logger.warning(
            "Runtime reconciliation: could not list pipelines",
            error=str(e),
        )
        return False

    for pipeline_id in pipeline_ids:
        with get_pipeline_state_lock(pipeline_id):
            try:
                pipeline = store.load_pipeline(pipeline_id)
            except Exception:
                continue

            if pipeline.status != PipelineStatus.RUNNING:
                continue

            changed = False

            for phase_execution in pipeline.phases.values():
                complete_agent_cids = {
                    a.container_id
                    for a in phase_execution.agents
                    if a.status == AgentExecutionStatus.COMPLETE and a.container_id
                }

                for ci in phase_execution.containers:
                    if (
                        ci.container_id == container_info.container_id
                        and ci.status == ContainerStatus.RUNNING
                    ):
                        if ci.container_id in complete_agent_cids:
                            logger.info(
                                "Runtime reconciliation: skipping pod whose agent is COMPLETE",
                                pipeline_id=pipeline_id,
                                container_id=ci.container_id[:12],
                            )
                            continue
                        if (
                            container_info.exit_code == 143
                            and phase_execution.status != PipelineStatus.RUNNING
                        ):
                            logger.info(
                                "Runtime reconciliation: SIGTERM (143) during "
                                "completed phase, skipping FAILED reconciliation",
                                pipeline_id=pipeline_id,
                                container_id=container_info.container_id[:12],
                            )
                            continue
                        logger.warning(
                            "Runtime reconciliation: pod exited, marking FAILED",
                            pipeline_id=pipeline_id,
                            container_id=container_info.container_id[:12],
                        )
                        ci.status = ContainerStatus.FAILED
                        ci.exit_code = (
                            container_info.exit_code
                            if container_info.exit_code is not None
                            else -1
                        )
                        ci.exited_at = container_info.exited_at or datetime.now(UTC)
                        changed = True

                for agent in phase_execution.agents:
                    if (
                        agent.status == AgentExecutionStatus.RUNNING
                        and agent.container_id == container_info.container_id
                    ):
                        if (
                            container_info.exit_code == 143
                            and phase_execution.status != PipelineStatus.RUNNING
                        ):
                            continue
                        logger.warning(
                            "Runtime reconciliation: agent pod exited, marking FAILED",
                            pipeline_id=pipeline_id,
                            agent_role=str(agent.role),
                            container_id=container_info.container_id[:12],
                        )
                        agent.status = AgentExecutionStatus.FAILED
                        agent.completed_at = datetime.now(UTC)
                        agent.error = (
                            "Pod exited unexpectedly during execution — "
                            "detected by Kubernetes runtime monitor"
                        )
                        changed = True

            if changed:
                pipeline.status = PipelineStatus.FAILED
                pipeline.error = (
                    "Pipeline marked FAILED: agent pod exited unexpectedly "
                    "during execution. Restart via POST /pipelines/{id}/start."
                )
                try:
                    store.save_pipeline(
                        pipeline,
                        expected_version=pipeline.version,
                    )
                    logger.warning(
                        "Runtime reconciliation: pipeline marked FAILED",
                        pipeline_id=pipeline_id,
                    )
                    return True
                except VersionConflictError:
                    logger.warning(
                        "Runtime reconciliation: version conflict, skipping "
                        "(concurrent writer updated pipeline)",
                        pipeline_id=pipeline_id,
                    )
                    return False
                except Exception as e:
                    logger.error(
                        "Runtime reconciliation: could not save pipeline",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
                    return False

    return False


def create_pipeline_reconciliation_handler(repo_path: str) -> EventHandler:
    """Create handler that updates pipeline state when pods exit.

    Only FAILED events trigger reconciliation — STOPPED (exit code 0)
    represents a graceful exit and should not mark pipelines as failed.

    Args:
        repo_path: Path to the repository (for StateStore access)

    Returns:
        Event handler function
    """

    def handler(event: ContainerEvent) -> None:
        if event.event_type != ContainerEvent.FAILED:
            return

        from state_store import get_state_store

        store = get_state_store(repo_path)
        _reconcile_pod_state(store, event.container_info)

    return handler
