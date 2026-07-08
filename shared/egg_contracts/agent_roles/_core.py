"""Core types for agent role definitions (#3543 decomposition).

Enums (``AgentCategory``, ``AgentRole``, ``AgentStatus``), the
``FileAccessPattern`` / ``AgentRoleDefinition`` dataclasses, the
``AgentExecution`` tracking dataclass, and the write-restriction list
shared by every reviewer role definition across the per-phase modules.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from egg_restrictions.matchers import match_pattern


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
    Analysis roles: ARCHITECT, TASK_PLANNER, RISK_ANALYST, REFINER, SIMPLIFIER
    Review roles: REVIEWER_CODE, REVIEWER_CODE_HOLISTIC,
                  REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN,
                  REVIEWER_REFINE, FIRST_PRINCIPLES_REVIEWER,
                  REVIEWER_PLAN, REVIEWER_SECURITY, REVIEWER_CONCURRENCY
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
    # Distills a producer's draft (refine analysis / plan) into a
    # human-focused, jargon-free companion copy. Runs in the refine and
    # plan phases as a producer gated on the upstream producer's
    # CONSENSUS_PROPOSE (the coder→tester dependency pattern).
    SIMPLIFIER = "simplifier"
    # Review roles
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CODE_HOLISTIC = "reviewer_code_holistic"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    # Adversarial first-principles reviewer of the refine phase. Reviews the
    # pipeline's *seed* (the operator's task statement) and the direction the
    # refiner's analysis is taking — questioning whether the premise is sound
    # and the direction appropriate. It never NACKs the refiner (a NACK only
    # re-runs the producer, which cannot change the operator-owned seed);
    # instead it ACKs and, when warranted, surfaces a redirect as a
    # phase-scoped HITL decision for the operator. Named mandate-first rather
    # than ``reviewer_first_principles`` so the role reads as what it does in
    # the escalations an operator sees.
    FIRST_PRINCIPLES_REVIEWER = "first_principles_reviewer"
    REVIEWER_PLAN = "reviewer_plan"
    REVIEWER_SECURITY = "reviewer_security"
    REVIEWER_CONCURRENCY = "reviewer_concurrency"
    # Utility roles (cross-cutting support)
    AUTOFIXER = "autofixer"
    CONFLICT_RESOLVER = "conflict_resolver"
    # Read-only shared-evidence gatherer (#3523 §5). Assembles the evidence
    # pack a review wave shares as a byte-identical prompt prefix. Casts no
    # verdict, posts nothing, has no GitHub access, writes only its handoff
    # dir — those capabilities are structurally excluded below (file_access +
    # a SYSTEM contract role + deny-by-default gh restrictions).
    EVIDENCE_GATHERER = "evidence_gatherer"
    # Interface roles (external system interaction)
    OVERSEER = "overseer"


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
