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


from kubernetes_client import (
    DEFAULT_NAMESPACE,
    LABEL_ORCHESTRATOR,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
    get_kubernetes_client,
)
from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.kubernetes_monitor")

# Newly-spawned agent pods may briefly appear "missing" to the
# periodic reconciler while the pod transitions through Pending /
# ContainerCreating (image resolution, volume mounts, Agent SDK
# handshake). Skip reconciliation for agents younger than this so
# those benign race windows do not trip a FAILED verdict. See #1760.
POD_STARTUP_GRACE_SECONDS = 60


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
        *,
        docker_client: Any | None = None,  # Backward compat — ignored
    ):
        """Initialize monitor.

        Args:
            k8s_client: Kubernetes client (default: singleton)
            check_interval: Seconds between health checks
            orphan_age_hours: Hours before a Job is considered orphaned
            docker_client: Accepted for backward compatibility. If provided
                and k8s_client is None, used as the k8s_client (the mock
                will satisfy the same interface in tests).
        """
        # Accept docker_client as k8s_client for backward compatibility
        effective_client = k8s_client or docker_client
        if effective_client is not None:
            self.k8s_client = effective_client
        else:
            self.k8s_client = get_kubernetes_client()
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
        with self._lock:
            old_status = self._pod_states.get(pod_id)
            new_status = pod_info.status

            if old_status == new_status:
                return
            self._pod_states[pod_id] = new_status

        # Emit events outside the lock to avoid deadlock with _emit_event
        if new_status == ContainerStatus.RUNNING:
            if old_status is None or old_status == ContainerStatus.PENDING:
                self._emit_event(ContainerEvent(ContainerEvent.STARTED, pod_info))

        elif new_status == ContainerStatus.EXITED:
            if pod_info.exit_code == 0:
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

        # Fire RUNTIME_TICK health checks on state transitions
        self._run_runtime_tick_checks()

    def _run_runtime_tick_checks(self) -> None:
        """Fire RUNTIME_TICK health checks on all running pipelines.

        Called from ``_check_pod`` on container state changes
        and from ``_reconciliation_sweep`` on the periodic interval so the
        post-consensus stall recovery still runs for pipelines where no
        pods are transitioning. Requires that ``set_health_check_runner``
        has been called to wire the runner.
        """
        runner = getattr(self, "_health_check_runner", None)
        if runner is None:
            return

        stores: list[Any] = list(self._reconciliation_stores)
        if not stores:
            return

        try:
            from health_checks.context import PipelineHealthContext
            from health_checks.types import HealthTrigger
        except ImportError:
            return

        for store in stores:
            try:
                for pid in store.list_pipelines():
                    try:
                        pipeline = store.load_pipeline(pid)
                        if pipeline.status.value != "running":
                            continue
                        ctx = PipelineHealthContext(
                            pipeline=pipeline,
                            repo_path=store.repo_path,
                            trigger=HealthTrigger.RUNTIME_TICK.value,
                            docker_client=self.k8s_client,
                            state_store=store,
                        )
                        results = runner.run(ctx, HealthTrigger.RUNTIME_TICK)
                        self._handle_consensus_stall_recovery(results, pipeline, store)
                    except Exception as e:
                        logger.debug(
                            "RUNTIME_TICK check failed for pipeline",
                            pipeline_id=pid,
                            error=str(e),
                        )
            except Exception as e:
                logger.debug("RUNTIME_TICK store iteration error", error=str(e))

    def _check_all_pods(self) -> None:
        """Check all orchestrator-managed pods."""
        try:
            pods = self.k8s_client.list_containers(all=True)
            current_ids = set()

            for pod in pods:
                pod_id = pod.pod_name or pod.container_id
                current_ids.add(pod_id)
                self._check_pod(pod)

            # Check for removed pods (hold lock for dict mutation)
            with self._lock:
                removed_ids = set(self._pod_states.keys()) - current_ids
                for pod_id in removed_ids:
                    del self._pod_states[pod_id]
                    # Prune from _clean_exit_skipped to prevent unbounded growth
                    self._clean_exit_skipped.discard(pod_id)
            for pod_id in removed_ids:
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
        # Sleep before the first sweep
        time.sleep(self._reconciliation_interval)

        while self._reconciliation_running:
            try:
                self._reconciliation_sweep()
            except Exception as e:
                logger.warning(
                    "Periodic reconciliation sweep failed",
                    error=str(e),
                )

            time.sleep(self._reconciliation_interval)

    def _reconciliation_sweep(self) -> None:
        """Run one pass of periodic reconciliation across all stores.

        Extracted from ``_reconciliation_loop`` so that individual sweeps
        can be exercised deterministically in unit tests.
        """
        from models import AgentExecutionStatus, PipelineStatus

        live_pods = self.k8s_client.list_containers(all=False)
        live_ids: set[str] = set()
        for pod in live_pods:
            live_ids.add(pod.container_id)
            if pod.pod_name:
                live_ids.add(pod.pod_name)
            if pod.job_name:
                live_ids.add(pod.job_name)

        # Agents register ``container_id`` as the Job UID
        # (``create_container`` returns ``job.metadata.uid``),
        # but ``list_containers`` returns *pod* UIDs. Without
        # also indexing Job UIDs, every running agent is
        # perpetually identified as "missing" and the next
        # block's detection path runs on every sweep.
        namespace = getattr(self.k8s_client, "namespace", DEFAULT_NAMESPACE)
        try:
            live_jobs = self.k8s_client.list_jobs(
                namespace,
                label_selector=f"{LABEL_ORCHESTRATOR}=true",
            )
            for job in live_jobs:
                live_ids.add(job.container_id)
                if job.job_name:
                    live_ids.add(job.job_name)
        except Exception as e:
            logger.debug(
                "Periodic reconciliation: list_jobs failed; live_ids will miss Job UIDs this sweep",
                error=str(e),
            )

        now = datetime.now(UTC)

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

                # Reconcile stale RUNNING records on non-RUNNING
                # pipelines too (#1840). Skip terminal pipelines with
                # nothing to clean up — the common case.
                current_phase_key = pipeline.current_phase.value
                phase_execution = pipeline.phases.get(current_phase_key)

                if pipeline.status not in (
                    PipelineStatus.RUNNING,
                    PipelineStatus.AWAITING_HUMAN,
                ):
                    has_running_records = phase_execution is not None and any(
                        a.status == AgentExecutionStatus.RUNNING for a in phase_execution.agents
                    )
                    if not has_running_records:
                        continue
                if phase_execution is None:
                    continue

                for agent in phase_execution.agents:
                    if not (
                        agent.status == AgentExecutionStatus.RUNNING
                        and agent.container_id
                        and agent.container_id not in live_ids
                    ):
                        continue

                    # Grace period for newly-spawned agents.
                    # Pods transition Pending → ContainerCreating
                    # → Running over ~5–30s; reconciling during
                    # that window produces false positives.
                    if agent.started_at is not None:
                        age = (now - agent.started_at).total_seconds()
                        if age < POD_STARTUP_GRACE_SECONDS:
                            continue

                    # Defense-in-depth: before marking FAILED,
                    # confirm the pod is actually terminated.
                    # A pod is genuinely exited only when its
                    # container has ``state.terminated`` set —
                    # Pending / Running / CreateContainerConfigError
                    # (waiting) are not exits. See #1760.
                    pod_gone = False
                    info: ContainerInfo | None = None
                    try:
                        info = self.k8s_client.get_container_info(agent.container_id)
                    except PodNotFoundError:
                        pod_gone = True
                    except KubernetesClientError as e:
                        logger.debug(
                            "Reconciliation: could not fetch pod info, skipping this sweep",
                            container_id=agent.container_id,
                            error=str(e),
                        )
                        continue

                    actual_exit_code: int | None = None
                    if info is not None:
                        if info.status in (
                            ContainerStatus.PENDING,
                            ContainerStatus.CREATING,
                            ContainerStatus.RUNNING,
                        ):
                            logger.debug(
                                "Reconciliation: pod still alive, skipping FAILED reconciliation",
                                pipeline_id=pipeline_id,
                                container_id=agent.container_id[:12],
                                pod_status=info.status.value,
                            )
                            continue
                        # EXITED / FAILED / REMOVED — the pod has
                        # reached a terminal state. Require exited_at
                        # to guard against partial status where only
                        # phase was mapped.
                        if info.exited_at is None:
                            logger.debug(
                                "Reconciliation: terminal status "
                                "without exited_at, skipping this sweep",
                                pipeline_id=pipeline_id,
                                container_id=agent.container_id[:12],
                                pod_status=info.status.value,
                            )
                            continue
                        actual_exit_code = info.exit_code

                    if actual_exit_code == 0:
                        if agent.container_id not in self._clean_exit_skipped:
                            logger.info(
                                "Pod exited cleanly (code 0), skipping FAILED reconciliation",
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

                    matching_ci = None
                    for ci in phase_execution.containers:
                        if ci.container_id == agent.container_id:
                            matching_ci = ci
                            break

                    if matching_ci is not None:
                        # Build the ContainerInfo we hand to
                        # ``_reconcile_pod_state`` from the *live* pod
                        # observation, not from the stored record.  The
                        # stored ``matching_ci`` still has
                        # ``exit_code=None`` for an agent that was
                        # RUNNING up to this sweep, so the reconciler's
                        # ``_classify_exit`` would only see "code
                        # unknown" and never reach its clean-exit branch
                        # — losing the actual exit code in the saved
                        # error string and miscategorising
                        # sweep-detected clean exits.  When the pod is
                        # gone (PodNotFoundError) we fall back to a
                        # synthesised record; the exit code is
                        # genuinely unknown in that case.  See #2210.
                        if info is not None:
                            observed_info = info
                        else:
                            observed_info = ContainerInfo(
                                container_id=agent.container_id,
                                container_name=matching_ci.container_name,
                                status=ContainerStatus.REMOVED,
                                exit_code=None,
                                exited_at=now,
                            )
                        # 143 in a RUNNING phase reaches this branch
                        # (the non-RUNNING-phase carve-out at L527 only
                        # skips when the phase has already moved past
                        # RUNNING).  ``_reconcile_pod_state`` →
                        # ``_classify_exit`` will mark such an agent
                        # COMPLETE, not FAILED, so log accordingly
                        # rather than asserting "marking agent FAILED".
                        is_clean = actual_exit_code in (0, 143)
                        if is_clean:
                            logger.info(
                                "Reconciliation: pod terminated cleanly, marking agent COMPLETE",
                                pipeline_id=pipeline_id,
                                container_id=agent.container_id,
                                agent_role=str(agent.role),
                                exit_code=actual_exit_code,
                                pod_gone=pod_gone,
                            )
                        else:
                            logger.warning(
                                "Reconciliation: pod terminated, marking agent FAILED",
                                pipeline_id=pipeline_id,
                                container_id=agent.container_id,
                                agent_role=str(agent.role),
                                exit_code=actual_exit_code,
                                pod_gone=pod_gone,
                            )
                        _reconcile_pod_state(store, observed_info)
                    else:
                        logger.debug(
                            "Stale agent has no matching ContainerInfo",
                            pipeline_id=pipeline_id,
                            container_id=agent.container_id,
                            agent_role=str(agent.role),
                        )

        # Fire RUNTIME_TICK checks every sweep so pipelines where no pods
        # are transitioning (e.g. all agents quietly polling post-BRC
        # consensus) still exercise the consensus-stall recovery path.
        # Without this, a stuck post-consensus pipeline never recovers
        # because _check_pod is the only other call site. (#1813)
        self._run_runtime_tick_checks()

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
        with self._lock:
            return self._pod_states.get(pod_id)

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

    def set_health_check_runner(self, runner: Any, repo_paths: list[Any] | None = None) -> None:
        """Wire a health-check runner for RUNTIME_TICK checks.

        Stores the runner and repo paths so that health checks can be
        triggered when container state changes are detected by the monitor.
        """
        self._health_check_runner = runner
        self._health_repo_paths = repo_paths or []

    # ------------------------------------------------------------------
    # Consensus stall recovery (ported from ContainerMonitor)
    # ------------------------------------------------------------------

    def _handle_consensus_stall_recovery(
        self,
        results: list[Any],
        pipeline: Any,
        store: Any,
    ) -> None:
        """Drive phase transition recovery when consensus stall is detected.

        Two-track recovery:
        1. Attempt tracker reconstruction so the polling loop picks up consensus.
        2. If reconstruction fails, aggressive recovery: reload the pipeline with
           optimistic locking and mark agents/phase COMPLETE.
        """
        from health_checks.types import HealthStatus  # type: ignore[import-untyped]

        for result in results:
            if result.check_name != "consensus_stall":
                continue
            if result.status != HealthStatus.DEGRADED:
                continue

            details = result.details or {}
            pipeline_id = details.get("pipeline_id")

            # Track 1: attempt tracker reconstruction (moved from health check
            # to keep the check purely diagnostic).
            if self._attempt_tracker_reconstruction(pipeline_id, pipeline):
                logger.warning(
                    "Consensus stall detected — Track 1 (tracker reconstruction) "
                    "succeeded, polling loop should recover",
                    pipeline_id=pipeline_id,
                )
                return

            logger.warning(
                "Consensus stall detected — performing aggressive recovery",
                pipeline_id=pipeline_id,
            )
            try:
                from models import AgentExecutionStatus, PipelineStatus
                from state_store import VersionConflictError

                phase_key = details.get("phase")
                if phase_key is None:
                    return

                # ``slice_id`` is optional in the health-check details dict
                # (the consensus_stall check is currently pipeline-level
                # only, but #2422's audit asks every walker of
                # ``phase_exec.agents`` to scope by ``(role, slice_id)`` so
                # the moment it becomes slice-aware this path doesn't flip
                # other slices' agents to COMPLETE).
                stall_slice_id = details.get("slice_id")

                fresh_pipeline = store.load_pipeline(pipeline_id)
                original_version = fresh_pipeline.version

                phase_exec = fresh_pipeline.phases.get(phase_key)
                if phase_exec is None:
                    return

                if phase_exec.status != PipelineStatus.RUNNING:
                    logger.info(
                        "Phase already transitioned, skipping aggressive recovery",
                        pipeline_id=pipeline_id,
                        phase=phase_key,
                    )
                    return

                now = datetime.now(UTC)
                completed_container_ids: set[str] = set()
                for agent in phase_exec.agents:
                    if getattr(agent, "slice_id", None) != stall_slice_id:
                        continue
                    if agent.status in (AgentExecutionStatus.RUNNING, AgentExecutionStatus.FAILED):
                        agent.status = AgentExecutionStatus.COMPLETE
                        agent.completed_at = now
                        if agent.container_id:
                            completed_container_ids.add(agent.container_id)

                # Synthetically mark containers EXITED (exit_code=0) for
                # state-shape consistency: the stored record matches
                # what a normal clean exit would have written, mirroring
                # the _update_agents_complete path in
                # routes/pipelines.py (see #1294).  This is purely
                # cosmetic / observability — the periodic reconciler
                # already skips these agents because they are now
                # COMPLETE (the sweep at line 451-454 only reconciles
                # ``agent.status == RUNNING``), and as of #2210 the
                # reconciler does not mutate pipeline.status anyway.
                pods_to_stop: list[str] = []
                for ci in phase_exec.containers:
                    if (
                        ci.container_id in completed_container_ids
                        and ci.status == ContainerStatus.RUNNING
                    ):
                        ci.status = ContainerStatus.EXITED
                        ci.exit_code = 0
                        ci.exited_at = now
                        pods_to_stop.append(ci.container_id)

                # Close the open cycle so cycle_timings reflects actual
                # duration, matching the failure-path writes in
                # routes/pipelines.py.  Left unset by the prior recovery
                # path, which made get_phase reports misleading (#1935).
                if phase_exec.cycle_timings and phase_exec.cycle_timings[-1].completed_at is None:
                    phase_exec.cycle_timings[-1].completed_at = now

                phase_exec.status = PipelineStatus.COMPLETE
                phase_exec.completed_at = now

                store.save_pipeline(fresh_pipeline, expected_version=original_version)
                logger.info(
                    "Aggressive consensus stall recovery complete",
                    pipeline_id=pipeline_id,
                    phase=phase_key,
                    pods_to_stop=len(pods_to_stop),
                )

                # Stop pods so the polling loop in _run_concurrent_phase
                # observes the exits on its next tick and returns, letting
                # _run_pipeline drive the post-phase transition (HITL gate
                # or advance_phase).  Without this, containers stayed
                # RUNNING after recovery and the pipeline sat with
                # phase.completed_at=null until a human intervened (#1935).
                for container_id in pods_to_stop:
                    try:
                        self.k8s_client.stop_container(container_id, timeout=10)
                    except Exception:
                        logger.error(
                            "Failed to stop pod during aggressive recovery — "
                            "stall check will not retry; manual intervention may be needed",
                            pipeline_id=pipeline_id,
                            container_id=container_id[:12],
                            exc_info=True,
                        )
            except VersionConflictError:
                # Expected in concurrent environments — another writer updated
                # the pipeline. Reload and check if the phase already transitioned.
                logger.info(
                    "Version conflict during consensus stall recovery — re-checking pipeline state",
                    pipeline_id=pipeline_id,
                    phase=phase_key,
                )
                try:
                    reloaded = store.load_pipeline(pipeline_id)
                    reloaded_phase = reloaded.phases.get(phase_key)
                    if reloaded_phase and reloaded_phase.status == PipelineStatus.RUNNING:
                        logger.warning(
                            "Phase still RUNNING after version conflict — recovery may need retry",
                            pipeline_id=pipeline_id,
                            phase=phase_key,
                        )
                    else:
                        logger.info(
                            "Phase already transitioned (concurrent writer) — no recovery needed",
                            pipeline_id=pipeline_id,
                            phase=phase_key,
                        )
                except Exception:
                    pass
            except Exception:
                logger.warning(
                    "Aggressive consensus stall recovery failed",
                    pipeline_id=pipeline_id,
                    exc_info=True,
                )
            return

    @staticmethod
    def _attempt_tracker_reconstruction(pipeline_id: str | None, pipeline: Any) -> bool:
        """Try to reconstruct the consensus tracker from messages.

        Returns True when the polling loop can handle things: either the
        tracker was missing and we successfully rebuilt it from messages,
        or it exists but reports incomplete (the polling loop will keep
        watching it).

        Returns False (let Track 2 fire) when the tracker already exists
        and reports ``is_complete=True``.  ``ConsensusStallCheck`` only
        fires DEGRADED in that exact state, so an existing complete
        tracker is not something the polling loop is going to pick up —
        it already had its chance.  Reconstructing nothing and reporting
        success here is what stalled pipeline ``issue-1748`` (see #1749).
        Also returns False when ``tracker.evaluate()`` raises, treating
        the tracker as broken.
        """
        try:
            from peer_consensus import (  # type: ignore[import-untyped]
                get_peer_consensus_tracker,
                reconstruct_tracker_from_messages,
            )
            from review_graph import get_review_graph_for_phase  # type: ignore[import-untyped]

            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker is not None:
                try:
                    evaluation = tracker.evaluate()
                except Exception:
                    # Tracker is broken — let Track 2 take over.
                    return False
                if evaluation.get("is_complete", False):
                    # Tracker agrees with the health check — Track 1 would
                    # be a no-op.  Fall through so Track 2 actually drives
                    # the transition.
                    return False
                # Tracker exists but reports incomplete — the polling loop
                # will keep watching it; nothing for Track 2 to do.
                return True

            current_phase = pipeline.current_phase
            phase_value = current_phase.value
            graph = get_review_graph_for_phase(phase_value, repo=pipeline.repo)
            reconstructed = reconstruct_tracker_from_messages(pipeline_id, graph)
            return reconstructed is not None
        except Exception:
            logger.debug(
                "Tracker reconstruction failed",
                pipeline_id=pipeline_id,
                exc_info=True,
            )
            return False


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


def _classify_exit(exit_code: int | None) -> tuple[bool, str]:
    """Classify a container exit code as clean or failed.

    Returns ``(is_clean, error_message)``.

    - exit_code 0 (normal exit) and 143 (SIGTERM, orchestrator-initiated
      stop) are treated as clean — these are the post-BRC and
      teardown-shutdown cases respectively.
    - Anything else is a failure; the error string includes the actual
      exit code so observers can distinguish OOM, crash, etc.

    Sub-record reconciliation only — the pipeline-level decision about
    whether the pipeline itself has failed lives in the BRC poll loop
    (see ``orchestrator/routes/pipelines.py::_run_concurrent_phase``),
    which has consensus context this monitor lacks.  See #2210.
    """
    if exit_code in (0, 143):
        return True, ""
    code_repr = "unknown" if exit_code is None else str(exit_code)
    return (
        False,
        f"Pod exited with code {code_repr} — detected by Kubernetes runtime monitor",
    )


def _reconcile_pod_state(store: Any, container_info: ContainerInfo) -> bool:
    """Update pipeline state for a single pod that has exited.

    Scans pipelines for a container matching the given container_info
    and reconciles stale RUNNING agent/container records based on the
    pod's exit code:

    - exit_code 0 / 143 → agent COMPLETE, container EXITED (clean exit
      after BRC protocol work, or orchestrator-initiated SIGTERM).
    - any other exit code → agent FAILED with the code in the error
      string, container FAILED.

    The pipeline's own ``status`` is never mutated here.  That decision
    belongs to the BRC poll loop in
    ``orchestrator/routes/pipelines.py::_run_concurrent_phase``, which
    has full consensus context.  See #2210 for why making that call
    from here was wrong: the K8s monitor cannot tell a clean post-BRC
    exit apart from a crash without consulting consensus state, so it
    used to escalate the pipeline to FAILED on agents that had finished
    their protocol obligations and exited 0.

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

            # Reconcile stale RUNNING agent/container records even on
            # non-RUNNING pipelines — a pipeline that went FAILED (or
            # sits at AWAITING_HUMAN) can still have records stuck at
            # RUNNING from aborted cleanup paths (#1837/#1840).
            # Optimization: skip terminal pipelines that have nothing
            # to clean up — the common case.
            if pipeline.status not in (
                PipelineStatus.RUNNING,
                PipelineStatus.AWAITING_HUMAN,
            ):
                has_running_records = any(
                    ci.status == ContainerStatus.RUNNING
                    for pe in pipeline.phases.values()
                    for ci in pe.containers
                ) or any(
                    a.status == AgentExecutionStatus.RUNNING
                    for pe in pipeline.phases.values()
                    for a in pe.agents
                )
                if not has_running_records:
                    continue

            changed = False

            for phase_execution in pipeline.phases.values():
                complete_agent_cids = {
                    a.container_id
                    for a in phase_execution.agents
                    if a.status == AgentExecutionStatus.COMPLETE and a.container_id
                }

                is_clean_exit, agent_error = _classify_exit(container_info.exit_code)

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
                        if is_clean_exit:
                            logger.info(
                                "Runtime reconciliation: pod exited cleanly, marking EXITED",
                                pipeline_id=pipeline_id,
                                container_id=container_info.container_id[:12],
                                exit_code=container_info.exit_code,
                            )
                            ci.status = ContainerStatus.EXITED
                        else:
                            logger.warning(
                                "Runtime reconciliation: pod exited with non-zero code, marking FAILED",
                                pipeline_id=pipeline_id,
                                container_id=container_info.container_id,
                                exit_code=container_info.exit_code,
                            )
                            ci.status = ContainerStatus.FAILED
                        ci.exit_code = (
                            container_info.exit_code if container_info.exit_code is not None else -1
                        )
                        ci.exited_at = container_info.exited_at or datetime.now(UTC)
                        changed = True

                for agent in phase_execution.agents:
                    if (
                        agent.status == AgentExecutionStatus.RUNNING
                        and agent.container_id == container_info.container_id
                    ):
                        if is_clean_exit:
                            logger.info(
                                "Runtime reconciliation: agent pod exited cleanly, marking COMPLETE",
                                pipeline_id=pipeline_id,
                                agent_role=str(agent.role),
                                container_id=container_info.container_id,
                                exit_code=container_info.exit_code,
                            )
                            agent.status = AgentExecutionStatus.COMPLETE
                        else:
                            logger.warning(
                                "Runtime reconciliation: agent pod exited with non-zero code, marking FAILED",
                                pipeline_id=pipeline_id,
                                agent_role=str(agent.role),
                                container_id=container_info.container_id,
                                exit_code=container_info.exit_code,
                            )
                            agent.status = AgentExecutionStatus.FAILED
                            agent.error = agent_error
                        agent.completed_at = datetime.now(UTC)
                        changed = True

            if changed:
                # The K8s monitor never mutates pipeline.status here.
                # Pipeline-level FAILED decisions belong to the BRC poll
                # loop, which has consensus context (#2210).  Sub-record
                # updates above are sufficient for this layer.
                try:
                    store.save_pipeline(
                        pipeline,
                        expected_version=pipeline.version,
                    )
                    logger.warning(
                        "Runtime reconciliation: pipeline records updated",
                        pipeline_id=pipeline_id,
                        pipeline_status=pipeline.status.value,
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
    is a graceful exit that needs no sub-record correction.  Since
    #2210 this handler never mutates ``pipeline.status``; it only
    reconciles agent + container records via ``_reconcile_pod_state``.
    Pipeline-level FAILED decisions belong to the BRC poll loop, which
    has consensus context.

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
