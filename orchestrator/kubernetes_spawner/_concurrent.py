"""Uncommitted-change detection + concurrent spawn fn (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from typing import TYPE_CHECKING, Any

import kubernetes_spawner as _pkg
from kubernetes_spawner import (
    DEFAULT_SPAWN_MAX_RETRIES,
    DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS,
    logger,
)
from models import AgentRole

if TYPE_CHECKING:
    pass


def detect_uncommitted_changes(
    self,
    pipeline_id: str,
    agent_role: str,
    slice_id: str | None = None,
) -> dict | None:
    """Detect uncommitted changes in an agent's worktree after Job exit.

    Checks the agent's worktree directly on the filesystem for uncommitted
    changes. Per-agent worktrees are at:
    /home/egg/.egg-worktrees/{pipeline_id}-{role}/{repo}/ (pipeline-level)
    /home/egg/.egg-worktrees/{pipeline_id}-{slice_id}-{role}/{repo}/
    (slice-scoped, #2410).

    Args:
        pipeline_id: Pipeline ID.
        agent_role: Agent role value string.
        slice_id: Optional slice scope. When supplied, the slice-scoped
            worktree id is inspected; pipeline-level callers omit this.

    Returns:
        Dict with change info if uncommitted changes found, None otherwise.
    """
    import subprocess

    # Mirrors ``_build_agent_worktree_id`` so a slice-scoped restart
    # path can detect uncommitted work in the slice's worktree, not
    # the (possibly absent) pipeline-level one.
    agent_worktree_id = (
        f"{pipeline_id}-{slice_id}-{agent_role}" if slice_id else f"{pipeline_id}-{agent_role}"
    )
    worktree_base = _pkg.WORKTREE_BASE_DIR / agent_worktree_id

    if not worktree_base.exists():
        return None

    for repo_dir in worktree_base.iterdir():
        if not repo_dir.is_dir():
            continue
        try:
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "-c",
                    "safe.directory=*",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "gc.auto=0",
                    "status",
                    "--porcelain",
                ],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                files = [
                    line[3:].strip()
                    for line in result.stdout.splitlines()
                    if line and len(line) > 3
                ]
                logger.info(
                    "Agent exited with uncommitted changes",
                    event_type="agent_uncommitted_changes",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role,
                    slice_id=slice_id,
                    worktree_path=str(repo_dir),
                    file_count=len(files),
                    changed_files=files[:20],
                )
                return {
                    "pipeline_id": pipeline_id,
                    "agent_role": agent_role,
                    "slice_id": slice_id,
                    "worktree_id": agent_worktree_id,
                    "worktree_path": str(repo_dir),
                    "file_count": len(files),
                    "changed_files": files[:20],
                }
        except Exception as e:
            logger.warning(
                "Failed to check worktree status",
                repo_dir=str(repo_dir),
                error=str(e),
            )
    return None


def create_concurrent_spawn_fn(
    self,
    pipeline_id: str,
    issue_number: int | None,
    repo_volumes: dict[str, str] | None,
    mode: str,
    repos: list[str] | None,
    phase: str | None,
    sandbox_env: dict[str, str] | None = None,
    image: str | None = None,
    base_branch: str | None = None,
    certs_volume: str | None = None,  # noqa: ARG002 — Docker-era compat
    spawn_max_retries: int = DEFAULT_SPAWN_MAX_RETRIES,
    spawn_retry_initial_backoff_seconds: float = (DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS),
    slice_id: str | None = None,
):
    """Create a spawn callable compatible with ConcurrentPhaseExecutor.

    Returns a function with signature (role, branch, extra_env, command)
    that spawns a Job via spawn_agent_job.

    Args:
        pipeline_id: Pipeline ID.
        issue_number: GitHub issue number.
        repo_volumes: Repo name to host path mappings.
        mode: Gateway mode (public/private/local).
        repos: Repositories for gateway session.
        phase: Current pipeline phase.
        sandbox_env: Base environment variables.
        image: Container image override.
        base_branch: Branch to base worktrees on.
        slice_id: Optional slice scope (#2403). When supplied, every
            spawn (including ``spawn_specific_roles`` retries) is
            tagged with this slice so concurrent slices in the same
            pipeline get distinct Job names and worktree ids. Without
            this, slice-N spawning ``coder`` would delete slice-(N-1)'s
            still-running ``coder`` Job during the pre-spawn cleanup.

    Returns:
        Callable suitable for ConcurrentPhaseExecutor.spawn_fn.
    """

    def _spawn(
        role: AgentRole,
        branch: str | None = None,
        extra_env: dict[str, str] | None = None,
        command: list[str] | None = None,
        upstream: str | None = None,
        upstream_model: str | None = None,
        event_action: str | None = None,
        event_dedupe_key: str | None = None,
        event_payload_refs: str | None = None,
    ) -> _pkg.SpawnedContainer | None:
        merged_env = {**(sandbox_env or {}), **(extra_env or {})}
        common_kwargs: dict[str, Any] = {
            "issue_number": issue_number,
            "repo_volumes": repo_volumes,
            "mode": mode,
            "image": image,
            "extra_env": merged_env,
            "repos": repos,
            "phase": phase,
            "branch": branch,
            "base_branch": base_branch,
            "command": command,
            "spawn_max_retries": spawn_max_retries,
            "spawn_retry_initial_backoff_seconds": (spawn_retry_initial_backoff_seconds),
            "upstream": upstream,
            "upstream_model": upstream_model,
        }
        # Orchestrator-owned one-shot event spawn. Routes
        # through ``spawn_one_shot_event_job`` so the Job gets the event
        # identity (env + labels) and adoption-on-dedupe-hit; the
        # long-lived ``spawn_agent_job`` pod-mode path is taken otherwise,
        # byte-identical to before.
        if event_dedupe_key is not None and event_action is not None:
            return self.spawn_event_job(
                pipeline_id,
                role,
                action=event_action,
                dedupe_key=event_dedupe_key,
                event_payload_refs=event_payload_refs,
                slice_id=slice_id,
                **common_kwargs,
            )
        return self.spawn_agent_job(
            pipeline_id=pipeline_id,
            agent_role=role,
            slice_id=slice_id,
            **common_kwargs,
        )

    def _teardown_event_session(role: AgentRole) -> None:
        """Tear down a role's reused orchestrator-mode gateway session.

        The event loop's supervisor calls this when a role's
        event arm exhausts its retry budget (the ``_exhausted`` transition).
        Routes to :meth:`_teardown_session` under this closure's captured
        ``pipeline_id`` / ``slice_id`` so the delete-by-base-id + cache
        eviction targets the same stable key the spawn path registered
        under. Best-effort: ``_teardown_session`` swallows gateway errors.
        """
        self._teardown_session(pipeline_id, role, slice_id=slice_id)

    # Expose the teardown alongside the spawn callable so the executor's
    # event-loop wiring can reach ``_teardown_session`` (which lives on the
    # spawner) without holding a separate spawner reference.
    _spawn.teardown_event_session = _teardown_event_session  # type: ignore[attr-defined]
    return _spawn
