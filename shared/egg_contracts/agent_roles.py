"""
Agent role definitions for multi-agent orchestration.

This module is the **canonical source of truth** for agent roles and their
definitions.  All other modules (``orchestrator/models.py``,
``shared/egg_orchestrator/types.py``, ``gateway/agent_restrictions.py``)
must import or mirror the ``AgentRole`` enum defined here.

Agent role categories:
- EXECUTION: Agents that produce code or artifacts (coder, tester, documenter)
- ANALYSIS: Agents that analyze and plan (architect, task_planner, risk_analyst, refiner)
- REVIEW: Agents that review work products (reviewer_*)
- UTILITY: Cross-cutting support agents (autofixer, conflict_resolver)
- INTERFACE: Agents that interact with external systems (overseer)

The orchestrator uses these definitions to:
1. Determine execution order based on dependencies
2. Enforce file access restrictions via the gateway
3. Build role-specific prompts with focused context
4. Compose dynamic agent teams via category queries
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from egg_restrictions.matchers import match_pattern

from .roles import Role


class AgentCategory(StrEnum):
    """Category classification for agent roles.

    Used for dynamic team composition queries (e.g., "all review agents")
    and for understanding the high-level purpose of each role.
    """

    EXECUTION = "execution"
    ANALYSIS = "analysis"
    REVIEW = "review"
    UTILITY = "utility"
    INTERFACE = "interface"


class AgentRole(StrEnum):
    """Specialized agent roles for multi-agent orchestration.

    This is the **canonical enum** — all other AgentRole definitions in the
    codebase must import from or stay in sync with this one.

    Each role has a focused responsibility and specific file access patterns.
    The orchestrator uses these roles to parallelize work where dependencies allow.

    Execution roles: CODER, TESTER, DOCUMENTER
    Analysis roles: ARCHITECT, TASK_PLANNER, RISK_ANALYST, REFINER
    Review roles: REVIEWER_CODE, REVIEWER_CODE_HOLISTIC,
                  REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN,
                  REVIEWER_REFINE, REVIEWER_PLAN,
                  REVIEWER_SECURITY, REVIEWER_CONCURRENCY
    Utility roles: AUTOFIXER, CONFLICT_RESOLVER
    Interface roles: OVERSEER
    """

    # Execution roles (produce code or artifacts)
    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    # Jira-epic SDLC support (issue #1557). The APPLIER role drives
    # Jira mutations (epic Description writes, child create/edit/link,
    # Won't-Do handoff) on operator approval of the refine/plan HITL
    # gates. It runs inside the sandbox and uses only the agent-facing
    # gateway Jira routes — credentials never leave the gateway.
    APPLIER = "applier"
    # Analysis roles (analyze and plan)
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    REFINER = "refiner"
    # Review roles
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CODE_HOLISTIC = "reviewer_code_holistic"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    REVIEWER_PLAN = "reviewer_plan"
    REVIEWER_SECURITY = "reviewer_security"
    REVIEWER_CONCURRENCY = "reviewer_concurrency"
    # Utility roles (cross-cutting support)
    AUTOFIXER = "autofixer"
    CONFLICT_RESOLVER = "conflict_resolver"
    # Interface roles (external system interaction)
    OVERSEER = "overseer"
    INSPECTOR = "inspector"


class AgentStatus(StrEnum):
    """Status values for agent executions."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class FileAccessPattern:
    """File access pattern for an agent role.

    Defines what files an agent can read and write. The gateway enforces
    these restrictions during git push operations.
    """

    allowed_read: list[str] = field(default_factory=list)
    allowed_write: list[str] = field(default_factory=list)
    blocked_write: list[str] = field(default_factory=list)

    def can_read(self, file_path: str) -> bool:
        """Check if the agent can read this file.

        Args:
            file_path: Path relative to repo root

        Returns:
            True if the file can be read (default: all files readable)
        """
        if not self.allowed_read:
            return True  # Empty list means all files readable

        return any(match_pattern(file_path, pattern) for pattern in self.allowed_read)

    def can_write(self, file_path: str) -> bool:
        """Check if the agent can write to this file.

        Args:
            file_path: Path relative to repo root

        Returns:
            True if the file can be written
        """
        # Check blocked patterns first
        if any(match_pattern(file_path, pattern) for pattern in self.blocked_write):
            return False

        # If no allowed patterns, block all writes
        if not self.allowed_write:
            return False

        return any(match_pattern(file_path, pattern) for pattern in self.allowed_write)


@dataclass
class AgentRoleDefinition:
    """Complete definition of an agent role.

    Combines the role identifier with its responsibilities, dependencies,
    and access constraints. Used by the orchestrator to plan execution.
    """

    role: AgentRole
    description: str
    responsibilities: list[str]
    category: AgentCategory | None = None
    dependencies: list[AgentRole] = field(default_factory=list)
    file_access: FileAccessPattern = field(default_factory=FileAccessPattern)
    can_run_in_parallel: bool = True  # Can run in parallel with other agents
    produces_outputs: list[str] = field(default_factory=list)
    requires_inputs: list[str] = field(default_factory=list)

    def depends_on(self, other: AgentRole) -> bool:
        """Check if this role depends on another role.

        Args:
            other: The role to check dependency against

        Returns:
            True if this role must wait for the other role to complete
        """
        return other in self.dependencies


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
            # proposed end-state here and the PR builder emits a manual
            # step asking the human reviewer to move the files into
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
    description="Updates documentation for the changes",
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read the list of changed files from coder",
        "Update relevant documentation",
        "Add or update API documentation",
        "Ensure README files are current",
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

# Plan-phase agent role definitions
# Plan-phase agents (architect, task_planner, risk_analyst) share a
# blocked_write list mirroring ``_PLAN_AGENT_BLOCKED`` in
# ``shared/egg_restrictions/patterns.py``. Keeping the two views in
# lockstep is the invariant issue #2532 closed; using a shared constant
# eliminates one future drift surface within ``agent_roles.py`` itself.
_PLAN_AGENT_BLOCKED_WRITE = [
    "**/*.py",
    "**/*.ts",
    "**/*.tsx",
    "**/*.js",
    "**/*.jsx",
    "**/*.go",
    "**/*.java",
    ".egg-state/contracts/",
    # Issue #2532: parity with _PLAN_AGENT_BLOCKED in patterns.py — see #2508 / #2521.
    ".github/",
]

ARCHITECT_ROLE = AgentRoleDefinition(
    role=AgentRole.ARCHITECT,
    description="Analyzes the task and produces architecture analysis",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Understand the problem or feature request",
        "Research the codebase to understand existing patterns",
        "Identify key files, constraints, and dependencies",
        "Recommend an approach with justification",
        "Document technical decisions",
    ],
    dependencies=[],  # Architect runs first in plan phase
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    produces_outputs=["architecture_analysis", "technical_decisions"],
    requires_inputs=[],
)

TASK_PLANNER_ROLE = AgentRoleDefinition(
    role=AgentRole.TASK_PLANNER,
    description="Decomposes architecture analysis into discrete tasks",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Review the architecture analysis from the architect",
        "Break down work into phases with discrete tasks",
        "Define clear acceptance criteria for each task",
        "Define dependency ordering between tasks",
        "Identify the test strategy",
    ],
    dependencies=[AgentRole.ARCHITECT],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    produces_outputs=["task_breakdown", "acceptance_criteria"],
    requires_inputs=["architecture_analysis"],
)

RISK_ANALYST_ROLE = AgentRoleDefinition(
    role=AgentRole.RISK_ANALYST,
    description="Assesses technical risks for the proposed implementation",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Review the architecture analysis from the architect",
        "Identify technical risks (security, performance, compatibility)",
        "Assess impact and likelihood of each risk",
        "Propose mitigation strategies and rollback plans",
        "Flag areas that need human review",
    ],
    dependencies=[AgentRole.ARCHITECT],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    can_run_in_parallel=True,  # Can run in parallel with task_planner
    produces_outputs=["risk_assessment", "mitigation_plan"],
    requires_inputs=["architecture_analysis"],
)


# Jira-epic SDLC support (issue #1557). The APPLIER drives Jira
# mutations after the refine/plan HITL gates resolve. It reads the
# contract + relevant draft and calls the agent-facing gateway Jira
# routes (``ticket/edit``, ``ticket/create``, ``issue-link/create``).
# ``Won't Do`` transitions are **not** in the applier's purview — the
# applier produces a handoff JSON that the orchestrator drains via the
# orchestrator-only ``/transition`` route. Restricted to write only the
# agent-outputs handoff directory; the applier never edits source.
APPLIER_ROLE = AgentRoleDefinition(
    role=AgentRole.APPLIER,
    description=(
        "Applies Jira mutations (epic Description writes, child "
        "create/edit/link, Won't-Do handoff) on operator approval of "
        "refine/plan HITL gates for epic-mode pipelines."
    ),
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read EGG_EPIC_MODE + the just-approved phase + contract path",
        "For refine-apply: write the analysis to the epic Description",
        "For plan-apply: walk Task.jira_action and dispatch per-action",
        "Write jira_action_status='in_flight' before each call; flip to "
        "'applied' or 'failed' after",
        "Emit a Won't-Do handoff JSON for the orchestrator to drain",
        "Refuse to mutate in-flight children without the override marker",
    ],
    dependencies=[],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
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
            "plugins/",
            "docs/",
            "tests/",
            "test/",
            ".egg-state/contracts/",
            ".egg-state/drafts/",
            ".github/",
        ],
    ),
    can_run_in_parallel=False,
    produces_outputs=["jira_apply_report", "wontdo_handoff"],
    requires_inputs=["analysis_draft", "task_breakdown"],
)


# Refine-phase agent role definitions

REFINER_ROLE = AgentRoleDefinition(
    role=AgentRole.REFINER,
    description="Analyzes the task and produces a structured analysis in the refine phase",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Understand the problem or feature request",
        "Research the current codebase to understand existing patterns",
        "Identify constraints and dependencies",
        "Consider multiple implementation approaches with pros/cons",
        "Recommend an approach with justification",
        "Surface open questions as HITL decisions or feedback requests",
        "Write analysis to the draft file (NOT an implementation plan)",
    ],
    dependencies=[],  # Refiner runs first, no dependencies
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.java",
            ".egg-state/contracts/",
        ],
    ),
    produces_outputs=["analysis_draft"],
    requires_inputs=[],
)

# Reviewer agent role definitions
# Reviewers can only write to reviews/ and agent-outputs/ directories.
# Use directory-based blocks instead of "**/*" which breaks can_write()
# by always matching before allowed_write is checked.

_REVIEWER_BLOCKED_WRITE = [
    "src/",
    "lib/",
    "docs/",
    "tests/",
    "test/",
    ".egg-state/contracts/",
    ".egg-state/drafts/",
    # Issue #2532: parity with _REVIEWER_BLOCKED in patterns.py — see #2508 / #2521.
    ".github/",
]

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

REVIEWER_AGENT_DESIGN_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_AGENT_DESIGN,
    description="Reviews agent-mode design alignment",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Check for agent-mode design anti-patterns",
        "Verify autonomous operation capability",
        "Assess human-in-the-loop integration",
    ],
    dependencies=[AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["analysis_draft"],
)

REVIEWER_REFINE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_REFINE,
    description="Reviews refine phase analysis quality and completeness",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Verify the analysis correctly identifies the core problem",
        "Assess research quality and codebase exploration",
        "Evaluate options analysis and trade-off reasoning",
        "Check that constraints and dependencies are identified",
        "Validate the recommendation is justified and actionable",
    ],
    dependencies=[AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["analysis_draft"],
)

REVIEWER_PLAN_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_PLAN,
    description="Reviews plan phase output quality and completeness",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Verify task breakdown is discrete, actionable, and properly scoped",
        "Assess acceptance criteria clarity and testability",
        "Evaluate dependency ordering between tasks",
        "Check that risks and mitigations are identified",
        "Validate test strategy coverage",
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
    requires_inputs=["task_breakdown", "risk_assessment"],
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


# Inspector role — health-check agent used by orchestrator tier-2 diagnostics

INSPECTOR_ROLE = AgentRoleDefinition(
    role=AgentRole.INSPECTOR,
    description="Runs targeted health-check diagnostics inside a sandbox",
    category=AgentCategory.INTERFACE,
    responsibilities=[
        "Execute diagnostic scripts in a sandbox container",
        "Collect health-check data for the orchestrator",
        "Report findings via agent-outputs",
    ],
    dependencies=[],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
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
    produces_outputs=["diagnostic_report"],
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


# Registry of all agent roles
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    # Execution roles
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.APPLIER: APPLIER_ROLE,
    # Analysis roles
    AgentRole.ARCHITECT: ARCHITECT_ROLE,
    AgentRole.TASK_PLANNER: TASK_PLANNER_ROLE,
    AgentRole.RISK_ANALYST: RISK_ANALYST_ROLE,
    AgentRole.REFINER: REFINER_ROLE,
    # Review roles
    AgentRole.REVIEWER_CODE: REVIEWER_CODE_ROLE,
    AgentRole.REVIEWER_CODE_HOLISTIC: REVIEWER_CODE_HOLISTIC_ROLE,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_ROLE,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_ROLE,
    AgentRole.REVIEWER_REFINE: REVIEWER_REFINE_ROLE,
    AgentRole.REVIEWER_PLAN: REVIEWER_PLAN_ROLE,
    AgentRole.REVIEWER_SECURITY: REVIEWER_SECURITY_ROLE,
    AgentRole.REVIEWER_CONCURRENCY: REVIEWER_CONCURRENCY_ROLE,
    # Utility roles
    AgentRole.AUTOFIXER: AUTOFIXER_ROLE,
    AgentRole.CONFLICT_RESOLVER: CONFLICT_RESOLVER_ROLE,
    # Interface roles
    AgentRole.OVERSEER: OVERSEER_ROLE,
    AgentRole.INSPECTOR: INSPECTOR_ROLE,
}


# Canonical set of execution role string values — used by the schema,
# plan parser, and orchestrator for role validation and filtering.
EXECUTION_ROLE_VALUES = frozenset({AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER})


# Maps the fine-grained ``AgentRole`` shipped in agent sessions to the
# coarse-grained ``Role`` enum used for contract field-ownership checks.
# The gateway's contract and phase APIs consult this to authorize an
# agent's request — without it, a session with ``agent_role="refiner"``
# fails ``Role("refiner")`` and the API responds 403 (#1766).
AGENT_ROLE_TO_CONTRACT_ROLE: dict[AgentRole, Role] = {
    # Execution: produce code, own implementer-owned contract fields
    AgentRole.CODER: Role.IMPLEMENTER,
    AgentRole.TESTER: Role.IMPLEMENTER,
    AgentRole.DOCUMENTER: Role.IMPLEMENTER,
    # Applier (issue #1557): mutates Task.jira_* lifecycle fields on the
    # contract during the apply phase; same contract privileges as other
    # execution producers.
    AgentRole.APPLIER: Role.IMPLEMENTER,
    # Analysis: draft plans and analyses; write the same contract fields
    # an implementer does (commits, notes, decisions).
    AgentRole.ARCHITECT: Role.IMPLEMENTER,
    AgentRole.TASK_PLANNER: Role.IMPLEMENTER,
    AgentRole.RISK_ANALYST: Role.IMPLEMENTER,
    AgentRole.REFINER: Role.IMPLEMENTER,
    # Review: verdicts and phase-status/current_phase mutations
    AgentRole.REVIEWER_CODE: Role.REVIEWER,
    AgentRole.REVIEWER_CODE_HOLISTIC: Role.REVIEWER,
    AgentRole.REVIEWER_CONTRACT: Role.REVIEWER,
    AgentRole.REVIEWER_AGENT_DESIGN: Role.REVIEWER,
    AgentRole.REVIEWER_REFINE: Role.REVIEWER,
    AgentRole.REVIEWER_PLAN: Role.REVIEWER,
    AgentRole.REVIEWER_SECURITY: Role.REVIEWER,
    AgentRole.REVIEWER_CONCURRENCY: Role.REVIEWER,
    # Utility: apply code fixes, share implementer privileges
    AgentRole.AUTOFIXER: Role.IMPLEMENTER,
    AgentRole.CONFLICT_RESOLVER: Role.IMPLEMENTER,
    # Interface: observers, not contract authors
    AgentRole.OVERSEER: Role.SYSTEM,
    AgentRole.INSPECTOR: Role.SYSTEM,
}


def get_contract_role(role: AgentRole | str) -> Role | None:
    """Translate a fine-grained ``AgentRole`` to the coarse contract ``Role``.

    The gateway stores the fine role in session metadata but the contract
    API enforces field ownership against the coarse ``Role`` enum. Returns
    ``None`` when the input does not name a known fine role.
    """
    if isinstance(role, str):
        try:
            role = AgentRole(role)
        except ValueError:
            return None
    return AGENT_ROLE_TO_CONTRACT_ROLE.get(role)


def get_role_definition(
    role: AgentRole | str,
) -> AgentRoleDefinition:
    """Get the definition for an agent role.

    Args:
        role: The role to get (string or AgentRole enum)

    Returns:
        The AgentRoleDefinition for this role

    Raises:
        KeyError: If the role is not defined
    """
    if isinstance(role, str):
        role = AgentRole(role)
    return AGENT_ROLES[role]


def get_all_roles() -> list[AgentRoleDefinition]:
    """Get all defined agent roles.

    Returns:
        List of all AgentRoleDefinition objects
    """
    return list(AGENT_ROLES.values())


def get_file_patterns(role_value: str) -> dict[str, list[str]] | None:
    """Return file write patterns for an agent role, or None if not defined.

    Returns:
        ``{"allowed": [...], "blocked": [...]}`` or ``None`` if the role is
        unknown or has no file access patterns.
    """
    try:
        role_def = get_role_definition(role_value)
    except ValueError, KeyError:
        return None
    if not role_def or not role_def.file_access:
        return None
    fa = role_def.file_access
    if not fa.allowed_write and not fa.blocked_write:
        return None
    return {"allowed": fa.allowed_write, "blocked": fa.blocked_write}


def get_roles_by_category(category: AgentCategory) -> list[AgentRole]:
    """Get all roles belonging to a given category.

    Args:
        category: The category to filter by

    Returns:
        List of AgentRole values with the given category
    """
    return [role for role, defn in AGENT_ROLES.items() if defn.category == category]


def get_role_dependencies(role: AgentRole | str) -> list[AgentRole]:
    """Get the dependencies for a role.

    Args:
        role: The role to get dependencies for

    Returns:
        List of roles this role depends on
    """
    definition = get_role_definition(role)
    return definition.dependencies


def can_run_in_parallel(role1: AgentRole | str, role2: AgentRole | str) -> bool:
    """Check if two roles can run in parallel.

    Two roles can run in parallel if:
    1. Neither depends on the other
    2. Both have can_run_in_parallel set to True

    Args:
        role1: First role
        role2: Second role

    Returns:
        True if the roles can run concurrently
    """
    def1 = get_role_definition(role1)
    def2 = get_role_definition(role2)

    # Check mutual dependencies
    if def1.depends_on(def2.role) or def2.depends_on(def1.role):
        return False

    # Both must allow parallel execution
    return def1.can_run_in_parallel and def2.can_run_in_parallel


@dataclass
class AgentExecution:
    """Tracks the execution state of a single agent.

    This is used by the orchestrator to track which agents have run,
    their results, and any handoff data they produced.
    """

    role: AgentRole
    status: AgentStatus = AgentStatus.PENDING
    started_at: str | None = None  # ISO timestamp
    completed_at: str | None = None  # ISO timestamp
    commit: str | None = None  # Git commit SHA if agent made changes
    outputs: dict[str, Any] = field(default_factory=dict)  # Handoff data
    error: str | None = None  # Error message if failed
    retry_count: int = 0

    def is_complete(self) -> bool:
        """Check if the agent has finished (successfully or not)."""
        return self.status in (AgentStatus.COMPLETE, AgentStatus.FAILED, AgentStatus.SKIPPED)

    def is_successful(self) -> bool:
        """Check if the agent completed successfully."""
        return self.status == AgentStatus.COMPLETE

    def can_retry(self, max_retries: int = 2) -> bool:
        """Check if the agent can be retried."""
        return self.status == AgentStatus.FAILED and self.retry_count < max_retries


# Phase-to-role mappings for multi-agent execution
# Note: Utility roles (AUTOFIXER, CONFLICT_RESOLVER) and INSPECTOR are excluded
# by design — they are spawned on-demand, not as part of standard phase execution.

_PHASE_ROLES: dict[str, list[AgentRole]] = {
    "implement": [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER],
    "plan": [AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    "refine": [AgentRole.REFINER],
    # Apply phase (issue #1557): single producer (APPLIER) reviewed by
    # REVIEWER_CONTRACT on contract-state convergence. Inserted between
    # PLAN and IMPLEMENT only for epic pipelines — the orchestrator
    # scheduler skips this phase when ``Pipeline.is_epic == False``.
    "apply": [AgentRole.APPLIER],
}

_PHASE_REVIEWERS: dict[str, list[AgentRole]] = {
    "implement": [
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CODE_HOLISTIC,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_SECURITY,
        AgentRole.REVIEWER_CONCURRENCY,
    ],
    "plan": [
        AgentRole.REVIEWER_PLAN,
    ],
    "refine": [
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_AGENT_DESIGN,
    ],
    # Apply phase reviewer (issue #1557 — architect's slice-3 design +
    # risk_analyst R1 mitigation). REVIEWER_CONTRACT ACKs on
    # contract-state convergence (every Task with jira_action='create'
    # has a non-null jira_key matching ^[A-Z][A-Z0-9_]*-[0-9]+$, every
    # Task has jira_action_status in {'applied', 'failed'}, no in-flight
    # child mutated without the 'in-flight-confirmed' marker). The
    # reviewer ACKs on contract state, NOT on prompt-output text
    # quality.
    "apply": [
        AgentRole.REVIEWER_CONTRACT,
    ],
}


EGG_REPO = "jwbron/egg"

# Reviewer roles that only apply to the egg repo itself
EGG_ONLY_REVIEWERS: set[AgentRole] = {AgentRole.REVIEWER_AGENT_DESIGN}

# String values for use by review_graph and other modules
EGG_ONLY_REVIEWER_NAMES: set[str] = {r.value for r in EGG_ONLY_REVIEWERS}


def get_roles_for_phase(
    phase: str,
    include_reviewers: bool = True,
    include_overseer: bool = False,
    repo: str | None = None,
    has_contract: bool = True,
) -> list[AgentRole]:
    """Return the agent roles for a given pipeline phase.

    Args:
        phase: Pipeline phase name (e.g., "implement", "plan")
        include_reviewers: Whether to include reviewer roles (default True)
        include_overseer: Whether to include the overseer role (default False).
            The overseer is cross-phase, so it's opt-in.
        repo: Repository in owner/name format. When provided, egg-specific
            reviewer roles (e.g., reviewer_agent_design) are excluded for
            non-egg repos.
        has_contract: Whether the pipeline has an upstream SDLC contract
            (default True). When False, reviewers whose upstream artifacts
            are absent are filtered out — currently ``reviewer_contract``.
            ISSUE-mode pipelines set this to False when they haven't yet
            produced a contract draft.

    Returns:
        List of AgentRole values for that phase.

    Raises:
        ValueError: If phase has no defined roles.
    """
    roles = _PHASE_ROLES.get(phase)
    if roles is None:
        raise ValueError(f"No agent roles defined for phase: {phase}")
    result = list(roles)
    if include_reviewers:
        reviewers = _PHASE_REVIEWERS.get(phase, [])
        if repo is not None and repo != EGG_REPO:
            reviewers = [r for r in reviewers if r not in EGG_ONLY_REVIEWERS]
        if not has_contract:
            # reviewer_contract has no artifacts to verify without a contract;
            # filter it out so BRC doesn't wait on an agent that cannot ACK.
            reviewers = [r for r in reviewers if r != AgentRole.REVIEWER_CONTRACT]
        result.extend(reviewers)
    if include_overseer:
        result.append(AgentRole.OVERSEER)
    return result


def detect_write_overlaps(
    roles: list[AgentRole],
) -> list[tuple[AgentRole, AgentRole, list[str]]]:
    """Detect overlapping write patterns between agents that may run in parallel.

    Only checks roles that can run in the same wave (share no dependency edge).

    Returns:
        List of (role1, role2, overlapping_patterns) tuples.
    """
    from .dependency_graph import build_dependency_graph

    graph = build_dependency_graph(roles)
    waves = graph.compute_waves()

    overlaps = []
    for wave in waves:
        if len(wave) < 2:
            continue
        for i, role1 in enumerate(wave):
            for role2 in wave[i + 1 :]:
                role1_def = get_role_definition(role1)
                role2_def = get_role_definition(role2)
                # Find common write patterns
                common = []
                for p1 in role1_def.file_access.allowed_write:
                    for p2 in role2_def.file_access.allowed_write:
                        if p1 == p2:
                            common.append(p1)
                        elif p1.endswith("/") and p2.startswith(p1):
                            common.append(p1)
                        elif p2.endswith("/") and p1.startswith(p2):
                            common.append(p2)
                if common:
                    overlaps.append((role1, role2, common))
    return overlaps


def create_execution_for_role(role: AgentRole | str) -> AgentExecution:
    """Create a new AgentExecution for a role.

    Args:
        role: The role to create execution tracking for

    Returns:
        A new AgentExecution in PENDING status
    """
    if isinstance(role, str):
        role = AgentRole(role)
    return AgentExecution(role=role)
