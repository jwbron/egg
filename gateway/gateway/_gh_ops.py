"""Gateway gh_ops cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import json
import re
from typing import Any

from flask import Response, g, request

try:
    from ..github_client import (
        GitHubClient,
    )
    from ..phase_filter import (
        OperationType,
        filter_operation,
    )
    from ..repo_parser import (
        OWNER_REPO_PATTERN,
        parse_owner_repo,
    )
except ImportError:  # flat/container import mode
    from github_client import (  # type: ignore[no-redef, import-untyped]
        GitHubClient,
    )
    from phase_filter import (  # type: ignore[no-redef, import-untyped]
        OperationType,
        filter_operation,
    )
    from repo_parser import (  # type: ignore[no-redef, import-untyped]
        OWNER_REPO_PATTERN,
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


def _apply_pr_labels(
    github: GitHubClient,
    repo: str,
    stdout: str,
    auth_mode: str,
    agent_role: str | None,
    pipeline_id: str | None,
) -> None:
    """Apply labels to a newly created PR. Failures are logged but non-fatal."""
    if not pipeline_id:
        return

    # Extract PR number from URL like https://github.com/owner/repo/pull/42
    match = re.search(r"/pull/(\d+)", stdout or "")
    if not match:
        return

    pr_number = match.group(1)
    labels = ["egg"]
    if agent_role:
        labels.append(f"agent:{agent_role}")

    try:
        # Ensure labels exist (idempotent)
        for label in labels:
            github.execute(
                ["label", "create", label, "--force", "--repo", repo],
                timeout=15,
                mode=auth_mode,
            )
        # Apply labels to the PR
        label_args = ["issue", "edit", pr_number, "--repo", repo]
        for label in labels:
            label_args.extend(["--add-label", label])
        github.execute(label_args, timeout=15, mode=auth_mode)
    except Exception:
        logger.warning(
            "Failed to apply labels to PR",
            pr_number=pr_number,
            repo=repo,
            labels=labels,
            exc_info=True,
        )


def gh_pr_create() -> tuple[Response, int] | Response:
    """
    Create a pull request.

    Request body:
        {
            "repo": "owner/repo",
            "title": "PR title",
            "body": "PR body",
            "base": "main",
            "head": "feature-branch",
            "draft": false  (optional, forced to true in user mode)
        }

    Policy:
        - Bot mode: allowed (egg can create PRs)
        - User mode: allowed (PRs are forced to draft mode)
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    title = data.get("title")
    body = data.get("body", "")
    base = data.get("base")  # None = gh uses repo's default branch
    head = data.get("head")

    if not repo:
        return make_error("Missing repo")
    if not title:
        return make_error("Missing title")
    if not head:
        return make_error("Missing head branch")

    # Determine auth mode for this repo
    auth_mode = _b().get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Get session phase from request context (set by @require_session_auth decorator)
    session_phase = getattr(g, "session_phase", None)

    # Check phase restrictions (if session has a phase set)
    if session_phase:
        try:
            phase_result = filter_operation(
                phase=session_phase,
                operation_type=OperationType.GH,
                command="pr create",
            )
            if not phase_result.allowed:
                _b().audit_log(
                    "pr_create_blocked_phase",
                    "gh_pr_create",
                    success=False,
                    details={
                        "repo": repo,
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
        except ValueError as e:
            # Invalid phase value - log warning and allow (backward compat)
            logger.warning(
                "Invalid session phase value",
                phase=session_phase,
                error=str(e),
            )
    else:
        # No phase set - allow by default for backward compatibility
        # Log a warning to track sessions without phase
        logger.debug(
            "PR create request from session without phase (backward compat)",
            repo=repo,
        )

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = _b().check_private_repo_access(
            operation="pr_create",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            _b().audit_log(
                "pr_create_denied_private_mode",
                "gh_pr_create",
                success=False,
                details={
                    "repo": repo,
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

    # Policy check: PR creation may be blocked in reviewer mode
    policy = _b().get_policy_engine()
    policy_result = policy.check_pr_create_allowed(repo, auth_mode=auth_mode)
    if not policy_result.allowed:
        _b().audit_log(
            "pr_create_blocked",
            "gh_pr_create",
            success=False,
            details={
                "repo": repo,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            policy_result.reason,
            status_code=403,
            details=policy_result.details,
        )

    # In user mode, force PRs to be created as drafts
    draft = data.get("draft", False)
    if policy_result.details and policy_result.details.get("force_draft"):
        draft = True

    # Inject machine-parseable pipeline metadata as an HTML comment so
    # downstream tooling (status reporters, audit scrapers) can recover
    # the pipeline_id / agent_role / issue from the PR body without
    # round-tripping through the orchestrator state store.
    session = getattr(g, "session", None)
    session_pipeline_id = getattr(session, "pipeline_id", None) if session else None
    if session_pipeline_id:
        session_agent_role = getattr(session, "agent_role", None) or ""
        session_issue_number = getattr(session, "issue_number", None) or ""

        # Sanitize values to prevent breaking the HTML comment structure
        def _safe(v: str) -> str:
            return str(v).replace("--", "").replace(">", "")

        metadata_comment = (
            f"<!-- egg-pipeline-context"
            f" pipeline_id={_safe(session_pipeline_id)}"
            f" agent_role={_safe(session_agent_role)}"
            f" issue={_safe(str(session_issue_number))}"
            f" -->"
        )
        body = f"{body}\n\n{metadata_comment}" if body else metadata_comment

    try:
        github = _b().get_github_client(mode=auth_mode)
        args = [
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
        ]

        if base:
            args.extend(["--base", base])

        if draft:
            args.append("--draft")

        result = github.execute(args, timeout=60, mode=auth_mode)

        if result.success:
            # Apply labels to the newly created PR
            _apply_pr_labels(
                github=github,
                repo=repo,
                stdout=result.stdout,
                auth_mode=auth_mode,
                agent_role=getattr(session, "agent_role", None) if session else None,
                pipeline_id=session_pipeline_id,
            )

            _b().audit_log(
                "pr_created",
                "gh_pr_create",
                success=True,
                details={
                    "repo": repo,
                    "title": title,
                    "base": base,
                    "head": head,
                    "auth_mode": auth_mode,
                    "draft": draft,
                },
            )
            return make_success(
                "PR created",
                {"stdout": result.stdout, "stderr": result.stderr, "auth_mode": auth_mode},
            )
        else:
            error_msg = result.stderr or "Unknown error"
            _b().audit_log(
                "pr_create_failed",
                "gh_pr_create",
                success=False,
                details={
                    "repo": repo,
                    "error": error_msg[:200] if error_msg else "",
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"Failed to create PR: {error_msg}",
                status_code=500,
                details=result.to_dict(),
            )
    except Exception as e:
        logger.exception("Unexpected error in gh_pr_create")
        return make_error(f"Internal error: {e}", status_code=500)


def gh_pr_comment() -> tuple[Response, int] | Response:
    """
    Add a comment to a PR.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "body": "Comment text"
        }

    Policy: pr_comment (allowed on any PR)
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")
    body = data.get("body")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")
    if not body:
        return make_error("Missing body")

    # Determine auth mode for this repo
    auth_mode = _b().get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = _b().check_private_repo_access(
            operation="pr_comment",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            _b().audit_log(
                "pr_comment_denied_private_mode",
                "gh_pr_comment",
                success=False,
                details={
                    "repo": repo,
                    "pr_number": pr_number,
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

    # Check if commenting is allowed (allowed on any PR)
    policy = _b().get_policy_engine()
    policy_result = policy.check_pr_comment_allowed(repo, pr_number, auth_mode=auth_mode)

    if not policy_result.allowed:
        _b().audit_log(
            "pr_comment_denied",
            "gh_pr_comment",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Comment denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = _b().get_github_client(mode=auth_mode)
    args = [
        "pr",
        "comment",
        str(pr_number),
        "--repo",
        repo,
        "--body",
        body,
    ]

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        _b().audit_log(
            "pr_comment_added",
            "gh_pr_comment",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("Comment added", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to add comment: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


def gh_pr_edit() -> tuple[Response, int] | Response:
    """
    Edit a PR title, body, or base branch.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "title": "New title",  # optional
            "body": "New body",     # optional
            "base": "main"          # optional — retarget the PR base
        }

    At least one of ``title``, ``body``, or ``base`` must be set.

    The ``base`` field is the merge target branch ref (e.g.
    ``main`` or ``egg/issue-N/slice-3``). It is the canonical
    surface for the stacked-PR reconciler (#2137) to retarget a
    child PR after the parent merges and the parent's branch is
    deleted on origin. The ref is forwarded as-is to the GitHub
    PATCH ``/repos/{owner}/{repo}/pulls/{pr_number}`` API.

    Policy: pr_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")
    title = data.get("title")
    body = data.get("body")
    base = data.get("base")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return make_error("Invalid pr_number: must be a positive integer")
    if not title and not body and not base:
        return make_error("Must provide title, body, or base to edit")
    if base is not None and (not isinstance(base, str) or not base.strip()):
        return make_error("Invalid base: must be a non-empty branch ref")

    # Validate repo format early (before any API calls)
    repo_info = parse_owner_repo(repo)
    if not repo_info:
        return make_error("Invalid repo format: expected 'owner/repo'")

    # Determine auth mode for this repo
    auth_mode = _b().get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
    priv_result = _b().check_private_repo_access(
        operation="pr_edit",
        owner=repo_info.owner,
        repo=repo_info.repo,
        for_write=True,
        session_mode=session_mode,
    )
    if not priv_result.allowed:
        _b().audit_log(
            "pr_edit_denied_private_mode",
            "gh_pr_edit",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
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

    # Check PR ownership (pass auth mode for relaxed policy in user mode)
    policy = _b().get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number, auth_mode=auth_mode)

    if not policy_result.allowed:
        _b().audit_log(
            "pr_edit_denied",
            "gh_pr_edit",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Edit denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = _b().get_github_client(mode=auth_mode)
    args = ["api", f"repos/{repo_info.owner}/{repo_info.repo}/pulls/{pr_number}", "-X", "PATCH"]
    if title:
        args.extend(["-f", f"title={title}"])
    if body:
        args.extend(["-f", f"body={body}"])
    if base:
        args.extend(["-f", f"base={base}"])

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        _b().audit_log(
            "pr_edited",
            "gh_pr_edit",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("PR edited", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to edit PR: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


def gh_pr_close() -> tuple[Response, int] | Response:
    """
    Close a PR.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123
        }

    Policy: pr_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")

    # Determine auth mode for this repo
    auth_mode = _b().get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = _b().check_private_repo_access(
            operation="pr_close",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            _b().audit_log(
                "pr_close_denied_private_mode",
                "gh_pr_close",
                success=False,
                details={
                    "repo": repo,
                    "pr_number": pr_number,
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

    # Check PR ownership (pass auth mode for relaxed policy in user mode)
    policy = _b().get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number, auth_mode=auth_mode)

    if not policy_result.allowed:
        _b().audit_log(
            "pr_close_denied",
            "gh_pr_close",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Close denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = _b().get_github_client(mode=auth_mode)
    args = ["pr", "close", str(pr_number), "--repo", repo]

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        _b().audit_log(
            "pr_closed",
            "gh_pr_close",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("PR closed", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to close PR: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


def gh_find_open_pr() -> tuple[Response, int] | Response:
    """Control-plane idempotency lookup: return the open ``head → base`` PR number.

    This is an **orchestrator-only** route, gated by ``@require_launcher_auth``
    rather than ``@require_session_auth``: the caller is the control plane
    (the orchestrator holds the launcher secret), not a sandboxed agent. It
    exists so the orchestrator's slice-PR idempotency pre-flight (#2777 cq-8)
    does not have to register a synthetic *agent* session and impersonate a
    role on ``/api/v1/gh/execute`` — the conflation that #2893 papered over by
    adding a bogus ``AgentRole.ORCHESTRATOR``. The orchestrator is not an
    agent role; it is the server that manages pipelines, so it authenticates
    as the control plane and uses a purpose-built read-only endpoint.

    Unlike ``/api/v1/gh/execute`` (arbitrary allowlisted argv), this route
    accepts only ``repo``/``head``/``base`` and constructs the fixed
    read-only argv server-side, so there is no general gh-command surface on
    the launcher-auth path.

    Request body:
        {"repo": "owner/name", "head": "<branch>", "base": "<branch>"}

    Returns:
        ``{"number": <int>}`` on hit, ``{"number": null}`` on miss. The GH
        API documents at most one open PR per (head, base) tuple, so the
        lookup is ``--limit 1``.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    # Validate and bind stripped values in one pass so mypy sees ``repo``
    # / ``head`` / ``base`` as ``str`` (not the ``Any`` returned by
    # ``data.get(...)``) below.
    fields: dict[str, str] = {}
    for name, value in (
        ("repo", data.get("repo")),
        ("head", data.get("head")),
        ("base", data.get("base")),
    ):
        if not isinstance(value, str) or not value.strip():
            return make_error(f"Missing or invalid {name}: must be a non-empty string")
        fields[name] = value.strip()
    repo, head, base = fields["repo"], fields["head"], fields["base"]

    # ``OWNER_REPO_PATTERN`` is stricter than ``parse_owner_repo`` (which
    # also accepts full GitHub URLs); the docstring and the validation
    # error below both promise the literal ``owner/name`` shape, so we
    # match against the pattern directly rather than the URL-permissive
    # helper.
    if OWNER_REPO_PATTERN.match(repo) is None:
        return make_error("Invalid repo: must be 'owner/name'")

    args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        head,
        "--base",
        base,
        "--state",
        "open",
        "--limit",
        "1",
        "--json",
        "number",
    ]

    auth_mode = _b().get_auth_mode(repo)
    github = _b().get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, mode=auth_mode)

    if not result.success:
        # ``gh`` should not print credentials to stderr, but truncate
        # defensively so we never page a giant stderr blob into the
        # audit log.
        stderr_excerpt = (result.stderr or "")[:500]
        _b().audit_log(
            "gh_find_open_pr_failed",
            "gh_find_open_pr",
            success=False,
            details={"repo": repo, "head": head, "base": base, "stderr": stderr_excerpt},
        )
        return make_error(
            f"Command failed: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )

    number: int | None = None
    stdout = (result.stdout or "").strip()
    if stdout:
        try:
            items = json.loads(stdout)
        except ValueError, TypeError:
            items = None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("number") is not None:
                    try:
                        number = int(item["number"])
                    except TypeError, ValueError:
                        number = None
                    break

    _b().audit_log(
        "gh_find_open_pr",
        "gh_find_open_pr",
        success=True,
        details={"repo": repo, "head": head, "base": base, "number": number},
    )
    return make_success("Open PR lookup complete", {"number": number})


def gh_list_open_prs() -> tuple[Response, int] | Response:
    """Control-plane listing: return the repo's open PRs (number/head/base).

    Like ``/api/v1/gh/find_open_pr``, this is an **orchestrator-only**
    route gated by ``@require_launcher_auth`` rather than
    ``@require_session_auth``: the caller is the control plane (the
    orchestrator holds the launcher secret), not a sandboxed agent. It
    exists so the orchestrator's context-PR idempotency pre-flight
    (``_open_context_pr_at_implement_start``) and stacked-PR reconciler
    do not have to register a synthetic *agent* session and impersonate a
    role on ``/api/v1/gh/execute`` — the conflation #2910 papered over by
    adding a bogus ``AgentRole.ORCHESTRATOR`` (removed in #2925). The
    orchestrator is not an agent role; it is the server that manages
    pipelines, so it authenticates as the control plane and uses a
    purpose-built read-only endpoint.

    Unlike ``/api/v1/gh/execute`` (arbitrary allowlisted argv), this route
    accepts only ``repo``/``limit`` and constructs the fixed read-only
    argv server-side, so there is no general gh-command surface on the
    launcher-auth path.

    Request body:
        {"repo": "owner/name", "limit": <int 1..1000, optional, default 200>}

    Returns:
        ``{"prs": [{"number": int, "headRefName": str, "baseRefName": str}, ...]}``.
        The caller (``GatewayClient.list_open_prs``) normalises this into
        the ``number``/``head_ref``/``base_ref`` shape its consumers expect.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")
    # ``request.get_json()`` returns whatever JSON parses — a launcher
    # caller could legitimately post an array or scalar. Reject anything
    # other than an object up front so ``data.get(...)`` below cannot
    # raise ``AttributeError`` → 500.
    if not isinstance(data, dict):
        return make_error("Invalid body: must be a JSON object")

    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return make_error("Missing or invalid repo: must be a non-empty string")
    repo = repo.strip()

    # ``OWNER_REPO_PATTERN`` is stricter than ``parse_owner_repo`` (which
    # also accepts full GitHub URLs); the docstring promises the literal
    # ``owner/name`` shape, so we match against the pattern directly.
    if OWNER_REPO_PATTERN.match(repo) is None:
        return make_error("Invalid repo: must be 'owner/name'")

    # ``bool`` is a subclass of ``int``; reject it explicitly so ``True``
    # cannot slip through as ``limit=1``.
    limit = data.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        return make_error("Invalid limit: must be an integer in [1, 1000]")

    args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,headRefName,baseRefName",
    ]

    auth_mode = _b().get_auth_mode(repo)
    github = _b().get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, mode=auth_mode)

    if not result.success:
        # ``gh`` should not print credentials to stderr, but truncate
        # defensively so we never page a giant stderr blob into the
        # audit log.
        stderr_excerpt = (result.stderr or "")[:500]
        _b().audit_log(
            "gh_list_open_prs_failed",
            "gh_list_open_prs",
            success=False,
            details={"repo": repo, "limit": limit, "stderr": stderr_excerpt},
        )
        return make_error(
            f"Command failed: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )

    prs: list[dict[str, Any]] = []
    stdout = (result.stdout or "").strip()
    if stdout:
        try:
            items = json.loads(stdout)
        except ValueError, TypeError:
            items = None
        if isinstance(items, list):
            prs = [item for item in items if isinstance(item, dict)]

    _b().audit_log(
        "gh_list_open_prs",
        "gh_list_open_prs",
        success=True,
        details={"repo": repo, "limit": limit, "count": len(prs)},
    )
    return make_success("Open PR list complete", {"prs": prs})


def gh_pr_merge_state() -> tuple[Response, int] | Response:
    """Control-plane PR merge-state read: return ``state`` + ``mergedAt`` (#3393).

    An **orchestrator-only** route gated by ``@require_launcher_auth``
    rather than ``@require_session_auth`` — the caller is the control
    plane (the orchestrator holds the launcher secret), not a sandboxed
    agent. It is the read half of the cq-1 cross-repo merge-sequencing
    gate: the orchestrator polls an upstream slice PR's merge state to
    decide when to mark a downstream draft PR ready. Modelled on
    ``gh_find_open_pr`` / ``gh_list_open_prs`` (#2925): the orchestrator
    is the server that manages pipelines, not an ``AgentRole``, so it
    authenticates as the control plane and uses a purpose-built,
    fixed-argv read-only endpoint (no general gh surface here).

    Merge detection deliberately keys off the PR's ``mergedAt`` /
    ``state`` — NOT head-SHA equality: a squash/rebase merge produces a
    merge-commit SHA that differs from the PR head, so a SHA comparison
    would misfire (#3393 task-5-1 pin (a)).

    Request body:
        {"repo": "owner/name", "pr_number": <int>}

    Returns:
        ``{"state": "OPEN|CLOSED|MERGED"|null, "mergedAt": "<ISO-8601>"|null}``.
    """
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return make_error("Invalid body: must be a JSON object")

    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return make_error("Missing or invalid repo: must be a non-empty string")
    repo = repo.strip()
    if OWNER_REPO_PATTERN.match(repo) is None:
        return make_error("Invalid repo: must be 'owner/name'")

    # ``bool`` is a subclass of ``int``; reject it explicitly so ``True``
    # cannot slip through as ``pr_number=1``.
    pr_number = data.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return make_error("Invalid pr_number: must be a positive integer")

    args = [
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state,mergedAt",
    ]

    auth_mode = _b().get_auth_mode(repo)
    github = _b().get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, mode=auth_mode)

    if not result.success:
        stderr_excerpt = (result.stderr or "")[:500]
        _b().audit_log(
            "gh_pr_merge_state_failed",
            "gh_pr_merge_state",
            success=False,
            details={"repo": repo, "pr_number": pr_number, "stderr": stderr_excerpt},
        )
        return make_error(
            f"Command failed: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )

    state_val: Any = None
    merged_at: Any = None
    stdout = (result.stdout or "").strip()
    if stdout:
        try:
            parsed = json.loads(stdout)
        except ValueError, TypeError:
            parsed = None
        if isinstance(parsed, dict):
            state_val = parsed.get("state")
            merged_at = parsed.get("mergedAt")

    _b().audit_log(
        "gh_pr_merge_state",
        "gh_pr_merge_state",
        success=True,
        details={"repo": repo, "pr_number": pr_number, "state": state_val},
    )
    return make_success(
        "PR merge-state lookup complete",
        {"state": state_val, "mergedAt": merged_at},
    )


def gh_pr_ready() -> tuple[Response, int] | Response:
    """Control-plane PR draft→ready transition: wrap ``gh pr ready`` (#3393).

    An **orchestrator-only** route gated by ``@require_launcher_auth`` —
    the write half of the cq-1 cross-repo merge-sequencing gate. When the
    upstream slice PR merges, the orchestrator transitions the downstream
    cross-repo dependent PR from draft to ready. Like the sibling
    control-plane PR routes (``gh_find_open_pr`` / ``gh_list_open_prs``),
    the caller is the control plane, so it authenticates with the
    launcher secret and this route constructs a **fixed, narrow argv**
    server-side (``pr ready <n> --repo <repo>``) — there is no arbitrary
    gh-command surface on the launcher-auth path, only this single
    ready-transition. ``pr ready`` is already on ``ALLOWED_GH_COMMANDS``
    (github_client.py) so the underlying ``gh`` invocation re-validates
    through the same allowlist floor.

    Request body:
        {"repo": "owner/name", "pr_number": <int>}

    Returns:
        ``{"stdout": "<gh output>"}`` on success.
    """
    data = request.get_json()
    if not data or not isinstance(data, dict):
        return make_error("Invalid body: must be a JSON object")

    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return make_error("Missing or invalid repo: must be a non-empty string")
    repo = repo.strip()
    if OWNER_REPO_PATTERN.match(repo) is None:
        return make_error("Invalid repo: must be 'owner/name'")

    pr_number = data.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return make_error("Invalid pr_number: must be a positive integer")

    args = [
        "pr",
        "ready",
        str(pr_number),
        "--repo",
        repo,
    ]

    auth_mode = _b().get_auth_mode(repo)
    github = _b().get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, mode=auth_mode)

    if not result.success:
        stderr_excerpt = (result.stderr or "")[:500]
        _b().audit_log(
            "gh_pr_ready_failed",
            "gh_pr_ready",
            success=False,
            details={"repo": repo, "pr_number": pr_number, "stderr": stderr_excerpt},
        )
        return make_error(
            f"Failed to mark PR ready: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )

    _b().audit_log(
        "gh_pr_ready",
        "gh_pr_ready",
        success=True,
        details={"repo": repo, "pr_number": pr_number},
    )
    return make_success("PR marked ready", {"stdout": result.stdout})
