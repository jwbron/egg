"""Gateway sessions cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from flask import Response, jsonify, request

try:
    from ..git_client import (
        git_cmd,
    )
    from ..phase_filter import (
        PipelinePhase,
    )
    from ..rate_limiter import (
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from ..repo_parser import (
        parse_owner_repo,
    )
    from ..worktree_manager import (
        WorktreeManager,
        validate_branch_ref,
    )
except ImportError:  # flat/container import mode
    from git_client import (  # type: ignore[no-redef, import-untyped]
        git_cmd,
    )
    from phase_filter import (  # type: ignore[no-redef, import-untyped]
        PipelinePhase,
    )
    from rate_limiter import (  # type: ignore[no-redef, import-untyped]
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from repo_parser import (  # type: ignore[no-redef, import-untyped]
        parse_owner_repo,
    )
    from worktree_manager import (  # type: ignore[no-redef, import-untyped]
        WorktreeManager,
        validate_branch_ref,
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


def _branch_exists_on_remote(manager: WorktreeManager, repo_name: str, branch: str) -> bool:
    """Check if a branch exists on the remote (origin) for a repository.

    Args:
        manager: WorktreeManager instance (provides repos_base path)
        repo_name: Name of the repository
        branch: Branch name without origin/ prefix (e.g., "egg/issue-42/work")

    Returns:
        True if origin/{branch} exists, False otherwise.
    """
    main_repo = manager.repos_base / repo_name
    if not main_repo.exists():
        return False
    # Uses local tracking refs (origin/*) rather than querying the remote.
    # This is reliable here because the gateway handles push/fetch operations
    # which keep tracking refs up to date.  If stale refs ever become an
    # issue, switch to `git ls-remote --exit-code origin {branch}`.
    result = subprocess.run(
        git_cmd("rev-parse", "--verify", f"origin/{branch}"),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def session_create() -> tuple[Response, int] | Response:
    """
    Create a session with atomic visibility query, filtering, worktree creation.

    This is the primary endpoint for session registration. It performs:
    1. Query repository visibility for all requested repos
    2. Filter repos based on mode (private keeps private/internal, public keeps public)
    3. Create worktrees for filtered repos
    4. Register session with the filtered repo list

    This atomic operation prevents TOCTOU race conditions between visibility
    check and session registration.

    Request body:
        {
            "container_id": "egg-xxx",
            "container_ip": "172.18.0.3",
            "mode": "private"|"public",
            "repos": ["owner/repo1", "owner/repo2"],
            "local_only_repos": ["repo-name"],  // optional; no GitHub remote
            "uid": 1000,
            "gid": 1000,
            // Optional: when the caller already created per-agent worktrees
            // via /api/v1/worktrees/create, pass that container_id here so
            // the session reuses the existing worktrees instead of racing
            // to re-create them on the same bare repo (#1857).
            "worktree_container_id": "pipeline-role"
        }

    Response:
        {
            "success": true,
            "session_token": "tok_...",
            "filtered_repos": ["owner/repo1"],
            "worktrees": {
                "repo1": "/path/to/worktree"
            }
        }

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    container_ip = data.get("container_ip")
    mode = data.get("mode")
    repos = data.get("repos", [])
    local_only_repos = data.get("local_only_repos", [])
    uid = data.get("uid")
    gid = data.get("gid")
    # Optional worktree container_id — when provided, look up existing
    # worktrees created by a prior /api/v1/worktrees/create call instead of
    # re-creating them here.  Prevents the double create that races on
    # .git/config.lock for concurrent per-agent spawns (#1857).
    worktree_container_id = data.get("worktree_container_id")
    phase = data.get("phase")  # Optional SDLC pipeline phase
    pipeline_id = data.get("pipeline_id")  # Optional pipeline run ID
    issue_number = data.get("issue_number")  # Optional GitHub issue number
    pr_number = data.get("pr_number")  # Optional GitHub PR number
    agent_role = data.get("agent_role")  # Optional agent role
    agent_anchor_id = data.get("agent_anchor_id")  # Optional agent anchor ID
    claude_code_version = data.get("claude_code_version")  # Optional Claude Code version
    branch = data.get("branch")  # Optional git branch for non-pushing sessions
    # Optional pipeline base branch (PR base). Stored on the session and used
    # as the preferred diff base for the new-branch restricted-path push check
    # (#3024). Distinct from the locally-derived ``worktree_base_branch`` below,
    # which selects the ref a fresh worktree forks from.
    session_base_branch = data.get("base_branch")
    jira_ticket = data.get("jira_ticket")  # Optional Atlassian ticket key — advisory only
    synthetic = data.get("synthetic", False)  # Orchestrator-internal temp session
    # Per-session upstream routing (issue #2769). Default to "anthropic" so
    # pre-#2769 callers keep byte-identical session shape.
    upstream = data.get("upstream", "anthropic")
    upstream_model = data.get("upstream_model")

    # Validate required fields
    if not container_id:
        return make_error("Missing container_id")
    # container_ip is optional — k8s pod IPs are ephemeral and may not be
    # known at session creation time.  When omitted, token-only auth is used.
    # Kept for backward compatibility with Docker-based deployments.
    if mode not in ("private", "public"):
        return make_error("Invalid mode: must be 'private' or 'public'")
    # repos can be omitted for orchestrator-internal sessions that have a pipeline_id
    if not repos and not local_only_repos and not pipeline_id:
        return make_error("Missing repos list")

    # Validate uid/gid if provided
    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

    # Validate phase if provided
    if phase is not None and phase not in VALID_PIPELINE_PHASES:
        return make_error(
            f"Invalid phase: {phase}. Must be one of: {', '.join(sorted(VALID_PIPELINE_PHASES))}"
        )

    # Validate pipeline_id if provided
    if pipeline_id is not None:
        if not isinstance(pipeline_id, str):
            return make_error("Invalid pipeline_id: must be a string")
        if not pipeline_id:
            return make_error("Invalid pipeline_id: must be a non-empty string")
        if len(pipeline_id) > 256:
            return make_error("Invalid pipeline_id: must be 256 characters or fewer")

    # Validate base_branch if provided (#3024)
    #
    # Defense-in-depth: ``base_branch`` flows into ``git fetch`` and
    # ``git merge-base origin/<base_branch> HEAD`` as positional argv (see
    # ``get_changed_files_in_push`` / ``_enumerate_push_commits``). A value
    # starting with ``-`` would otherwise be interpreted as a git flag (the
    # historical ``--upload-pack=...`` shape that has produced git RCEs).
    # The orchestrator already validates ``base_branch`` at pipeline submission
    # and the launcher secret gates this endpoint, but the gateway must not
    # rely on caller hygiene here — ``validate_branch_ref`` rejects leading
    # dashes, ``..``, null bytes, ``//``, and other unsafe ref shapes.
    if session_base_branch is not None:
        if not isinstance(session_base_branch, str):
            return make_error("Invalid base_branch: must be a string")
        if not session_base_branch:
            return make_error("Invalid base_branch: must be a non-empty string")
        if len(session_base_branch) > 256:
            return make_error("Invalid base_branch: must be 256 characters or fewer")
        try:
            validate_branch_ref(session_base_branch, "base_branch")
        except ValueError as exc:
            return make_error(str(exc))

    # Validate issue_number if provided
    if issue_number is not None and (not isinstance(issue_number, int) or issue_number < 1):
        return make_error("Invalid issue_number: must be a positive integer")

    # Validate pr_number if provided
    if pr_number is not None and (not isinstance(pr_number, int) or pr_number < 1):
        return make_error("Invalid pr_number: must be a positive integer")

    # Validate agent_role if provided
    if agent_role is not None:
        if not isinstance(agent_role, str):
            return make_error("Invalid agent_role: must be a string")
        if not agent_role:
            return make_error("Invalid agent_role: must be non-empty if provided")
        if len(agent_role) > 64:
            return make_error("Invalid agent_role: must be 64 characters or fewer")

    # Validate agent_anchor_id if provided
    if agent_anchor_id is not None:
        if not isinstance(agent_anchor_id, str):
            return make_error("Invalid agent_anchor_id: must be a string")
        if len(agent_anchor_id) > 128:
            return make_error("Invalid agent_anchor_id: must be 128 characters or fewer")
        if not re.match(r"^[a-zA-Z0-9_-]+$", agent_anchor_id):
            return make_error(
                "Invalid agent_anchor_id: must contain only alphanumeric characters, hyphens, and underscores"
            )

    # Validate claude_code_version if provided
    if claude_code_version is not None:
        if not isinstance(claude_code_version, str):
            return make_error("Invalid claude_code_version: must be a string")
        if len(claude_code_version) > 64:
            return make_error("Invalid claude_code_version: must be 64 characters or fewer")

    # Validate branch if provided
    #
    # Same argv-injection concern as ``base_branch`` above — ``branch`` flows
    # into ``git fetch origin <branch>`` and ``git rev-list origin/<branch>..HEAD``
    # in the push handler, so refuse leading dashes and other unsafe ref shapes
    # via ``validate_branch_ref`` regardless of upstream callers' own checks.
    if branch is not None:
        if not isinstance(branch, str):
            return make_error("Invalid branch: must be a string")
        if len(branch) > 256:
            return make_error("Invalid branch: must be 256 characters or fewer")
        try:
            validate_branch_ref(branch, "branch")
        except ValueError as exc:
            return make_error(str(exc))

    # Validate synthetic if provided
    if not isinstance(synthetic, bool):
        return make_error("Invalid synthetic: must be a boolean")

    # Validate upstream / upstream_model (issue #2769).
    # ``upstream`` must be a name the UpstreamRegistry will serve — refuse
    # silently routing unknown upstreams to Anthropic.
    if not isinstance(upstream, str):
        return make_error("Invalid upstream: must be a string")
    if not _b().get_upstream_registry().is_known(upstream):
        known = ", ".join(sorted(_b().get_upstream_registry().known_upstreams()))
        return make_error(f"Invalid upstream: '{upstream}'. Must be one of: {known}")
    if upstream_model is not None:
        if not isinstance(upstream_model, str):
            return make_error("Invalid upstream_model: must be a string")
        if not upstream_model:
            return make_error("Invalid upstream_model: must be non-empty if provided")
        if len(upstream_model) > 256:
            return make_error("Invalid upstream_model: must be 256 characters or fewer")

    # Validate worktree_container_id if provided
    if worktree_container_id is not None:
        if not isinstance(worktree_container_id, str):
            return make_error("Invalid worktree_container_id: must be a string")
        if not worktree_container_id:
            return make_error("Invalid worktree_container_id: must be non-empty if provided")
        if len(worktree_container_id) > 256:
            return make_error("Invalid worktree_container_id: must be 256 characters or fewer")
        if ".." in worktree_container_id or not re.match(
            r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", worktree_container_id
        ):
            return make_error("Invalid worktree_container_id: contains unsafe characters")

    # Validate local_only_repos if provided
    if local_only_repos:
        if not isinstance(local_only_repos, list):
            return make_error("Invalid local_only_repos: must be a list")
        if len(local_only_repos) > 50:
            return make_error("Invalid local_only_repos: too many entries")
        for repo_name in local_only_repos:
            if not isinstance(repo_name, str):
                return make_error("Invalid local_only_repos: all items must be strings")
            if not repo_name or len(repo_name) > 256:
                return make_error("Invalid local_only_repos: repo name must be 1-256 chars")
            if ".." in repo_name or "/" in repo_name:
                return make_error(f"Invalid local_only_repos: unsafe repo name '{repo_name}'")

    # Step 1: Query visibility for all repos
    repo_visibilities = {}
    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            visibility = _b().get_repo_visibility(repo_info.owner, repo_info.repo)
            repo_visibilities[repo] = visibility
        else:
            # Can't parse repo - skip it
            logger.warning(
                "Could not parse repository for visibility check",
                repo=repo,
                container_id=container_id,
            )

    # Step 2: Filter repos based on mode
    # private mode: include repos with known visibility (private, internal, public).
    #   Write access is controlled separately by push policy (only private/internal
    #   repos are writable). The network is locked down — mounting a public repo
    #   in private mode doesn't grant broader internet access.
    #   Repos with unknown visibility (None) are excluded (fail-closed).
    # public mode: keep only public repos (don't mount private repos on open network)
    filtered_repos = []
    for repo, visibility in repo_visibilities.items():
        if mode == "private":
            # Private mode: include repos with known visibility — network is
            # locked down anyway so mounting a public repo is safe.
            # Push policy enforces write restrictions to private/internal repos.
            if visibility is None:
                # Unknown visibility — repo may not exist or API unreachable.
                # Fail closed: don't attempt to mount a repo we can't verify.
                logger.warning(
                    "Unknown visibility for repo, excluding in private mode",
                    repo=repo,
                    container_id=container_id,
                )
                continue
            elif visibility not in ("private", "internal"):
                logger.info(
                    "Including public repo in private mode (network locked down)",
                    repo=repo,
                    visibility=visibility,
                    container_id=container_id,
                )
            filtered_repos.append(repo)
        else:
            # Public mode: only mount public repos
            if visibility is None:
                # Unknown visibility — can't confirm public, exclude
                logger.warning(
                    "Unknown visibility for repo, excluding in public mode",
                    repo=repo,
                    container_id=container_id,
                )
            elif visibility == "public":
                filtered_repos.append(repo)
            else:
                logger.debug(
                    "Excluding non-public repo in public mode",
                    repo=repo,
                    visibility=visibility,
                    container_id=container_id,
                )

    # Step 2b: Include local-only repos in private mode.
    # These repos have no GitHub remote so GitHub visibility cannot be checked.
    # They are always treated as private: included in private mode, excluded in public mode.
    if local_only_repos and mode == "private":
        for repo_name in local_only_repos:
            filtered_repos.append(repo_name)
            logger.info(
                "Including local-only repo in private mode",
                repo=repo_name,
                container_id=container_id,
            )
    elif local_only_repos and mode != "private":
        logger.debug(
            "Excluding local-only repos in public/local mode",
            repos=local_only_repos,
            mode=mode,
            container_id=container_id,
        )

    # Step 3: Create worktrees for filtered repos
    worktrees = {}
    worktree_errors = []
    first_worktree_path: str | None = None  # Gateway-side path for the session's repo context

    # Only initialise the worktree manager when there are repos to process.
    # Local-mode sessions (no repos) skip worktree creation entirely, so
    # avoid hitting the filesystem for the worktree base directory.
    if filtered_repos:
        manager = _b().get_worktree_manager()

    for repo in filtered_repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        try:
            if worktree_container_id:
                # Reuse worktrees created by a prior /api/v1/worktrees/create
                # call.  Avoids a second concurrent ``git worktree add`` on the
                # same bare repo, which races on ``.git/config.lock`` (#1857).
                info = manager.lookup_worktree(
                    repo_name=repo_name,
                    container_id=worktree_container_id,
                )
            else:
                # For pipeline sessions, prefer the pipeline's existing worktree
                # branch (which contains artifacts from prior agents) over a fresh
                # branch from origin/main.  This ensures HITL exec sessions can
                # see drafts, contracts, and reviews committed by pipeline agents.
                # See #1016.
                #
                # For fresh pipelines (no prior worktree branch), fall back to the
                # remote default branch (e.g., origin/main) instead of HEAD.  HEAD
                # may point to a feature branch in the main repo, which would
                # pollute the worktree with commits outside the current phase's
                # allowed scope and cause push rejections.  See #860.
                if pipeline_id:
                    pipeline_work_branch = f"egg/{pipeline_id}/work"
                    if _b()._branch_exists_on_remote(manager, repo_name, pipeline_work_branch):
                        worktree_base_branch = f"origin/{pipeline_work_branch}"
                    else:
                        worktree_base_branch = manager.resolve_default_branch(repo_name)
                else:
                    worktree_base_branch = "HEAD"

                info = manager.create_worktree(
                    repo_name=repo_name,
                    container_id=container_id,
                    base_branch=worktree_base_branch,
                    uid=uid,
                    gid=gid,
                    assigned_branch=branch,
                    repo_slug=repo,
                )
            # Capture the first worktree's gateway-side path for the session's repo context
            if first_worktree_path is None:
                first_worktree_path = str(info.worktree_path)
            # Translate container path to host path for egg launcher mount sources
            worktrees[repo_name] = _b().translate_to_host_path(str(info.worktree_path))
        except ValueError as e:
            worktree_errors.append(f"{repo_name}: {e}")
        except RuntimeError as e:
            worktree_errors.append(f"{repo_name}: {e}")
        except Exception as e:
            worktree_errors.append(f"{repo_name}: unexpected error - {e}")

    # If no worktrees could be created, fail
    if not worktrees and filtered_repos:
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": worktree_errors},
        )

    # Step 4: Register session
    session_manager = _b().get_session_manager()
    token, _session = session_manager.register_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
        phase=phase,
        pipeline_id=pipeline_id,
        issue_number=issue_number,
        pr_number=pr_number,
        agent_role=agent_role,
        agent_anchor_id=agent_anchor_id,
        claude_code_version=claude_code_version,
        branch=branch,
        base_branch=session_base_branch,
        jira_ticket=jira_ticket if isinstance(jira_ticket, str) and jira_ticket else None,
        synthetic=synthetic,
        upstream=upstream,
        upstream_model=upstream_model,
    )

    # Pre-populate the session's repo path so non-pushing sessions (reviewers,
    # architects, etc.) can resolve their worktree before any git push. This is
    # also set on git push, but pipeline agents that never push would otherwise
    # have a None value.
    if first_worktree_path is not None:
        _session.last_repo_path = first_worktree_path

    # Use the shared pipeline branch for push enforcement.
    # session_manager already sets assigned_branch from the `branch`
    # request parameter (session_manager.py:560-564). If that wasn't
    # provided, fall back to the canonical pipeline branch name.
    if pipeline_id and not _session.assigned_branch:
        _session.assigned_branch = f"egg/{pipeline_id}/work"

    _b().audit_log(
        "session_created",
        "session_create",
        success=True,
        details={
            "container_id": container_id,
            "container_ip": container_ip,
            "mode": mode,
            "phase": phase,
            "pipeline_id": pipeline_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "agent_role": agent_role,
            "filtered_repos": filtered_repos,
            "worktree_count": len(worktrees),
            "worktree_errors": worktree_errors if worktree_errors else None,
            "upstream": upstream,
            "upstream_model": upstream_model,
        },
    )

    return make_success(
        "Session created",
        {
            "session_token": token,
            "filtered_repos": filtered_repos,
            "worktrees": worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )


def _cleanup_container_worktrees(
    container_id: str,
) -> tuple[list[str], list[str]]:
    """Clean up all worktrees for a container.

    Returns:
        Tuple of (deleted_repo_names, errors).
    """
    manager = _b().get_worktree_manager()
    worktree_dir = manager.worktree_base / container_id
    deleted_worktrees: list[str] = []
    errors: list[str] = []
    if worktree_dir.exists():
        for repo_dir in list(worktree_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            repo_name = repo_dir.name
            try:
                result = manager.remove_worktree(
                    container_id=container_id,
                    repo_name=repo_name,
                    force=True,
                )
                if result.success:
                    deleted_worktrees.append(repo_name)
                elif result.error:
                    errors.append(f"{repo_name}: {result.error}")
                else:
                    errors.append(f"{repo_name}: removal failed")
            except Exception as e:
                errors.append(f"{repo_name}: unexpected error - {e}")
    return deleted_worktrees, errors


def session_delete(session_token: str) -> tuple[Response, int] | Response:
    """
    Delete a session.

    Only the launcher (with launcher_secret) can delete sessions.
    Containers CANNOT delete sessions.

    Also cleans up associated worktrees.

    Args:
        session_token: The session token to delete

    Auth: Bearer {launcher_secret}
    """
    session_manager = _b().get_session_manager()

    # Get session info for worktree cleanup
    session = session_manager.get_session(session_token)
    container_id = session.container_id if session else None

    # Delete the session
    deleted = session_manager.delete_session(session_token)

    if not deleted:
        return make_error("Session not found", status_code=404)

    # _capture_and_cleanup_session (called inside delete_session) auto-commits
    # the agent's WIP synchronously before returning, so the worktree is safe
    # to remove at this point.

    # Clean up worktrees for this container
    deleted_worktrees, worktree_errors = (
        _cleanup_container_worktrees(container_id) if container_id else ([], [])
    )

    _b().audit_log(
        "session_deleted",
        "session_delete",
        success=True,
        details={
            "container_id": container_id,
            "worktrees_deleted": deleted_worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )

    return make_success("Session deleted")


def session_delete_by_container(container_id: str) -> tuple[Response, int] | Response:
    """
    Delete a session by container ID.

    Used by the orchestrator for cleanup when the session token is not available.

    Args:
        container_id: The container ID whose session to delete

    Auth: Bearer {launcher_secret}
    """
    session_manager = _b().get_session_manager()
    deleted = session_manager.delete_session_by_container(container_id)

    if not deleted:
        return make_error("Session not found for container", status_code=404)

    # _capture_and_cleanup_session (called inside delete_session_by_container)
    # auto-commits the agent's WIP synchronously before returning, so the
    # worktree is safe to remove at this point.

    # Clean up worktrees for this container
    deleted_worktrees, worktree_errors = _cleanup_container_worktrees(container_id)

    _b().audit_log(
        "session_deleted",
        "session_delete_by_container",
        success=True,
        details={
            "container_id": container_id,
            "worktrees_deleted": deleted_worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )

    return make_success("Session deleted")


def session_heartbeat_by_container(container_id: str) -> tuple[Response, int] | Response:
    """
    Refresh a session's idle timer by container ID (orchestrator-only path).

    Used by the orchestrator to keep agent sessions alive while their
    container is heartbeating on the BRC bus but not making gateway
    requests — without this, the idle pruner evicts the session after
    EGG_SESSION_IDLE_TIMEOUT_MINUTES even though the agent is still
    working (see #2068).

    Auth: Bearer {launcher_secret}

    Returns 404 if no session exists for the container.  No per-session
    rate limit because the launcher secret already gates access — only
    the orchestrator can call this.
    """
    session_manager = _b().get_session_manager()
    refreshed = session_manager.heartbeat_session_by_container(container_id)

    if not refreshed:
        return make_error("Session not found for container", status_code=404)

    return make_success("Heartbeat recorded")


def session_get(session_token: str) -> tuple[Response, int] | Response:
    """
    Get session information and validate if it exists.

    Args:
        session_token: The session token

    Auth: Bearer {launcher_secret}

    Response:
        {
            "valid": true,
            "mode": "private"|"public",
            "container_id": "...",
            "expires_at": "...",
        }
    """
    session_manager = _b().get_session_manager()
    result = session_manager.validate_session(session_token)

    if not result.valid:
        return jsonify({"valid": False, "error": result.error or "Session not found"}), 404

    return jsonify(
        {
            "valid": True,
            "mode": result.session.mode if result.session else None,
            "container_id": result.session.container_id if result.session else None,
            "expires_at": result.session.expires_at.isoformat() if result.session else None,
        }
    )


def session_heartbeat(session_token: str) -> tuple[Response, int] | Response:
    """
    Explicit session heartbeat to extend TTL.

    Note: Heartbeats are also triggered implicitly on any successful
    session-authenticated request. This endpoint exists for edge cases
    where long-running operations need TTL extension without git/gh activity.

    Args:
        session_token: The session token

    Auth: Bearer {session_token}

    Rate limit: 100 per hour per session
    """
    # Validate the session
    result = _b().validate_session_for_request(session_token, request.remote_addr)
    if not result.valid:
        # Record failed lookup for rate limiting
        record_failed_lookup(request.remote_addr or "")
        return make_error(result.error or "Invalid session", status_code=401)

    # Check heartbeat rate limit (100 per hour per session)
    if result.session:
        rate_limit = check_heartbeat_rate_limit(result.session.session_token_hash)
        if not rate_limit.allowed:
            return make_error(
                f"Heartbeat rate limit exceeded. Retry after {rate_limit.retry_after_seconds}s",
                status_code=429,
            )

    # Session validation already extends TTL, just return success
    return make_success(
        "Heartbeat recorded",
        {
            "expires_at": result.session.expires_at.isoformat() if result.session else None,
        },
    )


def session_update(session_token: str) -> tuple[Response, int] | Response:
    """
    Update session container binding (container_id and/or container_ip).

    Used by the orchestrator to bind a session to the real container
    after pre-registering with a placeholder ID before container creation.

    Request body:
        {
            "container_id": "abc123...",  # Optional
            "container_ip": "172.32.0.10"  # Optional
        }

    At least one of container_id or container_ip must be provided.

    Args:
        session_token: The session token to update

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    container_ip = data.get("container_ip")

    if not container_id and not container_ip:
        return make_error("Must provide container_id and/or container_ip")

    session_manager = _b().get_session_manager()
    success = session_manager.update_session(
        session_token,
        container_id=container_id,
        container_ip=container_ip,
    )

    if not success:
        return make_error("Session not found or expired", status_code=404)

    _b().audit_log(
        "session_container_updated",
        "session_update",
        success=True,
        details={
            "container_id": container_id,
            "container_ip": container_ip,
        },
    )

    return make_success(
        "Session updated",
        {
            "container_id": container_id,
            "container_ip": container_ip,
        },
    )


VALID_PIPELINE_PHASES = frozenset(p.value for p in PipelinePhase)


def session_update_phase(session_token: str) -> tuple[Response, int] | Response:
    """
    Update the SDLC pipeline phase for a session.

    This endpoint allows the launcher/workflow to update the phase as
    the pipeline progresses. Phase restrictions are enforced by the
    gateway for operations like PR creation.

    Request body:
        {
            "phase": "refine"|"plan"|"implement"|"pr"
        }

    Args:
        session_token: The session token to update

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    phase = data.get("phase")
    if not phase:
        return make_error("Missing phase")

    if phase not in VALID_PIPELINE_PHASES:
        return make_error(
            f"Invalid phase: {phase}. Must be one of: {', '.join(sorted(VALID_PIPELINE_PHASES))}"
        )

    session_manager = _b().get_session_manager()
    success = session_manager.update_phase(session_token, phase)

    if not success:
        return make_error("Session not found or expired", status_code=404)

    _b().audit_log(
        "session_phase_updated",
        "session_update_phase",
        success=True,
        details={"phase": phase},
    )

    return make_success("Phase updated", {"phase": phase})


def repos_visibility() -> tuple[Response, int] | Response:
    """
    Query visibility for multiple repositories.

    Used by launcher for informational queries. For atomic session+worktree
    creation, use POST /api/v1/sessions/create instead.

    Query params:
        repos: Comma-separated list of owner/repo strings

    Response:
        {
            "visibilities": {
                "owner/repo1": "public",
                "owner/repo2": "private",
                "owner/repo3": "internal"
            }
        }

    Auth: Bearer {launcher_secret}
    """
    repos_param = request.args.get("repos", "")
    if not repos_param:
        return make_error("Missing repos query parameter")

    repos = [r.strip() for r in repos_param.split(",") if r.strip()]
    if not repos:
        return make_error("No valid repos provided")

    visibilities = {}
    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            visibility = _b().get_repo_visibility(repo_info.owner, repo_info.repo)
            visibilities[repo] = visibility
        else:
            visibilities[repo] = None

    return make_success("Visibility queried", {"visibilities": visibilities})


def sessions_list() -> tuple[Response, int] | Response:
    """
    List all active sessions.

    Auth: Bearer {launcher_secret}
    """
    session_manager = _b().get_session_manager()
    sessions = session_manager.list_sessions()
    return make_success("Sessions listed", {"sessions": sessions})
