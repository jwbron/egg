"""SpawnedContainer dataclass + event-job status view (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from dataclasses import dataclass
from typing import Any

import kubernetes_spawner as _pkg
from gateway_client import SessionInfo
from kubernetes_spawner import (
    LABEL_EVENT_DEDUPE,
    logger,
)
from models import LIVE_POD_STATUSES, AgentRole, ContainerInfo, ContainerStatus


class _EventJobStatusView:
    """Maps a one-shot event Job's k8s status onto the loop's outcome vocabulary.

    Constructed via :meth:`KubernetesSpawner.create_event_job_status_view` and
    consumed by ``OrchestratorEventLoop``. Kept module-level
    (not a closure) so it is trivially unit-testable with a stub spawner. The
    outcome strings are sourced from ``event_loop`` so the two sides can never
    drift; the dual-path import mirrors the rest of this module.
    """

    def __init__(self, spawner: Any) -> None:
        # ``spawner`` is the KubernetesSpawner; annotated ``Any`` because this
        # helper is defined ahead of that class (no future-annotations import
        # in this module, so a forward ref would NameError at definition time).
        self._spawner = spawner
        try:
            from orchestrator import event_loop as _event_loop
        except ImportError:
            import event_loop as _event_loop  # type: ignore[no-redef]
        self._RUNNING = _event_loop.JOB_OUTCOME_RUNNING
        self._SUCCESS = _event_loop.JOB_OUTCOME_SUCCESS
        self._ABNORMAL = _event_loop.JOB_OUTCOME_ABNORMAL

    def outcome_for(self, dedupe_key: str) -> str:
        selector = f"{LABEL_EVENT_DEDUPE}={_pkg._dedupe_label_value(dedupe_key)}"
        try:
            jobs = self._spawner.k8s.list_jobs(self._spawner._namespace, label_selector=selector)
        except Exception as exc:  # noqa: BLE001 — observation is best-effort
            logger.warning(
                "Failed to list Jobs for event-loop supervision",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return self._RUNNING
        if not isinstance(jobs, (list, tuple)) or not jobs:
            # No Job found (already GC'd, or an unconfigured mock) — never a
            # failure: treat as still-running so we can't manufacture a streak.
            return self._RUNNING
        statuses = [getattr(j, "status", None) for j in jobs]
        if any(s == ContainerStatus.FAILED for s in statuses):
            return self._ABNORMAL
        # Live = PENDING/CREATING/RUNNING — the same single-source set the
        # adoption filter (``_event_dedupe_key_live``) and live-pod accounting
        # use, so a CREATING Job is classified live here too (#3181).
        if any(s in LIVE_POD_STATUSES for s in statuses):
            return self._RUNNING
        if any(s == ContainerStatus.EXITED for s in statuses):
            return self._SUCCESS
        return self._RUNNING

    def reap_terminated(self, dedupe_key: str) -> int:
        """Delete terminal (FAILED/EXITED) Jobs carrying this dedupe-key label.

        Called by the loop's ``abnormal`` branch right after it records an
        abort. The crashed Job's ``FAILED`` status otherwise lingers for the
        ~600s ``ttlSecondsAfterFinished`` window, and that lingering object is
        what dead-ends the bounded respawn (#3181):

          * the *next* poll's :meth:`outcome_for` would re-read the same FAILED
            Job and re-increment the streak against one dead pod — climbing to
            the exhaustion/AGENT_FAILED threshold without a single real retry;
          * with a co-existing fresh ``RUNNING`` respawn, ``FAILED`` still wins
            in :meth:`outcome_for`, so the streak keeps climbing even after a
            successful respawn.

        Removing the terminated Job here makes the next ``outcome_for`` read
        only the live state. Best-effort: a list/delete failure is logged and
        swallowed — the live-only adoption filter in
        :meth:`KubernetesSpawner._event_dedupe_key_live` is the backstop that
        still lets the respawn create a new Job. Returns the number reaped.
        """
        selector = f"{LABEL_EVENT_DEDUPE}={_pkg._dedupe_label_value(dedupe_key)}"
        try:
            jobs = self._spawner.k8s.list_jobs(self._spawner._namespace, label_selector=selector)
        except Exception as exc:  # noqa: BLE001 — reaping is best-effort
            logger.warning(
                "Failed to list Jobs for event-loop reap",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return 0
        if not isinstance(jobs, (list, tuple)):
            return 0
        reaped = 0
        for job in jobs:
            if getattr(job, "status", None) not in (ContainerStatus.FAILED, ContainerStatus.EXITED):
                continue
            job_name = getattr(job, "job_name", None) or getattr(job, "container_name", None)
            if not job_name:
                continue
            try:
                self._spawner.remove_agent_job(job_name)
                reaped += 1
            except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                logger.warning(
                    "Failed to reap terminated event Job",
                    dedupe_key=dedupe_key,
                    job_name=job_name,
                    error=str(exc),
                )
        return reaped


@dataclass
class SpawnedContainer:
    """Information about a spawned Job with gateway session.

    Reuses the same dataclass as ContainerSpawner for compatibility.
    """

    container_info: ContainerInfo
    session_info: SessionInfo | None
    agent_role: AgentRole
    pipeline_id: str
    environment: dict[str, str]
    # Wall-clock spawn→invoke latency in milliseconds, measured
    # across ``spawn_agent_job`` (worktree re-attach/create + session
    # reuse/register + k8s Job create). Used by the p50<60s budget assertion.
    # ``None`` only on paths that bypass the spawn timer.
    spawn_ms: float | None = None
