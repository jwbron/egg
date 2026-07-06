"""Gateway git_ops cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from egg_restrictions.hints import derive_hint as _derive_push_denied_hint
from flask import Response, g, request

try:
    from ..git_client import (
        cleanup_credential_helper,
        create_credential_helper,
        git_cmd,
        validate_git_args,
    )
    from ..phase_filter import (
        check_anchor_write_permission,
    )
    from ..policy import (
        extract_branch_from_refspec,
        extract_repo_from_remote,
    )
    from ..repo_parser import (
        parse_owner_repo,
    )
except ImportError:  # flat/container import mode
    from git_client import (  # type: ignore[no-redef, import-untyped]
        cleanup_credential_helper,
        create_credential_helper,
        git_cmd,
        validate_git_args,
    )
    from phase_filter import (  # type: ignore[no-redef, import-untyped]
        check_anchor_write_permission,
    )
    from policy import (  # type: ignore[no-redef, import-untyped]
        extract_branch_from_refspec,
        extract_repo_from_remote,
    )
    from repo_parser import (  # type: ignore[no-redef, import-untyped]
        parse_owner_repo,
    )

from ._helpers import make_error, make_success, make_worktree_not_found_error


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


def _detached_head_hint(
    operation: str,
    exec_path: str,
    repo_path: str,
    container_id: str | None,
) -> str:
    """Return a recovery hint string when a `commit` lands on detached HEAD.

    Used by the git-execute handler to surface the exact ``update-ref``
    invocation an agent needs to set its work branch to the new commit
    (issue #2162).  The empty string means "no hint" — caller appends as-is.

    The trigger is intentionally narrow:

    * Only ``operation == "commit"`` and only when the session has an
      ``assigned_branch`` — we do not want to noise non-pipeline sessions.
    * ``git symbolic-ref --quiet HEAD`` must return exactly 1 with empty
      stdout AND empty stderr.  Returncode 128 (corrupt repo, .git missing,
      "fatal: ...") and any non-empty stderr are treated as ambiguous and
      yield no hint — telling the agent to run ``update-ref`` against a
      broken repository would be misleading.
    """
    if operation != "commit":
        return ""
    session = getattr(g, "session", None)
    assigned = getattr(session, "assigned_branch", None) if session else None
    if not isinstance(assigned, str) or not assigned:
        return ""
    try:
        head_check = subprocess.run(
            git_cmd("symbolic-ref", "--quiet", "HEAD"),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    # Tight check: returncode 1 with no stdout and no stderr is unambiguously
    # detached HEAD.  Anything else (corrupt repo, missing .git, EAGAIN) gets
    # no hint.
    if head_check.returncode != 1:
        return ""
    if head_check.stdout.strip():
        return ""
    if head_check.stderr.strip():
        # Symbolic-ref returncode==1 with empty stdout but non-empty stderr is
        # ambiguous (e.g. future git versions writing config-deprecation
        # warnings).  Log at debug so a missing hint is debuggable rather than
        # silent, and bail out — telling the agent to run update-ref against
        # an unclear HEAD state would be misleading.
        logger.debug(
            "detached_head_hint_suppressed_stderr",
            repo_path=repo_path,
            container_id=container_id,
            assigned_branch=assigned,
            stderr=head_check.stderr.strip()[:200],
        )
        return ""
    logger.info(
        "detached_head_commit_hint",
        repo_path=repo_path,
        container_id=container_id,
        assigned_branch=assigned,
    )
    return (
        f"\n[gateway] HEAD is detached. Your commit is not on "
        f"branch '{assigned}'. To set the branch to this commit, run:\n"
        f"  git update-ref refs/heads/{assigned} HEAD\n"
    )


_SLICE_INTEGRATION_BRANCH_RE = re.compile(r"^egg/[A-Za-z0-9][A-Za-z0-9_-]*/(?:slice|phase)-\d+$")


def git_push() -> tuple[Response, int] | Response:
    """
    Handle git push requests.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "remote": "origin",
            "refspec": "branch-name",
            "force": false,
            "force_with_lease": false,  # safer alternative to force
            "commit_sha": "<40-hex>",   # alternative to refspec; consensus pushes only
        }

    ``force_with_lease`` (#2137 stacked-PR reconciler) is preferred over
    ``force`` for non-fast-forward pushes. Both flags are mutually
    exclusive — ``force_with_lease`` takes precedence if both are set.

    Policy: branch_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    refspec = data.get("refspec", "")
    force = data.get("force", False)
    force_with_lease = data.get("force_with_lease", False)
    container_id = data.get("container_id")
    commit_sha = data.get("commit_sha", "")

    if not repo_path:
        return make_error("Missing repo_path")

    # Detached-HEAD-tolerant consensus push (#2200): when the agent's HEAD
    # is detached (post-rebase or otherwise), the helper cannot read
    # ``git branch --show-current`` and instead supplies ``commit_sha``.
    # The gateway derives the refspec server-side from the session's
    # assigned branch.  This is strictly tighter than an agent-supplied
    # refspec because the existing ``push_target_enforcement`` block
    # below already requires ``branch == session.assigned_branch``.
    if commit_sha and not refspec:
        if not data.get("consensus_push"):
            _b().audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": "commit_sha push requires consensus_push=true",
                },
            )
            return make_error(
                "commit_sha push requires consensus_push=true",
                status_code=400,
            )
        # Require a full SHA (40 = SHA-1, 64 = SHA-256). Abbreviated SHAs
        # (7-39 chars) can resolve ambiguously on the gateway side; the
        # helper always emits the full output of ``git rev-parse HEAD`` so
        # there is no legitimate caller of the shorter range. The explicit
        # ``isinstance`` guard turns a non-string payload into a clean 400
        # rather than a 500 from ``re.fullmatch``.
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            _b().audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": f"Invalid commit_sha {commit_sha!r}",
                },
            )
            return make_error(
                f"Invalid commit_sha {commit_sha!r}: must be 40-64 hex chars",
                status_code=400,
            )
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if not isinstance(assigned, str) or not assigned:
            _b().audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": "commit_sha push requires a pipeline session with assigned_branch",
                },
            )
            return make_error(
                "commit_sha push requires a pipeline session with an assigned branch",
                status_code=400,
            )
        refspec = f"{commit_sha}:refs/heads/{assigned}"
        # Distinct audit event so post-incident review can distinguish a
        # gateway-constructed refspec (commit_sha path) from an
        # agent-supplied refspec; both flow through the same downstream
        # ``push_*`` audit events and would otherwise be indistinguishable.
        _b().audit_log(
            "push_via_commit_sha",
            "git_push",
            success=True,
            details={
                "repo_path": repo_path,
                "commit_sha": commit_sha,
                "assigned_branch": assigned,
                "constructed_refspec": refspec,
            },
        )

    # Validate repo_path to prevent path traversal attacks
    path_valid, path_error = _b().validate_repo_path(repo_path)
    if not path_valid:
        _b().audit_log(
            "push_blocked",
            "git_push",
            success=False,
            details={"repo_path": repo_path, "reason": path_error},
        )
        return make_error(path_error, status_code=403)

    # Map container path to worktree path if container_id is provided
    exec_path = _b().map_container_path_to_worktree(repo_path, container_id, "push")
    if exec_path is None:
        return make_worktree_not_found_error(container_id)

    # Get remote URL to determine repo
    remote_url, url_error = _b().resolve_remote_url(remote, exec_path)
    if url_error:
        return make_error(url_error)

    # Extract repo from URL
    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    # Extract branch from refspec
    branch = extract_branch_from_refspec(refspec)
    if not branch:
        # Try to get current branch
        try:
            result = subprocess.run(
                git_cmd("branch", "--show-current"),
                cwd=exec_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            branch = result.stdout.strip()
        except Exception:
            pass

    if not branch:
        return make_error("Could not determine branch to push")

    # Determine auth mode for this repo
    auth_mode = _b().get_auth_mode(repo)

    # Check Private Repo Mode policy (if enabled)
    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)
    session_phase = getattr(g, "session_phase", None)

    # Orchestrator-authenticated push (launcher secret).  The orchestrator
    # has a different trust boundary than sandboxed agents — its pushes are
    # programmatic (contract init, state-sync, completion) and bypass the
    # session-derived enforcement (pipeline-push block, push-target check,
    # role/phase file restrictions) that exists to sandbox agent commits.
    # session_mode comes from the request body since there is no session.
    is_orchestrator_push = getattr(g, "auth_actor", None) == "launcher"
    if is_orchestrator_push:
        mode_in = data.get("mode")
        if mode_in is not None and mode_in not in ("public", "private"):
            _b().audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": f"Invalid mode {mode_in!r} on launcher-auth push",
                },
            )
            return make_error(
                f"Invalid mode {mode_in!r}: must be 'public' or 'private'",
                status_code=400,
            )
        session_mode = mode_in or session_mode
        _b().audit_log(
            "push_orchestrator_authenticated",
            "git_push",
            success=True,
            details={
                "repo_path": repo_path,
                "remote": remote,
                "refspec": refspec,
                "reason": "Push authenticated with launcher secret — orchestrator-trusted",
            },
        )

    # Infrastructure branch bypass: pushes to infrastructure branches always succeed
    # regardless of session mode or phase (pipeline state can be written at any time).
    from egg_config.constants import PIPELINE_STATE_BRANCH

    INFRASTRUCTURE_BRANCHES = {PIPELINE_STATE_BRANCH}
    is_infrastructure_push = branch in INFRASTRUCTURE_BRANCHES

    # Slice integration-branch creation (#2368): the orchestrator pre-creates
    # ``egg/<base>/(slice|phase)-N`` on origin from the parent branch via a
    # synthetic, launcher-authenticated session before any agent runs.  That
    # push is orchestrator infrastructure — not an agent BRC propose — so it
    # must bypass the pipeline-session push block introduced in #2028.  The
    # ``synthetic=True`` flag can only be set by the launcher (the
    # ``/api/v1/sessions/create`` endpoint is gated by ``require_launcher_auth``),
    # so a sandboxed agent's session token cannot reach this branch.
    #
    # The legacy ``egg/<base>/context`` context-branch exemption (#2548) was
    # removed in #2777 (cq-2 / cq-4): the dedicated context branch is gone
    # and the context PR now opens on ``egg/<id>/work → main`` directly,
    # which is already covered by the pipeline-session push-allow list.
    is_slice_integration_push = False
    if not is_infrastructure_push and _SLICE_INTEGRATION_BRANCH_RE.match(branch):
        # ``Session.synthetic`` is a ``bool`` (default ``False``); only an
        # orchestrator-issued session can carry ``synthetic=True`` because
        # ``/api/v1/sessions/create`` is gated on the launcher secret.  Use
        # an identity check rather than a truthiness test so a future
        # surface that ever stores something other than ``True`` (and any
        # MagicMock fake whose default attr is truthy) cannot accidentally
        # opt into the exemption.
        if hasattr(g, "session") and getattr(g.session, "synthetic", False) is True:
            is_slice_integration_push = True
            is_infrastructure_push = True
            _b().audit_log(
                "push_slice_integration_exempt",
                "git_push",
                success=True,
                details={
                    "repo_path": repo_path,
                    "remote": remote,
                    "refspec": refspec,
                    "branch": branch,
                    "reason": (
                        "Synthetic-session slice integration branch push — "
                        "orchestrator infrastructure (#2368)"
                    ),
                },
            )

    repo_info = parse_owner_repo(repo)
    if repo_info:
        # Infrastructure operations — always accessible regardless of
        # session mode. This covers infrastructure branch pushes
        # (pipeline state) and synthetic slice-integration pushes.
        if is_infrastructure_push:
            if is_slice_integration_push:
                exempt_type = "slice_integration_branch"
            else:
                exempt_type = "infrastructure_branch"
            # A successful slice-integration push intentionally emits BOTH
            # ``push_slice_integration_exempt`` (above, the orchestrator-
            # specific event) AND ``push_infrastructure_exempt`` with
            # ``exempt_type="slice_integration_branch"`` (here, the generic
            # exemption event).  Operators grepping ``push_infrastructure_exempt``
            # for "infra pushes" should filter out the slice variant via
            # ``exempt_type``; the dual emission is intentional so the
            # orchestrator-specific path is also visible to operators
            # filtering on the slice-integration event name (#2370 review).
            _b().audit_log(
                "push_infrastructure_exempt",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "reason": "Infrastructure operation exempt from private mode policy",
                    "exempt_type": exempt_type,
                },
            )
        else:
            priv_result = _b().check_private_repo_access(
                operation="push",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=True,
                session_mode=session_mode,
            )
            if not priv_result.allowed:
                _b().audit_log(
                    "push_denied_private_mode",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "reason": priv_result.reason,
                        "visibility": priv_result.visibility,
                        "auth_mode": auth_mode,
                    },
                )
                return make_error(
                    priv_result.reason,
                    status_code=403,
                    details=priv_result.to_dict(),
                )

    # SECURITY: Pipeline push enforcement.
    # All SDLC producer phases (refine/plan/implement) are BRC phases, so every
    # pipeline-session push must route through mcp__brc__propose (which sets the
    # consensus_push marker).  A direct git push from a pipeline session — whether
    # bare, mis-targeted, or correctly-targeted — is rejected with a single
    # unambiguous error pointing at the right tool, instead of the three-layer
    # error cascade that previously sent agents refspec-hunting (#2028).
    # Infrastructure pushes (pipeline-state branch, etc.) are exempt.
    if not is_infrastructure_push:
        # Killswitch: PIPELINE_PUSH_ENFORCEMENT=false (legacy alias:
        # CONCURRENT_PUSH_ENFORCEMENT=false) disables the block.
        enforcement_env = os.environ.get(
            "PIPELINE_PUSH_ENFORCEMENT",
            os.environ.get("CONCURRENT_PUSH_ENFORCEMENT", "true"),
        )
        pipeline_push_enforcement = enforcement_env.lower() not in ("false", "0", "no")
        if pipeline_push_enforcement:
            session_pipeline_id = None
            if hasattr(g, "session") and g.session:
                session_pipeline_id = getattr(g.session, "pipeline_id", None)
            if isinstance(session_pipeline_id, str) and session_pipeline_id:
                if not data.get("consensus_push"):
                    _b().audit_log(
                        "push_denied_pipeline_session",
                        "git_push",
                        success=False,
                        details={
                            "repo": repo,
                            "branch": branch,
                            "pipeline_id": session_pipeline_id,
                            "reason": "Direct push blocked for pipeline session",
                        },
                    )
                    return make_error(
                        "Direct git push is blocked for pipeline sessions. "
                        "Publish your artifact via the mcp__brc__propose tool "
                        "(which pushes to origin and sends CONSENSUS_PROPOSE "
                        "in one step). Fallback CLI: "
                        "`egg-orch consensus propose --push`.",
                        status_code=403,
                        details={
                            "pipeline_id": session_pipeline_id,
                            "requirement": "consensus_push",
                            "recommended_tool": "mcp__brc__propose",
                        },
                    )

        # Push-target enforcement: a consensus_push request must still target the
        # session's assigned branch.  Defense-in-depth against a malformed propose
        # call (consensus_push=true but wrong refspec).  Non-pipeline sessions
        # (e.g. user-mode pushes) are not subject to this check.
        # Killswitch: PUSH_TARGET_ENFORCEMENT=false.
        push_target_enforcement = os.environ.get("PUSH_TARGET_ENFORCEMENT", "true").lower() not in (
            "false",
            "0",
            "no",
        )
        if push_target_enforcement and hasattr(g, "session") and g.session:
            session_pipeline_id = getattr(g.session, "pipeline_id", None)
            session_assigned_branch = getattr(g.session, "assigned_branch", None)
            if isinstance(session_pipeline_id, str) and isinstance(session_assigned_branch, str):
                if branch != session_assigned_branch:
                    _b().audit_log(
                        "push_denied_wrong_branch",
                        "git_push",
                        success=False,
                        details={
                            "repo": repo,
                            "branch": branch,
                            "assigned_branch": session_assigned_branch,
                            "pipeline_id": session_pipeline_id,
                        },
                    )
                    return make_error(
                        f"Pipeline sessions must push to their assigned branch "
                        f"'{session_assigned_branch}'. Got '{branch}'. "
                        f"mcp__brc__propose handles branch targeting for you.",
                        status_code=403,
                        details={
                            "assigned_branch": session_assigned_branch,
                            "attempted_branch": branch,
                            "pipeline_id": session_pipeline_id,
                        },
                    )

    # Check branch ownership policy (pass auth mode for relaxed policy in user mode)
    policy = _b().get_policy_engine()
    policy_result = policy.check_branch_ownership(repo, branch, auth_mode=auth_mode)

    if not policy_result.allowed:
        _b().audit_log(
            "push_denied",
            "git_push",
            success=False,
            details={
                "repo": repo,
                "branch": branch,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Push denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    # SECURITY: Resolve the changed-file set + fail closed if we can't.
    # The agent-role and phase-based restriction checks below both consume
    # ``changed_files``; computing it once here keeps the security gates
    # consistent and lets the fail-closed branch run even if neither
    # session has a role (the phase check still runs in that case).
    #
    # Infrastructure pushes (pipeline-state and synthetic-session slice
    # integration-branch creation pushes; see is_infrastructure_push above)
    # are exempt for two distinct reasons:
    #   1. ``egg/pipeline-state`` is an orphan/disjoint-history branch written
    #      by orchestrator infrastructure, not agent BRC pushes, so role-based
    #      file restrictions don't conceptually apply.
    #   2. Synthetic-session slice integration-branch creation pushes (#2368)
    #      diff against `main` because the target ref doesn't exist yet, which
    #      would otherwise pull in every file modified on the parent branch's
    #      history (drafts, contracts, brc-history, ...) and falsely block a
    #      logical no-op branch-creation push (#2372).
    # The downstream anchor/phase/agent-restriction checks already gate on
    # `not is_infrastructure_push`; this gate makes the role check symmetric.
    session_role = None
    # Pipeline base branch (#3024): used as the preferred diff base for the
    # new-branch fallback so a branch forked from a non-trunk base is not
    # blamed for files it inherited unchanged from that base.
    session_base_branch = None
    changed_files = None  # populated below; reused by attribution + phase checks
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)
        session_base_branch = getattr(g.session, "base_branch", None)

    if session_role and not is_infrastructure_push:
        # Get the list of files being pushed for downstream attribution-aware
        # role enforcement (the canonical agent-role check below) and the
        # phase-restriction check further down.
        changed_files, check_error = _b().get_changed_files_in_push(
            exec_path, remote, branch, base_branch=session_base_branch
        )

        # SECURITY: Fail closed - if we can't determine changed files, block the push.
        # This prevents bypass via git diff manipulation (timeout, corrupt refs, etc.)
        if check_error:
            _b().audit_log(
                "push_denied_file_check_failed",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "error": check_error,
                },
            )
            return make_error(
                f"Push denied: Could not verify file changes for security check: {check_error}",
                status_code=500,
                details={
                    "role": session_role,
                    "error": check_error,
                    "hint": "This is a security precaution. Try again or contact support.",
                },
            )

        # Note: the legacy whole-push-diff role check that used to live here
        # (``check_file_restrictions(session_role, changed_files)``) was
        # removed in #2489.  It treated every file in the diff range as the
        # pushing role's responsibility, even files modified only by pulled
        # commits authored by other roles, which trapped role-restricted
        # producers whose branches inherited unrelated upstream commits
        # (the role had no sanctioned recovery path).  The attribution-
        # aware block below partitions own-authored vs pulled files via the
        # commit-authorship registry and is now the canonical agent-role
        # restriction enforcer; it preserves fail-closed semantics when
        # attribution is unavailable.

    # Agent-role file restrictions (#2039 restricted-path rejection).
    # The gateway partitions the push range into own-authored vs
    # pulled-from-other-role files via the commit-authorship registry,
    # checks the pushing role's write permissions against only the
    # own-authored set, and either pushes unchanged (all allowed)
    # or rejects with 403 restricted_path_modified (any blocked).
    #
    # EGG_AGENT_RESTRICTIONS_ENFORCE=false short-circuits the filter
    # (warn-only, same as the old 403 path).
    auto_filter_response: dict[str, Any] | None = None
    attributed_push: Any = None
    if session_role and changed_files and not is_infrastructure_push:
        enforce = os.environ.get("EGG_AGENT_RESTRICTIONS_ENFORCE", "true").lower() not in (
            "false",
            "0",
            "no",
        )
        _ar_mod = sys.modules.get("agent_restrictions") or sys.modules.get(
            "gateway.agent_restrictions"
        )
        _partition_fn: Any = getattr(_ar_mod, "partition_files_by_role", None) if _ar_mod else None
        if _partition_fn is None:
            try:
                from agent_restrictions import (  # type: ignore[import-untyped]
                    partition_files_by_role as _imported_partition,
                )

                _partition_fn = _imported_partition
            except ImportError:  # pragma: no cover
                from ..agent_restrictions import (
                    partition_files_by_role as _imported_partition,
                )

                _partition_fn = _imported_partition

        _gc_mod = sys.modules.get("git_client") or sys.modules.get("gateway.git_client")
        _get_attributed_fn: Any = (
            getattr(_gc_mod, "get_attributed_changed_files_in_push", None) if _gc_mod else None
        )
        if _get_attributed_fn is None:
            try:
                from git_client import (
                    get_attributed_changed_files_in_push as _imported_attr,
                )

                _get_attributed_fn = _imported_attr
            except ImportError:  # pragma: no cover
                from ..git_client import (
                    get_attributed_changed_files_in_push as _imported_attr,
                )

                _get_attributed_fn = _imported_attr

        # Resolve attribution for every commit in the push range.
        try:
            attributed_push = _get_attributed_fn(
                exec_path,
                remote,
                branch,
                session_role=session_role,
                base_branch=session_base_branch,
            )
        except Exception as exc:
            logger.warning("attribution_lookup_exception", error=str(exc), exc_info=True)
            # Fail-closed: an unexpected exception is treated as
            # attribution-unavailable so the rewrite path never
            # pushes unvetted files.
            _apr_cls = getattr(_gc_mod, "AttributedPushRange", None) if _gc_mod else None
            if _apr_cls is not None:
                attributed_push = _apr_cls(error=f"Attribution lookup failed: {exc}")
            else:
                from types import SimpleNamespace

                attributed_push = SimpleNamespace(
                    error=f"Attribution lookup failed: {exc}",
                    commits=[],
                    files=[],
                    attribution={},
                )

        # When the per-commit attribution can't be computed (e.g. the
        # caller mocked only the legacy file-detection path, or git
        # rev-list returned zero commits but there are staged-but-not-
        # pushed changes we can't walk with commit-tree), we FAIL
        # CLOSED.  Treat every file in ``changed_files`` as own-authored
        # and unregistered; if any file is blocked the push is rejected
        # by the restricted-path arm below (#2039).
        attribution_fallback = bool(attributed_push.error or not attributed_push.commits)
        if attribution_fallback:
            own_files: list[str] = list(dict.fromkeys(changed_files))
            pulled_files: list[str] = []
            unregistered_files: list[str] = list(own_files)
            attributed_commits_list: list[str] = []
        else:
            # Split files by author role (pushing role's own vs pulled).
            own_files = []
            pulled_files = []
            unregistered_files = []
            for attr in attributed_push.files:
                if attr.authored_by is None:
                    # Fail-closed: unregistered commits are treated as
                    # own-authored.
                    own_files.append(attr.path)
                    unregistered_files.append(attr.path)
                elif attr.authored_by == session_role:
                    own_files.append(attr.path)
                else:
                    pulled_files.append(attr.path)
            own_files = list(dict.fromkeys(own_files))
            pulled_files = list(dict.fromkeys(pulled_files))
            attributed_commits_list = list(attributed_push.commits)

        # Build the pulled_commits list for the response + audit log.
        pulled_commits_summary: list[dict[str, Any]] = []
        for sha in attributed_commits_list:
            role_for_sha = attributed_push.attribution.get(sha) if attributed_push else None
            if role_for_sha and role_for_sha != session_role:
                pulled_commits_summary.append({"sha": sha, "author_role": role_for_sha})

        allowed_own, blocked_own = _partition_fn(session_role, own_files, repo=repo)

        if unregistered_files and enforce:
            _b().audit_log(
                "push_authorship_unregistered_fallback",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "unregistered_files": unregistered_files,
                    "blocked_paths": blocked_own,
                    "pulled_commits": pulled_commits_summary,
                },
            )

        if blocked_own and enforce:
            # #2039: reject any push whose diff modifies a path the
            # pushing role cannot write.  The previous behavior — silent
            # tree rewrite (mixed) or silent ``nothing_to_push=true``
            # (all-blocked) — produced destructive deletions on the
            # shared branch and gave the agent no actionable signal.
            # Reject loudly with a structured 403 that points at the
            # supported recovery pattern (#1998 conditional ACK with
            # ``--pre-merge-condition``).
            sorted_blocked = sorted(set(blocked_own))
            _b().audit_log(
                "push_denied_restricted_path_modified",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "blocked_paths": sorted_blocked,
                    "pulled_commits": pulled_commits_summary,
                    "attribution_fallback": attribution_fallback,
                },
            )
            recommended_action = (
                "Drop the edits to the listed paths and re-propose with "
                "--pre-merge-condition flagging a manual change for the "
                "human reviewer (see issue #1998 for the conditional-ACK "
                "pattern)."
            )
            details: dict[str, Any] = {
                "error": "restricted_path_modified",
                "role": session_role,
                "blocked_paths": sorted_blocked,
                "recommended_action": recommended_action,
                "doc_ref": "#1998",
                "pulled_commits": pulled_commits_summary,
                "attribution_fallback": attribution_fallback,
            }
            # #2355 hint catalogue: surface category-specific guidance
            # (e.g. "Use egg-contract CLI commands…" for contract paths,
            # "Documentation changes belong to the documenter role." for
            # docs/) alongside the generic conditional-ACK pointer.  The
            # legacy whole-push-diff check used to do this; restoring it
            # here keeps the response shape consistent with the anchor-
            # write 403 below.
            hint = _derive_push_denied_hint(sorted_blocked)
            if hint is not None:
                details["hint"] = hint
            return make_error(
                (
                    f"Push denied: role '{session_role}' cannot modify restricted "
                    f"paths: {', '.join(sorted_blocked)}. "
                    f"{recommended_action}"
                ),
                status_code=403,
                details=details,
            )
        elif blocked_own and not enforce:
            # Warn-only mode: log but let the plain push proceed.
            # Explicitly flag ``enforce=false`` so operators scanning
            # audit logs during a kill-switch window can distinguish
            # this from the enforced paths.
            logger.warning(
                "Agent-role file restriction would block push (warn-only)",
                event_type="agent_role_restriction_warning",
                repo=repo,
                branch=branch,
                role=session_role,
                blocked_files=blocked_own,
                enforce=False,
            )
            # Observability parity (#1882 TASK-3-3): even the warn-
            # only passthrough must surface pulled_commits and the
            # filtered=false flag in the success response so
            # downstream tooling sees a consistent schema.
            auto_filter_response = {
                "filtered": False,
                "excluded_files": [],
                "pushed_files": own_files + pulled_files,
                "pulled_commits": pulled_commits_summary,
            }
        else:
            # All own-files are allowed.  No rewrite needed.  We still
            # stash the pulled_commits summary so the success path can
            # surface it in the response for observability.
            auto_filter_response = {
                "filtered": False,
                "excluded_files": [],
                "pushed_files": own_files + pulled_files,
                "pulled_commits": pulled_commits_summary,
            }

    # SECURITY: Check anchor file write scoping.
    # Agents can only write to their own anchor file (.egg-state/agent-anchors/<id>.json).
    # The agent_anchor_id is set via the AGENT_ANCHOR_ID env var in the container.
    if changed_files and not is_infrastructure_push:
        session_anchor_id = None
        if hasattr(g, "session") and g.session:
            session_anchor_id = getattr(g.session, "agent_anchor_id", None)
        for changed_file in changed_files:
            anchor_result = check_anchor_write_permission(changed_file, session_anchor_id)
            if not anchor_result.allowed:
                _b().audit_log(
                    "push_denied_anchor_write",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "agent_anchor_id": session_anchor_id,
                        "blocked_files": anchor_result.blocked_files,
                        "blocked_reason": anchor_result.blocked_reason,
                    },
                )
                anchor_details: dict[str, Any] = {
                    "agent_anchor_id": session_anchor_id,
                    "blocked_files": anchor_result.blocked_files,
                    "blocked_reason": anchor_result.blocked_reason,
                }
                # Anchor-write violations bypass the role-level partition (the
                # coder blocklist exempts .egg-state/agent-anchors/), so they
                # need their own derive_hint call to deliver the
                # orchestrator-API guidance from BLOCKED_HINTS. See #2355.
                anchor_hint = _derive_push_denied_hint(anchor_result.blocked_files)
                if anchor_hint is not None:
                    anchor_details["hint"] = anchor_hint
                return make_error(
                    f"Push denied: {anchor_result.message}",
                    status_code=403,
                    details=anchor_details,
                )

    # SECURITY: Check phase-based file restrictions for local mode sessions.
    # This replaces the blanket local-mode push block with granular phase-based
    # restrictions. Each phase has specific allowed/blocked file patterns:
    # - refine/plan: Can only push .egg-state/ files (contracts, drafts, checkpoints)
    # - implement: Can push code but not .egg-state/ (except checkpoints)
    # - pr: Can push everything
    if session_phase and not is_infrastructure_push:
        # Get the list of files being pushed (reuse if already fetched for role check)
        if changed_files is None:
            changed_files, check_error = _b().get_changed_files_in_push(
                exec_path, remote, branch, base_branch=session_base_branch
            )
            if check_error:
                _b().audit_log(
                    "push_denied_file_check_failed",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "phase": session_phase,
                        "error": check_error,
                    },
                )
                return make_error(
                    f"Push denied: Could not verify file changes for phase check: {check_error}",
                    status_code=500,
                    details={
                        "phase": session_phase,
                        "error": check_error,
                        "hint": "This is a security precaution. Try again or contact support.",
                    },
                )

        # Check phase-based file restrictions
        phase_result = _b().check_phase_file_restrictions(session_phase, changed_files)
        if not phase_result.allowed:
            _b().audit_log(
                "push_denied_phase_restrictions",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "phase": session_phase,
                    "blocked_files": phase_result.blocked_files,
                    "blocked_reason": phase_result.blocked_reason,
                },
            )
            has_non_state_files = any(
                not f.startswith(".egg-state/") for f in phase_result.blocked_files
            )
            # Pipeline sessions get a pipeline-specific hint pointing to
            # egg-orch; non-pipeline sessions see the original generic hint.
            session_pipeline_id = None
            if hasattr(g, "session") and g.session:
                session_pipeline_id = getattr(g.session, "pipeline_id", None)

            if has_non_state_files and isinstance(session_pipeline_id, str):
                hint = (
                    "Push contains files from prior pipeline phases that this phase "
                    "cannot modify. This indicates the worktree was not properly synced. "
                    "Signal an error with `egg-orch signal error --error 'Push denied: "
                    "phase file restrictions'` and include this message. "
                    f"Blocked files: {phase_result.blocked_files}"
                )
            elif has_non_state_files:
                hint = (
                    "Branch contains files outside .egg-state/ from a previous phase. "
                    "Create a clean branch from origin/main with only your state files."
                )
            else:
                hint = f"Phase '{session_phase}' has file restrictions. Check allowed patterns."
            return make_error(
                f"Push denied: {phase_result.message}",
                status_code=403,
                details={
                    "phase": session_phase,
                    "blocked_files": phase_result.blocked_files,
                    "blocked_reason": phase_result.blocked_reason,
                    "hint": hint,
                },
            )

    # Get authentication token using shared helper
    token_str, auth_mode, token_error = _b().get_token_for_repo(repo)
    if not token_str:
        return make_error(token_error, status_code=503)

    # Build push command with safe.directory for worktree paths
    # Convert SSH URLs to HTTPS since gateway uses token auth
    push_target = _b().get_authenticated_remote_target(remote, remote_url)
    if push_target != remote:
        logger.debug(
            "Converting SSH URL to HTTPS for push",
            original_url=remote_url,
            https_url=push_target,
        )
    # SECURITY: Belt-and-suspenders hook prevention. The primary protection is
    # core.hooksPath=/dev/null in git_cmd() which disables ALL hooks globally.
    # --no-verify is added as defense-in-depth for the pre-push hook. See issue #58.
    push_args = ["push", "--no-verify"]
    if force_with_lease:
        # ``--force-with-lease`` rejects the push if the remote has moved
        # since we last fetched it — preferred over ``--force`` for
        # non-fast-forward pushes (e.g. the stacked-PR reconciler's
        # rebase-then-push heal path, #2137).
        push_args.append("--force-with-lease")
    elif force:
        push_args.append("--force")
    # NOTE: The push uses the original refspec (not a SHA-based refspec)
    # because it never calls ``update-ref`` pre-push, so the directory-
    # style ref collision (sibling worktree refs like
    # ``refs/heads/<branch>/work``) does not apply here.  See #1994.
    push_args.extend([push_target, refspec] if refspec else [push_target])
    # Clear any http.extraheader from .git/config to ensure the gateway's
    # credential helper (GIT_ASKPASS) is used. actions/checkout@v4 persists
    # GITHUB_TOKEN as an extraheader by default, which takes precedence over
    # GIT_ASKPASS and may lack permissions (e.g., workflows scope).
    cmd = git_cmd("-c", "http.extraheader=", *push_args)

    # NOTE: Git author/committer info is set at COMMIT time, not push time.
    # For user mode, the user must configure their local git:
    #   git config user.name "Your Name"
    #   git config user.email "your@email.com"
    if auth_mode == "user":
        logger.debug("User mode push", repo=repo)

    # Create credential helper and execute push
    credential_helper_path = None
    try:
        credential_helper_path, env = create_credential_helper(token_str, os.environ.copy())

        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

        if result.returncode == 0:
            _b().audit_log(
                "push_success",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "force": force,
                    "auth_mode": auth_mode,
                },
            )

            # Update session bookkeeping after a successful push so other
            # request handlers can resolve the session's current worktree.
            session = getattr(g, "session", None)
            if session is not None:
                session.last_repo_path = exec_path
                session.last_branch = branch

            success_payload: dict[str, Any] = {
                "repo": repo,
                "branch": branch,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "auth_mode": auth_mode,
            }
            # Surface pulled_commits / filtered=False on plain pushes so
            # agents get consistent response shape across paths (#1882).
            if auto_filter_response is not None:
                success_payload.setdefault("filtered", auto_filter_response.get("filtered", False))
                success_payload.setdefault("nothing_to_push", False)
                success_payload.setdefault(
                    "excluded_files", auto_filter_response.get("excluded_files", [])
                )
                success_payload.setdefault(
                    "pushed_files", auto_filter_response.get("pushed_files", [])
                )
                success_payload.setdefault(
                    "pulled_commits", auto_filter_response.get("pulled_commits", [])
                )
            return make_success(
                "Push successful",
                success_payload,
            )
        else:
            _b().audit_log(
                "push_failed",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "returncode": result.returncode,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"Push failed: {result.stderr}",
                status_code=500,
                details={"stdout": result.stdout, "stderr": result.stderr},
            )

    except subprocess.TimeoutExpired:
        return make_error("Push timed out", status_code=504)
    except Exception as e:
        return make_error(f"Push failed: {e}", status_code=500)
    finally:
        cleanup_credential_helper(credential_helper_path)


LS_REMOTE_VALUE_FLAGS: frozenset[str] = frozenset({"--sort"})


def git_fetch() -> tuple[Response, int] | Response:
    """
    Handle git fetch requests.

    Required because the container doesn't have direct access to GitHub tokens
    (they are held by the gateway sidecar). This endpoint provides authenticated
    fetch for git fetch, git ls-remote, and similar read operations.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "remote": "origin",
            "args": ["--tags"]  # optional additional args
        }

    For ls-remote:
        {
            "repo_path": "/path/to/repo",
            "operation": "ls-remote",
            "remote": "origin",
            "args": ["HEAD"]  # optional refs to query
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    operation = data.get("operation", "fetch")  # fetch or ls-remote
    extra_args = data.get("args", [])
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")

    # Validate repo_path to prevent path traversal attacks
    path_valid, path_error = _b().validate_repo_path(repo_path)
    if not path_valid:
        _b().audit_log(
            "fetch_blocked",
            "git_fetch",
            success=False,
            details={"repo_path": repo_path, "reason": path_error},
        )
        return make_error(path_error, status_code=403)

    if operation not in ("fetch", "ls-remote"):
        return make_error(f"Unsupported operation: {operation}")

    # Validate extra args against operation-specific allowlist
    args_valid, args_error, validated_args = validate_git_args(operation, extra_args)
    if not args_valid:
        _b().audit_log(
            "fetch_blocked",
            "git_fetch",
            success=False,
            details={"reason": args_error, "operation": operation},
        )
        return make_error(args_error, status_code=400)

    # Map container path to worktree path if container_id is provided
    exec_path = _b().map_container_path_to_worktree(repo_path, container_id, operation)
    if exec_path is None:
        return make_worktree_not_found_error(container_id)

    # Get remote URL to determine repo
    remote_url, url_error = _b().resolve_remote_url(remote, exec_path)
    if url_error:
        return make_error(url_error)

    # Extract repo from URL
    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = _b().check_private_repo_access(
            operation=operation,
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=False,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            _b().audit_log(
                f"{operation}_denied_private_mode",
                f"git_{operation}",
                success=False,
                details={
                    "repo": repo,
                    "reason": priv_result.reason,
                    "visibility": priv_result.visibility,
                },
            )
            return make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    # Get authentication token using shared helper
    token_str, auth_mode, token_error = _b().get_token_for_repo(repo)
    if not token_str:
        return make_error(token_error, status_code=503)

    # Convert SSH URLs to HTTPS since gateway uses token auth
    fetch_target = _b().get_authenticated_remote_target(remote, remote_url)
    if fetch_target != remote:
        logger.debug(
            f"Converting SSH URL to HTTPS for {operation}",
            original_url=remote_url,
            https_url=fetch_target,
        )

    # Build command using validated args
    if operation == "fetch":
        # Don't include remote when --all is specified (fetches from all remotes)
        if "--all" in validated_args:
            cmd_args = ["fetch"] + validated_args
        else:
            cmd_args = ["fetch", fetch_target] + validated_args
    else:  # ls-remote
        # ``git ls-remote`` stops option parsing at the first positional
        # argument: anything after <repository> is a <ref> pattern, not a
        # flag. ``ls-remote <url> --heads`` therefore filters by the
        # literal pattern "--heads", matching nothing and exiting 0 with
        # empty output (#3479: the stacked-PR reconciler read that empty
        # listing as "every branch deleted" and hot-looped rebases of
        # healthy PRs). Emit flags before the repository and ref patterns
        # after it.
        #
        # A naive startswith("-") partition would strand a separate-
        # argument flag *value* after the URL as a bogus ref pattern
        # (#3484 review note 1): ``--sort committerdate`` is an allowlisted
        # ls-remote flag whose value does not start with "-", so it would
        # become ``ls-remote --sort <url> committerdate`` — ``committerdate``
        # silently matching nothing. Keep such a value adjacent to its flag
        # on the pre-URL side. No caller passes the separate-value form
        # today; this hardens the route against a future footgun.
        flags: list[str] = []
        patterns: list[str] = []
        arg_idx = 0
        while arg_idx < len(validated_args):
            token = validated_args[arg_idx]
            if not token.startswith("-"):
                patterns.append(token)
                arg_idx += 1
                continue
            flags.append(token)
            # Inline ``--sort=key`` is self-contained; only the separate
            # ``--sort key`` form needs its value pulled along with it.
            takes_separate_value = (
                token.split("=", 1)[0] in LS_REMOTE_VALUE_FLAGS and "=" not in token
            )
            if (
                takes_separate_value
                and arg_idx + 1 < len(validated_args)
                and not validated_args[arg_idx + 1].startswith("-")
            ):
                flags.append(validated_args[arg_idx + 1])
                arg_idx += 2
                continue
            arg_idx += 1
        cmd_args = ["ls-remote", *flags, fetch_target, *patterns]

    cmd = git_cmd(*cmd_args)

    # Create credential helper and execute operation
    credential_helper_path = None
    try:
        credential_helper_path, env = create_credential_helper(token_str, os.environ.copy())

        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

        if result.returncode == 0:
            _b().audit_log(
                f"{operation}_success",
                f"git_{operation}",
                success=True,
                details={
                    "repo": repo,
                    "auth_mode": auth_mode,
                },
            )
            return make_success(
                f"{operation.capitalize()} successful",
                {
                    "repo": repo,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "auth_mode": auth_mode,
                },
            )
        else:
            _b().audit_log(
                f"{operation}_failed",
                f"git_{operation}",
                success=False,
                details={
                    "repo": repo,
                    "returncode": result.returncode,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"{operation.capitalize()} failed: {result.stderr}",
                status_code=500,
                details={"stdout": result.stdout, "stderr": result.stderr},
            )

    except subprocess.TimeoutExpired:
        _b()._cleanup_stale_pack_files(exec_path)
        return make_error(f"{operation.capitalize()} timed out", status_code=504)
    except Exception as e:
        return make_error(f"{operation.capitalize()} failed: {e}", status_code=500)
    finally:
        cleanup_credential_helper(credential_helper_path)
