"""
Agent-role-based file restrictions for multi-agent orchestration.

This module extends the phase_filter system to enforce file access patterns
for specialized agents. Each agent
role has specific file paths it can read and write to, preventing agents
from modifying files outside their responsibility.

Security model:
- Architect/Task Planner/Risk Analyst: Can write drafts and agent-outputs only, blocked from source code, docs, contracts, reviews
- Coder: Can write source code, blocked from docs and contracts
- Tester: Can write test files and conftest.py only
- Documenter: Can write docs and markdown only
- Refiner: Can write drafts and agent-outputs only, blocked from source code and contracts
- Reviewers: Can write reviews and agent-outputs only

The gateway uses these restrictions during git push to validate that
commits only modify files allowed for the agent's role.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Re-export from shared package for backwards compatibility
from egg_restrictions.checker import (
    AgentRestrictionResult,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)
from egg_restrictions.patterns import (
    AGENT_PATTERNS,
    AUTOFIXER_PATTERNS,
    CONFLICT_RESOLVER_PATTERNS,
    DEFAULT_CODE_GLOBS,
    DEFAULT_DOCS_GLOBS,
    DEFAULT_TESTS_GLOBS,
    INSPECTOR_PATTERNS,
    OVERSEER_PATTERNS,
    AgentFilePattern,
    AgentRole,
    build_agent_patterns,
    get_agent_pattern_for_repo,
    reset_pattern_cache,
)

logger = logging.getLogger("gateway.agent_restrictions")


def partition_files_by_role(
    role: str,
    files: list[str],
    repo: str | None = None,
) -> tuple[list[str], list[str]]:
    """Split ``files`` into ``(allowed, blocked)`` by what ``role`` may write.

    Used by the gateway's push handler to decide which files in a push
    diff would be rejected by the role's AgentFilePattern and therefore
    need to be auto-filtered out by the per-commit rewriter (#1882).

    Args:
        role: The agent role identifier.
        files: Files in the push diff.
        repo: Optional ``owner/repo`` for per-repo pattern overrides
            (#2528). When set, the role's pattern reflects the
            ``role_patterns:`` block in ``repositories.yaml`` for this
            repo; when ``None``, falls back to global defaults.

    Behaviour:

    - Unknown role → ``([], list(files))`` — every file is blocked, and
      a WARNING is logged so misconfig surfaces.  This matches the
      deny-by-default semantics in
      ``shared/egg_restrictions/checker.py::check_agent_file_access``.
    - Empty ``files`` → ``([], [])``.
    - Otherwise → delegates per-file to
      ``AgentFilePattern.can_write`` so the existing blocked /
      block-exempt / allowed precedence is preserved.
    """
    if not files:
        return [], []

    pattern = get_agent_pattern(role, repo=repo)
    if pattern is None:
        logger.warning(
            "partition_files_by_role_unknown_role",
            extra={"role": role, "file_count": len(files)},
        )
        return [], list(files)

    allowed: list[str] = []
    blocked: list[str] = []
    for path in files:
        if pattern.can_write(path):
            allowed.append(path)
        else:
            blocked.append(path)
    return allowed, blocked


__all__ = [
    "AGENT_PATTERNS",
    "AUTOFIXER_PATTERNS",
    "AgentFilePattern",
    "AgentRestrictionResult",
    "AgentRole",
    "CONFLICT_RESOLVER_PATTERNS",
    "DEFAULT_CODE_GLOBS",
    "DEFAULT_DOCS_GLOBS",
    "DEFAULT_TESTS_GLOBS",
    "INSPECTOR_PATTERNS",
    "OVERSEER_PATTERNS",
    "build_agent_patterns",
    "check_agent_file_access",
    "check_agent_gh_operation",
    "get_agent_pattern",
    "get_agent_pattern_for_repo",
    "partition_files_by_role",
    "reset_pattern_cache",
    "validate_agent_push",
]

# --- GitHub operation restrictions ---
# Blocks agents from executing specific gh CLI commands (e.g., issue comment).
# This is defense-in-depth: phase permissions also block these, but role-based
# restrictions catch cases where phase is not set or not enforced.


@dataclass
class AgentGHRestriction:
    """GitHub operation restrictions for an agent role.

    Defines which gh CLI operations an agent is blocked from executing.
    """

    role: str
    blocked_operations: list[str] = field(default_factory=list)
    description: str = ""

    def is_blocked(self, command: str) -> bool:
        """Check if a gh command is blocked for this role.

        Args:
            command: The gh command string (e.g., "issue comment 123")

        Returns:
            True if the command is blocked
        """
        cmd_lower = command.lower()
        for blocked in self.blocked_operations:
            blocked_lower = blocked.lower()
            if blocked_lower.endswith(" *"):
                # Prefix match: "issue comment *" blocks "issue comment 123"
                prefix = blocked_lower[:-2]  # Strip " *"
                if cmd_lower.startswith(prefix):
                    return True
            elif cmd_lower == blocked_lower:
                return True
        return False


# All pipeline agent roles are blocked from posting issue comments and editing issues.
# These operations should go through .egg-state/reviews/ or the contract API.
_BLOCKED_GH_OPS = ["issue comment *", "issue edit *"]

# Overseer has additional restrictions: blocked from PR operations and phase control.
# It can create issues (for diagnostic filing) but cannot merge, create PRs, or advance phases.
_OVERSEER_BLOCKED_GH_OPS = [
    "issue comment *",
    "issue edit *",
    "pr merge *",
    "pr create *",
]

AGENT_GH_RESTRICTIONS: dict[str, AgentGHRestriction] = {
    role: AgentGHRestriction(
        role=role,
        blocked_operations=_BLOCKED_GH_OPS,
        description=f"Agent role '{role}' cannot post issue comments or edit issues",
    )
    for role in [
        AgentRole.CODER,
        AgentRole.TESTER,
        AgentRole.DOCUMENTER,
        AgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.REFINER,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_PLAN,
        AgentRole.REVIEWER_SECURITY,
        AgentRole.REVIEWER_CONCURRENCY,
        AgentRole.AUTOFIXER,
        AgentRole.CONFLICT_RESOLVER,
        AgentRole.INSPECTOR,
    ]
}

# Add overseer with its specific restrictions
AGENT_GH_RESTRICTIONS[AgentRole.OVERSEER] = AgentGHRestriction(
    role=AgentRole.OVERSEER,
    blocked_operations=_OVERSEER_BLOCKED_GH_OPS,
    description="Overseer agent cannot post issue comments, edit issues, merge PRs, or create PRs",
)


def check_agent_gh_operation(role: str, command: str) -> tuple[bool, str]:
    """Check if an agent role is allowed to execute a gh command.

    Args:
        role: The agent role identifier (e.g., "coder", "reviewer_refine")
        command: The gh command string (e.g., "issue comment 1032")

    Returns:
        Tuple of (allowed, reason). allowed is False if blocked.
    """
    if not isinstance(role, str) or not role:
        return False, "Invalid or missing agent role"

    role_lower = role.lower()
    restriction = AGENT_GH_RESTRICTIONS.get(role_lower)
    if restriction is None:
        # Unknown roles are denied for consistency with file access deny-by-default (#1494 review)
        return False, f"Unknown agent role '{role}' — all GH operations denied"

    if restriction.is_blocked(command):
        return False, (
            f"Agent role '{role}' is not allowed to execute 'gh {command}'. "
            f"Write reviews to .egg-state/reviews/ instead."
        )

    return True, f"Operation allowed for agent role '{role}'"


# ---------------------------------------------------------------------------
# Overseer-specific guardrails for `gh issue create` (issue #1962, TASK-2-2)
# ---------------------------------------------------------------------------

# Hard limits matched by the sandbox-side CLI for symmetry. The gateway
# is defense-in-depth: it rejects oversized inputs even if the CLI
# bypasses its local check.
OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS = 120
OVERSEER_FILE_ISSUE_BODY_MAX_BYTES = 50_000

# Required labels the gateway auto-injects if the caller forgot.
OVERSEER_REQUIRED_LABEL = "agent:overseer"
OVERSEER_VALID_PRIORITY_LABELS = frozenset({"p0", "p1", "p2", "p3"})


@dataclass(frozen=True)
class OverseerGhCheckResult:
    """Outcome of ``check_overseer_gh_issue_create``.

    Attributes:
        allowed: Whether the request passes all guardrails.
        reason: Structured rejection reason (or success message).
        injected_labels: Labels the gateway intends to add to the
            request even when the caller did not pass them. Empty if
            the caller already supplied both required labels.
        secret_kinds: Secret-pattern kinds detected in the body. Empty
            on success; populated when ``allowed=False`` and the cause
            was a defense-in-depth secret-scan match.
    """

    allowed: bool
    reason: str
    injected_labels: tuple[str, ...] = ()
    secret_kinds: tuple[str, ...] = ()


def check_overseer_gh_issue_create(
    *,
    role: str,
    repo: str,
    pipeline_repo: str | None,
    labels: list[str],
    title: str,
    body: str,
) -> OverseerGhCheckResult:
    """Validate an overseer ``gh issue create`` request (issue #1962).

    The reviewer NACK on the original draft correctly flagged that the
    role-based ``check_agent_gh_operation`` does NOT block
    ``gh issue create`` from the overseer today. This function adds the
    *additional guardrails* the planner specified — repo enforcement,
    label injection, size limits, secret-scan rejection — without
    flipping the deny → allow on the simpler role-level rule.

    Args:
        role: Calling agent role.
        repo: ``owner/repo`` the request targets (parsed from the gh
            ``--repo`` flag).
        pipeline_repo: ``owner/repo`` the gateway expects for this
            sandbox (sourced from the ``EGG_PIPELINE_REPO`` env var
            injected by the orchestrator). When set, the request's
            ``--repo`` MUST equal it (no cross-repo filing). When None
            (e.g., dev shells), this constraint is skipped.
        labels: All ``--label`` arguments parsed off the gh argv.
        title: Issue title (already read from ``--title-file``).
        body: Issue body (already read from ``--body-file``).

    Returns:
        ``OverseerGhCheckResult``. The handler should re-emit the gh
        argv with ``injected_labels`` appended.
    """
    if (role or "").lower() != "overseer":
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                f"check_overseer_gh_issue_create: only the overseer role "
                f"may invoke this guardrail (got {role!r})"
            ),
        )

    if pipeline_repo and repo and repo != pipeline_repo:
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                f"cross-repo filing rejected: --repo={repo!r} != "
                f"EGG_PIPELINE_REPO={pipeline_repo!r}"
            ),
        )

    if len(title) > OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS:
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                f"title exceeds {OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS} chars (got {len(title)})"
            ),
        )

    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    if len(body_bytes) > OVERSEER_FILE_ISSUE_BODY_MAX_BYTES:
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                f"body exceeds {OVERSEER_FILE_ISSUE_BODY_MAX_BYTES} bytes (got {len(body_bytes)})"
            ),
        )

    # Defense-in-depth secret scan. Importing lazily avoids forcing
    # gateway tests to depend on the egg_overseer package when they
    # don't exercise this code path.
    try:
        from egg_overseer.scrubbing import find_secret_kinds

        kinds = find_secret_kinds(body)
    except ImportError:  # pragma: no cover - defensive
        kinds = []
    if kinds:
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                "body contains known secret patterns; rejecting per "
                "defense-in-depth (advisor-side scrubber should have "
                f"caught this): {sorted(kinds)}"
            ),
            secret_kinds=tuple(sorted(kinds)),
        )

    # Reject `agent:*` labels other than `agent:overseer` so a buggy
    # caller cannot sneak `agent:fake` in alongside the auto-injected
    # `agent:overseer` (reviewer_code blocker against the prior
    # version's permissive auto-inject).
    bad_agent_labels = [
        label
        for label in labels
        if label.lower().startswith("agent:") and label.lower() != OVERSEER_REQUIRED_LABEL
    ]
    if bad_agent_labels:
        return OverseerGhCheckResult(
            allowed=False,
            reason=(
                f"non-overseer agent label(s) rejected; only "
                f"{OVERSEER_REQUIRED_LABEL!r} is allowed on overseer-filed "
                f"issues: {bad_agent_labels}"
            ),
        )

    # Auto-inject required labels if the caller forgot. Lower-case both
    # sides so the comparison is case-insensitive.
    have_lower = {label.lower() for label in labels}
    injected: list[str] = []
    if OVERSEER_REQUIRED_LABEL not in have_lower:
        injected.append(OVERSEER_REQUIRED_LABEL)
    if not (have_lower & OVERSEER_VALID_PRIORITY_LABELS):
        # Caller did not pass any priority label; the gateway picks p2
        # as the safe default. The caller SHOULD always pass one;
        # logging the auto-injection makes operator review easy.
        injected.append("p2")

    return OverseerGhCheckResult(
        allowed=True,
        reason="overseer gh issue create allowed",
        injected_labels=tuple(injected),
    )


__all__ = list(__all__) + [
    "OVERSEER_FILE_ISSUE_TITLE_MAX_CHARS",
    "OVERSEER_FILE_ISSUE_BODY_MAX_BYTES",
    "OVERSEER_REQUIRED_LABEL",
    "OVERSEER_VALID_PRIORITY_LABELS",
    "OverseerGhCheckResult",
    "check_overseer_gh_issue_create",
]
