"""SpawnedContainer dataclass + event-job status view (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from dataclasses import dataclass
from typing import Any

import kubernetes_spawner as _pkg
from egg_agent.auth_errors import EX_AUTH_FATAL, EX_RATE_LIMITED
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
        self._FATAL = _event_loop.JOB_OUTCOME_FATAL
        self._RATE_LIMITED = _event_loop.JOB_OUTCOME_RATE_LIMITED
        self._LEGITIMATE = _event_loop.JOB_OUTCOME_LEGITIMATE

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
            # #3373: distinguish a non-retryable credential failure (the agent
            # exited with EX_AUTH_FATAL) from an ordinary crash. Reading the
            # pod's exit code costs one extra list call, but only on the cold
            # path where a Job has already FAILED. Any read failure (or an exit
            # code that doesn't match) falls back to ``abnormal`` — today's
            # behaviour — so this can never manufacture a spurious fatal.
            if self._failed_with_auth_fatal(dedupe_key):
                return self._FATAL
            # #3364 PR C: a TRANSIENT throttle / cap wall (the agent exited
            # EX_RATE_LIMITED) is neither a credential-fatal failure nor an
            # ordinary crash — map it to a distinct rate-limit outcome the
            # supervisor paces across the cap window instead of counting toward
            # the abnormal fail-streak halt. Checked AFTER auth-fatal so a
            # weekly-cap-as-77 still wins. Any other exit code falls through to
            # ``abnormal`` (today's behaviour) — this can never manufacture a
            # spurious rate-limit.
            if self._failed_with_rate_limited(dedupe_key):
                return self._RATE_LIMITED
            # #3665: a SIGTERM (exit 143) from the sandbox's 2-hour agent
            # timeout is a legitimate lifecycle termination, not a crash.
            # Without this check it falls through to ``abnormal``, incrementing
            # the fail-streak budget against a timeout the agent couldn't see
            # coming. Treat it as a legitimate outcome so the event loop
            # does not count it against the streak.
            if self._failed_with_timeout_sigterm(dedupe_key):
                return self._LEGITIMATE
            return self._ABNORMAL
        # Live = PENDING/CREATING/RUNNING — the same single-source set the
        # adoption filter (``_event_dedupe_key_live``) and live-pod accounting
        # use, so a CREATING Job is classified live here too (#3181).
        if any(s in LIVE_POD_STATUSES for s in statuses):
            return self._RUNNING
        if any(s == ContainerStatus.EXITED for s in statuses):
            return self._SUCCESS
        return self._RUNNING

    def _failed_with_auth_fatal(self, dedupe_key: str) -> bool:
        """Return True iff the failed event pod exited with ``EX_AUTH_FATAL``.

        Reads the pod(s) carrying this event's dedupe-key label and checks the
        terminated container's exit code. Best-effort: a list error, a missing
        pod (already GC'd), or an unreadable exit code all return ``False`` so
        the caller falls back to the ordinary ``abnormal`` classification.
        """
        try:
            containers = self._spawner.k8s.list_containers(
                labels={LABEL_EVENT_DEDUPE: _pkg._dedupe_label_value(dedupe_key)}
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to abnormal
            logger.warning(
                "Failed to read pod exit code for event-loop supervision",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return False
        if not isinstance(containers, (list, tuple)):
            return False
        return any(getattr(c, "exit_code", None) == EX_AUTH_FATAL for c in containers)

    def _failed_with_rate_limited(self, dedupe_key: str) -> bool:
        """Return True iff the failed event pod exited with ``EX_RATE_LIMITED``.

        Mirrors :meth:`_failed_with_auth_fatal` for the #3364 transient
        rate-limit path: reads the pod(s) carrying this event's dedupe-key
        label and checks the terminated container's exit code. Best-effort: a
        list error, a missing pod (already GC'd), or an unreadable exit code
        all return ``False`` so the caller falls back to the ordinary
        ``abnormal`` classification.
        """
        try:
            containers = self._spawner.k8s.list_containers(
                labels={LABEL_EVENT_DEDUPE: _pkg._dedupe_label_value(dedupe_key)}
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to abnormal
            logger.warning(
                "Failed to read pod exit code for rate-limit supervision",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return False
        if not isinstance(containers, (list, tuple)):
            return False
        return any(getattr(c, "exit_code", None) == EX_RATE_LIMITED for c in containers)

    def _failed_with_timeout_sigterm(self, dedupe_key: str) -> bool:
        """Return True iff the failed event pod exited with SIGTERM (143).

        #3665: a SIGTERM (exit 143) is produced when the sandbox's
        ``ClaudeConfig.timeout`` (default 7200s = 2h) fires, or when the
        orchestrator sends SIGTERM during phase teardown. Both are legitimate
        lifecycle terminations, not crashes — counting them against the
        fail-streak budget causes false exhaustion escalations. Best-effort:
        a list error, a missing pod (already GC'd), or an unreadable exit code
        all return ``False`` so the caller falls back to the ordinary
        ``abnormal`` classification.
        """
        try:
            containers = self._spawner.k8s.list_containers(
                labels={LABEL_EVENT_DEDUPE: _pkg._dedupe_label_value(dedupe_key)}
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to abnormal
            logger.debug(
                "Failed to read pod exit code for timeout-sigterm check",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return False
        if not isinstance(containers, (list, tuple)):
            return False
        return any(getattr(c, "exit_code", None) == 143 for c in containers)

    def exit_detail_for(self, dedupe_key: str) -> str | None:
        """Return a short operator-facing exit description for a dead pod (#3496).

        Reads the pod(s) carrying this event's dedupe-key label and renders
        their terminated exit codes (e.g. ``exit_code=137``). The event loop
        calls this on the ``abnormal``/``fatal`` observation branch — before
        the Job is reaped — and records it into the supervisor's per-key
        termination history, so the exhaustion escalation can name WHY the
        arm died. Best-effort: a list failure, a missing pod (already GC'd),
        or no readable exit code returns ``None``.
        """
        try:
            containers = self._spawner.k8s.list_containers(
                labels={LABEL_EVENT_DEDUPE: _pkg._dedupe_label_value(dedupe_key)}
            )
        except Exception as exc:  # noqa: BLE001 — detail capture is best-effort
            logger.warning(
                "Failed to read pod exit detail for event-loop supervision",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return None
        if not isinstance(containers, (list, tuple)):
            return None
        codes = sorted(
            {c.exit_code for c in containers if getattr(c, "exit_code", None) is not None}
        )
        if not codes:
            return None
        detail = "exit_code=" + ",".join(str(code) for code in codes)
        # #3665: annotate SIGTERM (143) as a timeout, not a crash
        if 143 in codes:
            detail += " (SIGTERM — likely sandbox timeout or orchestrator teardown)"
        return detail

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
        return self._reap(dedupe_key, only_terminal=True)

    def reap(self, dedupe_key: str) -> int:
        """Delete every Job carrying this dedupe-key label, live or terminal (#3337).

        The same-role serialization path in ``OrchestratorEventLoop`` calls
        this to tear down a *superseded* sibling: a still-RUNNING one-shot Job
        whose event has been overtaken by a newer one for the same role. Unlike
        :meth:`reap_terminated` (which only sweeps FAILED/EXITED stragglers),
        this removes the live pod too, so the role's shared worktree is left to
        the single newest producer. The delete uses force (foreground)
        propagation, which begins teardown of the pod and orders it ahead of the
        Job's own removal — narrowing the overlap window against the newest
        producer, though K8s garbage-collects the pod asynchronously, so it is
        not guaranteed gone by the time this returns. Best-effort, same as
        ``reap_terminated``; returns the number reaped.
        """
        return self._reap(dedupe_key, only_terminal=False)

    def _reap(self, dedupe_key: str, *, only_terminal: bool) -> int:
        """Remove Jobs labelled with ``dedupe_key``; ``only_terminal`` gates status.

        ``only_terminal=True`` removes only FAILED/EXITED Jobs (the #3181
        observe-once sweep); ``only_terminal=False`` removes any matching Job
        regardless of status (the #3337 superseded-sibling teardown).

        Propagation tracks the path: the terminal sweep deletes Jobs whose pod
        has already exited, so background propagation is fine. The supersession
        path (``only_terminal=False``) targets a *still-RUNNING* sibling whose
        whole purpose for being reaped is that it can still write the shared
        worktree; force (foreground) propagation begins teardown of its pod and
        orders that ahead of the Job's own removal (unlike background, which
        removes the owner first and garbage-collects the pod afterward). The pod
        is still garbage-collected asynchronously, so it is not necessarily gone
        by the time the call returns — but the ordering narrows the residual
        overlap window against the newest producer that is about to spawn.
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
            if only_terminal and getattr(job, "status", None) not in (
                ContainerStatus.FAILED,
                ContainerStatus.EXITED,
            ):
                continue
            job_name = getattr(job, "job_name", None) or getattr(job, "container_name", None)
            if not job_name:
                continue
            try:
                self._spawner.remove_agent_job(job_name, force=not only_terminal)
                reaped += 1
            except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                logger.warning(
                    "Failed to reap event Job",
                    dedupe_key=dedupe_key,
                    job_name=job_name,
                    only_terminal=only_terminal,
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
