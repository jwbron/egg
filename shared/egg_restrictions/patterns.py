"""
Agent-role-based file restriction patterns.

Defines which files each agent role can read and write to. These patterns
are used by both the gateway (for git push validation) and other components
that need to enforce agent file access boundaries.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field

# Re-export the canonical AgentRole StrEnum from egg_contracts so the
# gateway sees a single source of truth — adding a role in egg_contracts
# is now automatically visible here, removing the silent-drift failure
# mode from #2066.
from egg_contracts.agent_roles import AgentRole

from .matchers import match_pattern

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_PATTERNS",
    "AgentFilePattern",
    "AgentRole",
    "DEFAULT_CODE_GLOBS",
    "DEFAULT_DOCS_GLOBS",
    "DEFAULT_TESTS_GLOBS",
    "build_agent_patterns",
    "get_agent_pattern_for_repo",
    "get_agent_patterns_for_repo",
    "load_repo_pattern_override",
    "reset_pattern_cache",
]


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
#
# Per-repo overrides (#2528): the three test/code/docs glob lists below
# are the load-bearing language conventions. Repos can override them via
# ``role_patterns:`` in ``repositories.yaml`` so non-Python conventions
# (Go ``*_test.go``, JS ``__tests__/``, etc.) get correct role boundaries.
# Security-relevant blocklists (``.egg-state/contracts/``, ``.github/``)
# are NOT overridable — they enforce the policy boundary independent of
# repo-specific conventions.

# Default test-file conventions: directory patterns + file-name patterns.
# fnmatch does NOT support brace expansion, so each suffix is spelled out.
DEFAULT_TESTS_GLOBS: list[str] = [
    # Test directories
    "tests/",
    "test/",
    "**/tests/",
    "**/test/",
    # Python
    "**/*_test.py",
    "**/test_*.py",
    "**/conftest.py",
    # Go
    "**/*_test.go",
    "**/test_*.go",
    # JS/TS
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
]

# Default production-code conventions (used by tester's auto-fix allow
# list and documenter's blocklist).
DEFAULT_CODE_GLOBS: list[str] = [
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
]

# Default documentation conventions.
DEFAULT_DOCS_GLOBS: list[str] = [
    "docs/",
    "**/*.md",
    "**/README.md",
]


def _build_coder_pattern(
    *,
    tests_globs: list[str],
    docs_globs: list[str],
) -> AgentFilePattern:
    """Build the coder role's file pattern.

    Coder owns every file that is NOT carved out by the blocklist (the
    catch-all ``**`` allow). The per-repo knobs widen the test/docs
    blocks so non-Python repos route those files to the appropriate
    role.
    """
    return AgentFilePattern(
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
            *docs_globs,
            # Tester scope
            *tests_globs,
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


def _build_tester_pattern(
    *,
    tests_globs: list[str],
    code_globs: list[str],  # noqa: ARG001 — kept for signature parity with other builders
    docs_globs: list[str],
) -> AgentFilePattern:
    """Build the tester role's file pattern.

    Tester writes test files only — source-code edits are the coder's
    or autofixer's job. Per-repo knobs change the test-file allowlist
    + the docs blocklist.
    """
    return AgentFilePattern(
        role=AgentRole.TESTER,
        description="test files and pytest infrastructure only",
        allowed_patterns=[
            *tests_globs,
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
            *docs_globs,
            # Contracts
            ".egg-state/contracts/",
            # Issue #2521: parity with TESTER_ROLE.blocked_write — see CODER_PATTERNS for rationale.
            ".github/",
        ],
    )


def _build_documenter_pattern(
    *,
    tests_globs: list[str],
    code_globs: list[str],
    docs_globs: list[str],
) -> AgentFilePattern:
    """Build the documenter role's file pattern.

    Documenter writes only docs. Per-repo knobs change the docs allow
    list + the test/code blocklist.
    """
    return AgentFilePattern(
        role=AgentRole.DOCUMENTER,
        description="documentation and markdown files",
        allowed_patterns=[
            *docs_globs,
            # Handoff output
            ".egg-state/agent-outputs/",
        ],
        blocked_patterns=[
            # Source code (Coder handles)
            *code_globs,
            # Test files (Tester handles)
            *tests_globs,
            # Contracts
            ".egg-state/contracts/",
            # Issue #2508: branch-protection invariant — even markdown
            # files under `.github/` (PULL_REQUEST_TEMPLATE.md,
            # ISSUE_TEMPLATE.md, etc.) must go through the
            # `.github-staging/` convention so a human reviewer moves them
            # in before merge. Without this block, `**/*.md` would let the
            # documenter rewrite `.github/PULL_REQUEST_TEMPLATE.md`.
            ".github/",
        ],
    )


CODER_PATTERNS = _build_coder_pattern(
    tests_globs=DEFAULT_TESTS_GLOBS,
    docs_globs=DEFAULT_DOCS_GLOBS,
)

TESTER_PATTERNS = _build_tester_pattern(
    tests_globs=DEFAULT_TESTS_GLOBS,
    code_globs=DEFAULT_CODE_GLOBS,
    docs_globs=DEFAULT_DOCS_GLOBS,
)

DOCUMENTER_PATTERNS = _build_documenter_pattern(
    tests_globs=DEFAULT_TESTS_GLOBS,
    code_globs=DEFAULT_CODE_GLOBS,
    docs_globs=DEFAULT_DOCS_GLOBS,
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


def _build_autofixer_pattern(
    *,
    code_globs: list[str],
    docs_globs: list[str],
) -> AgentFilePattern:
    """Build the autofixer role's file pattern.

    Autofixer writes source + config (lint/type-check fixes) and never
    docs. Per-repo ``code_globs`` widens the allow list; ``docs_globs``
    widens the docs block.
    """
    return AgentFilePattern(
        role=AgentRole.AUTOFIXER,
        description="source code and config files for automated fixes",
        allowed_patterns=[
            *code_globs,
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
            *docs_globs,
            # Contracts
            ".egg-state/contracts/",
            # Issue #2508: branch-protection invariant — even an autofixer
            # YAML lint fix to `.github/workflows/*.yml` must go through
            # the `.github-staging/` convention so a human reviewer moves
            # it in before merge. Without this block, `**/*.yml` would let
            # the autofixer rewrite workflows directly.
            ".github/",
        ],
    )


# Conflict resolver agent pattern
# The conflict resolver can modify source, test, docs, and config files to
# resolve merge conflicts.  It cannot modify pipeline state directories.


def _build_conflict_resolver_pattern(
    *,
    tests_globs: list[str],
    code_globs: list[str],
    docs_globs: list[str],
) -> AgentFilePattern:
    """Build the conflict-resolver role's file pattern.

    Conflict resolver writes source, tests, and docs (any file may be in
    a merge conflict). Per-repo knobs widen all three allow lists.
    """
    return AgentFilePattern(
        role=AgentRole.CONFLICT_RESOLVER,
        description="source, test, docs, and config files",
        allowed_patterns=[
            *code_globs,
            *tests_globs,
            *docs_globs,
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
            # Issue #2508: branch-protection invariant — when resolving a
            # merge conflict that touches a workflow / CODEOWNERS file,
            # the resolution must go through the `.github-staging/`
            # convention so a human reviewer moves it in before merge.
            ".github/",
        ],
    )


AUTOFIXER_PATTERNS = _build_autofixer_pattern(
    code_globs=DEFAULT_CODE_GLOBS,
    docs_globs=DEFAULT_DOCS_GLOBS,
)

CONFLICT_RESOLVER_PATTERNS = _build_conflict_resolver_pattern(
    tests_globs=DEFAULT_TESTS_GLOBS,
    code_globs=DEFAULT_CODE_GLOBS,
    docs_globs=DEFAULT_DOCS_GLOBS,
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


# ---------------------------------------------------------------------------
# #2528 — per-repo role-pattern overrides
# ---------------------------------------------------------------------------
#
# Repos can override the language conventions in ``repositories.yaml`` via
# a ``role_patterns:`` block (parsed in ``config/repo_config.py``). Only
# the three glob lists are configurable; security-relevant blocklists
# (``.egg-state/contracts/``, ``.github/``, draft/review state dirs) are
# fixed and cannot be relaxed by a target repo.
#
# Default callers don't need to change: ``AGENT_PATTERNS`` keeps its
# legacy global-default shape (= ``build_agent_patterns(None)``).
# Callers that have a repo identifier in scope (gateway push handler,
# tool interceptor, plan-time validator, planner prompt) can opt into
# per-repo patterns via ``get_agent_pattern_for_repo(role, repo)``.


def build_agent_patterns(
    repo: str | None = None,
    *,
    tests_globs: list[str] | None = None,
    code_globs: list[str] | None = None,
    docs_globs: list[str] | None = None,
) -> dict[str, AgentFilePattern]:
    """Build the per-role pattern registry for a repo.

    When ``repo`` is provided and the optional glob arguments are not,
    this function reads ``repositories.yaml`` via
    ``config.repo_config.get_repo_role_patterns(repo)`` and substitutes
    only the keys the repo configured. Unset keys fall back to the
    ``DEFAULT_*_GLOBS`` constants. When all three glob arguments are
    None (and the repo has no override), the result is identical to the
    module-level ``AGENT_PATTERNS`` registry.

    The explicit ``tests_globs`` / ``code_globs`` / ``docs_globs``
    keyword arguments exist so unit tests can build a registry without
    requiring a ``repositories.yaml`` lookup; production callers should
    only pass ``repo``.

    Args:
        repo: Repository in ``owner/repo`` format. ``None`` means
            global defaults.
        tests_globs: Override for the test-file convention. Falls back
            to repo config or ``DEFAULT_TESTS_GLOBS``.
        code_globs: Override for the production-code convention.
        docs_globs: Override for the documentation convention.

    Returns:
        A fresh dict mapping role name to ``AgentFilePattern``.

    Security note:
        Security-relevant blocklists (``.egg-state/contracts/``,
        ``.github/``, etc.) are sourced from this module's hard-coded
        builders and CANNOT be overridden by a repo's config. The
        per-repo knobs only widen the language-convention lists.
    """
    if repo is not None and tests_globs is None and code_globs is None and docs_globs is None:
        override = load_repo_pattern_override(repo)
        if override:
            tests_globs = override.get("tests_globs")
            code_globs = override.get("code_globs")
            docs_globs = override.get("docs_globs")

    final_tests = list(tests_globs) if tests_globs is not None else DEFAULT_TESTS_GLOBS
    final_code = list(code_globs) if code_globs is not None else DEFAULT_CODE_GLOBS
    final_docs = list(docs_globs) if docs_globs is not None else DEFAULT_DOCS_GLOBS

    coder = _build_coder_pattern(tests_globs=final_tests, docs_globs=final_docs)
    tester = _build_tester_pattern(
        tests_globs=final_tests, code_globs=final_code, docs_globs=final_docs
    )
    documenter = _build_documenter_pattern(
        tests_globs=final_tests, code_globs=final_code, docs_globs=final_docs
    )
    autofixer = _build_autofixer_pattern(code_globs=final_code, docs_globs=final_docs)
    conflict_resolver = _build_conflict_resolver_pattern(
        tests_globs=final_tests, code_globs=final_code, docs_globs=final_docs
    )

    return {
        AgentRole.CODER: coder,
        AgentRole.TESTER: tester,
        AgentRole.DOCUMENTER: documenter,
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
        AgentRole.AUTOFIXER: autofixer,
        AgentRole.CONFLICT_RESOLVER: conflict_resolver,
        AgentRole.INSPECTOR: INSPECTOR_PATTERNS,
    }


def load_repo_pattern_override(repo: str) -> dict[str, list[str]] | None:
    """Resolve the per-repo role-pattern override for ``repo``.

    Production runtimes have three different shapes for reading the
    override:

    1. **Sandbox** — has no access to ``repositories.yaml``. The
       orchestrator pre-resolves the override at spawn time and passes
       it via the ``EGG_PIPELINE_REPO_PATTERNS_JSON`` env var (a JSON
       object whose top-level keys are repos). This is checked first.
    2. **Gateway** — copies ``config/repo_config.py`` to ``/app/`` (top
       level) and to ``/config/`` (no ``__init__.py``). Only the
       top-level layout is on ``PYTHONPATH``, so the import has to fall
       back from ``config.repo_config`` to ``repo_config``.
    3. **Orchestrator** — copies ``config/repo_config.py`` to its
       working directory as ``repo_config.py`` (top level). Same
       fallback path as the gateway.

    The two-step import mirrors the existing
    ``gateway.gateway._reload_all_config`` pattern. Failures are
    swallowed (the feature degrades to defaults) but logged at DEBUG so
    operators can correlate a missing override with the import path.
    """
    env_blob = os.environ.get("EGG_PIPELINE_REPO_PATTERNS_JSON")
    if env_blob:
        try:
            decoded = json.loads(env_blob)
        except json.JSONDecodeError:
            logger.warning(
                "EGG_PIPELINE_REPO_PATTERNS_JSON is not valid JSON; ignoring",
                extra={"repo": repo},
            )
            decoded = None
        if isinstance(decoded, dict):
            entry = decoded.get(repo)
            if isinstance(entry, dict):
                cleaned: dict[str, list[str]] = {}
                for key in ("tests_globs", "code_globs", "docs_globs"):
                    value = entry.get(key)
                    if isinstance(value, list):
                        only_strings = [v for v in value if isinstance(v, str) and v]
                        if only_strings:
                            cleaned[key] = only_strings
                if cleaned:
                    return cleaned

    _loader: Callable[[str], dict[str, list[str]] | None] | None = None
    try:
        from config.repo_config import get_repo_role_patterns as _loader
    except ImportError:
        try:
            from repo_config import get_repo_role_patterns as _loader  # type: ignore[no-redef]
        except ImportError:
            logger.debug(
                "repo_config not importable; per-repo overrides disabled",
                extra={"repo": repo},
            )
            return None

    assert _loader is not None
    try:
        override = _loader(repo)
    except FileNotFoundError:
        # No repositories.yaml mounted — expected outside the gateway.
        return None
    except Exception:
        logger.exception(
            "Failed to load per-repo role-pattern override; using defaults",
            extra={"repo": repo},
        )
        return None
    return override or None


def _normalize_repo_key(repo: str | None) -> str | None:
    """Normalize the repo cache key so ``Owner/Repo`` and ``owner/repo``
    share one entry. Mirrors the case-insensitive lookup in
    ``config.repo_config.get_repo_setting``.
    """
    if repo is None:
        return None
    return repo.lower()


# Per-repo cache. The build is cheap but called on every push, plan-time
# validation, and tool-interceptor write check; memoizing avoids redoing
# the YAML read per-call. Reset via ``reset_pattern_cache`` (wired into
# the gateway's existing config-reload path).
_repo_pattern_cache: dict[str | None, dict[str, AgentFilePattern]] = {None: AGENT_PATTERNS}


def get_agent_pattern_for_repo(role: str, repo: str | None = None) -> AgentFilePattern | None:
    """Look up the role pattern for a given repo, or ``None`` if the
    role is not defined.

    Equivalent to ``AGENT_PATTERNS.get(role)`` when ``repo`` is None or
    no override exists. Memoizes per repo (case-insensitive).
    """
    key = _normalize_repo_key(repo)
    cached = _repo_pattern_cache.get(key)
    if cached is None:
        cached = build_agent_patterns(repo)
        _repo_pattern_cache[key] = cached
    return cached.get(role)


def get_agent_patterns_for_repo(repo: str | None = None) -> dict[str, AgentFilePattern]:
    """Return the full per-role pattern registry for ``repo`` (memoized).

    Used by callers that need to scan every role (e.g. the tool
    interceptor's ``_find_owning_role``); cheaper than calling
    ``build_agent_patterns`` once per call site since the cache is
    shared with ``get_agent_pattern_for_repo``.
    """
    key = _normalize_repo_key(repo)
    cached = _repo_pattern_cache.get(key)
    if cached is None:
        cached = build_agent_patterns(repo)
        _repo_pattern_cache[key] = cached
    return cached


def reset_pattern_cache() -> None:
    """Clear the per-repo pattern cache.

    Called by ``config.repo_config.reload_config`` so a SIGHUP /
    ``/api/v1/config/reload`` picks up edited ``role_patterns:``
    overrides without restarting the gateway.

    Atomic rebind avoids a race where a concurrent reader sees the
    cache mid-clear and rebuilds the ``None`` entry as a fresh dict
    (functionally identical but a different object than
    ``AGENT_PATTERNS`` — which the parity tests assert against).
    """
    global _repo_pattern_cache
    _repo_pattern_cache = {None: AGENT_PATTERNS}
