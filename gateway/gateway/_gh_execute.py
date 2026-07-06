"""Gateway gh_execute cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import os
import re
from typing import Any

from flask import Response, g, request

try:
    from ..agent_restrictions import (
        check_agent_gh_operation,
    )
    from ..github_client import (
        ALLOWED_GH_COMMANDS,
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        find_gh_command_index,
        is_gh_command_allowed,
        parse_gh_api_args,
        validate_gh_api_path,
    )
    from ..phase_filter import (
        OperationType,
        filter_operation,
    )
    from ..repo_parser import (
        parse_owner_repo,
    )
except ImportError:  # flat/container import mode
    from agent_restrictions import (  # type: ignore[no-redef, import-untyped]
        check_agent_gh_operation,
    )
    from github_client import (  # type: ignore[no-redef, import-untyped]
        ALLOWED_GH_COMMANDS,
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        find_gh_command_index,
        is_gh_command_allowed,
        parse_gh_api_args,
        validate_gh_api_path,
    )
    from phase_filter import (  # type: ignore[no-redef, import-untyped]
        OperationType,
        filter_operation,
    )
    from repo_parser import (  # type: ignore[no-redef, import-untyped]
        parse_owner_repo,
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


def gh_execute() -> tuple[Response, int] | Response:
    """
    Execute a generic gh command.

    Request body:
        {
            "args": ["pr", "view", "123"],
            "cwd": "/path/to/repo"  # optional
        }

    Policy: Filtered - only read-only operations allowed by default.
    Blocked commands return 403.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    args = data.get("args", [])
    cwd = data.get("cwd")
    # Repo passed from container - container can detect repo from worktree,
    # but gateway can't (different git structure)
    payload_repo = data.get("repo")

    if not args:
        return make_error("Missing args")

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check for commands blocked entirely in private mode (too broad to filter by repo)
    if session_mode == "private" and args and args[0] in GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE:
        _b().audit_log(
            "gh_command_blocked_private_mode",
            "gh_execute",
            success=False,
            details={
                "command": args[0],
                "reason": "Command blocked in private mode (too broad)",
            },
        )
        return make_error(
            f"Command 'gh {args[0]}' is not allowed in private mode",
            status_code=403,
            details={"command": args[0], "session_mode": "private"},
        )

    # Check for blocked commands
    cmd_str = " ".join(args[:2]) if len(args) >= 2 else args[0] if args else ""

    for blocked in BLOCKED_GH_COMMANDS:
        if cmd_str.startswith(blocked):
            _b().audit_log(
                "blocked_command",
                "gh_execute",
                success=False,
                details={"command_args": args, "blocked_command": blocked},
            )
            return make_error(
                f"Command '{blocked}' is not allowed through the gateway. "
                f"Allowed: {', '.join(sorted(ALLOWED_GH_COMMANDS))}, api.",
                status_code=403,
                details={"blocked_command": blocked, "command_args": args},
            )

    # --- Deny-by-default allowlist (parity with git_execute) ---
    # A generic gh command must be on ALLOWED_GH_COMMANDS, or be `gh api`
    # (further constrained below by GH_API_ALLOWED_PATHS). Anything else fails
    # closed — this is what keeps credential-adjacent and otherwise
    # unanticipated subcommands from executing by default.
    gh_allowed, gh_cmd_key = is_gh_command_allowed(args)
    if not gh_allowed:
        _b().audit_log(
            "gh_command_not_allowed",
            "gh_execute",
            success=False,
            details={"command_args": args, "command_key": gh_cmd_key},
        )
        _display_key = gh_cmd_key or "(no subcommand)"
        return make_error(
            f"Command 'gh {_display_key}' is not permitted through the gateway. "
            f"Allowed: {', '.join(sorted(ALLOWED_GH_COMMANDS))}, api.",
            status_code=403,
            details={"command_key": gh_cmd_key, "command_args": args},
        )

    # --- Phase and role-based operation filtering ---
    # Block operations like "issue comment" / "issue edit" when phase or role restricts them.
    # Build a command string from the first 3 non-flag args for matching.
    #
    # Normalize past any leading -R/--repo selector before constructing the
    # command string used by the phase and role filters — parity with the
    # overseer block below (line 4379) and the api-path guard further down
    # (line 4541). Without this, an argv like `["-R", "owner/repo", "issue",
    # "comment", "1032", "--body", "..."]` keys as `"owner/repo issue
    # comment"`, which doesn't fnmatch `"issue comment *"` (phase filter)
    # and doesn't `startswith("issue comment")` (_BLOCKED_GH_OPS), letting
    # the role/phase enforcement be bypassed entirely. The allowlist check
    # above already normalizes via `find_gh_command_index`, so doing the
    # same here keeps the three positional-key call sites consistent.
    _filter_cmd_idx = find_gh_command_index(args)
    non_flag_args = [a for a in args[_filter_cmd_idx:] if not a.startswith("-")]
    gh_command_str = " ".join(non_flag_args[:3])

    session_phase = getattr(g, "session_phase", None)
    if session_phase:
        try:
            phase_result = filter_operation(
                phase=session_phase,
                operation_type=OperationType.GH,
                command=gh_command_str,
            )
            if not phase_result.allowed:
                _b().audit_log(
                    "gh_execute_blocked_phase",
                    "gh_execute",
                    success=False,
                    details={
                        "command": gh_command_str,
                        "phase": session_phase,
                        "reason": phase_result.blocked_reason,
                    },
                )
                return make_error(
                    phase_result.message,
                    status_code=403,
                    details={
                        "phase": session_phase,
                        "blocked_reason": phase_result.blocked_reason,
                    },
                )
        except ValueError:
            # Invalid phase value - allow for backward compat
            logger.warning("Invalid session phase in gh_execute", phase=session_phase)

    # Role-based operation filtering — block agents from posting issue comments regardless of phase.
    session_role = None
    if hasattr(g, "session") and g.session:
        _role = getattr(g.session, "agent_role", None)
        if isinstance(_role, str) and _role:
            session_role = _role
        elif _role is not None and not isinstance(_role, str):
            # Non-string agent_role — corrupted session, deny
            return make_error(
                "Invalid agent role type",
                status_code=403,
                details={"role": str(_role), "command": gh_command_str},
            )
    if session_role:
        role_allowed, role_reason = check_agent_gh_operation(session_role, gh_command_str)
        if not role_allowed:
            _b().audit_log(
                "gh_execute_blocked_agent_role",
                "gh_execute",
                success=False,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "reason": role_reason,
                },
            )
            return make_error(
                role_reason,
                status_code=403,
                details={"role": session_role, "command": gh_command_str},
            )

    # Issue #1962 TASK-2-2: extra guardrails for `gh issue create`
    # from the overseer role. The role-level check above does NOT
    # block `gh issue create` from the overseer (the operation is
    # not on _OVERSEER_BLOCKED_GH_OPS) so the existing handler lets
    # it through. We now layer additional defenses on top:
    # repo enforcement against EGG_PIPELINE_REPO, label injection,
    # title/body size limits, and a defense-in-depth secret-pattern
    # scan on the body. Failure is a structured 403.
    #
    # The guard looks past any leading `-R`/`--repo` selector via
    # `find_gh_command_index` so an argv like
    # `[-R owner/repo issue create --title ... --body <secret>]`
    # still runs the secret-pattern scan; otherwise the leading
    # selector would put the `"issue"` token at args[2] instead of
    # args[0], so an `args[0] == "issue"` check would miss it and
    # the entire overseer block would be silently skipped (parity
    # fix with the api-path guard below).
    _overseer_cmd_idx = find_gh_command_index(args)
    if (
        session_role
        and session_role.lower() == "overseer"
        and _overseer_cmd_idx + 1 < len(args)
        and args[_overseer_cmd_idx] == "issue"
        and args[_overseer_cmd_idx + 1] == "create"
    ):
        try:
            from ..agent_restrictions import check_overseer_gh_issue_create
        except ImportError:
            from agent_restrictions import (  # type: ignore[no-redef]
                check_overseer_gh_issue_create,
            )

        # Parse the relevant flags from the gh argv. We accept both
        # --title-file/--body-file (the new CLI verb's preferred path)
        # and --title/--body (the historical form) so old callers do
        # not break. Each known flag MUST be followed by a value that
        # does not start with '-' (otherwise a malformed argv like
        # `--repo --label foo` would consume `--label` as the repo
        # value and walk past every subsequent flag — reviewer_code
        # blocker against the original loop's order-dependence).
        repo_arg: str | None = None
        title_text: str = ""
        body_text: str = ""
        labels: list[str] = []
        _OVERSEER_VALUE_FLAGS = {
            "--repo",
            "--label",
            "--title",
            "--title-file",
            "--body",
            "--body-file",
        }

        def _value_for(flag: str, idx: int) -> tuple[str | None, tuple[Response, int] | None]:
            """Return (value, error_response) for a known --flag at args[idx]."""
            if idx + 1 >= len(args):
                return None, make_error(
                    f"Flag {flag!r} requires a value (end of argv)",
                    status_code=400,
                    details={"command": gh_command_str},
                )
            val = args[idx + 1]
            if val.startswith("-"):
                return None, make_error(
                    f"Flag {flag!r} requires a value (got another flag {val!r})",
                    status_code=400,
                    details={"command": gh_command_str},
                )
            return val, None

        # Start past the `issue create` tokens; `_overseer_cmd_idx` is the
        # index of `"issue"`, so the flag walk begins at `_overseer_cmd_idx
        # + 2`. With no leading selector this collapses to the original
        # `i = 2`.
        i = _overseer_cmd_idx + 2
        while i < len(args):
            tok = args[i]
            if tok in _OVERSEER_VALUE_FLAGS:
                val, err = _value_for(tok, i)
                if err is not None:
                    return err
                if tok == "--repo":
                    repo_arg = val
                elif tok == "--label":
                    labels.append(val or "")
                elif tok == "--title":
                    title_text = val or ""
                elif tok == "--title-file":
                    try:
                        with open(val or "", encoding="utf-8", errors="strict") as _f:
                            title_text = _f.read().strip()
                    except UnicodeDecodeError as _exc:
                        return make_error(
                            f"--title-file {val!r} contains invalid UTF-8: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                    except OSError as _exc:
                        return make_error(
                            f"Cannot read --title-file {val!r}: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                elif tok == "--body":
                    body_text = val or ""
                elif tok == "--body-file":
                    try:
                        # errors="strict" so invalid UTF-8 in the body
                        # is rejected loudly (reviewer_code blocker:
                        # silent corruption could swap a leaked-secret
                        # byte sequence past the regex check).
                        with open(val or "", encoding="utf-8", errors="strict") as _f:
                            body_text = _f.read()
                    except UnicodeDecodeError as _exc:
                        return make_error(
                            f"--body-file {val!r} contains invalid UTF-8: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                    except OSError as _exc:
                        return make_error(
                            f"Cannot read --body-file {val!r}: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                i += 2
                continue
            else:
                i += 1

        pipeline_repo = os.environ.get("EGG_PIPELINE_REPO")
        ov_check = check_overseer_gh_issue_create(
            role=session_role,
            repo=repo_arg or "",
            pipeline_repo=pipeline_repo,
            labels=labels,
            title=title_text,
            body=body_text,
        )
        if not ov_check.allowed:
            _b().audit_log(
                "gh_overseer_issue_create_blocked",
                "gh_execute",
                success=False,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "reason": ov_check.reason,
                    "secret_kinds": list(ov_check.secret_kinds),
                },
            )
            return make_error(
                ov_check.reason,
                status_code=403,
                details={
                    "role": session_role,
                    "command": gh_command_str,
                    "secret_kinds": list(ov_check.secret_kinds),
                },
            )
        # Auto-inject any required labels the caller forgot. The
        # injected labels are tagged in the audit log so operators can
        # spot bypass attempts.
        if ov_check.injected_labels:
            for lbl in ov_check.injected_labels:
                args = (*args, "--label", lbl)
            _b().audit_log(
                "gh_overseer_issue_create_labels_injected",
                "gh_execute",
                success=True,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "injected_labels": list(ov_check.injected_labels),
                },
            )

    # For 'gh api' commands, validate the path against allowlist.
    # Look past any leading -R/--repo selector so `gh -R owner/repo api /path`
    # is still subjected to GH_API_ALLOWED_PATHS — otherwise the leading
    # selector would shift args[0] off "api" and the path check would be
    # silently skipped.
    api_path: str | None = None
    method: str = "GET"
    _gh_cmd_idx = find_gh_command_index(args)
    if _gh_cmd_idx < len(args) and args[_gh_cmd_idx] == "api" and len(args) > _gh_cmd_idx + 1:
        # Parse arguments to find the actual API path (skip flags like -X, --method, etc.)
        api_path, method = parse_gh_api_args(args[_gh_cmd_idx + 1 :])
        if api_path is None:
            _b().audit_log(
                "api_path_missing",
                "gh_execute",
                success=False,
                details={"command_args": args},
            )
            return make_error("No API path provided in gh api command", status_code=400)

        # Resolve {owner} and {repo} template variables if present
        # The gh CLI resolves these from the current repo's git remote
        resolved_api_path = _b().resolve_gh_api_template_variables(api_path, cwd)
        if resolved_api_path is None:
            _b().audit_log(
                "api_path_template_resolution_failed",
                "gh_execute",
                success=False,
                details={
                    "api_path": api_path,
                    "cwd": cwd,
                    "reason": "Could not resolve template variables",
                },
            )
            return make_error(
                "Could not resolve {owner}/{repo} template variables. "
                "Ensure you are in a git repository with an 'origin' remote.",
                status_code=400,
            )

        # If template variables were resolved, update the args to use resolved path
        if resolved_api_path != api_path:
            # Find and replace the API path in args
            args = list(args)  # Make a mutable copy
            for i, arg in enumerate(args):
                if arg == api_path:
                    args[i] = resolved_api_path
                    break
            api_path = resolved_api_path

        path_valid, path_error = validate_gh_api_path(api_path, method)
        if not path_valid:
            _b().audit_log(
                "api_path_blocked",
                "gh_execute",
                success=False,
                details={"api_path": api_path, "method": method, "reason": path_error},
            )
            return make_error(path_error, status_code=403)

        # Detect issue comment/edit via gh api (bypass prevention).
        # These API calls are equivalent to "gh issue comment/edit {id}" —
        # apply the same phase + role checks.
        synthesized_cmd = None

        # POST to repos/{owner}/{repo}/issues/{id}/comments → issue comment
        _api_issue_comment_match = re.match(r"^repos/[^/]+/[^/]+/issues/(\d+)/comments$", api_path)
        if _api_issue_comment_match and method.upper() == "POST":
            synthesized_cmd = f"issue comment {_api_issue_comment_match.group(1)}"

        # PATCH to repos/{owner}/{repo}/issues/{id} → issue edit
        _api_issue_edit_match = re.match(r"^repos/[^/]+/[^/]+/issues/(\d+)$", api_path)
        if _api_issue_edit_match and method.upper() == "PATCH":
            synthesized_cmd = f"issue edit {_api_issue_edit_match.group(1)}"

        if synthesized_cmd:
            # Phase check
            if session_phase:
                try:
                    api_phase_result = filter_operation(
                        phase=session_phase,
                        operation_type=OperationType.GH,
                        command=synthesized_cmd,
                    )
                    if not api_phase_result.allowed:
                        _b().audit_log(
                            "gh_api_issue_op_blocked_phase",
                            "gh_execute",
                            success=False,
                            details={
                                "api_path": api_path,
                                "synthesized_command": synthesized_cmd,
                                "phase": session_phase,
                            },
                        )
                        return make_error(
                            api_phase_result.message,
                            status_code=403,
                            details={
                                "phase": session_phase,
                                "blocked_reason": api_phase_result.blocked_reason,
                            },
                        )
                except ValueError:
                    pass
            # Role check
            if session_role:
                api_role_allowed, api_role_reason = check_agent_gh_operation(
                    session_role, synthesized_cmd
                )
                if not api_role_allowed:
                    _b().audit_log(
                        "gh_api_issue_op_blocked_role",
                        "gh_execute",
                        success=False,
                        details={
                            "api_path": api_path,
                            "role": session_role,
                            "reason": api_role_reason,
                        },
                    )
                    return make_error(
                        api_role_reason,
                        status_code=403,
                        details={"role": session_role, "api_path": api_path},
                    )

    # Extract repo using comprehensive extractor (handles --repo, gh repo *, gh api paths)
    repo = extract_repo_from_gh_command(args)

    # Fall back to payload_repo if command doesn't contain repo
    if not repo and payload_repo:
        repo = payload_repo
        # Inject --repo into args so gh command uses it
        # NOTE: Don't inject for commands that don't support --repo flag:
        # - 'gh repo' commands - they take repo as positional arg
        # - 'gh auth' commands - global commands, no repo context
        # - 'gh config' commands - global commands, no repo context
        # - 'gh api' commands - repo is in the API path, not a flag
        commands_without_repo_flag = {"repo", "auth", "config", "api"}
        if args and args[0] not in commands_without_repo_flag:
            args = ["--repo", payload_repo] + list(args)

    # Determine auth mode (default to bot if repo not specified)
    auth_mode = _b().get_auth_mode(repo) if repo else "bot"

    # Check Private Repo Mode policy (if enabled and repo is known)
    if repo:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            priv_result = _b().check_private_repo_access(
                operation="gh_execute",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=False,  # Assume read for generic gh execute
                session_mode=session_mode,
            )
            if not priv_result.allowed:
                _b().audit_log(
                    "gh_execute_denied_private_mode",
                    "gh_execute",
                    success=False,
                    details={
                        "repo": repo,
                        "command_args": args[:3] if len(args) > 3 else args,
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

    # Use reviewer token for PR reviews when available. This allows the
    # reviewer bot (a separate GitHub App) to post approve/request-changes
    # on PRs authored by the main bot — something the bot can't do on its own PRs.
    # This applies to both bot and user modes since the reviewer token is a
    # separate identity specifically for reviews.
    # Note: args may have "--repo owner/repo" prepended, so we check if "pr" and "review"
    # appear in sequence anywhere in the args (not just at positions 0 and 1).
    def is_pr_review_command(cmd_args: list[str]) -> bool:
        for i in range(len(cmd_args) - 1):
            if cmd_args[i] == "pr" and cmd_args[i + 1] == "review":
                return True
        return False

    if is_pr_review_command(args) and auth_mode in ("bot", "user"):
        try:
            from token_refresher import is_reviewer_token_available

            if is_reviewer_token_available():
                auth_mode = "reviewer"
                logger.info("Using reviewer token for pr review command")
            else:
                logger.debug(
                    "Reviewer token not available, using %s token for pr review", auth_mode
                )
        except ImportError:
            pass

    # For mutating operations on specific resources via gh api, verify ownership
    if api_path is not None:
        policy = _b().get_policy_engine()

        # PATCH on comment endpoints — verify bot/configured user owns the comment
        comment_info = extract_comment_edit_info(api_path, method)
        if comment_info:
            c_owner, c_repo_name, c_comment_id, c_comment_type = comment_info
            ownership_result = policy.check_comment_ownership(
                f"{c_owner}/{c_repo_name}",
                c_comment_id,
                c_comment_type,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                _b().audit_log(
                    "comment_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "comment_id": c_comment_id,
                        "comment_type": c_comment_type,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST/PATCH on issue labels — verify bot/configured user owns the issue/PR
        label_info = extract_issue_label_info(api_path, method)
        if label_info:
            l_owner, l_repo_name, l_issue_number = label_info
            ownership_result = policy.check_issue_ownership(
                f"{l_owner}/{l_repo_name}",
                l_issue_number,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                _b().audit_log(
                    "label_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "issue_number": l_issue_number,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST on PR requested reviewers — verify bot/configured user owns the PR
        reviewer_info = extract_pr_reviewer_info(api_path, method)
        if reviewer_info:
            r_owner, r_repo_name, r_pr_number = reviewer_info
            ownership_result = policy.check_pr_ownership(
                f"{r_owner}/{r_repo_name}",
                r_pr_number,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                _b().audit_log(
                    "reviewer_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "pr_number": r_pr_number,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST on PR reviews — verify PR exists and review is allowed
        review_info = extract_pr_review_info(api_path, method)
        if review_info:
            rv_owner, rv_repo_name, rv_pr_number = review_info
            review_result = policy.check_pr_review_allowed(
                f"{rv_owner}/{rv_repo_name}",
                rv_pr_number,
                auth_mode=auth_mode,
            )
            if not review_result.allowed:
                _b().audit_log(
                    "review_create_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "pr_number": rv_pr_number,
                        "reason": review_result.reason,
                    },
                )
                return make_error(
                    review_result.reason,
                    status_code=403,
                    details=review_result.to_dict(),
                )

    # Execute the command
    github = _b().get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, cwd=cwd, mode=auth_mode)

    if result.success:
        response_data = result.to_dict()
        response_data["auth_mode"] = auth_mode
        return make_success("Command executed", response_data)
    else:
        return make_error(
            f"Command failed: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )
