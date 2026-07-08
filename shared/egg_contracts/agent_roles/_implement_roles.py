"""Implement-phase agent role definitions (#3543 decomposition).

Execution producers (coder, tester, documenter) and the implement-phase
reviewer roles (code, code-holistic, contract, security, concurrency).
``REVIEWER_CONTRACT_ROLE`` also reviews the apply phase.
"""

from ._core import (
    _REVIEWER_BLOCKED_WRITE,
    AgentCategory,
    AgentRole,
    AgentRoleDefinition,
    FileAccessPattern,
)

# Default agent role definitions
# These define the standard agent roles used in multi-agent orchestration

CODER_ROLE = AgentRoleDefinition(
    role=AgentRole.CODER,
    description="Implements code changes based on the plan tasks",
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read and understand the implementation plan",
        "Implement code changes for assigned tasks",
        "Link commits to contract tasks",
        "Output list of changed files for downstream agents",
    ],
    dependencies=[],  # Coder runs first, no dependencies
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
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
            "**/*.yml",
            "**/*.yaml",
            "**/*.json",
            ".egg-state/agent-outputs/",  # For handoff data
            # Issue #2508: staging dir for proposed `.github/` changes.
            # `.github/` itself is blocked below; the agent stages the
            # proposed end-state here and calls the staged files out
            # in its PR body so the human reviewer moves them into
            # `.github/` before merge.
            ".github-staging/",
        ],
        blocked_write=[
            "docs/",  # Documenter handles docs
            "**/README.md",  # Documenter handles READMEs
            ".egg-state/contracts/",  # Contracts are managed by API
            # Issue #2508: branch-protection invariant — agents cannot
            # push to `.github/` directly. Use `.github-staging/` (above)
            # to propose CI workflow / CODEOWNERS changes for human
            # review.
            ".github/",
        ],
    ),
    produces_outputs=["changed_files", "commits"],
    requires_inputs=[],
)

TESTER_ROLE = AgentRoleDefinition(
    role=AgentRole.TESTER,
    description="Validates implementation by writing tests, running lint/type-checks, and applying auto-fixes",
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read the list of changed files from coder",
        "Identify gaps in the implementation (missing error handling, boundary conditions, uncovered branches)",
        "Write or update tests targeting identified gaps",
        "Run linters and type checkers, apply auto-fixes where possible",
        "Report test coverage, lint/type-check results, and document deficiencies found",
    ],
    dependencies=[AgentRole.CODER],  # Must wait for coder
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            # Test files
            "tests/",
            "test/",
            "**/tests/",
            "**/test/",
            "**/*_test.py",
            "**/*_test.go",
            "**/test_*.py",
            "**/*.test.ts",
            "**/*.test.tsx",
            "**/*.test.js",
            "**/*.test.jsx",
            "**/*.spec.ts",
            "**/*.spec.tsx",
            "**/*.spec.js",
            "**/*.spec.jsx",
            # Source files (for lint/type-check auto-fixes)
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
            # Configuration (for auto-fix updates)
            "**/*.yml",
            "**/*.yaml",
            "**/*.json",
            "**/*.toml",
            ".egg-state/agent-outputs/",  # For handoff data
        ],
        blocked_write=[
            "docs/",
            "**/README.md",
            "**/*.md",
            ".egg-state/contracts/",
            # Issue #2508: branch-protection invariant — even a
            # tester-applied auto-fix to `.github/workflows/*.yml` must
            # go through the `.github-staging/` convention so a human
            # reviewer moves it in before merge.
            ".github/",
        ],
    ),
    produces_outputs=["test_files", "coverage_report", "gaps_found", "check_results"],
    requires_inputs=["changed_files"],
)

DOCUMENTER_ROLE = AgentRoleDefinition(
    role=AgentRole.DOCUMENTER,
    description="Documents the current state of the code",
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read the list of changed files from coder",
        "Describe how the code works now — a snapshot of the current "
        "state, not a log of what changed or when",
        "Never embed SDLC artifacts (slice numbers, TASK-N ids, phase or "
        "HITL iteration numbers) in docs, docstrings, or comments",
        "Prefer rationale (why it is this way) over chronology; fold new "
        "state into the snapshot and remove now-stale ledger entries",
        "Keep README and API documentation current",
    ],
    dependencies=[AgentRole.CODER],  # Must wait for coder
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            "docs/",
            "**/README.md",
            "**/*.md",  # All markdown files
            ".egg-state/agent-outputs/",  # For handoff data
        ],
        blocked_write=[
            "**/*.py",  # Cannot modify code
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.java",
            "tests/",  # Cannot modify tests
            ".egg-state/contracts/",
            # Issue #2508: branch-protection invariant — even markdown
            # files under `.github/` (PULL_REQUEST_TEMPLATE.md,
            # ISSUE_TEMPLATE.md, etc.) must go through the
            # `.github-staging/` convention so a human reviewer moves
            # them in before merge.
            ".github/",
        ],
    ),
    can_run_in_parallel=True,  # Can run in parallel with tester
    produces_outputs=["doc_files"],
    requires_inputs=["changed_files"],
)

REVIEWER_CODE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CODE,
    description="Performs a comprehensive code review",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Review code quality, security, and correctness",
        "Check for OWASP top 10 vulnerabilities",
        "Verify error handling and edge cases",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=[],
)

# Holistic generalist counterpart to ``reviewer_code`` (issue #2126).
# Skims the full diff once and runs four cross-module passes rather
# than verifying every line — that is ``reviewer_code``'s job. Its
# focus is the architectural-coherence question line-by-line review
# does not own: does the primary advertised use case work end-to-end,
# do docs and code agree, do synthetic keys round-trip across modules,
# are silent fallbacks hiding operator-visible failures.
REVIEWER_CODE_HOLISTIC_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CODE_HOLISTIC,
    description="Single-pass holistic code review focused on cross-module coherence",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Walk the primary advertised use case end-to-end across the full diff",
        "Cross-check doc-claimed behaviour against what the code actually does",
        "Audit synthetic keys, sentinels, and 'magic' values for cross-module agreement",
        "Surface silent fallbacks that swallow operator-visible misconfiguration",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=[],
)

# Contract reviewer needs write access to .egg-state/contracts/ to mark
# items as done, so it uses a custom blocked_write list that excludes it.
_REVIEWER_CONTRACT_BLOCKED_WRITE = [
    "src/",
    "lib/",
    "docs/",
    "tests/",
    "test/",
    ".egg-state/drafts/",
    # Issue #2532: parity with _REVIEWER_CONTRACT_BLOCKED in patterns.py — see #2508 / #2521.
    ".github/",
]

REVIEWER_CONTRACT_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CONTRACT,
    description="Verifies implementation matches the contract",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Verify acceptance criteria are met",
        "Check task completion status",
        "Validate contract consistency",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
            ".egg-state/contracts/",
        ],
        blocked_write=_REVIEWER_CONTRACT_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["integration_report"],
)

REVIEWER_SECURITY_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_SECURITY,
    description="ADVISORY security-lens reviewer for the implement phase",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Detect cross-file allowlist mismatches",
        "Flag handler-vs-validator path mismatches",
        "Identify information-disclosure and authorization-bypass patterns",
        "Spot uncommitted-artifact / Dockerfile-symlink mismatches",
        "Catch secret leakage via logs, error text, or environment dumps",
        "Surface OWASP top-10 patterns spanning more than one changed file",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=[],
)

REVIEWER_CONCURRENCY_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CONCURRENCY,
    description="ADVISORY concurrency-lens reviewer for the implement phase",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Identify race conditions and deadlocks",
        "Flag shared-state mutation and async-context leakage",
        "Detect retry-storm patterns and resource-cleanup ordering bugs",
        "Verify BRC-protocol invariants (send→wait, cursor threading, heartbeats)",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=[],
)
