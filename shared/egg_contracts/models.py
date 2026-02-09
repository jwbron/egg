"""
Pydantic models for SDLC contract schema.

These models match the JSON schema defined in .egg/schemas/contract.schema.json
and provide validation and type safety for contract operations.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskStatus(StrEnum):
    """Status values for tasks."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


class PhaseStatus(StrEnum):
    """Status values for phases."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class PipelinePhase(StrEnum):
    """Current pipeline phase."""

    REFINE = "refine"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR = "pr"


class DecisionType(StrEnum):
    """Types of decisions."""

    HITL = "hitl"
    AUTO = "auto"


class CircuitBreakerStatus(StrEnum):
    """Circuit breaker status values."""

    CLOSED = "closed"
    OPEN = "open"


class AuditAction(StrEnum):
    """Types of audit actions."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    TRANSITION = "transition"


class AuditRole(StrEnum):
    """Roles for audit entries."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"
    SYSTEM = "system"


class IssueInfo(BaseModel):
    """Issue metadata."""

    number: int = Field(..., ge=1, description="GitHub issue number")
    title: str = Field(..., min_length=1, description="Issue title")
    url: str = Field(..., description="Issue URL")


class AcceptanceCriterion(BaseModel):
    """Top-level acceptance criterion."""

    id: str = Field(..., pattern=r"^ac-[0-9]+$", description="Unique identifier")
    description: str = Field(..., min_length=1, description="Human-readable description")
    verified: bool = Field(default=False, description="Whether verified by reviewer")


class ReviewFeedback(BaseModel):
    """Feedback from reviewer on a task."""

    timestamp: datetime = Field(..., description="When feedback was given")
    task_id: str = Field(..., description="Task this feedback applies to")
    feedback: str = Field(..., min_length=1, description="Reviewer feedback")
    status: TaskStatus | None = Field(default=None, description="Status assigned by reviewer")


class Task(BaseModel):
    """A task within a phase."""

    id: str = Field(
        ...,
        pattern=r"^task-[0-9]+(-[0-9]+)?$",
        description="Unique task identifier (task-N or task-P-N format)",
    )
    description: str = Field(..., min_length=1, description="Task description")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Task status")
    commit: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA",
    )
    notes: str = Field(default="", description="Implementation notes")
    acceptance_criteria: str = Field(default="", description="Acceptance criteria")
    files_affected: list[str] = Field(default_factory=list, description="Files affected")
    review_cycles: int = Field(default=0, ge=0, description="Number of review cycles")
    max_cycles: int = Field(default=3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(default=False, description="Whether escalated")

    @field_validator("commit", mode="before")
    @classmethod
    def validate_commit(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        return str(v)


class Phase(BaseModel):
    """An implementation phase containing tasks."""

    id: str = Field(..., pattern=r"^phase-[0-9]+$", description="Unique phase identifier")
    name: str = Field(..., min_length=1, description="Human-readable phase name")
    status: PhaseStatus = Field(default=PhaseStatus.PENDING, description="Phase status")
    review_cycles: int = Field(default=0, ge=0, description="Number of review cycles")
    max_cycles: int = Field(default=3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(default=False, description="Whether escalated")
    escalation_reason: str | None = Field(default=None, description="Reason for escalation")
    tasks: list[Task] = Field(default_factory=list, description="Tasks in this phase")
    review_feedback: list[ReviewFeedback] = Field(
        default_factory=list, description="Feedback from reviewer"
    )


class DecisionOption(BaseModel):
    """An option for a decision."""

    id: str = Field(..., description="Option identifier")
    label: str = Field(..., description="Option label")
    description: str | None = Field(default=None, description="Option description")


class Decision(BaseModel):
    """A HITL decision point."""

    id: str = Field(..., pattern=r"^decision-[0-9]+$", description="Unique decision identifier")
    question: str = Field(..., min_length=1, description="The decision question")
    type: DecisionType = Field(..., description="Decision type")
    options: list[DecisionOption] = Field(default_factory=list, description="Available options")
    resolved: bool = Field(default=False, description="Whether resolved")
    resolution: str | None = Field(default=None, description="Selected resolution")
    resolved_by: str | None = Field(default=None, description="Who resolved")
    resolved_at: datetime | None = Field(default=None, description="When resolved")
    debounce_until: datetime | None = Field(default=None, description="Debounce expiration")


class CircuitBreaker(BaseModel):
    """Circuit breaker state for pipeline."""

    total_cycles: int = Field(default=0, ge=0, description="Total pipeline cycles")
    max_total_cycles: int = Field(default=10, ge=1, description="Max cycles before escalation")
    status: CircuitBreakerStatus = Field(
        default=CircuitBreakerStatus.CLOSED, description="Circuit breaker status"
    )


class PRMetadata(BaseModel):
    """Planner-generated PR title and description."""

    title: str = Field(..., min_length=1, description="PR title (recommended max 70 chars)")
    description: str = Field(default="", description="PR description/body")


class FeedbackQuestion(BaseModel):
    """A question for human feedback."""

    id: str = Field(..., pattern=r"^Q[0-9]+$", description="Unique question identifier (e.g., Q1)")
    question: str = Field(..., min_length=1, description="The question text")
    answer: str | None = Field(default=None, description="Human-provided answer (free-form text)")


class Feedback(BaseModel):
    """Feedback request for collecting open-ended questions from humans."""

    id: str = Field(
        ...,
        pattern=r"^feedback-[0-9]+$",
        description="Unique feedback identifier (e.g., feedback-1)",
    )
    phase: PipelinePhase | None = Field(
        default=None, description="Pipeline phase this feedback belongs to"
    )
    questions: list[FeedbackQuestion] = Field(
        ..., min_length=1, description="List of questions for the human"
    )
    submitted: bool = Field(default=False, description="Whether the feedback has been submitted")
    submitted_by: str | None = Field(
        default=None, description="GitHub username who submitted the feedback"
    )
    submitted_at: datetime | None = Field(
        default=None, description="When the feedback was submitted"
    )
    comment_id: int | None = Field(
        default=None, description="GitHub comment ID containing this feedback"
    )
    debounce_until: datetime | None = Field(
        default=None, description="Debounce expiration timestamp"
    )

    def get_question(self, question_id: str) -> FeedbackQuestion | None:
        """Get a specific question by ID."""
        for question in self.questions:
            if question.id == question_id:
                return question
        return None

    def get_unanswered_questions(self) -> list[FeedbackQuestion]:
        """Get all questions that haven't been answered."""
        return [q for q in self.questions if q.answer is None]

    def all_questions_answered(self) -> bool:
        """Check if all questions have been answered."""
        return all(q.answer is not None for q in self.questions)


class AuditEntry(BaseModel):
    """Audit log entry for contract modifications."""

    timestamp: datetime = Field(..., description="When the action occurred")
    actor: str = Field(..., description="Who performed the action")
    role: AuditRole = Field(..., description="Role of the actor")
    action: AuditAction = Field(..., description="Action performed")
    field_path: str = Field(..., description="JSON path of modified field")
    old_value: Any = Field(default=None, description="Previous value")
    new_value: Any = Field(default=None, description="New value")
    reason: str | None = Field(default=None, description="Reason for change")


class Contract(BaseModel):
    """The complete SDLC contract."""

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    issue: IssueInfo = Field(..., description="Issue metadata")
    current_phase: PipelinePhase = Field(
        default=PipelinePhase.REFINE, description="Current pipeline phase"
    )
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        default_factory=list, description="Top-level acceptance criteria"
    )
    phases: list[Phase] = Field(default_factory=list, description="Implementation phases")
    decisions: list[Decision] = Field(default_factory=list, description="HITL decisions")
    circuit_breaker: CircuitBreaker = Field(
        default_factory=CircuitBreaker, description="Circuit breaker state"
    )
    workflow_owner: str | None = Field(
        default=None,
        description="GitHub username of the user who initiated the SDLC workflow",
    )
    audit_log: list[AuditEntry] = Field(default_factory=list, description="Audit trail")
    refine_review_cycles: int = Field(
        default=0, ge=0, description="Number of refine phase review cycles"
    )
    refine_review_feedback: str = Field(default="", description="Feedback from last refine review")
    plan_review_cycles: int = Field(
        default=0, ge=0, description="Number of plan phase review cycles"
    )
    plan_review_feedback: str = Field(default="", description="Feedback from last plan review")
    pr: PRMetadata | None = Field(
        default=None, description="Planner-generated PR metadata for use during PR creation"
    )
    feedback: Feedback | None = Field(
        default=None, description="Active feedback request for collecting open-ended questions"
    )

    def get_task(self, phase_id: str, task_id: str) -> Task | None:
        """Get a specific task by phase and task ID."""
        for phase in self.phases:
            if phase.id == phase_id:
                for task in phase.tasks:
                    if task.id == task_id:
                        return task
        return None

    def get_phase(self, phase_id: str) -> Phase | None:
        """Get a specific phase by ID."""
        for phase in self.phases:
            if phase.id == phase_id:
                return phase
        return None

    def get_decision(self, decision_id: str) -> Decision | None:
        """Get a specific decision by ID."""
        for decision in self.decisions:
            if decision.id == decision_id:
                return decision
        return None
