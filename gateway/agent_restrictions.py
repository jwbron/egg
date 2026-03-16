"""
Agent-role-based file restrictions for multi-agent orchestration.

This module extends the phase_filter system to enforce file access patterns
for specialized agents. Each agent
role has specific file paths it can read and write to, preventing agents
from modifying files outside their responsibility.

Security model:
- Architect/Task Planner/Risk Analyst: Can write drafts and agent-outputs only, blocked from source code, docs, contracts, reviews
- Coder: Can write source code, blocked from docs and contracts
- Tester: Can write test files only
- Documenter: Can write docs and markdown only
- Refiner: Can write drafts and agent-outputs only, blocked from source code and contracts
- Reviewers: Can write reviews and agent-outputs only

The gateway uses these restrictions during git push to validate that
commits only modify files allowed for the agent's role.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class AgentRole:
    """Agent role identifiers.

    Note: Mirrors egg_contracts.agent_roles.AgentRole to avoid import
    complexity in the gateway module. Values must be kept in sync.
    """

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    CHECKER = "checker"
    # Plan-phase roles
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    # Refine-phase roles
    REFINER = "refiner"
    # Reviewer roles
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    REVIEWER_PLAN = "reviewer_plan"
    REVIEWER_UNIFIED = "reviewer_unified"  # Vestigial: kept for backwards compatibility with persisted pipeline state


@dataclass
class AgentFilePattern:
    """File access pattern for an agent role.

    Defines which files an agent can write to. The gateway enforces these
    restrictions during git push operations.
    """

    role: str
    allowed_patterns: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    description: str = ""

    def can_write(self, file_path: str) -> bool:
        """Check if the agent can write to this file.

        Args:
            file_path: Path relative to repo root

        Returns:
            True if the file can be written

        Security note:
            Blocked patterns are checked FIRST to prevent bypass via
            directory allow patterns. For example, if allowed patterns
            include ".egg-state/agent-outputs/" but blocked patterns
            include ".egg-state/contracts/", we must ensure the blocked
            pattern takes precedence.
        """
        normalized = self._normalize_path(file_path)

        # Reject invalid paths (e.g., path traversal attempts)
        if normalized == "__INVALID_PATH_TRAVERSAL__":
            return False

        # Check blocked patterns FIRST - security takes precedence
        if any(self._matches_pattern(normalized, p) for p in self.blocked_patterns):
            return False

        # If no allowed patterns, nothing is allowed
        if not self.allowed_patterns:
            return False

        return any(self._matches_pattern(normalized, p) for p in self.allowed_patterns)

    @staticmethod
    def _normalize_path(file_path: str) -> str:
        """Normalize a file path to prevent bypass via path manipulation.

        Rejects paths containing '..' to prevent path traversal attacks.
        """
        # First normalize to resolve . and ..
        normalized = posixpath.normpath(file_path)

        # Reject any path that contains .. (traversal attempt)
        if ".." in normalized.split("/"):
            # Return a path that won't match any patterns
            return "__INVALID_PATH_TRAVERSAL__"

        if normalized.startswith("./"):
            normalized = normalized[2:]

        # Also strip leading /
        normalized = normalized.lstrip("/")

        return normalized

    @staticmethod
    def _matches_pattern(file_path: str, pattern: str) -> bool:
        """Check if a file path matches a glob-like pattern.

        Supports:
        - Exact match: "foo/bar.py"
        - Prefix match: "foo/" (matches any file under foo/)
        - Wildcard: "*.py" (matches files ending in .py)
        - Double wildcard: "**/*.py" (matches .py files at any depth)

        Args:
            file_path: Path to check
            pattern: Pattern to match against

        Returns:
            True if the path matches the pattern
        """
        # Normalize both paths
        file_path = file_path.lstrip("./")
        pattern = pattern.lstrip("./")

        # Prefix match (directory pattern)
        if pattern.endswith("/"):
            return file_path.startswith(pattern) or file_path + "/" == pattern

        # Handle ** patterns for recursive matching
        if "**" in pattern:
            # Convert ** to regex-style matching
            # "**/*.py" should match "foo/bar/baz.py"
            parts = pattern.split("**")
            if len(parts) == 2:
                prefix, suffix = parts
                # Check if path starts with prefix (if any) and ends with suffix
                prefix_match = not prefix or file_path.startswith(prefix.rstrip("/"))
                suffix = suffix.lstrip("/")
                suffix_match = not suffix or fnmatch.fnmatch(file_path.split("/")[-1], suffix)
                if prefix_match and suffix_match:
                    # For patterns like "**/*.py", also check the full suffix matches
                    if suffix.startswith("*"):
                        return fnmatch.fnmatch(file_path, "*" + suffix)
                    return True

        # Standard fnmatch for simple wildcards
        return fnmatch.fnmatch(file_path, pattern)


# Default agent file patterns
# These define what each agent role can and cannot modify

CODER_PATTERNS = AgentFilePattern(
    role=AgentRole.CODER,
    description="Coder agent: source code and configuration",
    allowed_patterns=[
        # Source code
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
        "**/*.go",
        "**/*.java",
        "**/*.rb",
        "**/*.rs",
        "**/*.sh",
        # Configuration
        "**/*.yml",
        "**/*.yaml",
        "**/*.json",
        "**/*.toml",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Documentation (Documenter handles)
        "docs/",
        "**/README.md",
        "**/*.md",
        # Contracts (API only)
        ".egg-state/contracts/",
        # Test files (Tester handles) - more specific patterns
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
        "**/*_test.py",
        "**/test_*.py",
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.test.jsx",
        "**/*.spec.ts",
        "**/*.spec.tsx",
        "**/*.spec.js",
        "**/*.spec.jsx",
    ],
)

TESTER_PATTERNS = AgentFilePattern(
    role=AgentRole.TESTER,
    description="Tester agent: test files only",
    allowed_patterns=[
        # Test directories
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
        # Test file patterns
        "**/*_test.py",
        "**/test_*.py",
        "**/*_test.go",
        "**/test_*.go",
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.test.jsx",
        "**/*.spec.ts",
        "**/*.spec.tsx",
        "**/*.spec.js",
        "**/*.spec.jsx",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Source code (Coder handles)
        "src/",
        "lib/",
        "shared/",
        "gateway/",
        "sandbox/",
        "action/",
        # Documentation (Documenter handles)
        "docs/",
        "**/README.md",
        # Contracts
        ".egg-state/contracts/",
    ],
)

DOCUMENTER_PATTERNS = AgentFilePattern(
    role=AgentRole.DOCUMENTER,
    description="Documenter agent: documentation and markdown files",
    allowed_patterns=[
        # Documentation directories
        "docs/",
        # Markdown files anywhere
        "**/*.md",
        "**/README.md",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Source code (Coder handles)
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
        "**/*.go",
        "**/*.java",
        "**/*.rb",
        "**/*.rs",
        # Test files (Tester handles)
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
        # Contracts
        ".egg-state/contracts/",
    ],
)

# Plan-phase agent patterns
# These agents can only write to drafts and agent-outputs directories.

_PLAN_AGENT_BLOCKED = [
    "src/",
    "lib/",
    "shared/",
    "gateway/",
    "sandbox/",
    "action/",
    "docs/",
    "tests/",
    "test/",
    ".egg-state/contracts/",
    ".egg-state/reviews/",
    ".github/",
]

ARCHITECT_PATTERNS = AgentFilePattern(
    role=AgentRole.ARCHITECT,
    description="Architect agent: drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=_PLAN_AGENT_BLOCKED,
)

TASK_PLANNER_PATTERNS = AgentFilePattern(
    role=AgentRole.TASK_PLANNER,
    description="Task planner agent: drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=_PLAN_AGENT_BLOCKED,
)

RISK_ANALYST_PATTERNS = AgentFilePattern(
    role=AgentRole.RISK_ANALYST,
    description="Risk analyst agent: drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=_PLAN_AGENT_BLOCKED,
)

# Reviewer agent patterns
# Reviewers can only write to reviews and agent-outputs directories.

_REVIEWER_ALLOWED = [
    ".egg-state/reviews/",
    ".egg-state/agent-outputs/",
]

_REVIEWER_BLOCKED = [
    "src/",
    "lib/",
    "shared/",
    "gateway/",
    "sandbox/",
    "action/",
    "docs/",
    "tests/",
    "test/",
    ".egg-state/contracts/",
    ".egg-state/drafts/",
    ".github/",
]

REVIEWER_CODE_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_CODE,
    description="Code reviewer agent: reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_CONTRACT_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_CONTRACT,
    description="Contract reviewer agent: reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_AGENT_DESIGN_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_AGENT_DESIGN,
    description="Agent design reviewer: reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

# Refine-phase agent patterns

REFINER_PATTERNS = AgentFilePattern(
    role=AgentRole.REFINER,
    description="Refiner agent: drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Source code (refiner must not modify code)
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
        "**/*.go",
        "**/*.java",
        # Contracts
        ".egg-state/contracts/",
    ],
)

REVIEWER_REFINE_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_REFINE,
    description="Refine reviewer agent: reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_PLAN_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_PLAN,
    description="Plan reviewer agent: reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_UNIFIED_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_UNIFIED,
    description="Unified reviewer agent (vestigial): reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

CHECKER_PATTERNS = AgentFilePattern(
    role=AgentRole.CHECKER,
    description="Checker agent: source code access for auto-fixes (sequential) and reviews (concurrent)",
    allowed_patterns=[
        # Source code (needed for sequential check-and-fix mode)
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        "**/*.jsx",
        "**/*.go",
        "**/*.java",
        "**/*.rb",
        "**/*.rs",
        "**/*.sh",
        # Configuration
        "**/*.yml",
        "**/*.yaml",
        "**/*.json",
        "**/*.toml",
        # Review output (BRC concurrent mode)
        ".egg-state/reviews/",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Documentation (Documenter handles)
        "docs/",
        "**/README.md",
        "**/*.md",
        # Contracts (API only)
        ".egg-state/contracts/",
    ],
)


# Registry of all agent patterns
AGENT_PATTERNS: dict[str, AgentFilePattern] = {
    AgentRole.CODER: CODER_PATTERNS,
    AgentRole.TESTER: TESTER_PATTERNS,
    AgentRole.DOCUMENTER: DOCUMENTER_PATTERNS,
    AgentRole.ARCHITECT: ARCHITECT_PATTERNS,
    AgentRole.TASK_PLANNER: TASK_PLANNER_PATTERNS,
    AgentRole.RISK_ANALYST: RISK_ANALYST_PATTERNS,
    AgentRole.REVIEWER_CODE: REVIEWER_CODE_PATTERNS,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_PATTERNS,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_PATTERNS,
    AgentRole.REFINER: REFINER_PATTERNS,
    AgentRole.REVIEWER_REFINE: REVIEWER_REFINE_PATTERNS,
    AgentRole.REVIEWER_PLAN: REVIEWER_PLAN_PATTERNS,
    AgentRole.REVIEWER_UNIFIED: REVIEWER_UNIFIED_PATTERNS,
    AgentRole.CHECKER: CHECKER_PATTERNS,
}


def get_agent_pattern(role: str) -> AgentFilePattern | None:
    """Get the file pattern for an agent role.

    Args:
        role: The agent role identifier

    Returns:
        AgentFilePattern for the role, or None if not found
    """
    return AGENT_PATTERNS.get(role.lower())


def check_agent_file_access(
    role: str,
    files: list[str],
) -> tuple[bool, list[str], str]:
    """Check if an agent can modify the given files.

    Args:
        role: The agent role identifier
        files: List of file paths being modified

    Returns:
        Tuple of (allowed, blocked_files, reason)
    """
    pattern = get_agent_pattern(role)
    if pattern is None:
        # Unknown role - allow for backwards compatibility
        return True, [], f"Unknown agent role: {role}"

    blocked_files = []
    for file_path in files:
        if not pattern.can_write(file_path):
            blocked_files.append(file_path)

    if blocked_files:
        return (
            False,
            blocked_files,
            f"Agent role '{role}' cannot modify: {', '.join(blocked_files[:5])}"
            + (f" and {len(blocked_files) - 5} more" if len(blocked_files) > 5 else ""),
        )

    return True, [], "All files allowed for agent role"


@dataclass
class AgentRestrictionResult:
    """Result of checking agent file restrictions."""

    allowed: bool
    message: str
    role: str
    blocked_files: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls, role: str, message: str = "Files allowed") -> AgentRestrictionResult:
        """Create an allowed result."""
        return cls(allowed=True, message=message, role=role)

    @classmethod
    def block(
        cls,
        role: str,
        blocked_files: list[str],
        message: str,
    ) -> AgentRestrictionResult:
        """Create a blocked result."""
        return cls(
            allowed=False,
            message=message,
            role=role,
            blocked_files=blocked_files,
        )


def validate_agent_push(
    role: str,
    files: list[str],
) -> AgentRestrictionResult:
    """Validate that an agent can push changes to the given files.

    This is the main entry point for gateway validation of agent pushes.

    Args:
        role: The agent role identifier (e.g., "coder", "tester")
        files: List of file paths being modified in the push

    Returns:
        AgentRestrictionResult indicating whether the push is allowed
    """
    if not role:
        return AgentRestrictionResult.allow("", "No agent role specified")

    if not files:
        return AgentRestrictionResult.allow(role, "No files to validate")

    allowed, blocked_files, reason = check_agent_file_access(role, files)

    if allowed:
        return AgentRestrictionResult.allow(role, reason)
    else:
        return AgentRestrictionResult.block(role, blocked_files, reason)


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
        AgentRole.CHECKER,
        AgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.REFINER,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_PLAN,
        AgentRole.REVIEWER_UNIFIED,
    ]
}


def check_agent_gh_operation(role: str, command: str) -> tuple[bool, str]:
    """Check if an agent role is allowed to execute a gh command.

    Args:
        role: The agent role identifier (e.g., "coder", "reviewer_refine")
        command: The gh command string (e.g., "issue comment 1032")

    Returns:
        Tuple of (allowed, reason). allowed is False if blocked.
    """
    if not role:
        return True, "No agent role specified"

    role_lower = role.lower()
    restriction = AGENT_GH_RESTRICTIONS.get(role_lower)
    if restriction is None:
        # Unknown role - allow for backwards compatibility
        return True, f"Unknown agent role: {role}"

    if restriction.is_blocked(command):
        return False, (
            f"Agent role '{role}' is not allowed to execute 'gh {command}'. "
            f"Write reviews to .egg-state/reviews/ instead."
        )

    return True, f"Operation allowed for agent role '{role}'"
