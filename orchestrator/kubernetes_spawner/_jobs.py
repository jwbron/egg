"""Job stop / remove / list / pipeline cleanup (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import re

import kubernetes_spawner as _pkg
from kubernetes_client import (
    LABEL_PIPELINE_ID,
    LABEL_SLICE_ID,
    JobOperationError,
    PodNotFoundError,
)
from kubernetes_spawner import (
    logger,
)
from models import AgentRole, ContainerInfo


def stop_agent_job(
    self,
    job_name: str,
    cleanup_session: bool = True,
    timeout: int = 10,
) -> ContainerInfo:
    """Stop an agent Job and optionally clean up session.

    Args:
        job_name: Job name or container ID
        cleanup_session: Whether to delete gateway session
        timeout: Grace period in seconds (passed to stop_container)

    Returns:
        ContainerInfo after stopping
    """
    try:
        info = self.k8s.stop_container(job_name, timeout=timeout)

        if cleanup_session:
            try:
                self.gateway.delete_session_by_container(job_name)
            except _pkg.GatewayError as e:
                logger.warning(
                    "Failed to clean up gateway session",
                    job_name=job_name,
                    error=str(e),
                )

        return info

    except PodNotFoundError:
        if cleanup_session:
            try:
                self.gateway.delete_session_by_container(job_name)
            except _pkg.GatewayError:
                pass
        raise


def remove_agent_job(
    self,
    job_name: str,
    force: bool = False,
    cleanup_session: bool = True,
) -> None:
    """Remove an agent Job and clean up session.

    Args:
        job_name: Job name or container ID
        force: Force removal (foreground propagation)
        cleanup_session: Whether to delete gateway session
    """
    try:
        self.k8s.remove_container(job_name, force=force)
    finally:
        if cleanup_session:
            try:
                self.gateway.delete_session_by_container(job_name)
            except _pkg.GatewayError as e:
                logger.warning(
                    "Failed to clean up gateway session",
                    job_name=job_name,
                    error=str(e),
                )


def list_pipeline_jobs(
    self,
    pipeline_id: str,
) -> list[ContainerInfo]:
    """List all Jobs for a pipeline.

    Args:
        pipeline_id: Pipeline ID

    Returns:
        List of ContainerInfo
    """
    return self.k8s.list_containers(
        labels={LABEL_PIPELINE_ID: pipeline_id},
    )


def list_slice_jobs(
    self,
    pipeline_id: str,
    slice_id: str,
) -> list[ContainerInfo]:
    """List slice-scoped Jobs within *pipeline_id*.

    Filters on ``egg.slice.id`` (#2666) so callers don't have to
    parse Job names to scope an operation to a single slice.
    Returns an empty list when no Jobs match.
    """
    return self.k8s.list_containers(
        labels={LABEL_PIPELINE_ID: pipeline_id, LABEL_SLICE_ID: slice_id},
    )


def cleanup_pipeline(
    self,
    pipeline_id: str,
    force: bool = True,
    preserve_worktrees: bool = False,
    salvage_mode: str | None = None,
    salvage_base_branch: str | None = None,
) -> int:
    """Clean up all Jobs and sessions for a pipeline.

    Args:
        pipeline_id: Pipeline ID
        force: Force removal
        preserve_worktrees: When True, skip worktree deletion so a
            subsequent retry can reuse the pipeline-level and per-agent
            worktrees. Jobs and gateway sessions are still removed so the
            retry spawns fresh pods. Default False preserves the prior
            behavior of deleting every worktree.
        salvage_mode: Gateway session mode (``"public"`` / ``"private"``)
            used by the auto-salvage hook for the launcher-auth push to
            ``egg/recovered/...``. Callers with a ``Pipeline`` in scope
            should compute this via ``_compute_gateway_mode(pipeline)``
            so private-repo / private-network pipelines salvage with
            the policy they ran under. ``None`` (the default) falls
            back to ``"public"`` and is only correct for callers that
            cannot load the pipeline.
        salvage_base_branch: Base branch (e.g. ``"main"``) used by the
            salvage hook as the secondary ``^anchor`` cut when
            ``origin/<assigned_branch>`` is missing. ``None`` is safe
            — the hook falls back to the full HEAD history (capped at
            200 commits) — but threading ``pipeline.base_branch``
            produces tighter recovery refs.

    Returns:
        Number of Jobs removed
    """
    jobs = self.list_pipeline_jobs(pipeline_id)
    removed = 0

    for job in jobs:
        try:
            self.remove_agent_job(
                job.job_name or job.container_id,
                force=force,
                cleanup_session=True,
            )
            removed += 1
        except (PodNotFoundError, JobOperationError) as e:
            logger.warning(
                "Failed to remove Job during cleanup",
                job_name=job.job_name,
                error=str(e),
            )

    # Tear down any long-lived event-mode gateway sessions
    # reused across this pipeline's one-shot event spawns. They are keyed
    # by a stable base ``container_id`` (not the per-event Job name), so the
    # per-Job ``remove_agent_job`` calls above do not reach them; this is
    # the phase/pipeline-end teardown that releases them and bounds the
    # session-token cache. Delegates to ``_teardown_session`` (the same
    # delete-by-base-id + cache-eviction primitive used by the
    # streak-exhaustion path) so the two callers share one implementation.
    # Snapshot the keys first: ``_teardown_session`` pops from the cache it
    # is iterated over.
    for _pid, role_value, slice_id, _session_id in [
        k for k in self._session_token_cache if k[0] == pipeline_id
    ]:
        try:
            self._teardown_session(pipeline_id, AgentRole(role_value), slice_id=slice_id)
        except ValueError:
            # A cache entry whose role string is not a known AgentRole
            # (should not happen) — drop it directly so cleanup stays bounded.
            logger.warning(
                "Unknown role in session-token cache during cleanup; evicting",
                pipeline_id=pipeline_id,
                role=role_value,
            )
            self._session_token_cache.pop((_pid, role_value, slice_id, _session_id), None)

    if preserve_worktrees:
        logger.info(
            "Pipeline cleanup complete (worktrees preserved for retry)",
            pipeline_id=pipeline_id,
            jobs_removed=removed,
        )
        return removed

    # Clean up per-agent worktrees
    worktree_ids_to_clean: set[str] = {pipeline_id}
    for job in jobs:
        role_label = None
        # Extract role string from AgentRole enum
        if hasattr(job, "agent_role") and job.agent_role is not None:
            try:
                role_label = (
                    job.agent_role.value
                    if isinstance(job.agent_role, AgentRole)
                    else str(job.agent_role)
                )
            except AttributeError, TypeError:
                pass
        if role_label and isinstance(role_label, str):
            worktree_ids_to_clean.add(f"{pipeline_id}-{role_label}")

    # Also scan filesystem for any per-agent worktrees.  Only match
    # entries that are either the pipeline-level worktree, a
    # "{pipeline_id}-{role}" directory, or a slice-scoped
    # "{pipeline_id}-slice-{N}-{role}" directory where {role} is a
    # known AgentRole value (#2403). A naive `startswith(f"{pipeline_id}-")`
    # collides with longer pipeline IDs that share the prefix — e.g.
    # cleanup of `issue-1758` would match active worktrees of
    # `issue-1758-worktree-fix-tester`, wiping another pipeline's
    # state mid-phase (#1865).
    if _pkg.WORKTREE_BASE_DIR.exists():
        valid_role_suffixes = {f"-{role.value}" for role in AgentRole}
        slice_segment_re = re.compile(r"^-slice-[0-9]+(-.+)$")
        try:
            for entry in _pkg.WORKTREE_BASE_DIR.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name
                if name == pipeline_id:
                    worktree_ids_to_clean.add(name)
                    continue
                if not name.startswith(pipeline_id):
                    continue
                suffix = name[len(pipeline_id) :]
                if suffix in valid_role_suffixes:
                    worktree_ids_to_clean.add(name)
                    continue
                # Slice-scoped: "{pipeline_id}-slice-{N}-{role}".
                # The trailing "-{role}" inside the captured group
                # is matched against the role allowlist so this
                # branch can't sweep an unrelated sibling worktree.
                slice_match = slice_segment_re.match(suffix)
                if slice_match and slice_match.group(1) in valid_role_suffixes:
                    worktree_ids_to_clean.add(name)
        except Exception as e:
            logger.warning(
                "Filesystem worktree scan failed during cleanup",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    # Auto-salvage unpushed agent commits before deleting worktrees
    # (#2429). Best-effort: any failure logs and continues so cleanup
    # cannot be blocked by salvage. The default policy here used to
    # be silent loss when an agent's pushes were wedged — this hook
    # makes the default policy "push to egg/recovered/<pipeline>/...
    # then delete" so salvageable work is always reachable from
    # origin before the worktree filesystem state is gone.
    try:
        _pkg.agent_salvage.auto_salvage_pipeline(
            self.gateway,
            pipeline_id,
            worktree_filter=worktree_ids_to_clean,
            # Mismatching the running-pipeline mode would re-create
            # the silent-loss class this hook exists to prevent for
            # private-mode pipelines. Callers without a Pipeline in
            # scope keep the historical default ("public") via
            # ``mode=None`` → omitted-kwarg below.
            **({"mode": salvage_mode} if salvage_mode is not None else {}),
            base_branch=salvage_base_branch,
        )
    except Exception as e:
        logger.warning(
            "Auto-salvage failed during cleanup; proceeding with worktree deletion",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    for wt_id in worktree_ids_to_clean:
        try:
            self.gateway.delete_worktrees(container_id=wt_id, force=True)
            logger.info(
                "Worktree cleaned up",
                pipeline_id=pipeline_id,
                worktree_id=wt_id,
            )
        except Exception as e:
            logger.warning(
                "Worktree cleanup failed",
                pipeline_id=pipeline_id,
                worktree_id=wt_id,
                error=str(e),
            )

    logger.info(
        "Pipeline cleanup complete",
        pipeline_id=pipeline_id,
        jobs_removed=removed,
    )

    return removed
