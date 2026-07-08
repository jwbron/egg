"""Oversight and utility agent role definitions (#3543 decomposition).

The cross-phase interface role (overseer) and the on-demand utility
roles (autofixer, conflict_resolver, evidence_gatherer) that spawn
outside the standard per-phase producer/reviewer sets.
"""

from ._core import (
    AgentCategory,
    AgentRole,
    AgentRoleDefinition,
    FileAccessPattern,
)

# Overseer role — monitors pipeline health, classifies anomalies, escalates issues
OVERSEER_ROLE = AgentRoleDefinition(
    role=AgentRole.OVERSEER,
    description="Monitors pipeline health, classifies anomalies, and escalates issues",
    category=AgentCategory.INTERFACE,
    responsibilities=[
        "Monitor agent progress events and heartbeats",
        "Classify stalls, errors, and loops using Haiku tier",
        "Decide corrective actions using Sonnet/Opus tier",
        "Send redirect messages to stuck agents",
        "Escalate to HITL when redirects are exhausted",
        "File diagnostic GitHub issues for persistent problems",
        "Track self-monitoring metrics (poll timing, LLM costs)",
    ],
    dependencies=[],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/oversight/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
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
    ),
    can_run_in_parallel=True,
    produces_outputs=["health_report", "oversight_logs"],
)


# Utility role definitions

AUTOFIXER_ROLE = AgentRoleDefinition(
    role=AgentRole.AUTOFIXER,
    description="Applies automated fixes for lint, type-check, and formatting issues",
    category=AgentCategory.UTILITY,
    responsibilities=[
        "Run linters, type checkers, and formatters on changed files",
        "Apply auto-fixable corrections (e.g., import sorting, formatting)",
        "Report unfixable issues for human or coder review",
        "Commit auto-fix changes with clear attribution",
    ],
    dependencies=[AgentRole.CODER],
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            # Source code (for auto-fixes)
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
            # Handoff output
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "docs/",
            "**/*.md",
            ".egg-state/contracts/",
            # Issue #2508: branch-protection invariant — even an
            # auto-fix to `.github/workflows/*.yml` must go through the
            # `.github-staging/` convention so a human reviewer moves
            # it in before merge.
            ".github/",
        ],
    ),
    produces_outputs=["autofix_report", "fixed_files"],
    requires_inputs=["changed_files"],
)

CONFLICT_RESOLVER_ROLE = AgentRoleDefinition(
    role=AgentRole.CONFLICT_RESOLVER,
    description="Resolves merge conflicts and integration issues between agent branches",
    category=AgentCategory.UTILITY,
    responsibilities=[
        "Detect and resolve merge conflicts between agent work branches",
        "Ensure combined changes maintain consistency",
        "Update tests and docs affected by conflict resolution",
        "Report unresolvable conflicts for human review",
    ],
    dependencies=[],  # Runs on-demand, no fixed dependencies
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
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
            # Handoff output
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            ".egg-state/contracts/",
            ".egg-state/drafts/",
            ".egg-state/pipelines/",
            ".egg-state/reviews/",
            ".egg-state/oversight/",
            # Issue #2508: branch-protection invariant — when resolving
            # a conflict that touches a workflow / CODEOWNERS file, the
            # resolution must go through the `.github-staging/`
            # convention so a human reviewer moves it in before merge.
            ".github/",
        ],
    ),
    produces_outputs=["conflict_report", "resolved_files"],
    requires_inputs=[],
)


# Read-only evidence gatherer (#3523 §5, S7 / task-7-1). Assembles the shared
# evidence pack (diff + changed files + caller/callee context + verified env
# facts) that every same-model reviewer in a wave consumes as a byte-identical
# prompt prefix. Its capabilities are the whole point: it is *unprivileged*.
#   * No verdict-casting: it is NOT a reviewer role (absent from
#     ``_PHASE_REVIEWERS``) and maps to the coarse ``Role.SYSTEM`` (an
#     observer, not a contract author) — so it can neither ACK/NACK nor mutate
#     the contract.
#   * No posting / no GitHub access: it is deliberately left OUT of
#     ``gateway.agent_restrictions.AGENT_GH_RESTRICTIONS`` so the gateway's
#     deny-by-default posture rejects EVERY ``gh`` operation for it (stronger
#     than the per-op block every producer gets).
#   * Read-only checkout: ``file_access`` permits writes ONLY to the
#     agent-outputs handoff dir and blocks source/tests/docs/contracts/reviews/
#     drafts/.github — it can read everything, write essentially nothing.
EVIDENCE_GATHERER_ROLE = AgentRoleDefinition(
    role=AgentRole.EVIDENCE_GATHERER,
    description="Read-only gatherer that assembles the shared review-wave evidence pack",
    category=AgentCategory.UTILITY,
    responsibilities=[
        "Assemble a slice's evidence pack: diff, changed files with enclosing "
        "context, caller/callee lists for changed symbols, verified env facts",
        "Order the pack strictly by path — collect evidence, never analyze it",
        "Cast no verdict, post nothing, touch no network — read-only",
    ],
    dependencies=[],  # Runs on-demand ahead of a review wave; no fixed deps
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files (read-only by design)
        allowed_write=[
            # Handoff output only — the assembled pack. Nothing else.
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "src/",
            "lib/",
            "docs/",
            "tests/",
            "test/",
            ".egg-state/contracts/",
            ".egg-state/drafts/",
            ".egg-state/reviews/",
            ".egg-state/pipelines/",
            ".egg-state/oversight/",
            ".github/",
        ],
    ),
    produces_outputs=["evidence_pack"],
    requires_inputs=[],
)
