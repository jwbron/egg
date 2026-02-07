"""
Pydantic models for SDLC contracts.

These models match the JSON schema at .egg/schemas/contract.schema.json
and provide type-safe manipulation of contract data.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Task completion status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class PhaseStatus(StrEnum):
    """Phase completion status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class PipelinePhase(StrEnum):
    """Pipeline phase identifiers."""

    REFINE = "refine"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR = "pr"


class DecisionType(StrEnum):
    """Decision type - whether human input is required."""

    HITL = "hitl"
    AUTO = "auto"


class CircuitBreakerStatus(StrEnum):
    """Circuit breaker state."""

    CLOSED = "closed"
    OPEN = "open"


class AuditAction(StrEnum):
    """Types of auditable actions."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    BLOCKED = "blocked"


class Issue(BaseModel):
    """Issue metadata."""

    number: int = Field(..., ge=1, description="GitHub issue number")
    title: str = Field(..., min_length=1, description="Issue title")
    url: str = Field(..., description="Full GitHub issue URL")


class DecisionOption(BaseModel):
    """Option for a checkbox decision."""

    id: str = Field(..., description="Option identifier")
    label: str = Field(..., description="Display label")
    description: str | None = Field(None, description="Longer description")


class Decision(BaseModel):
    """Human-in-the-loop decision point."""

    id: str = Field(..., pattern=r"^decision-[0-9]+$", description="Decision ID")
    question: str = Field(..., min_length=1, description="Question requiring decision")
    type: DecisionType = Field(..., description="Decision type")
    options: list[DecisionOption] | None = Field(None, description="Available options")
    resolved: bool = Field(False, description="Whether decision is resolved")
    resolution: str | None = Field(None, description="Chosen resolution")
    resolved_by: str | None = Field(None, description="Who resolved")
    resolved_at: datetime | None = Field(None, description="When resolved")
    debounce_until: datetime | None = Field(None, description="Debounce expiry")


class ReviewFeedback(BaseModel):
    """Reviewer feedback entry."""

    timestamp: datetime = Field(..., description="When feedback was given")
    feedback: str = Field(..., description="Feedback content")
    cycle: int | None = Field(None, ge=1, description="Which review cycle")


class Task(BaseModel):
    """A task within a phase."""

    id: str = Field(..., pattern=r"^task-[0-9]+$", description="Task ID")
    description: str = Field(..., min_length=1, description="Task description")
    acceptance_criteria: str | None = Field(None, description="Completion criteria")
    files: list[str] | None = Field(None, description="Affected files")
    status: TaskStatus = Field(TaskStatus.PENDING, description="Task status")
    commit: str | None = Field(None, pattern=r"^[a-f0-9]{7,40}$", description="Git commit SHA")
    notes: str = Field("", description="Implementation notes")
    review_cycles: int = Field(0, ge=0, description="Per-task review cycles")
    max_cycles: int = Field(3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(False, description="Whether escalated to human")
    feedback: list[str] | None = Field(None, description="Reviewer feedback")


class Phase(BaseModel):
    """An implementation phase containing tasks."""

    id: str = Field(..., pattern=r"^phase-[0-9]+$", description="Phase ID")
    name: str = Field(..., min_length=1, description="Phase name")
    status: PhaseStatus = Field(PhaseStatus.PENDING, description="Phase status")
    review_cycles: int = Field(0, ge=0, description="Implement->review cycles")
    max_cycles: int = Field(3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(False, description="Whether escalated")
    escalation_reason: str | None = Field(None, description="Escalation reason")
    review_feedback: list[ReviewFeedback] | None = Field(None, description="Feedback history")
    tasks: list[Task] = Field(default_factory=list, description="Tasks in this phase")


class AcceptanceCriterion(BaseModel):
    """Overall acceptance criterion for the issue."""

    id: str = Field(..., pattern=r"^ac-[0-9]+$", description="Criterion ID")
    description: str = Field(..., min_length=1, description="Criterion description")
    verified: bool = Field(False, description="Whether verified by reviewer")


class CircuitBreaker(BaseModel):
    """Circuit breaker state for escalation control."""

    total_cycles: int = Field(0, ge=0, description="Total pipeline cycles")
    max_total_cycles: int = Field(10, ge=1, description="Max before full escalation")
    status: CircuitBreakerStatus = Field(CircuitBreakerStatus.CLOSED, description="State")
    opened_at: datetime | None = Field(None, description="When opened")
    opened_reason: str | None = Field(None, description="Why opened")


class AuditEntry(BaseModel):
    """Audit log entry for tracking modifications."""

    timestamp: datetime = Field(..., description="When action occurred")
    actor: str = Field(..., description="Who performed the action")
    role: str | None = Field(None, description="Role of actor")
    action: AuditAction = Field(..., description="Type of action")
    field_path: str = Field(..., description="JSON path of modified field")
    old_value: Any | None = Field(None, description="Previous value")
    new_value: Any | None = Field(None, description="New value")
    reason: str | None = Field(None, description="Reason for blocked operations")


class Contract(BaseModel):
    """
    SDLC Contract - tracks state for structurally enforced agent checkpoints.

    The contract is the source of truth for:
    - Current pipeline phase
    - Task completion status
    - Human decisions
    - Review feedback
    - Audit trail
    """

    schemaVersion: str = Field("1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version")
    issue: Issue = Field(..., description="Issue metadata")
    currentPhase: PipelinePhase = Field(PipelinePhase.REFINE, description="Current phase")
    branch: str | None = Field(None, pattern=r"^egg[-/].+", description="Git branch")
    acceptance_criteria: list[AcceptanceCriterion] | None = Field(
        None, description="Overall acceptance criteria"
    )
    phases: list[Phase] = Field(default_factory=list, description="Implementation phases")
    decisions: list[Decision] | None = Field(None, description="HITL decisions")
    circuit_breaker: CircuitBreaker | None = Field(None, description="Circuit breaker state")
    audit_log: list[AuditEntry] | None = Field(None, description="Audit trail")

    def get_phase(self, phase_id: str) -> Phase | None:
        """Get a phase by ID."""
        for phase in self.phases:
            if phase.id == phase_id:
                return phase
        return None

    def get_task(self, task_id: str) -> tuple[Phase | None, Task | None]:
        """Get a task by ID, returning (phase, task) tuple."""
        for phase in self.phases:
            for task in phase.tasks:
                if task.id == task_id:
                    return phase, task
        return None, None

    def get_decision(self, decision_id: str) -> Decision | None:
        """Get a decision by ID."""
        if not self.decisions:
            return None
        for decision in self.decisions:
            if decision.id == decision_id:
                return decision
        return None

    def next_task_id(self) -> str:
        """Generate the next task ID."""
        max_id = 0
        for phase in self.phases:
            for task in phase.tasks:
                if task.id.startswith("task-"):
                    try:
                        num = int(task.id[5:])
                        max_id = max(max_id, num)
                    except ValueError:
                        pass
        return f"task-{max_id + 1}"

    def next_phase_id(self) -> str:
        """Generate the next phase ID."""
        max_id = 0
        for phase in self.phases:
            if phase.id.startswith("phase-"):
                try:
                    num = int(phase.id[6:])
                    max_id = max(max_id, num)
                except ValueError:
                    pass
        return f"phase-{max_id + 1}"

    def next_decision_id(self) -> str:
        """Generate the next decision ID."""
        max_id = 0
        if self.decisions:
            for decision in self.decisions:
                if decision.id.startswith("decision-"):
                    try:
                        num = int(decision.id[9:])
                        max_id = max(max_id, num)
                    except ValueError:
                        pass
        return f"decision-{max_id + 1}"
