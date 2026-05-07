"""
Agent-role-based file restriction patterns.

Defines which files each agent role can read and write to. These patterns
are used by both the gateway (for git push validation) and other components
that need to enforce agent file access boundaries.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

# Re-export the canonical AgentRole StrEnum from egg_contracts so the
# gateway sees a single source of truth — adding a role in egg_contracts
# is now automatically visible here, removing the silent-drift failure
# mode from #2066.
from egg_contracts.agent_roles import AgentRole

from .matchers import match_pattern

__all__ = ["AGENT_PATTERNS", "AgentFilePattern", "AgentRole"]


@dataclass
class AgentFilePattern:
    """File access pattern for an agent role.

    Defines which files an agent can write to. The gateway enforces these
    restrictions during git push operations.
    """

    role: str
    allowed_patterns: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    block_exempt_patterns: list[str] = field(default_factory=list)
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

            However, block_exempt_patterns can carve out narrow exceptions
            from blocked patterns. For example, ``**/*.md`` is blocked for
            coders (documentation), but ``.md`` files in agent-config
            directories (rules, skills, commands) are functional code and
            are exempted via block_exempt_patterns.
        """
        normalized = self._normalize_path(file_path)

        # Reject invalid paths (e.g., path traversal attempts)
        if normalized == "__INVALID_PATH_TRAVERSAL__":
            return False

        # Check blocked patterns FIRST - security takes precedence
        # BUT skip the block if the path matches a block-exemption pattern.
        if any(match_pattern(normalized, p) for p in self.blocked_patterns):
            if not any(match_pattern(normalized, p) for p in self.block_exempt_patterns):
                return False

        # If no allowed patterns, nothing is allowed
        if not self.allowed_patterns:
            return False

        return any(match_pattern(normalized, p) for p in self.allowed_patterns)

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


# Default agent file patterns
# These define what each agent role can and cannot modify.
#
# This module is the single source of truth for per-role file boundaries
# (#1903). The gateway's PhaseFilter and the sandbox container's readonly
# mounts derive from here; do not duplicate these patterns elsewhere.
CODER_PATTERNS = AgentFilePattern(
    role=AgentRole.CODER,
    description=(
        "everything except tester's test scope, documenter's "
        "docs/markdown scope, and the pipeline-state .egg-state/ "
        "directory (agent-outputs/ carved back)"
    ),
    # Catch-all allow list — coder owns every file that is NOT carved out
    # by the blocklist below. This replaces the legacy extension-based
    # allowlist so extensionless scripts (bin/egg, sandbox/egg, LICENSE,
    # .dockerignore, etc.) and future file types no longer need the
    # allowlist to be edited.  See #1901.
    allowed_patterns=["**"],
    blocked_patterns=[
        # Pipeline state (catch-all for every current and future subdir;
        # .egg-state/agent-outputs/ is carved back via block_exempt_patterns)
        ".egg-state/",
        # Documenter scope
        "docs/",
        "**/*.md",
        "**/README.md",
        # Tester scope — directory patterns
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
        # Tester scope — file-name patterns (fnmatch does NOT support
        # brace expansion, so each language/suffix is spelled out)
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
        "**/conftest.py",
        # Defense-in-depth: CI workflows and CODEOWNERS — preserves the
        # branch-protection invariant. Agents that need to propose
        # `.github/` changes (CI workflow edits, CODEOWNERS rotation)
        # write the proposed end-state to top-level `.github-staging/`
        # mirroring the `.github/` structure; the prefix-match below
        # leaves `.github-staging/` allowed via the `**` allowlist, and
        # `_build_pr_body` auto-emits a manual step for the human
        # reviewer to move the files into `.github/` before merge
        # (issue #2508).
        ".github/",
    ],
    block_exempt_patterns=[
        # Coder's handoff directory — the only .egg-state/ subdir the coder
        # owns.
        ".egg-state/agent-outputs/",
        # Per-agent anchor files. The gateway's `check_anchor_write_permission`
        # (gateway/phase_filter.py::check_anchor_write_permission) enforces
        # per-agent scoping — an agent may only write its own
        # `.egg-state/agent-anchors/<AGENT_ANCHOR_ID>.json`. That downstream
        # guard can only run if the role-level check lets the path through,
        # so this exemption restores the pre-#1901 behavior where the legacy
        # `**/*.json` allowlist matched anchor files. The contract task
        # TASK-1-1 for #1901 did not enumerate this exemption — the gap was
        # surfaced by tester's pre-existing test_push_*_anchor_write tests
        # against the new blocklist-complement. See #1901 NACK discussion.
        ".egg-state/agent-anchors/",
        # Agent config .md files are functional code (rules, skills, commands),
        # not documentation. See #1537. Paths are specific to avoid bypassing
        # other blocked patterns (docs/, tests/, .egg-state/contracts/).
        "sandbox/agent-config/rules/*.md",
        "sandbox/agent-config/commands/*.md",
        # Top-level skills directory (skill definitions are functional code)
        "skills/",
    ],
)

TESTER_PATTERNS = AgentFilePattern(
    role=AgentRole.TESTER,
    description="test files and pytest infrastructure only",
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
    description="documentation and markdown files",
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
    description="drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=_PLAN_AGENT_BLOCKED,
)

TASK_PLANNER_PATTERNS = AgentFilePattern(
    role=AgentRole.TASK_PLANNER,
    description="drafts and agent-outputs only",
    allowed_patterns=[
        ".egg-state/drafts/",
        ".egg-state/agent-outputs/",
    ],
    blocked_patterns=_PLAN_AGENT_BLOCKED,
)

RISK_ANALYST_PATTERNS = AgentFilePattern(
    role=AgentRole.RISK_ANALYST,
    description="drafts and agent-outputs only",
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
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_CODE_HOLISTIC_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_CODE_HOLISTIC,
    description="reviews and agent-outputs only",
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
    description="reviews, agent-outputs, and contracts",
    allowed_patterns=_REVIEWER_CONTRACT_ALLOWED,
    blocked_patterns=_REVIEWER_CONTRACT_BLOCKED,
)

REVIEWER_AGENT_DESIGN_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_AGENT_DESIGN,
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

# Refine-phase agent patterns

REFINER_PATTERNS = AgentFilePattern(
    role=AgentRole.REFINER,
    description="drafts and agent-outputs only",
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
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_PLAN_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_PLAN,
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_SECURITY_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_SECURITY,
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

REVIEWER_CONCURRENCY_PATTERNS = AgentFilePattern(
    role=AgentRole.REVIEWER_CONCURRENCY,
    description="reviews and agent-outputs only",
    allowed_patterns=_REVIEWER_ALLOWED,
    blocked_patterns=_REVIEWER_BLOCKED,
)

# Overseer agent pattern
# The overseer monitors pipeline health but cannot modify source, test, doc,
# or config files. It can only write to .egg-state/oversight/ for structured logs.

OVERSEER_PATTERNS = AgentFilePattern(
    role=AgentRole.OVERSEER,
    description="oversight logs only, no source/test/doc/config access",
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
    description="source code and config files for automated fixes",
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
    description="source, test, docs, and config files",
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
    description="agent-outputs only, no source/test/doc/config access",
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
    AgentRole.REVIEWER_CODE_HOLISTIC: REVIEWER_CODE_HOLISTIC_PATTERNS,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_PATTERNS,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_PATTERNS,
    AgentRole.REFINER: REFINER_PATTERNS,
    AgentRole.REVIEWER_REFINE: REVIEWER_REFINE_PATTERNS,
    AgentRole.REVIEWER_PLAN: REVIEWER_PLAN_PATTERNS,
    AgentRole.REVIEWER_SECURITY: REVIEWER_SECURITY_PATTERNS,
    AgentRole.REVIEWER_CONCURRENCY: REVIEWER_CONCURRENCY_PATTERNS,
    AgentRole.OVERSEER: OVERSEER_PATTERNS,
    AgentRole.AUTOFIXER: AUTOFIXER_PATTERNS,
    AgentRole.CONFLICT_RESOLVER: CONFLICT_RESOLVER_PATTERNS,
    AgentRole.INSPECTOR: INSPECTOR_PATTERNS,
}
