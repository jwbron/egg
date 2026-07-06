"""Gateway worktree cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from flask import Response, request

try:
    from ..worktree_manager import (
        REPOS_BASE_DIR,
        validate_identifier,
    )
except ImportError:  # flat/container import mode
    from worktree_manager import (  # type: ignore[no-redef, import-untyped]
        REPOS_BASE_DIR,
        validate_identifier,
    )

from ._helpers import make_error, make_success


def _b() -> Any:
    """Return the gateway barrel for call-time lookup of patched symbols.

    Seam getters/validators and gateway-local helpers are patched by tests at
    ``gateway.gateway.<name>``; resolving them on the barrel at call time keeps
    those patches effective after the split.
    """
    import sys

    return sys.modules.get("gateway.gateway") or sys.modules["gateway"]


class _BarrelLogger:
    """Proxy to the barrel ``logger`` so tests patching ``gateway.logger``
    observe log calls emitted from this submodule."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_b().logger, name)


logger: Any = _BarrelLogger()


def map_container_path_to_worktree(
    repo_path: str, container_id: str | None, operation: str = "git"
) -> str | None:
    """
    Map a container's repo path to the corresponding worktree path.

    Container sends paths like /home/egg/repos/{repo} or subdirectories like
    /home/egg/repos/{repo}/src/foo, but the gateway needs to run git in the
    worktree at /home/egg/.egg-worktrees/{container_id}/{repo}[/subdir].

    Args:
        repo_path: The path sent by the container (e.g., /home/egg/repos/myrepo/src)
        container_id: The container's unique identifier
        operation: Name of the operation for logging purposes

    Returns:
        The worktree path if mapping succeeds, the original repo_path if no
        container_id was provided (interactive session), or None if a
        container_id was provided but the worktree could not be found.
    """
    if not container_id:
        return repo_path

    # Extract repo name and any subdirectory from paths like:
    # /home/egg/repos/myrepo -> repo_name=myrepo, subdir=""
    # /home/egg/repos/myrepo/src/foo -> repo_name=myrepo, subdir="src/foo"
    repos_prefix = "/home/egg/repos/"
    if not repo_path.startswith(repos_prefix):
        return repo_path

    # Get the path relative to /home/egg/repos/
    relative_path = repo_path[len(repos_prefix) :].rstrip("/")
    if not relative_path:
        # Path is exactly /home/egg/repos/ - not a repo
        return repo_path

    # Split into repo name and subdirectory
    parts = relative_path.split("/", 1)
    repo_name = parts[0]
    subdir = parts[1] if len(parts) > 1 else ""

    if not repo_name:
        return repo_path

    manager = _b().get_worktree_manager()
    try:
        worktree_path, _main_repo = manager.get_worktree_paths(container_id, repo_name)
        if worktree_path.exists():
            # Append subdirectory if present
            final_path = worktree_path / subdir if subdir else worktree_path
            logger.debug(
                f"Mapped container path to worktree for {operation}",
                container_path=repo_path,
                worktree_path=str(final_path),
                container_id=container_id,
            )
            return str(final_path)
        else:
            logger.warning(
                f"Worktree path does not exist for {operation} — "
                f"container_id may not match any created worktree",
                container_path=repo_path,
                expected_worktree=str(worktree_path),
                container_id=container_id,
            )
            return None
    except ValueError as e:
        logger.warning(
            f"Failed to map container path to worktree for {operation}",
            error=str(e),
            container_id=container_id,
            repo_name=repo_name,
        )
        return None


def _cleanup_stale_pack_files(exec_path: str) -> None:
    """Best-effort opportunistic cleanup of previously-orphaned temporary pack files.

    Called after a git operation times out, but does NOT target the specific
    operation's artifacts (those are too recent to match the age filter).
    Instead, it scans for ``tmp_pack_*``/``tmp_obj_*``/``tmp_idx_*`` files
    older than 5 minutes — orphans left by *earlier* interrupted operations.
    The age filter avoids racing with concurrent fetch operations on the same
    repository.
    """
    try:
        # Determine repo_name from exec_path.
        # Worktree paths:  /home/egg/.egg-worktrees/{container_id}/{repo_name}[/subdir]
        # Main repo paths: /home/egg/repos/{repo_name}[/subdir]
        repo_name = None
        worktree_prefix = str(_b().WORKTREE_BASE_DIR) + "/"
        repos_prefix = str(REPOS_BASE_DIR) + "/"

        if exec_path.startswith(worktree_prefix):
            # e.g. /home/egg/.egg-worktrees/container-123/my-repo/src → my-repo
            relative = exec_path[len(worktree_prefix) :]
            parts = relative.split("/")
            if len(parts) >= 2:
                repo_name = parts[1]
        elif exec_path.startswith(repos_prefix):
            # e.g. /home/egg/repos/my-repo/src → my-repo
            relative = exec_path[len(repos_prefix) :]
            parts = relative.split("/")
            if parts and parts[0]:
                repo_name = parts[0]

        if not repo_name:
            return

        manager = _b().get_worktree_manager()
        manager.cleanup_orphaned_pack_files(
            repo_name=repo_name,
            max_age_seconds=300,
        )
    except Exception as e:
        logger.debug(
            "Stale pack file cleanup failed (best-effort)",
            exec_path=exec_path,
            error=str(e),
        )


def _cleanup_empty_container_dir(container_id: str) -> None:
    """Best-effort removal of an orphan container worktree directory.

    Called from the total-failure branch of worktree_create.  Only acts
    when the directory is actually empty — if any per-repo subdir is
    present (partial failure with leftover state), we leave it for an
    operator-driven prune so we don't accidentally drop in-progress
    work.  See #2186.

    Defense-in-depth: validate `container_id` and verify the resolved
    path stays under `WORKTREE_BASE_DIR` before any filesystem mutation.
    `worktree_create` already validates at the route boundary, but a
    raw `..`-bearing identifier reaching `Path / container_id` would
    otherwise let `rmdir(2)` follow the literal path and unlink an
    empty directory adjacent to the base dir.
    """
    try:
        validate_identifier(container_id, "container_id")
    except ValueError as e:
        logger.warning(
            "Skipping orphan container dir cleanup: invalid container_id",
            container_id=container_id,
            error=str(e),
        )
        return

    target = _b().WORKTREE_BASE_DIR / container_id
    try:
        # Resolve and verify containment as a second line of defense
        # against any future caller that bypasses validate_identifier.
        base_resolved = _b().WORKTREE_BASE_DIR.resolve()
        target_resolved = target.resolve(strict=False)
        if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
            logger.warning(
                "Skipping orphan container dir cleanup: outside base dir",
                container_id=container_id,
                resolved=str(target_resolved),
                base=str(base_resolved),
            )
            return
        if not target.exists():
            return
        if any(target.iterdir()):
            logger.warning(
                "Skipping orphan container dir cleanup: not empty",
                container_id=container_id,
                path=str(target),
            )
            return
        target.rmdir()
        logger.info(
            "Removed empty orphan container worktree dir",
            container_id=container_id,
            path=str(target),
        )
    except OSError as e:
        logger.warning(
            "Failed to clean orphan container dir",
            container_id=container_id,
            path=str(target),
            error=str(e),
        )


def worktree_create() -> tuple[Response, int] | Response:
    """
    Create worktrees for a container.

    Called by the egg launcher before starting a container. Creates isolated
    worktrees for each repository the container needs access to.

    Request body:
        {
            "container_id": "egg-xxx-yyy",
            "repos": ["owner/repo1", "owner/repo2"],
            "uid": 1000,  // optional, defaults to 1000 (egg user)
            "gid": 1000   // optional, defaults to 1000 (egg group)
        }

    Returns:
        {
            "success": true,
            "message": "Worktrees created",
            "data": {
                "worktrees": {
                    "repo1": "/home/user/.egg-worktrees/egg-xxx-yyy/repo1",
                    "repo2": "/home/user/.egg-worktrees/egg-xxx-yyy/repo2"
                }
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    repos = data.get("repos", [])
    base_branch = data.get("base_branch")  # None = resolve per-repo
    assigned_branch = data.get("assigned_branch")  # None = skip upstream config
    # #3393 slice-7: when True, materialize each repo's pipeline work
    # branch on its OWN remote right after the worktree exists (push the
    # worktree HEAD to refs/heads/{assigned_branch or work-branch}).
    # Multi-repo pipelines set this so secondary-repo context / slice PRs
    # find a head branch; single-repo (N=1) callers leave it False, so
    # the path stays byte-identical to pre-#3393.
    push_branch = bool(data.get("push_branch", False))
    # UID/GID for worktree ownership (default: 1000 for egg user)
    uid = data.get("uid")
    gid = data.get("gid")

    if not container_id:
        return make_error("Missing container_id")
    if not repos:
        return make_error("Missing repos list")

    # Validate container_id at the route boundary so every downstream
    # filesystem touch (including the post-failure cleanup helper) is
    # safe.  Without this, a `..`-bearing container_id would reach
    # `_cleanup_empty_container_dir` after `manager.create_worktree`
    # raised ValueError into the per-repo `errors[]` list, letting
    # `rmdir(2)` follow the literal path out of WORKTREE_BASE_DIR.
    # See #2186 review feedback.
    try:
        validate_identifier(container_id, "container_id")
    except ValueError as e:
        return make_error(str(e))

    # Validate uid/gid if provided
    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

    manager = _b().get_worktree_manager()
    worktrees = {}
    errors = []

    for repo in repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        # Pre-bind so the except clauses below can reference
        # effective_branch even if resolve_default_branch raises (e.g.,
        # OSError if the git binary is missing).  See #2186 review
        # feedback — previously hoisted out of the try, which let
        # resolve_default_branch failures bypass the per-repo errors[]
        # safety net entirely.
        effective_branch = base_branch
        try:
            # Resolve the default branch per-repo when no explicit base is given.
            # This ensures repos with non-standard default branches (e.g., master)
            # are handled correctly.  See #860.
            effective_branch = base_branch or manager.resolve_default_branch(repo_name)
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                uid=uid,
                gid=gid,
                assigned_branch=assigned_branch,
                repo_slug=repo,
                push_branch=push_branch,
            )
            # Translate container path to host path for egg launcher mount sources.
            # Key by the full ``owner/repo`` slug (#3393 slice-3, operator
            # ruling #6) so two repos with the same short name under different
            # owners (``ownerA/foo`` vs ``ownerB/foo``) no longer collide on a
            # single map entry. When the caller passed a bare repo name (no
            # ``/``), ``repo`` equals ``repo_name`` so bare-name callers are
            # unaffected. The on-disk worktree directory (and the container
            # mount target) stays the bare ``repo_name`` — only the map KEY
            # carries the owner prefix.
            worktrees[repo] = _b().translate_to_host_path(str(info.worktree_path))
        except (ValueError, RuntimeError) as e:
            # Capture full traceback so operators can diagnose without
            # re-instrumenting the gateway.  See #2186.
            logger.exception(
                "worktree_create per-repo failure",
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                assigned_branch=assigned_branch,
                error_type=type(e).__name__,
            )
            errors.append(f"{repo_name}: {e}")
        except Exception as e:
            logger.exception(
                "worktree_create unexpected per-repo failure",
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                assigned_branch=assigned_branch,
                error_type=type(e).__name__,
            )
            errors.append(f"{repo_name}: unexpected error - {e}")

    if errors and not worktrees:
        # Total failure: surface aggregate errors in logs + audit, then
        # best-effort clean up the empty container directory so retries
        # aren't blocked by stale state.  See #2186.
        logger.error(
            "worktree_create failed for all repos",
            container_id=container_id,
            errors=errors,
        )
        _b().audit_log(
            "worktrees_create_failed",
            "worktree_create",
            success=False,
            details={
                "container_id": container_id,
                "errors": errors,
            },
        )
        _b()._cleanup_empty_container_dir(container_id)
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": errors},
        )

    _b().audit_log(
        "worktrees_created",
        "worktree_create",
        success=True,
        details={
            "container_id": container_id,
            "repos": list(worktrees.keys()),
            "errors": errors,
        },
    )

    return make_success(
        "Worktrees created",
        {
            "worktrees": worktrees,
            "errors": errors if errors else None,
        },
    )


def worktree_delete() -> tuple[Response, int] | Response:
    """
    Delete worktrees for a container.

    Called by the egg launcher when a container exits. Removes the worktrees
    and associated branches.

    Request body:
        {
            "container_id": "egg-xxx-yyy",
            "force": false  # optional, force remove even with uncommitted changes
        }

    Returns:
        {
            "success": true,
            "message": "Worktrees deleted",
            "data": {
                "deleted": ["repo1", "repo2"],
                "warnings": ["repo1: had uncommitted changes"]
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    force = data.get("force", False)

    if not container_id:
        return make_error("Missing container_id")

    manager = _b().get_worktree_manager()

    # Get list of worktrees for this container
    worktree_dir = manager.worktree_base / container_id
    if not worktree_dir.exists():
        return make_success("No worktrees to delete", {"deleted": []})

    deleted = []
    errors = []
    warnings = []

    # Iterate through worktree directories
    for repo_dir in list(worktree_dir.iterdir()):
        if not repo_dir.is_dir():
            continue

        repo_name = repo_dir.name

        try:
            result = manager.remove_worktree(
                container_id=container_id,
                repo_name=repo_name,
                force=force,
            )

            if result.success:
                deleted.append(repo_name)
                if result.warning:
                    warnings.append(f"{repo_name}: {result.warning}")
            elif result.uncommitted_changes and not force:
                errors.append(f"{repo_name}: has uncommitted changes (use force=true)")
            elif result.error:
                errors.append(f"{repo_name}: {result.error}")
            else:
                errors.append(f"{repo_name}: removal failed")
        except Exception as e:
            errors.append(f"{repo_name}: unexpected error - {e}")

    _b().audit_log(
        "worktrees_deleted",
        "worktree_delete",
        success=True,
        details={
            "container_id": container_id,
            "deleted": deleted,
            "errors": errors,
        },
    )

    return make_success(
        "Worktrees deleted",
        {
            "deleted": deleted,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None,
        },
    )


def worktree_list() -> tuple[Response, int] | Response:
    """
    List all active worktrees.

    Returns information about all worktrees managed by the gateway.
    """
    manager = _b().get_worktree_manager()
    worktrees = manager.list_worktrees()
    return make_success("Worktrees listed", {"worktrees": worktrees})


_worktree_prune_lock = threading.Lock()


_SLICE_WORKTREE_SUFFIX_RE = re.compile(r"^-slice-[0-9]+-[a-z_]+$")


def _derive_worktree_anchor_ids(sessions: list[dict[str, Any]]) -> set[str]:
    """Return the set of worktree directory names implied by active sessions.

    Session ``container_id`` is the k8s Job name / Docker container name
    (e.g. ``egg-agent-issue-1758-again-coder``), but per-agent worktrees
    on disk are named after the orchestrator-assigned
    ``agent_worktree_id`` of the form ``{pipeline_id}-{agent_role}`` and
    the pipeline-level worktree is just ``{pipeline_id}``.  Without this
    derivation, ``cleanup_orphaned_worktrees`` treats every per-agent
    worktree as an orphan, which wiped live pipelines' worktrees during
    gateway startup cleanup (#1874).

    Slice-scoped per-agent worktrees (``{pipeline_id}-slice-{N}-{role}``,
    introduced in #2403) carry no slice context on the session, so for
    each live pipeline we additionally scan the worktree base for
    matching directories and add them to the anchor set.  Without this
    scan, slice-scoped agent worktrees with unpushed local commits are
    wiped during gateway startup cleanup — silently destroying the work
    that ``salvage_agent_commits`` was designed to recover (#2463).
    """
    anchors: set[str] = set()
    pipeline_ids: set[str] = set()
    for session_info in sessions:
        pipeline_id = session_info.get("pipeline_id")
        agent_role = session_info.get("agent_role")
        if pipeline_id:
            anchors.add(pipeline_id)
            pipeline_ids.add(pipeline_id)
            if agent_role:
                anchors.add(f"{pipeline_id}-{agent_role}")

    if pipeline_ids:
        try:
            entries = list(_b().WORKTREE_BASE_DIR.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            name = entry.name
            for pid in pipeline_ids:
                if not name.startswith(pid):
                    continue
                suffix = name[len(pid) :]
                if _SLICE_WORKTREE_SUFFIX_RE.match(suffix):
                    anchors.add(name)
                    break
    return anchors


def _container_ids_from_sessions(sessions: list[dict[str, Any]]) -> set[str]:
    """Return container IDs and worktree anchor IDs from session dicts.

    Each session contributes its own ``container_id`` plus the derived
    per-agent (``{pipeline_id}-{agent_role}``), pipeline-level
    (``{pipeline_id}``), and slice-scoped
    (``{pipeline_id}-slice-{N}-{role}``) worktree anchor IDs so that
    cleanup never wipes a live pipeline's worktrees (#1874, #2463).
    """
    ids: set[str] = set()
    for session_info in sessions:
        cid = session_info.get("container_id")
        if cid:
            ids.add(cid)
    ids |= _b()._derive_worktree_anchor_ids(sessions)
    return ids


def _collect_active_container_ids() -> set[str]:
    """Return the best-effort set of container IDs that back live sessions.

    Mirrors the startup-cleanup logic at module level so the prune route
    never issues a sweep with an empty active set (which would otherwise
    treat every worktree as an orphan — see #1759 review).

    Consults:

    1. Persisted sessions via :func:`get_session_manager` (primary source
       of truth — survives gateway restarts).  Each session contributes
       its own ``container_id`` plus the derived per-agent and
       pipeline-level worktree anchor IDs so that cleanup never wipes a
       live pipeline's worktrees (#1874).
    2. ``docker ps`` when Docker is reachable (safety net for sessions
       that outlive the session-manager snapshot).

    Failures in either step degrade silently so the prune still runs —
    the worst case is that a genuinely orphaned dir is preserved, which
    the next scheduled prune will clean up.
    """
    active_container_ids: set[str] = set()
    try:
        session_manager = _b().get_session_manager()
        sessions = session_manager.list_sessions()
        active_container_ids |= _b()._container_ids_from_sessions(sessions)
    except Exception as exc:
        logger.warning(
            "prune: session-manager active-container lookup failed",
            error=str(exc),
        )
    try:
        active_container_ids |= _b().get_active_docker_containers()
    except Exception as exc:
        # Non-fatal: on k3s there is no dockerd reachable from the
        # orchestrator's sidecar, and that is fine.
        logger.debug(
            "prune: docker active-container probe unavailable",
            error=str(exc),
        )
    return active_container_ids


def worktrees_prune() -> tuple[Response, int] | Response:
    """
    Run ``git worktree prune`` across every repo and sweep orphan dirs
    under the worktree base.

    Request body::

        {"dry_run": bool = true}

    When ``dry_run`` is true, returns the set of orphan directories
    that would be removed but does not mutate the filesystem. When
    false, removes them using the existing ``cleanup_orphaned_worktrees``
    helper.  The active-container set is derived from the session
    manager (plus an opportunistic ``docker ps`` fallback), and worktrees
    anchored to a non-terminal pipeline (per the orchestrator) are
    preserved even with no live container — a pipeline parked at a HITL
    gate has neither (#3070). Returns 503 when pipeline liveness cannot
    be verified.

    Proxied from the orchestrator's
    ``/api/v1/deployment/prune-worktrees`` endpoint (#1759).
    """
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", True))

    manager = _b().get_worktree_manager()

    # Serialize all prune activity — git operations on the same repo
    # must not interleave even if two callers hit this endpoint
    # concurrently.
    if not _b()._worktree_prune_lock.acquire(timeout=60):
        return make_error("Another worktree prune is in progress", status_code=409)
    try:
        # Pipeline liveness is required, not best-effort: a parked pipeline
        # has no containers or sessions, so the container-derived set alone
        # would mark its worktree an orphan (#3070). This endpoint is proxied
        # from the orchestrator, so it is normally up; refuse rather than
        # sweep blind if it cannot answer.
        active_pipeline_ids = _b().fetch_active_pipeline_ids()
        if active_pipeline_ids is None:
            return make_error(
                "Cannot verify pipeline liveness (orchestrator unreachable); "
                "refusing to prune worktrees",
                status_code=503,
            )

        active_container_ids = _b()._collect_active_container_ids()
        git_prune_report = manager.git_worktree_prune_all()
        orphan_dirs = manager.list_orphan_worktree_dirs(
            active_containers=active_container_ids,
            active_pipeline_ids=active_pipeline_ids,
        )

        removed_count = 0
        removed_paths: list[str] = []
        if not dry_run and orphan_dirs:
            removed_count = manager.cleanup_orphaned_worktrees(
                active_containers=active_container_ids,
                active_pipeline_ids=active_pipeline_ids,
            )
            # Any orphan we enumerated that no longer exists on disk
            # was removed by the helper.
            for path in orphan_dirs:
                try:
                    if not Path(path).exists():
                        removed_paths.append(path)
                except OSError:
                    pass

        _b().audit_log(
            "worktrees_pruned",
            "worktrees_prune",
            success=True,
            details={
                "dry_run": dry_run,
                "git_worktree_prune": git_prune_report,
                "orphan_dirs_count": len(orphan_dirs),
                "active_containers_count": len(active_container_ids),
                "active_pipelines_count": len(active_pipeline_ids),
                "removed_count": removed_count,
            },
        )

        return make_success(
            "Worktree prune complete",
            {
                "dry_run": dry_run,
                "git_worktree_prune": git_prune_report,
                "orphan_dirs": orphan_dirs,
                "active_containers_count": len(active_container_ids),
                "active_pipelines_count": len(active_pipeline_ids),
                "removed_count": removed_count,
                "removed_paths": removed_paths,
            },
        )
    finally:
        _b()._worktree_prune_lock.release()
