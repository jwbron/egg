"""
Agent-role-based file restriction patterns.

Defines which files each agent role can read and write to. These patterns
are used by both the gateway (for git push validation) and other components
that need to enforce agent file access boundaries.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field


class AgentRole:
    """Agent role identifiers.

    Note: Mirrors egg_contracts.agent_roles.AgentRole to avoid import
    complexity in the gateway module. Values must be kept in sync.
    """

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    # Analysis roles
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    REFINER = "refiner"
    # Review roles
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    REVIEWER_PLAN = "reviewer_plan"
    # Utility roles
    AUTOFIXER = "autofixer"
    CONFLICT_RESOLVER = "conflict_resolver"
    # Interface roles
    OVERSEER = "overseer"
    INSPECTOR = "inspector"


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
        # Build/config files (extensionless or uncommon extensions)
        "Makefile",
        "**/Makefile",
        "Dockerfile",
        "**/Dockerfile",
        "Procfile",
        ".python-version",
        ".node-version",
        ".nvmrc",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        # Lock files (dependency management)
        "**/*.lock",
        # Requirements files
        "**/requirements*.txt",
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
        # Test files (Tester handles)
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
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
        # Pytest infrastructure (Tester handles)
        "**/conftest.py",
    ],
)

TESTER_PATTERNS = AgentFilePattern(
    role=AgentRole.TESTER,
    description="Tester agent: test files and pytest infrastructure only",
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
        # Pytest infrastructure
        "**/conftest.py",
        # Config files needed for test environment setup
        ".python-version",
        # Dependency files (test dependency management)
        "**/*.lock",
        "**/requirements*.txt",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Documentation (Documenter handles)
        "docs/",
        "**/README.md",
        "**/*.md",
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

# Contract reviewer needs write access to .egg-state/contracts/ to mark
# items as done, so it uses custom lists that include/exclude contracts.
_REVIEWER_CONTRACT_ALLOWED = [
    ".egg-state/reviews/",
    ".egg-state/agent-outputs/",
    ".egg-state/contracts/",
]

_REVIEWER_CONTRACT_BLOCKED = [
    "src/",
    "lib/",
    "shared/",
    "gateway/",
    "sandbox/",
    "action/",
    "docs/",
    "tests/",
    "test/",
    ".egg-state/drafts/",
    ".github/",
]

REVIEWER_CONTRACT_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_CONTRACT,
    description="Contract reviewer agent: reviews, agent-outputs, and contracts",
    allowed_patterns=_REVIEWER_CONTRACT_ALLOWED,
    blocked_patterns=_REVIEWER_CONTRACT_BLOCKED,
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

# Overseer agent pattern
# The overseer monitors pipeline health but cannot modify source, test, doc,
# or config files. It can only write to .egg-state/oversight/ for structured logs.

OVERSEER_PATTERNS = AgentFilePattern(
    role=AgentRole.OVERSEER,
    description="Overseer agent: oversight logs only, no source/test/doc/config access",
    allowed_patterns=[
        ".egg-state/oversight/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        "src/",
        "lib/",
        "shared/",
        "gateway/",
        "sandbox/",
        "action/",
        "orchestrator/",
        "docs/",
        "tests/",
        "test/",
        ".egg-state/contracts/",
        ".egg-state/drafts/",
        ".egg-state/reviews/",
        ".github/",
    ],
)


# Autofixer agent pattern
# The autofixer applies automated lint/type-check/formatting fixes to source
# and config files.  It cannot modify docs or contracts.

AUTOFIXER_PATTERNS = AgentFilePattern(
    role=AgentRole.AUTOFIXER,
    description="Autofixer agent: source code and config files for automated fixes",
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
        # Build/config files (extensionless)
        "Makefile",
        "**/Makefile",
        "Dockerfile",
        "**/Dockerfile",
        ".python-version",
        ".node-version",
        ".nvmrc",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        # Lock files
        "**/*.lock",
        "**/requirements*.txt",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Documentation
        "docs/",
        "**/*.md",
        # Contracts
        ".egg-state/contracts/",
    ],
)

# Conflict resolver agent pattern
# The conflict resolver can modify source, test, docs, and config files to
# resolve merge conflicts.  It cannot modify pipeline state directories.

CONFLICT_RESOLVER_PATTERNS = AgentFilePattern(
    role=AgentRole.CONFLICT_RESOLVER,
    description="Conflict resolver agent: source, test, docs, and config files",
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
        # Tests
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
        # Documentation
        "docs/",
        "**/*.md",
        # Configuration
        "**/*.yml",
        "**/*.yaml",
        "**/*.json",
        "**/*.toml",
        # Build/config files (extensionless)
        "Makefile",
        "**/Makefile",
        "Dockerfile",
        "**/Dockerfile",
        "Procfile",
        ".python-version",
        ".node-version",
        ".nvmrc",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        # Lock files
        "**/*.lock",
        "**/requirements*.txt",
        # Handoff output
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        # Pipeline state (specific subdirs, excluding agent-outputs)
        ".egg-state/contracts/",
        ".egg-state/drafts/",
        ".egg-state/pipelines/",
        ".egg-state/reviews/",
        ".egg-state/oversight/",
    ],
)

# Inspector agent pattern
# Inspectors run diagnostic scripts and write only to agent-outputs.

INSPECTOR_PATTERNS = AgentFilePattern(
    role=AgentRole.INSPECTOR,
    description="Inspector agent: agent-outputs only, no source/test/doc/config access",
    allowed_patterns=[
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=[
        "src/",
        "lib/",
        "shared/",
        "gateway/",
        "sandbox/",
        "action/",
        "orchestrator/",
        "docs/",
        "tests/",
        "test/",
        ".egg-state/contracts/",
        ".egg-state/drafts/",
        ".egg-state/reviews/",
        ".github/",
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
    AgentRole.OVERSEER: OVERSEER_PATTERNS,
    AgentRole.AUTOFIXER: AUTOFIXER_PATTERNS,
    AgentRole.CONFLICT_RESOLVER: CONFLICT_RESOLVER_PATTERNS,
    AgentRole.INSPECTOR: INSPECTOR_PATTERNS,
}
