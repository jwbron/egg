"""Restart budget + agent-job restart (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from typing import TYPE_CHECKING

import kubernetes_spawner as _pkg
from kubernetes_client import (
    JobOperationError,
    PodNotFoundError,
)
from kubernetes_spawner import (
    DEFAULT_SPAWN_MAX_RETRIES,
    DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS,
    KubernetesSpawnError,
    logger,
)
from models import AgentRole

if TYPE_CHECKING:
    from egg_container import MountSpec


def _apply_restart_budget(
    self,
    restart_key: tuple[str, str, str | None],
    max_restarts: int,
) -> int:
    """Check + increment the restart budget for ``restart_key``.

    The caller MUST already hold ``_get_restart_lock(restart_key)`` so
    the read-modify-write of ``_restart_counts`` is atomic. Raises
    :class:`KubernetesSpawnError` when the budget is already exhausted;
    otherwise increments the count and returns the new value.
    """
    pipeline_id, agent_role_value, _slice_id = restart_key
    current_count = self._restart_counts.get(restart_key, 0)
    if current_count >= max_restarts:
        raise KubernetesSpawnError(
            f"Restart limit ({max_restarts}) exceeded for {agent_role_value} "
            f"in pipeline {pipeline_id} (restarted {current_count} times)"
        )
    self._restart_counts[restart_key] = current_count + 1
    return current_count + 1


def check_and_increment_restart_count(
    self,
    pipeline_id: str,
    agent_role: AgentRole,
    slice_id: str | None = None,
    max_restarts: int = 2,
) -> int:
    """Atomically enforce + bump the per-(pipeline, role, slice) budget.

    Extracted from :meth:`restart_agent_job` (#3244). After #3164 moved
    respawn ownership to the orchestrator event loop, the
    ``restart_agent`` route no longer calls ``restart_agent_job`` and so
    must enforce the restart budget itself before delegating the respawn.
    Acquires the per-key restart lock, raises
    :class:`KubernetesSpawnError` when the budget is exhausted, otherwise
    increments and returns the new count. The increment-before-respawn
    semantics match the in-band check ``restart_agent_job`` performs, so
    an operator/overseer cannot reset consensus an unbounded number of
    times on a converging phase.

    Args:
        pipeline_id: Pipeline ID.
        agent_role: Agent role being restarted.
        slice_id: Optional slice scope (#2410). Slice-scoped restarts get
            an independent budget bucket keyed on the slice.
        max_restarts: Maximum restart attempts per agent per phase.

    Returns:
        The new restart count after incrementing.

    Raises:
        KubernetesSpawnError: If the budget is exhausted or the per-key
            restart lock cannot be acquired.
    """
    restart_key = (pipeline_id, agent_role.value, slice_id)
    lock = self._get_restart_lock(restart_key)
    if not lock.acquire(timeout=120):
        raise KubernetesSpawnError(
            f"Timed out waiting to acquire restart lock for "
            f"{agent_role.value} in pipeline {pipeline_id}"
        )
    try:
        return self._apply_restart_budget(restart_key, max_restarts)
    finally:
        lock.release()


def restart_agent_job(
    self,
    pipeline_id: str,
    agent_role: AgentRole,
    issue_number: int | None = None,
    repo_volumes: dict[str, str] | None = None,
    mode: str | None = "public",
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
    repos: list[str] | None = None,
    phase: str | None = None,
    command: list[str] | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    extra_mounts: list["MountSpec"] | None = None,  # noqa: UP037
    max_restarts: int = 2,
    reason: str = "",
    spawn_max_retries: int = DEFAULT_SPAWN_MAX_RETRIES,
    spawn_retry_initial_backoff_seconds: float = (DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS),
    slice_id: str | None = None,
    wait_for_gateway: bool = True,
    upstream: str | None = None,
    upstream_model: str | None = None,
) -> _pkg.SpawnedContainer:
    """Restart an agent Job: delete and respawn preserving worktree.

    Args:
        pipeline_id: Pipeline ID.
        agent_role: Agent role to restart.
        issue_number: GitHub issue number.
        repo_volumes: Repo name to host path mappings.
        mode: Gateway mode ('public' or 'private'). Must be explicitly provided.
        image: Container image override.
        extra_env: Additional environment variables.
        repos: Repositories for gateway session.
        phase: Current pipeline phase.
        command: Command to execute in the container.
        branch: Branch name.
        base_branch: Branch to base worktrees on.
        extra_mounts: Additional mount specs.
        max_restarts: Maximum restart attempts per agent per phase.
        reason: Human-readable reason for the restart.
        spawn_max_retries: Retry attempts for transient gateway failures
            during worktree creation (forwarded to ``spawn_agent_job``).
        spawn_retry_initial_backoff_seconds: Initial backoff for spawn
            retries (forwarded to ``spawn_agent_job``).
        slice_id: Optional slice scope (#2410). When supplied, the
            slice-scoped Job name (``egg-agent-{pid}-{slice_id}-{role}``)
            is the one deleted and respawned, the slice-scoped worktree
            id is preserved, and ``EGG_SLICE_ID`` is propagated so the
            restarted agent re-enters the per-slice consensus tracker.
            The restart-budget key includes the slice scope so each
            slice gets an independent budget.
        wait_for_gateway: Wait for gateway health before respawning.
            Forwarded to ``spawn_agent_job``.
        upstream: Per-agent upstream identifier,
            forwarded to ``spawn_agent_job`` so the restarted Job
            registers its gateway session against the same upstream
            as the initial spawn. ``None`` keeps the default
            Anthropic routing.
        upstream_model: Upstream-side model name to rewrite the
            request body's ``model`` field to,
            forwarded to ``spawn_agent_job``. ``None`` on the
            Anthropic path — the body is forwarded unchanged.

    Returns:
        SpawnedContainer with new Job info.

    Raises:
        ValueError: If mode is None.
        KubernetesSpawnError: If restart limit exceeded or spawning fails.
    """
    if mode is None:
        raise ValueError("mode must be explicitly provided ('public' or 'private')")

    # Slice scope is part of the restart key so concurrent slice-N
    # and slice-M agents of the same role each get an independent
    # restart budget and lock. ``reset_restart_counts(pipeline_id)``
    # still clears all of them because it filters on ``k[0]``.
    restart_key = (pipeline_id, agent_role.value, slice_id)
    lock = self._get_restart_lock(restart_key)

    # Timeout prevents indefinite blocking if a concurrent restart of the
    # same agent is stuck — the lock is held across remove_agent_job() and
    # spawn_agent_job(), both of which invoke k8s API calls that can hang
    # on network or control-plane issues.
    if not lock.acquire(timeout=120):
        raise KubernetesSpawnError(
            f"Timed out waiting to acquire restart lock for "
            f"{agent_role.value} in pipeline {pipeline_id}"
        )
    try:
        # Increment count before spawn so failed attempts burn a restart
        # budget slot. Shared with the orchestrator-native ``restart_agent``
        # route via ``check_and_increment_restart_count`` (#3244) so both
        # paths enforce the same per-(pipeline, role, slice) cap.
        new_count = self._apply_restart_budget(restart_key, max_restarts)

        # ``job_name`` matches the gateway session container_id used at
        # spawn time (hyphenated, no JOB_PREFIX); ``actual_k8s_job_name``
        # is the real k8s Job name. Using the wrong form for either side
        # broke restart for every role with an underscore — see #2070.
        # Slice scope (#2410) must be threaded through here so the
        # delete + respawn target the slice-scoped Job name, not the
        # pipeline-level one.
        job_name, actual_k8s_job_name = self._build_k8s_job_names(
            pipeline_id, agent_role, slice_id=slice_id
        )

        logger.info(
            "Restarting agent Job",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            restart_count=new_count,
            max_restarts=max_restarts,
            reason=reason,
        )

        # Delete the existing Job (best effort) and clean up the
        # gateway session.  We can't go through ``remove_agent_job``
        # here because it would route both the k8s and gateway calls
        # through the same identifier, but k8s wants the prefixed form
        # and the gateway session is keyed by the unprefixed form.
        delete_attempted = False
        try:
            self.k8s.delete_job(
                actual_k8s_job_name,
                self._namespace,
                propagation_policy="Foreground",
            )
            delete_attempted = True
        except PodNotFoundError:
            logger.debug(
                "No existing Job found during restart (already removed)",
                job_name=actual_k8s_job_name,
            )
        except JobOperationError as e:
            logger.warning(
                "Failed to delete existing Job during restart, continuing",
                job_name=actual_k8s_job_name,
                error=str(e),
            )
        # Foreground propagation returns as soon as the deletion is
        # accepted; the Job lingers with its finalizer until pods are
        # gone. Wait for the API server to actually remove it before
        # spawning a Job with the same name, otherwise we race the
        # finalizer and 409 on AlreadyExists (#2655).
        if delete_attempted and not self.k8s.wait_for_job_gone(
            actual_k8s_job_name, self._namespace, timeout_s=30.0
        ):
            logger.warning(
                "Job still present after 30s wait; respawn may 409 on AlreadyExists",
                job_name=actual_k8s_job_name,
            )
        try:
            self.gateway.delete_session_by_container(job_name)
        except _pkg.GatewayError as e:
            logger.warning(
                "Failed to clean up gateway session during restart",
                job_name=job_name,
                error=str(e),
            )

        # Salvage agent work before respawning (#2807). The respawn
        # reuses the on-disk worktree and hard-resets it to a remote
        # ref (gateway _reset_reused_worktree_to_safe_ref), destroying
        # both unpushed commits and the dirty working tree. The modal
        # #2807 crash window is mid-Edit, before any commit, so
        # salvage_uncommitted=True commits the dirty tree onto the work
        # branch first; auto-salvage then pushes everything to
        # egg/recovered/<pipeline>/<scope>/<sha> for manual triage.
        #
        # This runs on the respawn critical path while the restart lock
        # is held. It is scoped to just this agent's own worktree, the
        # working-tree commit is local git, and the gateway push carries
        # its own HTTP timeout, so the added lock-hold is bounded. The
        # best-effort try/except keeps a salvage failure from blocking
        # the respawn.
        agent_worktree_id = self._build_agent_worktree_id(pipeline_id, agent_role, slice_id)
        try:
            _pkg.agent_salvage.auto_salvage_pipeline(
                self.gateway,
                pipeline_id,
                worktree_filter={agent_worktree_id},
                mode=mode,
                base_branch=base_branch,
                salvage_uncommitted=True,
            )
        except Exception as e:
            logger.warning(
                "Auto-salvage failed during agent restart; proceeding",
                pipeline_id=pipeline_id,
                agent_role=agent_role.value,
                worktree_id=agent_worktree_id,
                error=str(e),
            )

        # Respawn — gateway's create_worktrees() is idempotent.
        # ``slice_id`` is forwarded so spawn_agent_job builds the
        # slice-scoped Job + worktree id and sets ``EGG_SLICE_ID``
        # on the new Job (#2410).
        spawned = self.spawn_agent_job(
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            issue_number=issue_number,
            repo_volumes=repo_volumes,
            mode=mode,
            image=image,
            extra_env=extra_env,
            wait_for_gateway=wait_for_gateway,
            repos=repos,
            phase=phase,
            command=command,
            branch=branch,
            base_branch=base_branch,
            extra_mounts=extra_mounts,
            preserve_worktree_on_failure=True,
            spawn_max_retries=spawn_max_retries,
            spawn_retry_initial_backoff_seconds=spawn_retry_initial_backoff_seconds,
            slice_id=slice_id,
            # Per-agent upstream routing. Forwarded so a
            # restart picks the same upstream as the initial spawn — the
            # gateway session is otherwise rebuilt against the
            # ``anthropic`` default and would silently route the
            # restarted agent to the wrong upstream.
            upstream=upstream,
            upstream_model=upstream_model,
        )

        logger.info(
            "Agent Job restarted successfully",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            new_job_name=spawned.container_info.job_name,
            restart_count=new_count,
        )

        return spawned
    finally:
        lock.release()


def get_restart_count(
    self,
    pipeline_id: str,
    agent_role: str,
    slice_id: str | None = None,
) -> int:
    """Get the current restart count for an agent.

    Args:
        pipeline_id: Pipeline ID.
        agent_role: Agent role value string.
        slice_id: Optional slice scope (#2410). Pipeline-level callers
            pass ``None``; slice-aware callers pass the same
            ``slice-<N>`` string they used at restart time so each
            slice's budget is reported independently.

    Returns:
        Number of times the agent has been restarted.
    """
    key = (pipeline_id, agent_role, slice_id)
    lock = self._get_restart_lock(key)
    with lock:
        return self._restart_counts.get(key, 0)


def reset_restart_counts(self, pipeline_id: str) -> None:
    """Reset all restart counts for a pipeline (e.g., on phase transition).

    Args:
        pipeline_id: Pipeline ID.
    """
    # Acquire the global lock to iterate safely, then clear matching count
    # entries.  We intentionally do NOT delete per-key locks from
    # _restart_locks: a concurrent restart_agent_job may still hold one of
    # those locks, and deleting it would allow _get_restart_lock to create a
    # new lock for the same key — breaking mutual exclusion.  The per-key
    # locks are lightweight and bounded by the number of (pipeline, role)
    # pairs, so the growth is negligible.
    with self._restart_locks_lock:
        keys_to_remove = [k for k in self._restart_counts if k[0] == pipeline_id]
        for k in keys_to_remove:
            del self._restart_counts[k]
