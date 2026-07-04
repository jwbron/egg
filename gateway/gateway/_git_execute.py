"""Gateway git_execute cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from flask import Response, g, request

try:
    from ..git_client import (
        GIT_ALLOWED_COMMANDS,
        extract_reset_target_ref,
        git_cmd,
        is_branch_switch,
        is_branch_switching_operation,
        is_repos_parent_directory,
        validate_git_args,
    )
except ImportError:  # flat/container import mode
    from git_client import (  # type: ignore[no-redef, import-untyped]
        GIT_ALLOWED_COMMANDS,
        extract_reset_target_ref,
        git_cmd,
        is_branch_switch,
        is_branch_switching_operation,
        is_repos_parent_directory,
        validate_git_args,
    )

from ._git_ops import _detached_head_hint
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


def git_execute() -> tuple[Response, int] | Response:
    """
    Execute a git command in the gateway's worktree.

    This is the primary endpoint for all git operations in the gateway-managed
    worktree architecture. The container has no direct git access (its .git is
    shadowed by tmpfs), so all git commands route through this endpoint.

    Request body:
        {
            "repo_path": "/home/egg/repos/myrepo",
            "operation": "status",
            "args": ["--porcelain"],
            "container_id": "egg-xxx"  # For path mapping
        }

    Supported operations: status, add, commit, log, diff, show, branch,
    checkout, switch, reset, restore, stash, merge, rebase, cherry-pick,
    tag, clean, config, rev-parse, remote, apply, format-patch

    Network operations (push, fetch, ls-remote) should use dedicated endpoints.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    operation = data.get("operation")
    args = data.get("args", [])
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")
    if not operation:
        return make_error("Missing operation")

    # Validate repo_path
    path_valid, path_error = _b().validate_repo_path(repo_path)
    if not path_valid:
        _b().audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": path_error,
            },
        )
        return make_error(path_error, status_code=403)

    # Check if this is a "repos parent" directory (contains repos but isn't one)
    # Git operations in these directories are expected to fail - this is commonly
    # caused by tools like Claude Code running `git rev-parse` to detect if they're
    # in a repo. Return a clear error without logging a warning (since this is
    # expected behavior, not an error condition).
    if is_repos_parent_directory(repo_path):
        logger.debug(
            "Git operation in repos parent directory",
            operation=operation,
            repo_path=repo_path,
            container_id=container_id,
        )
        return make_error(
            f"Path '{repo_path}' is a directory containing repositories, not a git repository. "
            "Run git commands from within a specific repository directory.",
            status_code=400,
            details={
                "hint": "This directory contains repositories but is not itself a git repository.",
                "repo_path": repo_path,
            },
        )

    # Validate operation is in allowlist
    if operation not in GIT_ALLOWED_COMMANDS:
        _b().audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": "Operation not allowed",
            },
        )
        return make_error(
            f"Operation '{operation}' not allowed. "
            f"Allowed: {', '.join(sorted(GIT_ALLOWED_COMMANDS.keys()))}",
            status_code=403,
        )

    # Network operations should use dedicated endpoints
    if operation in ("push", "fetch", "ls-remote"):
        return make_error(
            f"Use dedicated endpoint for {operation}: /api/v1/git/{operation}",
            status_code=400,
        )

    # Validate args against allowlist
    args_valid, args_error, validated_args = validate_git_args(operation, args)
    if not args_valid:
        _b().audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": args_error,
            },
        )
        return make_error(args_error, status_code=400)

    # SECURITY: Scope `git update-ref` to the agent's own assigned branch.
    # update-ref is the supported recovery primitive when an agent ends up on
    # detached HEAD with a useful commit (see issue #2162). To keep the blast
    # radius tight, the gateway rejects any update-ref that is not of the form
    # `update-ref <ref> <newvalue> [<oldvalue>]` and force-prepends
    # `--no-deref` below so symref-following semantics never apply.
    if operation == "update-ref":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        positional = [a for a in validated_args if not a.startswith("-")]
        denial_reason: str | None = None
        if not isinstance(assigned, str) or not assigned:
            denial_reason = (
                "git update-ref is only allowed in pipeline sessions with an assigned branch."
            )
        elif len(positional) < 2 or len(positional) > 3:
            denial_reason = (
                "git update-ref must be of the form `git update-ref <ref> <newvalue> [<oldvalue>]`."
            )
        else:
            expected_ref = f"refs/heads/{assigned}"
            if positional[0] != expected_ref:
                denial_reason = (
                    f"git update-ref target '{positional[0]}' is not allowed. "
                    f"Only '{expected_ref}' (your assigned branch) may be updated. "
                    f"If you are trying to manually retarget your branch to drop "
                    f"pulled upstream commits and recover from a "
                    f"'restricted_path_modified' push 403, that is no longer "
                    f"necessary (#2489) — pulled commits authored by other roles "
                    f"are exempt from your role allowlist; retry the push as-is."
                )
        if denial_reason is not None:
            _b().audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": denial_reason,
                },
            )
            return make_error(denial_reason, status_code=403)

    # SECURITY: Scope `git symbolic-ref HEAD <ref>` to the agent's own
    # assigned or local per-role branch.  symbolic-ref is the canonical
    # reattach primitive when a worktree ends up on detached HEAD (e.g.
    # post-rebase, see issue #2200).  Restricted to the two-positional
    # form `symbolic-ref HEAD <ref>` — read forms (one-arg) and the
    # delete form (`-d`) are rejected because they do not participate
    # in the recovery flow.
    if operation == "symbolic-ref":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        positional = [a for a in validated_args if not a.startswith("-")]
        denial_reason = None
        if not isinstance(assigned, str) or not assigned:
            denial_reason = (
                "git symbolic-ref is only allowed in pipeline sessions with an assigned branch."
            )
        elif len(positional) != 2:
            denial_reason = "git symbolic-ref must be of the form `git symbolic-ref HEAD <ref>`."
        elif positional[0] != "HEAD":
            denial_reason = (
                f"git symbolic-ref source '{positional[0]}' is not allowed. "
                f"Only HEAD may be retargeted."
            )
        else:
            allowed_refs = {f"refs/heads/{assigned}"}
            # Defense in depth: scope the per-role local work branch from
            # ``session.container_id`` (canonical, set by the orchestrator at
            # session registration), not ``data.get("container_id")`` which
            # is agent-supplied.  Mirrors the ``update-ref`` guard above which
            # also ignores the request-body container_id.
            session_container_id = getattr(session, "container_id", None)
            if isinstance(session_container_id, str) and session_container_id:
                # Per-role local work branch (`egg/{container_id}/work`)
                # — see worktree_manager._create_or_reuse_worktree.
                allowed_refs.add(f"refs/heads/egg/{session_container_id}/work")
            if positional[1] not in allowed_refs:
                denial_reason = (
                    f"git symbolic-ref target '{positional[1]}' is not allowed. "
                    f"Allowed targets: {sorted(allowed_refs)}."
                )
        if denial_reason is not None:
            _b().audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": denial_reason,
                },
            )
            return make_error(denial_reason, status_code=403)

    # SECURITY: Block agent-initiated ``git rebase`` against the base
    # branch from pipeline sessions (#2224, follow-up to #2222).  The
    # pipeline branch is rebased onto the base branch only via the
    # orchestrator's controlled rebase in
    # ``orchestrator/routes/pipelines.py::_rebase_pipeline_branch_onto_base``
    # — which itself uses the *bare* form ``git rebase origin/<base>``
    # but is safe because steps 1–5 of the helper enforce ancestry
    # preconditions and reset HEAD to the pipeline-branch tip *before*
    # the rebase replays.  Crucially, that helper runs as a subprocess
    # on the orchestrator-side worktree and does *not* route through
    # this endpoint, so this guard does not interfere with it.  An
    # agent reaching for ``git rebase origin/main`` (intentionally or
    # via a "resolve conflicts" intuition) reproduces the contamination
    # shape from #2222 even with the orchestrator-side fixes in place.
    #
    # The ``--onto X UP <branch>`` form is allowed when ``X`` (the
    # *new* base) is *not* a protected ref — that shape is used by the
    # stacked-PR healer in
    # ``orchestrator/gateway_client.py::rebase_onto``, which always
    # passes a slice/issue branch as ``new_base`` (never ``origin/main``;
    # see ``stacked_pr_reconciler._resolve_extant_new_base``).  Calls
    # with ``--onto origin/main …`` are *blocked*: when ``X == UP ==
    # origin/main`` the operation reduces to bare ``git rebase
    # origin/main`` and reproduces the contamination shape (the value
    # of ``UP`` is irrelevant — the new HEAD is whatever ``X``
    # resolves to, with the upstream-to-HEAD commits replayed on top).
    if operation == "rebase":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            # ``protected_refs`` lists every form an agent (or an
            # innocent rename) could use to name the base branch.  We
            # normalise inputs by stripping ``refs/remotes/`` and
            # ``refs/heads/`` prefixes before comparing so canonical
            # full ref names hit the same guard.  Pipelines whose base
            # is not ``main`` are not currently in production
            # (orchestrator's ``base_branch`` defaults to ``main``); if
            # non-main bases ship, derive this set from the session's
            # recorded base branch instead of hardcoding it.
            protected_refs = {
                "origin/main",
                "main",
                "origin/HEAD",
                "FETCH_HEAD",
            }

            def _normalise_ref(value: str) -> str:
                # Strip ``refs/remotes/`` (canonical full remote-tracking
                # ref) and ``refs/heads/`` (canonical local-branch ref)
                # so e.g. ``refs/remotes/origin/main`` matches
                # ``origin/main`` in ``protected_refs``.  Other shapes
                # (SHAs, ``origin/main~1``, ``origin/main^``) are caught
                # by exact-match below or fall through — they are
                # acknowledged in the docstring as residual gaps.
                if value.startswith("refs/remotes/"):
                    return value[len("refs/remotes/") :]
                if value.startswith("refs/heads/"):
                    return value[len("refs/heads/") :]
                return value

            offender: str | None = None

            # Branch 1: ``--onto <new_base>`` is present.  Reject when
            # the *new base* (the value of ``--onto``) is a protected
            # ref, regardless of what the upstream positional is.  This
            # closes the ``--onto origin/main origin/main`` bypass:
            # ``git rebase --onto X UP`` rebases HEAD onto X using UP as
            # the upstream, so when X is the base branch the operation
            # produces the same contamination shape as bare ``git
            # rebase origin/main``.
            #
            # Collect *every* ``--onto`` occurrence rather than the
            # first — git's ``OPT_STRING`` semantics make duplicate
            # ``--onto`` flags overwrite, so the *last* value wins, and
            # an adversarial ``--onto safe --onto origin/main`` would
            # otherwise slip past a first-match check.  Reject when any
            # of the supplied values is a protected ref.  Empty values
            # (``--onto=`` with nothing after) are treated as "not
            # provided" so the bare-form upstream check below still
            # runs against the positional args.
            onto_values: list[str] = []
            j = 0
            while j < len(validated_args):
                arg = validated_args[j]
                if arg.startswith("--onto="):
                    value = arg.split("=", 1)[1]
                    if value:
                        onto_values.append(value)
                elif arg == "--onto" and j + 1 < len(validated_args):
                    value = validated_args[j + 1]
                    if value:
                        onto_values.append(value)
                    j += 1
                j += 1

            if onto_values:
                offender = next(
                    (v for v in onto_values if _normalise_ref(v) in protected_refs),
                    None,
                )
            else:
                # Branch 2: bare ``git rebase <upstream> [<branch>]``
                # form — first positional is the upstream.  Reject when
                # the upstream is a protected ref.
                positional = [a for a in validated_args if not a.startswith("-")]
                offender = next(
                    (p for p in positional if _normalise_ref(p) in protected_refs),
                    None,
                )

            if offender is not None:
                denial_reason = (
                    f"git rebase against '{offender}' is not allowed in "
                    f"pipeline sessions. The pipeline branch is rebased "
                    f"onto the base branch only via the orchestrator's "
                    f"controlled rebase (`_rebase_pipeline_branch_onto_base`), "
                    f"which runs as a subprocess that does not route through "
                    f"this endpoint; an agent-initiated `git rebase "
                    f"origin/main` (or `--onto origin/main …`) reproduces "
                    f"the contamination shape from #2222. If you need to "
                    f"bring in new commits from the base, ask the operator "
                    f"to resume the pipeline so the orchestrator-side "
                    f"rebase runs. If you were trying to drop pulled upstream "
                    f"commits to recover from a 'restricted_path_modified' "
                    f"push 403, that is no longer necessary (#2489) — pulled "
                    f"commits authored by other roles are exempt from your "
                    f"role allowlist; retry the push as-is."
                )
                _b().audit_log(
                    "git_execute_blocked",
                    operation,
                    success=False,
                    details={
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "container_id": container_id,
                        "assigned_branch": assigned,
                        "reason": denial_reason,
                    },
                )
                return make_error(denial_reason, status_code=403)

    # SECURITY: Block branch-switching for pipeline sessions.
    # Pipeline containers are locked to their worktree branch to prevent
    # cross-contamination between pipeline tasks.
    if is_branch_switch(operation, validated_args):
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            _b().audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": "Branch switching blocked in pipeline session",
                },
            )
            return make_error(
                f"Branch switching is not allowed in pipeline sessions. "
                f"You are locked to branch '{assigned}'. "
                f"Use 'git checkout [<commit-ish>] -- <file>' to restore files instead "
                f"(e.g. 'git checkout HEAD -- <file>' or 'git checkout <sha> -- <file>'). "
                f"If you are recovering from a 'restricted_path_modified' push 403, "
                f"note that pulled commits authored by other roles are exempt from "
                f"your role allowlist (#2489) — only your own commits' paths trigger "
                f"the rejection, so retry the push first; if it still rejects, drop "
                f"the disallowed paths from your own commits and re-propose with "
                f"--pre-merge-condition (#1998 conditional ACK).",
                status_code=403,
            )

    # Map container path to worktree path if container_id is provided
    exec_path = _b().map_container_path_to_worktree(repo_path, container_id, operation)
    if exec_path is None:
        return make_worktree_not_found_error(container_id)
    is_worktree = exec_path != repo_path

    # SECURITY: Block off-lineage `git reset` in pipeline sessions.
    # `git reset <ref>` (any mode) moves HEAD; if <ref> is not an ancestor of
    # HEAD on the assigned branch, the agent's commits are silently dropped
    # from the working tree — the same effect as a branch switch. The
    # checkout/switch lock at :1924 does not catch this (see issue #2089).
    if operation == "reset":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            target_ref = extract_reset_target_ref(validated_args)
            if target_ref is not None:
                ancestor_stderr: str | None = None
                try:
                    ancestor_check = subprocess.run(
                        git_cmd("merge-base", "--is-ancestor", target_ref, "HEAD"),
                        cwd=exec_path,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    is_ancestor = ancestor_check.returncode == 0
                    if not is_ancestor and ancestor_check.stderr:
                        ancestor_stderr = ancestor_check.stderr.strip() or None
                except (OSError, subprocess.TimeoutExpired) as exc:
                    # Fail closed — if we cannot verify safety, treat as off-lineage.
                    is_ancestor = False
                    ancestor_stderr = str(exc)
                if not is_ancestor:
                    audit_details = {
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "container_id": container_id,
                        "assigned_branch": assigned,
                        "target_ref": target_ref,
                        "reason": "Off-lineage reset blocked in pipeline session",
                    }
                    if ancestor_stderr:
                        audit_details["merge_base_stderr"] = ancestor_stderr
                    _b().audit_log(
                        "git_execute_blocked",
                        operation,
                        success=False,
                        details=audit_details,
                    )
                    return make_error(
                        f"Off-lineage 'git reset' is not allowed in pipeline sessions. "
                        f"Target ref '{target_ref}' is not an ancestor of HEAD on your "
                        f"assigned branch '{assigned}'. To incorporate new commits from the "
                        f"remote, use 'git rebase origin/{assigned}' instead. "
                        f"If you are trying to drop pulled upstream commits to recover "
                        f"from a 'restricted_path_modified' push 403, that is no longer "
                        f"necessary (#2489) — pulled commits authored by other roles are "
                        f"exempt from your role allowlist; retry the push as-is.",
                        status_code=403,
                    )

    # SECURITY: Enforce branch isolation in pipeline worktree sessions.
    # Pipeline agents in worktrees must stay on their assigned branch.
    # Interactive sessions are unrestricted even if they use worktrees.
    # We detect pipeline sessions by the presence of pipeline_id on the
    # session, rather than checking session_mode.
    # See issue #773.
    session = getattr(g, "session", None)
    is_pipeline = session is not None and getattr(session, "pipeline_id", None) is not None
    if is_pipeline and is_worktree and is_branch_switching_operation(operation, validated_args):
        assert session is not None  # guaranteed by is_pipeline check above
        _b().audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "pipeline_id": session.pipeline_id,
                "session_mode": getattr(g, "session_mode", None),
                "reason": "Branch switching blocked in pipeline worktree session",
            },
        )
        return make_error(
            "Branch switching is not allowed in pipeline worktree sessions. "
            "You are locked to your assigned branch. "
            "Use 'git restore' for file operations instead of 'git checkout'. "
            "If you are recovering from a 'restricted_path_modified' push 403, "
            "note that pulled commits authored by other roles are exempt from "
            "your role allowlist (#2489) — retry the push as-is; if it still "
            "rejects, drop the disallowed paths from your own commits and "
            "re-propose with --pre-merge-condition (#1998 conditional ACK).",
            status_code=403,
        )

    # SECURITY: Validate staged files at commit time for pipeline sessions.
    # This is an early-catch complement to push-time validation — prevents the
    # agent from building up invalid commits that would only be rejected at push.
    if operation == "commit":
        session = getattr(g, "session", None)
        session_phase = getattr(g, "session_phase", None) if session else None
        if session_phase:
            import subprocess as _sp

            try:
                staged_result = _sp.run(
                    git_cmd("diff", "--cached", "--name-only"),
                    cwd=exec_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if staged_result.returncode == 0:
                    staged_files = [
                        f.strip() for f in staged_result.stdout.strip().split("\n") if f.strip()
                    ]
                    if staged_files:
                        phase_result = _b().check_phase_file_restrictions(
                            session_phase, staged_files
                        )
                        if not phase_result.allowed:
                            _b().audit_log(
                                "git_execute_blocked",
                                operation,
                                success=False,
                                details={
                                    "repo_path": repo_path,
                                    "git_args": validated_args,
                                    "container_id": container_id,
                                    "phase": session_phase,
                                    "blocked_files": phase_result.blocked_files,
                                    "reason": "Staged files violate phase restrictions",
                                },
                            )
                            return make_error(
                                f"Commit blocked: {phase_result.message}. "
                                f"Unstage the blocked files with 'git reset HEAD <file>'.",
                                status_code=403,
                            )
            except Exception:
                # Fail open for commit-time check — push-time check is the
                # authoritative gate and will catch any violations.
                logger.warning(
                    "Staged-file check skipped due to error",
                    operation=operation,
                    container_id=container_id,
                )

    # SECURITY: Belt-and-suspenders hook prevention for operations that support it.
    # The primary protection is core.hooksPath=/dev/null in git_cmd() which disables
    # ALL hooks globally. However, we also add --no-verify for operations that
    # support it as defense-in-depth. See issue #58.
    #
    # Operations that support --no-verify:
    # - commit: pre-commit, prepare-commit-msg, commit-msg, post-commit
    # - merge: pre-merge-commit, prepare-commit-msg, commit-msg, post-merge
    # - am: pre-applypatch, applypatch-msg, post-applypatch
    #
    # Note: cherry-pick is NOT included here. While git 2.36+ added --no-verify
    # for cherry-pick, older versions (including 2.34) reject it with a usage error.
    # The primary protection (core.hooksPath=/dev/null) already covers cherry-pick.
    # See issue #118.
    if operation in ("commit", "merge", "am"):
        validated_args = ["--no-verify", *validated_args]

    # SECURITY: Force-prepend `--no-deref` for `update-ref` (#2162). Without it,
    # update-ref follows symref targets — the underlying ref is updated, not
    # `refs/heads/<assigned_branch>`. In practice agent branches are never
    # symrefs, but the gateway is a defense-in-depth boundary and the recovery
    # flow never wants symref-following semantics.
    if operation == "update-ref":
        validated_args = ["--no-deref", *validated_args]

    # Build command
    cmd = git_cmd(operation, *validated_args)

    # Set GIT_EDITOR=true so operations that need an editor (e.g., rebase
    # --continue after conflict resolution) succeed without a terminal.
    # `true` accepts the default commit message, which is the expected
    # behavior for an agent that always provides messages via -m.
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"

    # Commit-authorship observer (#1882): snapshot HEAD before the git
    # subcommand so we can compute which commits (if any) it created
    # and register them with the orchestrator's authorship registry.
    # Only agent sessions participate; internal gateway ops skip.
    _observer_role: str | None = None
    _observer_pipeline_id: str | None = None
    _observer_repo: str | None = None
    _observer_branch: str | None = None
    _observer_before_head: str | None = None
    _observer_armed: bool = False
    _session_for_observer = getattr(g, "session", None)
    if _session_for_observer is not None:
        _observer_role = getattr(_session_for_observer, "agent_role", None)
        _observer_pipeline_id = getattr(_session_for_observer, "pipeline_id", None)
        _observer_repo = getattr(_session_for_observer, "repo", None)
        _observer_branch = getattr(_session_for_observer, "assigned_branch", None) or getattr(
            _session_for_observer, "branch", None
        )
    # Intentionally exhaustive list of commit-creating operations.
    # ``stash`` and ``pull`` can also create commit objects, but agents
    # do not use them — all pushes go through the gateway's push handler
    # which resolves attribution independently.  Extend this list if
    # agent workflows ever include stash or pull.
    if _observer_role and operation in (
        "commit",
        "merge",
        "cherry-pick",
        "revert",
        "rebase",
        "am",
    ):
        _observer_armed = True
        _capture_head = _b()._lookup_commit_observer_fn("capture_head")
        if _capture_head is not None:
            try:
                _observer_before_head = _capture_head(exec_path)
            except Exception:  # pragma: no cover - defensive
                # before_head stays None; observe handles the
                # unborn-branch case via its [after_head] fallback.
                _observer_before_head = None

    try:
        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )

        if result.returncode == 0:
            # Fire the observer only on the narrow list of ref-mutating
            # operations that armed the observer above.  For all other
            # operations (status, checkout, restore, ...) we skip the
            # post-op rev-parse entirely so callers' subprocess
            # mocking isn't perturbed.  Note: _observer_before_head
            # may be None on unborn branches — observe() handles that
            # via its [after_head] fallback.
            if _observer_role and _observer_armed:
                try:
                    _observe_after = _b()._lookup_commit_observer_fn("observe_after_git_execute")
                    if _observe_after is not None:
                        _observe_after(
                            exec_path,
                            before_head=_observer_before_head,
                            branch=_observer_branch,
                            session_role=_observer_role,
                            pipeline_id=_observer_pipeline_id,
                            repo=_observer_repo,
                        )
                except Exception:
                    # Observer is best-effort — never block the git
                    # response on a registry failure.
                    logger.debug(
                        "commit_observer_swallowed",
                        exc_info=True,
                    )
            _b().audit_log(
                "git_execute_success",
                operation,
                success=True,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                },
            )

            # Detached-HEAD recovery hint (#2162). After a successful commit
            # in a pipeline session, surface a clear hint if HEAD is detached
            # so the agent doesn't spend minutes guessing at policy bypasses
            # to update its work branch ref.
            hint = _detached_head_hint(operation, exec_path, repo_path, container_id)
            stderr_out = (result.stderr or "") + hint if hint else result.stderr

            return make_success(
                f"git {operation} successful",
                {
                    "stdout": result.stdout,
                    "stderr": stderr_out,
                    "returncode": result.returncode,
                },
            )
        else:
            # Check if this is an expected failure (e.g., repo detection queries)
            # These happen when tools check if a directory is a git repo
            is_expected_failure = result.stderr and (
                "not a git repository" in result.stderr
                or "not inside a git repository" in result.stderr
            )

            if is_expected_failure:
                # Log at debug level for expected failures - these are typically
                # from tools probing to detect if they're in a git repo
                logger.debug(
                    "Git operation failed (expected - not a git repository)",
                    operation=operation,
                    repo_path=repo_path,
                    container_id=container_id,
                )
            else:
                # Log at warning level for unexpected failures
                _b().audit_log(
                    "git_execute_failed",
                    operation,
                    success=False,
                    details={
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "returncode": result.returncode,
                        "container_id": container_id,
                        "stderr": result.stderr[:500] if result.stderr else None,
                    },
                )

            # Surface the detached-HEAD recovery hint on failure too. Common
            # cases (rebase --onto mid-conflict, missing --allow-empty, index
            # locks) produce a *failed* commit while detached, and the hint is
            # exactly what cuts that confusion short.
            failure_hint = _detached_head_hint(operation, exec_path, repo_path, container_id)
            failure_stderr = (result.stderr or "") + failure_hint if failure_hint else result.stderr
            return make_error(
                f"git {operation} failed",
                status_code=500,
                details={
                    "stdout": result.stdout,
                    "stderr": failure_stderr,
                    "returncode": result.returncode,
                },
            )

    except subprocess.TimeoutExpired:
        return make_error(f"git {operation} timed out", status_code=504)
    except Exception as e:
        return make_error(f"git {operation} failed: {e}", status_code=500)
