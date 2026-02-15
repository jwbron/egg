"""
Agent role definitions for multi-agent orchestration.

This module defines specialized agent roles used during the implement phase.
Each role has specific responsibilities, file access constraints, and
dependency declarations that enable parallel execution where possible.

Agent roles:
- CODER: Implements code changes based on the plan tasks
- TESTER: Writes tests for the implemented changes
- DOCUMENTER: Updates documentation for the changes
- INTEGRATOR: Runs full test suite and validates integration
- REFINER: Analyzes tasks and produces structured analysis in the refine phase

The orchestrator uses these definitions to:
1. Determine execution order based on dependencies
2. Enforce file access restrictions via the gateway
3. Build role-specific prompts with focused context
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentRole(StrEnum):
    """Specialized agent roles for multi-agent orchestration.

    Each role has a focused responsibility and specific file access patterns.
    The orchestrator uses these roles to parallelize work where dependencies allow.

    Implement-phase roles: CODER, TESTER, DOCUMENTER, INTEGRATOR
    Plan-phase roles: ARCHITECT, TASK_PLANNER, RISK_ANALYST
    Refine-phase roles: REFINER
    Reviewer roles: REVIEWER_UNIFIED, REVIEWER_CODE, REVIEWER_CONTRACT,
                    REVIEWER_AGENT_DESIGN, REVIEWER_REFINE, REVIEWER_PLAN
    """

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"
    # Plan-phase roles
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    # Refine-phase roles
    REFINER = "refiner"
    # Reviewer roles
    REVIEWER_UNIFIED = "reviewer_unified"
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    REVIEWER_PLAN = "reviewer_plan"


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

        return any(self._matches_pattern(file_path, pattern) for pattern in self.allowed_read)

    def can_write(self, file_path: str) -> bool:
        """Check if the agent can write to this file.

        Args:
            file_path: Path relative to repo root

        Returns:
            True if the file can be written
        """
        # Check blocked patterns first
        if any(self._matches_pattern(file_path, pattern) for pattern in self.blocked_write):
            return False

        # If no allowed patterns, block all writes
        if not self.allowed_write:
            return False

        return any(self._matches_pattern(file_path, pattern) for pattern in self.allowed_write)

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
        import fnmatch

        # Normalize paths
        file_path = file_path.lstrip("./")
        pattern = pattern.lstrip("./")

        # Prefix match (directory pattern)
        if pattern.endswith("/"):
            return file_path.startswith(pattern) or file_path + "/" == pattern

        # Use fnmatch for wildcard matching
        return fnmatch.fnmatch(file_path, pattern)


@dataclass
class AgentRoleDefinition:
    """Complete definition of an agent role.

    Combines the role identifier with its responsibilities, dependencies,
    and access constraints. Used by the orchestrator to plan execution.
    """

    role: AgentRole
    description: str
    responsibilities: list[str]
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
        ],
        blocked_write=[
            "docs/",  # Documenter handles docs
            "**/README.md",  # Documenter handles READMEs
            ".egg-state/contracts/",  # Contracts are managed by API
        ],
    ),
    produces_outputs=["changed_files", "commits"],
    requires_inputs=[],
)

TESTER_ROLE = AgentRoleDefinition(
    role=AgentRole.TESTER,
    description="Writes tests for the implemented changes",
    responsibilities=[
        "Read the list of changed files from coder",
        "Write or update tests for the changes",
        "Report test coverage for new code",
        "Ensure tests pass before completing",
    ],
    dependencies=[AgentRole.CODER],  # Must wait for coder
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
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
            ".egg-state/agent-outputs/",  # For handoff data
        ],
        blocked_write=[
            "docs/",
            ".egg-state/contracts/",
        ],
    ),
    produces_outputs=["test_files", "coverage_report"],
    requires_inputs=["changed_files"],
)

DOCUMENTER_ROLE = AgentRoleDefinition(
    role=AgentRole.DOCUMENTER,
    description="Updates documentation for the changes",
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
            "**/CHANGELOG.md",
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
        ],
    ),
    can_run_in_parallel=True,  # Can run in parallel with tester
    produces_outputs=["doc_files"],
    requires_inputs=["changed_files"],
)

INTEGRATOR_ROLE = AgentRoleDefinition(
    role=AgentRole.INTEGRATOR,
    description="Runs full test suite and validates integration",
    responsibilities=[
        "Run the full test suite",
        "Validate all changes work together",
        "Check for integration issues",
        "Produce integration report",
    ],
    dependencies=[AgentRole.CODER, AgentRole.TESTER],  # Waits for code and tests
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/agent-outputs/",  # For integration report
        ],
        blocked_write=[
            # Use directory-based blocks instead of "**/*" which breaks
            # can_write() by always matching before allowed_write is checked.
            "src/",
            "lib/",
            "docs/",
            "tests/",
            "test/",
            ".egg-state/contracts/",
        ],
    ),
    can_run_in_parallel=False,  # Runs after others complete
    produces_outputs=["integration_report"],
    requires_inputs=["changed_files", "test_files"],
)


# Plan-phase agent role definitions

ARCHITECT_ROLE = AgentRoleDefinition(
    role=AgentRole.ARCHITECT,
    description="Analyzes the task and produces architecture analysis",
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
    produces_outputs=["architecture_analysis", "technical_decisions"],
    requires_inputs=[],
)

TASK_PLANNER_ROLE = AgentRoleDefinition(
    role=AgentRole.TASK_PLANNER,
    description="Decomposes architecture analysis into discrete tasks",
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
    produces_outputs=["task_breakdown", "acceptance_criteria"],
    requires_inputs=["architecture_analysis"],
)

RISK_ANALYST_ROLE = AgentRoleDefinition(
    role=AgentRole.RISK_ANALYST,
    description="Assesses technical risks for the proposed implementation",
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
    can_run_in_parallel=True,  # Can run in parallel with task_planner
    produces_outputs=["risk_assessment", "mitigation_plan"],
    requires_inputs=["architecture_analysis"],
)


# Refine-phase agent role definitions

REFINER_ROLE = AgentRoleDefinition(
    role=AgentRole.REFINER,
    description="Analyzes the task and produces a structured analysis in the refine phase",
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
]

REVIEWER_UNIFIED_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_UNIFIED,
    description="Performs a unified review of the phase output",
    responsibilities=[
        "Review all changes made in this phase",
        "Apply comprehensive review criteria",
        "Write a structured verdict (approved/needs_revision)",
    ],
    # Depend on terminal roles from both implement and plan phases.
    # build_from_roles() only adds edges for deps present in the role set,
    # so INTEGRATOR is used for implement phase, TASK_PLANNER/RISK_ANALYST for plan.
    dependencies=[AgentRole.INTEGRATOR, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["integration_report"],
)

REVIEWER_CODE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CODE,
    description="Performs a comprehensive code review",
    responsibilities=[
        "Review code quality, security, and correctness",
        "Check for OWASP top 10 vulnerabilities",
        "Verify error handling and edge cases",
    ],
    dependencies=[AgentRole.INTEGRATOR, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["integration_report"],
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
]

REVIEWER_CONTRACT_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CONTRACT,
    description="Verifies implementation matches the contract",
    responsibilities=[
        "Verify acceptance criteria are met",
        "Check task completion status",
        "Validate contract consistency",
    ],
    dependencies=[AgentRole.INTEGRATOR, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
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
    responsibilities=[
        "Check for agent-mode design anti-patterns",
        "Verify autonomous operation capability",
        "Assess human-in-the-loop integration",
    ],
    dependencies=[AgentRole.INTEGRATOR, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST, AgentRole.REFINER],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["integration_report"],
)

REVIEWER_REFINE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_REFINE,
    description="Reviews refine phase analysis quality and completeness",
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


# Registry of all agent roles
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    # Implement-phase roles
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.INTEGRATOR: INTEGRATOR_ROLE,
    # Plan-phase roles
    AgentRole.ARCHITECT: ARCHITECT_ROLE,
    AgentRole.TASK_PLANNER: TASK_PLANNER_ROLE,
    AgentRole.RISK_ANALYST: RISK_ANALYST_ROLE,
    # Refine-phase roles
    AgentRole.REFINER: REFINER_ROLE,
    # Reviewer roles
    AgentRole.REVIEWER_UNIFIED: REVIEWER_UNIFIED_ROLE,
    AgentRole.REVIEWER_CODE: REVIEWER_CODE_ROLE,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_ROLE,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_ROLE,
    AgentRole.REVIEWER_REFINE: REVIEWER_REFINE_ROLE,
    AgentRole.REVIEWER_PLAN: REVIEWER_PLAN_ROLE,
}


def get_role_definition(role: AgentRole | str) -> AgentRoleDefinition:
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

_PHASE_ROLES: dict[str, list[AgentRole]] = {
    "implement": [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR],
    "plan": [AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    "refine": [AgentRole.REFINER],
}

_PHASE_REVIEWERS: dict[str, list[AgentRole]] = {
    "implement": [
        AgentRole.REVIEWER_UNIFIED,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
    ],
    "plan": [
        AgentRole.REVIEWER_UNIFIED,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_PLAN,
    ],
    "refine": [
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_AGENT_DESIGN,
    ],
}


def get_roles_for_phase(
    phase: str,
    include_reviewers: bool = False,
) -> list[AgentRole]:
    """Return the agent roles for a given pipeline phase.

    Args:
        phase: Pipeline phase name (e.g., "implement", "plan")
        include_reviewers: Whether to include reviewer roles

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
        result.extend(_PHASE_REVIEWERS.get(phase, []))
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
