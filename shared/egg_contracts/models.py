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


class CheckStatus(StrEnum):
    """Status values for check results."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class HumanReviewMechanism(StrEnum):
    """Mechanism for human review in a phase."""

    ISSUE_CHECKBOX = "ISSUE_CHECKBOX"
    PR_REVIEW = "PR_REVIEW"


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


class PRMetadata(BaseModel):
    """Planner-generated PR title and description."""

    title: str = Field(..., min_length=1, description="PR title (recommended max 70 chars)")
    description: str = Field(default="", description="PR description/body")


class CheckDefinition(BaseModel):
    """Definition of a check to run during a phase."""

    id: str = Field(
        ...,
        pattern=r"^check-[a-z0-9-]+$",
        description="Unique check identifier (e.g., check-lint)",
    )
    name: str = Field(..., min_length=1, description="Human-readable check name")
    script: str = Field(..., min_length=1, description="Script to run for this check")
    required: bool = Field(default=True, description="Whether this check must pass")
    retry_on_fail: bool = Field(default=False, description="Whether to retry on failure")
    max_retries: int = Field(default=0, ge=0, description="Maximum number of retries")


class CheckResult(BaseModel):
    """Result of running a check."""

    check_id: str = Field(
        ...,
        pattern=r"^check-[a-z0-9-]+$",
        description="ID of the check that was run",
    )
    status: CheckStatus = Field(..., description="Check result status")
    message: str = Field(default="", description="Human-readable result message")
    details: dict[str, Any] = Field(default_factory=dict, description="Additional details")
    fixable: bool = Field(default=False, description="Whether this failure can be auto-fixed")


class PhaseConfig(BaseModel):
    """Configuration for a pipeline phase."""

    checks: list[CheckDefinition] = Field(
        default_factory=list, description="Checks to run in this phase"
    )
    max_review_cycles: int = Field(
        default=3, ge=1, description="Max review cycles before escalation"
    )
    human_review_mechanism: HumanReviewMechanism = Field(
        default=HumanReviewMechanism.ISSUE_CHECKBOX,
        description="Mechanism for human review",
    )


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


class AgentExecutionStatus(StrEnum):
    """Status values for agent executions."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class AgentRoleType(StrEnum):
    """Agent role types for multi-agent orchestration."""

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"


class AgentExecutionModel(BaseModel):
    """Tracks the execution state of a single agent.

    Used by the orchestrator to track which agents have run,
    their results, and any handoff data they produced.
    """

    role: AgentRoleType = Field(..., description="The agent role")
    status: AgentExecutionStatus = Field(
        default=AgentExecutionStatus.PENDING, description="Current execution status"
    )
    started_at: datetime | None = Field(default=None, description="When agent started")
    completed_at: datetime | None = Field(default=None, description="When agent completed")
    commit: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA if agent made changes",
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Handoff data produced by agent"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, ge=0, description="Number of retry attempts")


class MultiAgentConfig(BaseModel):
    """Configuration for multi-agent orchestration.

    Controls how agents are dispatched during the implement phase.
    """

    enabled: bool = Field(default=True, description="Whether multi-agent mode is enabled")
    max_retries: int = Field(default=2, ge=0, description="Max retries per agent")
    parallel_execution: bool = Field(
        default=True, description="Allow parallel execution of independent agents"
    )
    roles_enabled: list[AgentRoleType] = Field(
        default_factory=lambda: list(AgentRoleType),
        description="Which agent roles are enabled",
    )


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
    phase_configs: dict[PipelinePhase, PhaseConfig] | None = Field(
        default=None,
        description="Optional phase-specific configurations (overrides defaults)",
    )
    # Multi-agent orchestration fields
    agent_executions: list[AgentExecutionModel] = Field(
        default_factory=list,
        description="Execution state for each agent in multi-agent mode",
    )
    multi_agent_config: MultiAgentConfig | None = Field(
        default=None,
        description="Configuration for multi-agent orchestration",
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

    def get_agent_execution(self, role: AgentRoleType | str) -> AgentExecutionModel | None:
        """Get the execution state for an agent role.

        Args:
            role: The agent role (string or AgentRoleType enum)

        Returns:
            AgentExecutionModel for the role, or None if not found
        """
        if isinstance(role, str):
            role = AgentRoleType(role)
        for execution in self.agent_executions:
            if execution.role == role:
                return execution
        return None

    def is_multi_agent_enabled(self) -> bool:
        """Check if multi-agent mode is enabled for this contract.

        Returns:
            True if multi-agent orchestration is enabled
        """
        if self.multi_agent_config is None:
            return True  # Default to enabled
        return self.multi_agent_config.enabled

    def get_pending_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that are pending execution.

        Returns:
            List of AgentExecutionModel with PENDING status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.PENDING]

    def get_running_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that are currently running.

        Returns:
            List of AgentExecutionModel with RUNNING status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.RUNNING]

    def get_completed_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that have completed (successfully).

        Returns:
            List of AgentExecutionModel with COMPLETE status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.COMPLETE]

    def get_failed_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that have failed.

        Returns:
            List of AgentExecutionModel with FAILED status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.FAILED]

    def all_agents_complete(self) -> bool:
        """Check if all agents have completed (successfully or skipped).

        Returns:
            True if all agents are in a terminal successful state
        """
        if not self.agent_executions:
            return True  # No agents means nothing to complete
        return all(
            ex.status in (AgentExecutionStatus.COMPLETE, AgentExecutionStatus.SKIPPED)
            for ex in self.agent_executions
        )
