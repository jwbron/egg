"""
Agent role definitions for multi-agent orchestration.

This module defines specialized agent roles used during the implement and plan phases.
Each role has specific responsibilities, file access constraints, and
dependency declarations that enable parallel execution where possible.

Implement-phase roles:
- CODER: Implements code changes based on the plan tasks
- TESTER: Writes tests for the implemented changes
- DOCUMENTER: Updates documentation for the changes
- INTEGRATOR: Runs full test suite and validates integration

Plan-phase roles:
- ARCHITECT: Designs system architecture and makes design decisions
- TASK_PLANNER: Breaks down work into discrete tasks with acceptance criteria
- RISK_ANALYST: Identifies risks, constraints, and mitigation strategies

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
    Reviewer roles: REVIEWER_UNIFIED, REVIEWER_CODE, REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN
    """

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    REVIEWER_UNIFIED = "reviewer_unified"
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"


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
            "**/*",  # Cannot modify any other files
        ],
    ),
    can_run_in_parallel=False,  # Runs after others complete
    produces_outputs=["integration_report"],
    requires_inputs=["changed_files", "test_files"],
)


# Plan-phase agent role definitions

ARCHITECT_ROLE = AgentRoleDefinition(
    role=AgentRole.ARCHITECT,
    description="Designs system architecture and makes design decisions",
    responsibilities=[
        "Analyze the issue requirements and constraints",
        "Design the system architecture for the solution",
        "Make technology and pattern decisions",
        "Output architecture decisions and design document",
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
            ".egg-state/contracts/",
        ],
    ),
    produces_outputs=["architecture_decisions", "design_document"],
    requires_inputs=[],
)

TASK_PLANNER_ROLE = AgentRoleDefinition(
    role=AgentRole.TASK_PLANNER,
    description="Breaks down work into discrete tasks with acceptance criteria",
    responsibilities=[
        "Read architecture decisions from architect",
        "Break down the solution into discrete implementation tasks",
        "Define acceptance criteria for each task",
        "Estimate file changes per task",
    ],
    dependencies=[AgentRole.ARCHITECT],  # Needs architecture first
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
            ".egg-state/contracts/",
        ],
    ),
    can_run_in_parallel=True,
    produces_outputs=["task_breakdown", "acceptance_criteria"],
    requires_inputs=["architecture_decisions"],
)

RISK_ANALYST_ROLE = AgentRoleDefinition(
    role=AgentRole.RISK_ANALYST,
    description="Identifies risks, constraints, and mitigation strategies",
    responsibilities=[
        "Read architecture decisions from architect",
        "Identify technical and process risks",
        "Assess risk likelihood and impact",
        "Propose mitigation strategies",
    ],
    dependencies=[AgentRole.ARCHITECT],  # Needs architecture first
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
            ".egg-state/contracts/",
        ],
    ),
    can_run_in_parallel=True,  # Can run parallel with TASK_PLANNER
    produces_outputs=["risk_assessment", "mitigation_strategies"],
    requires_inputs=["architecture_decisions"],
)


# Reviewer role definitions — read-only file access, write only to agent-outputs

_REVIEWER_FILE_ACCESS = FileAccessPattern(
    allowed_read=[],  # Can read all files
    allowed_write=[
        ".egg-state/agent-outputs/",
        ".egg-state/reviews/",
    ],
    blocked_write=[
        "**/*.py",
        "**/*.ts",
        "**/*.tsx",
        "**/*.js",
        ".egg-state/contracts/",
    ],
)

REVIEWER_UNIFIED_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_UNIFIED,
    description="Performs unified review of phase output",
    responsibilities=[
        "Review overall quality and correctness",
        "Assess adherence to requirements",
        "Provide approval or revision feedback",
    ],
    dependencies=[AgentRole.INTEGRATOR],  # Implement phase: after integrator
    file_access=_REVIEWER_FILE_ACCESS,
    can_run_in_parallel=True,  # Reviewers can run in parallel
    produces_outputs=["review_verdict"],
    requires_inputs=["integration_report"],
)

REVIEWER_CODE_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CODE,
    description="Performs comprehensive code review",
    responsibilities=[
        "Review code for security, correctness, and robustness",
        "Check for OWASP top 10 vulnerabilities",
        "Assess code quality and maintainability",
    ],
    dependencies=[AgentRole.INTEGRATOR],
    file_access=_REVIEWER_FILE_ACCESS,
    can_run_in_parallel=True,
    produces_outputs=["code_review_verdict"],
    requires_inputs=["changed_files"],
)

REVIEWER_CONTRACT_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_CONTRACT,
    description="Verifies implementation matches contract",
    responsibilities=[
        "Verify acceptance criteria are met",
        "Check contract compliance",
        "Validate task completeness",
    ],
    dependencies=[AgentRole.INTEGRATOR],
    file_access=_REVIEWER_FILE_ACCESS,
    can_run_in_parallel=True,
    produces_outputs=["contract_review_verdict"],
    requires_inputs=["integration_report"],
)

REVIEWER_AGENT_DESIGN_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_AGENT_DESIGN,
    description="Reviews agent-mode design principles",
    responsibilities=[
        "Check for agent-mode design anti-patterns",
        "Verify agent autonomy and constraint compliance",
        "Assess agent interaction patterns",
    ],
    dependencies=[AgentRole.INTEGRATOR],
    file_access=_REVIEWER_FILE_ACCESS,
    can_run_in_parallel=True,
    produces_outputs=["agent_design_verdict"],
    requires_inputs=[],
)


# Registry of all agent roles
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.INTEGRATOR: INTEGRATOR_ROLE,
    AgentRole.ARCHITECT: ARCHITECT_ROLE,
    AgentRole.TASK_PLANNER: TASK_PLANNER_ROLE,
    AgentRole.RISK_ANALYST: RISK_ANALYST_ROLE,
    AgentRole.REVIEWER_UNIFIED: REVIEWER_UNIFIED_ROLE,
    AgentRole.REVIEWER_CODE: REVIEWER_CODE_ROLE,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_ROLE,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_ROLE,
}

# Phase-to-roles mapping (worker agents only, without reviewers)
_IMPLEMENT_ROLES = [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR]
_PLAN_ROLES = [AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST]

# Implement-phase roles including reviewers (for unified orchestrator model)
_IMPLEMENT_ROLES_WITH_REVIEWERS = _IMPLEMENT_ROLES + [
    AgentRole.REVIEWER_UNIFIED,
    AgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CONTRACT,
    AgentRole.REVIEWER_AGENT_DESIGN,
]

# Plan-phase roles including reviewers
_PLAN_ROLES_WITH_REVIEWERS = _PLAN_ROLES + [
    AgentRole.REVIEWER_UNIFIED,
    AgentRole.REVIEWER_AGENT_DESIGN,
]

# Reviewer roles list
_REVIEWER_ROLES = [
    AgentRole.REVIEWER_UNIFIED,
    AgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CONTRACT,
    AgentRole.REVIEWER_AGENT_DESIGN,
]


def get_roles_for_phase(phase: str, include_reviewers: bool = False) -> list[AgentRole]:
    """Get the agent roles defined for a given phase.

    Args:
        phase: Phase name ('implement' or 'plan')
        include_reviewers: If True, include reviewer roles in the list

    Returns:
        List of AgentRole values for the phase

    Raises:
        ValueError: If the phase has no defined multi-agent roles
    """
    if include_reviewers:
        phase_mapping = {
            "implement": _IMPLEMENT_ROLES_WITH_REVIEWERS,
            "plan": _PLAN_ROLES_WITH_REVIEWERS,
        }
    else:
        phase_mapping = {
            "implement": _IMPLEMENT_ROLES,
            "plan": _PLAN_ROLES,
        }
    roles = phase_mapping.get(phase)
    if roles is None:
        raise ValueError(f"No multi-agent roles defined for phase: {phase}")
    return list(roles)


def get_reviewer_roles() -> list[AgentRole]:
    """Get all reviewer roles.

    Returns:
        List of reviewer AgentRole values
    """
    return list(_REVIEWER_ROLES)


def is_reviewer_role(role: AgentRole | str) -> bool:
    """Check if a role is a reviewer role.

    Args:
        role: Role to check

    Returns:
        True if the role is a reviewer
    """
    if isinstance(role, str):
        role = AgentRole(role)
    return role in _REVIEWER_ROLES


def detect_write_overlaps(
    roles: list[AgentRole],
) -> list[tuple[AgentRole, AgentRole, list[str]]]:
    """Detect overlapping write patterns between roles.

    Used for conflict detection before wave dispatch.

    Args:
        roles: List of roles to check for overlaps

    Returns:
        List of (role1, role2, overlapping_patterns) tuples
    """
    overlaps = []
    for i, role1 in enumerate(roles):
        def1 = AGENT_ROLES[role1]
        for role2 in roles[i + 1 :]:
            def2 = AGENT_ROLES[role2]
            shared = set(def1.file_access.allowed_write) & set(def2.file_access.allowed_write)
            # Remove common handoff directories from overlap detection
            shared.discard(".egg-state/agent-outputs/")
            if shared:
                overlaps.append((role1, role2, sorted(shared)))
    return overlaps


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
