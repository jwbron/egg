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
    INSPECTOR_PATTERNS,
    OVERSEER_PATTERNS,
    AgentFilePattern,
    AgentRole,
)

__all__ = [
    "AGENT_PATTERNS",
    "AUTOFIXER_PATTERNS",
    "AgentFilePattern",
    "AgentRestrictionResult",
    "AgentRole",
    "CONFLICT_RESOLVER_PATTERNS",
    "INSPECTOR_PATTERNS",
    "OVERSEER_PATTERNS",
    "check_agent_file_access",
    "check_agent_gh_operation",
    "get_agent_pattern",
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
