"""
Pydantic models for orchestrator pipeline state.

These models represent the orchestrator's view of pipeline execution,
including container state, HITL decisions, and agent coordination.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, Field, field_validator, model_validator


class PipelineStatus(StrEnum):
    """Overall status of a pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentExecutionStatus(StrEnum):
    """Status of an individual agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class ContainerStatus(StrEnum):
    """Status of a sandbox container."""

    PENDING = "pending"
    CREATING = "creating"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    REMOVED = "removed"


class DecisionStatus(StrEnum):
    """Status of a HITL decision."""

    PENDING = "pending"
    RESOLVED = "resolved"
    TIMEOUT = "timeout"  # Vestigial: kept for backwards compatibility with persisted pipeline state
    CANCELLED = "cancelled"


# Import AgentRole from the canonical source in egg_contracts.
# Re-exported here for backward compatibility.
from egg_contracts.agent_roles import AgentRole  # noqa: F401


class ReviewerType(StrEnum):
    """Reviewer specialization types matching GHA reviewer matrix."""

    AGENT_DESIGN = "agent-design"
    CODE = "code"
    CONTRACT = "contract"


class ReviewVerdict(BaseModel):
    """Verdict from an agentic review cycle."""

    verdict: str = Field(..., description="'approved' or 'needs_revision'")
    summary: str = Field(default="", description="Brief summary of review findings")
    analysis: str = Field(
        default="",
        description="Detailed analysis of the reviewed work, populated regardless of verdict",
    )
    suggestions: str = Field(
        default="",
        description="Non-blocking suggestions for improvement, even when approving",
    )
    feedback: str = Field(default="", description="Blocking feedback requiring revision")
    timestamp: str = Field(default="", description="ISO 8601 timestamp")


class AggregatedReviewResult(NamedTuple):
    """Result of aggregating multiple review verdicts.

    Attributes:
        verdict: Overall verdict — 'approved' or 'needs_revision'.
        blocking_feedback: Combined feedback from needs_revision verdicts only.
        advisory_content: Combined analysis and suggestions from ALL verdicts
            (including approved), for observability and logging.
    """

    verdict: str
    blocking_feedback: str
    advisory_content: str


class ContainerInfo(BaseModel):
    """Information about a sandbox container."""

    container_id: str = Field(..., description="Docker container ID")
    container_name: str = Field(..., description="Container name")
    status: ContainerStatus = Field(default=ContainerStatus.PENDING, description="Container status")
    started_at: datetime | None = Field(default=None, description="When container started")
    exited_at: datetime | None = Field(default=None, description="When container exited")
    exit_code: int | None = Field(default=None, description="Container exit code")
    agent_role: AgentRole | None = Field(
        default=None, description="Agent role if multi-agent execution"
    )
    session_token: str | None = Field(default=None, description="Session token for gateway auth")

    @model_validator(mode="before")
    @classmethod
    def _migrate_removed_roles(cls, data: Any) -> Any:
        """Map removed agent_role values for backward compatibility."""
        if isinstance(data, dict):
            role = data.get("agent_role")
            if isinstance(role, str) and role in _REMOVED_ROLE_MIGRATION:
                data = {**data, "agent_role": _REMOVED_ROLE_MIGRATION[role]}
        return data


_REMOVED_ROLE_MIGRATION: dict[str, str] = {
    "checker": "tester",
    "reviewer_unified": "reviewer_code",
    "reviewer": "reviewer_code",
}


class AgentExecution(BaseModel):
    """State of a single agent execution."""

    role: AgentRole = Field(..., description="Agent role")
    status: AgentExecutionStatus = Field(
        default=AgentExecutionStatus.PENDING, description="Execution status"
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_removed_roles(cls, data: Any) -> Any:
        """Map removed role values to their replacements for backward compatibility.

        Persisted pipeline state may contain 'checker' or 'reviewer_unified' roles
        from before these roles were absorbed into tester/reviewer_code.
        """
        if isinstance(data, dict):
            role = data.get("role")
            if isinstance(role, str) and role in _REMOVED_ROLE_MIGRATION:
                data = {**data, "role": _REMOVED_ROLE_MIGRATION[role]}
        return data

    container_id: str | None = Field(default=None, description="Container ID if running")
    started_at: datetime | None = Field(default=None, description="When started")
    completed_at: datetime | None = Field(default=None, description="When completed")
    commit: str | None = Field(default=None, description="Commit SHA if changes made")
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Handoff data for dependent agents"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, ge=0, description="Number of retries")
    conflicts: list[str] = Field(
        default_factory=list, description="Files with unresolved merge conflicts"
    )


class HITLDecision(BaseModel):
    """A human-in-the-loop decision request."""

    id: str = Field(..., description="Unique decision ID")
    question: str = Field(..., min_length=1, description="Question for human")
    context: str = Field(default="", description="Additional context")
    options: list[str] = Field(
        default_factory=list, description="Available options (empty for free-form)"
    )
    decision_type: Literal["phase_gate", "choice", "feedback"] = Field(
        default="choice",
        description="Type of decision: 'phase_gate', 'choice', or 'feedback'",
    )
    questions: list[dict[str, str]] = Field(
        default_factory=list,
        description="Structured feedback questions with keys: id, question, answer",
    )
    status: DecisionStatus = Field(default=DecisionStatus.PENDING, description="Decision status")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="When created")
    resolved_at: datetime | None = Field(default=None, description="When resolved")
    resolution: str | None = Field(default=None, description="Human's response")
    phase: PipelinePhase | None = Field(
        default=None, description="Pipeline phase when decision was created"
    )
    content_changed: bool | None = Field(
        default=None,
        description="Whether the phase output changed compared to the previous decision's context (literal string comparison, not semantic)",
    )


class CycleTiming(BaseModel):
    """Timing for a single review cycle within a phase."""

    cycle: int = Field(..., description="Cycle number (0-indexed)")
    started_at: datetime = Field(..., description="When this cycle's work began")
    completed_at: datetime | None = Field(default=None, description="When this cycle ended")
    commit_sha: str | None = Field(
        default=None,
        description="HEAD commit SHA at cycle start, used for delta reviews",
    )


class PhaseExecution(BaseModel):
    """State of a single phase execution."""

    phase: PipelinePhase = Field(..., description="Phase being executed")
    status: PipelineStatus = Field(default=PipelineStatus.PENDING, description="Phase status")
    started_at: datetime | None = Field(default=None, description="When started")
    work_started_at: datetime | None = Field(default=None, description="When first agent spawned")
    completed_at: datetime | None = Field(default=None, description="When completed")
    containers: list[ContainerInfo] = Field(
        default_factory=list, description="Containers spawned for this phase"
    )
    agents: list[AgentExecution] = Field(
        default_factory=list, description="Agent executions (implement phase)"
    )
    review_cycles: int = Field(default=0, ge=0, description="Agentic review cycles completed")
    hitl_review_cycles: int = Field(default=0, ge=0, description="HITL revision cycles completed")
    cycle_timings: list[CycleTiming] = Field(
        default_factory=list, description="Per-cycle timing records"
    )
    artifacts: dict[str, str] = Field(
        default_factory=dict, description="Produced artifacts (file paths)"
    )
    error: str | None = Field(default=None, description="Error if failed")
    hitl_feedback: str | None = Field(
        default=None,
        description="HITL revision feedback preserved across recovery restarts",
    )
    phase_start_sha: str | None = Field(
        default=None,
        description="Branch tip SHA at phase start, for completion signal verification",
    )


class PipelineConfig(BaseModel):
    """Configuration for pipeline execution."""

    auto_create_pr: bool = Field(
        default=True,
        description="Deprecated: PR creation is now always handled by the orchestrator. "
        "This field is retained for backwards compatibility with existing pipeline configs.",
    )
    parallel_agents: bool = Field(default=True, description="Run independent agents in parallel")
    max_review_cycles: int = Field(default=3, ge=1, description="Max review cycles per phase")
    max_hitl_review_cycles: int = Field(
        default=3,
        ge=1,
        description="Max HITL revision cycles per phase (independent of agentic review budget)",
    )
    hitl_gates: bool = Field(
        default=True,
        description="Pause for human approval after refine and plan phases",
    )
    concurrent_execution: bool = Field(
        default=False,
        description="Enable concurrent agent execution within a phase (all agents start simultaneously)",
    )
    concurrent_phases: list[str] = Field(
        default=["refine", "plan", "implement"],
        description=(
            "Phases where BRC concurrent execution is activated even when "
            "concurrent_execution is False. Ignored when concurrent_execution is True."
        ),
    )
    max_concurrent_agents: int = Field(
        default=6, ge=1, description="Maximum concurrent agents per phase"
    )
    message_poll_hint_seconds: int = Field(
        default=30, ge=1, description="Suggested message polling interval for agents"
    )
    consensus_timeout_minutes: int = Field(
        default=30, ge=1, description="Timeout for consensus before HITL escalation"
    )
    agent_idle_timeout_minutes: int = Field(
        default=60, ge=1, description="Timeout for idle agents before termination"
    )
    # Overseer and tripwire configuration
    overseer_enabled: bool = Field(
        default=True, description="Enable the overseer agent for pipeline health monitoring"
    )
    orchestrator_heartbeat_timeout_seconds: int = Field(
        default=120, ge=10, description="Seconds without heartbeat/progress before auto-nudge"
    )
    orchestrator_error_repeat_threshold: int = Field(
        default=3, ge=1, description="Identical error count before escalation"
    )
    orchestrator_message_rate_limit: int = Field(
        default=20, ge=1, description="Max messages per minute before auto-throttle"
    )
    overseer_poll_interval_seconds: int = Field(
        default=30, ge=5, description="Overseer polling interval in seconds"
    )
    overseer_max_redirects_before_escalation: int = Field(
        default=2, ge=1, description="Max redirect attempts before HITL escalation"
    )
    overseer_decision_maker_model: str = Field(
        default="sonnet", description="LLM model for overseer decision-making tier"
    )
    overseer_max_respawns: int = Field(
        default=3,
        ge=0,
        le=50,
        description="Max times to respawn the overseer if it exits mid-pipeline",
    )
    start_phase: str | None = Field(
        default=None,
        description="Phase to start execution from, skipping earlier phases. "
        "Valid values: 'plan', 'implement'. When set, the pipeline starts "
        "at this phase instead of 'refine'.",
    )
    implement_roles: list[str] | None = Field(
        default=None,
        description="Override which roles run in the implement phase. "
        "When set, only these roles are spawned instead of the defaults. "
        "Example: ['coder', 'reviewer_code'] for a lightweight coder+reviewer flow.",
    )

    @field_validator("start_phase")
    @classmethod
    def validate_start_phase(cls, v: str | None) -> str | None:
        if v is not None:
            valid = {"plan", "implement"}
            if v not in valid:
                raise ValueError(f"Invalid start_phase: {v!r}. Must be one of {sorted(valid)}")
        return v

    @field_validator("implement_roles")
    @classmethod
    def validate_implement_roles(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if not v:
                raise ValueError("implement_roles cannot be empty — omit the field to use defaults")
            valid = {r.value for r in AgentRole}
            invalid = [name for name in v if name not in valid]
            if invalid:
                raise ValueError(
                    f"Invalid role names in implement_roles: {invalid}. "
                    f"Valid roles: {sorted(valid)}"
                )
        return v

    @field_validator("concurrent_phases")
    @classmethod
    def validate_concurrent_phases(cls, v: list[str]) -> list[str]:
        valid = {p.value for p in PipelinePhase}
        invalid = [p for p in v if p not in valid]
        if invalid:
            raise ValueError(f"Invalid phase names: {invalid}")
        return v


class Pipeline(BaseModel):
    """Complete state of an SDLC pipeline execution.

    This is the root model stored in .egg-state/pipelines/{id}.json.
    It tracks all state needed to orchestrate a pipeline from issue to PR.
    """

    id: str = Field(
        ..., description="Unique pipeline ID (e.g., 'issue-496' or 'pipeline-a1b2c3d4')"
    )
    issue_number: int | None = Field(default=None, ge=1, description="GitHub issue number")
    repo: str | None = Field(default=None, description="Repository in owner/name format")
    branch: str | None = Field(default=None, description="Work branch name")
    prompt: str | None = Field(default=None, description="User prompt for prompt-driven pipelines")
    status: PipelineStatus = Field(
        default=PipelineStatus.PENDING, description="Overall pipeline status"
    )
    current_phase: PipelinePhase = Field(default=PipelinePhase.REFINE, description="Current phase")
    config: PipelineConfig = Field(
        default_factory=PipelineConfig, description="Pipeline configuration"
    )
    phases: dict[str, PhaseExecution] = Field(
        default_factory=dict, description="Phase execution state by phase name"
    )
    decisions: list[HITLDecision] = Field(default_factory=list, description="HITL decisions")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When pipeline was created"
    )
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update time")
    contract_synced: bool = Field(default=True, description="Whether state is synced with contract")
    network_mode: str | None = Field(
        default=None,
        description="Network mode for spawned containers: 'public', 'private', or None (auto from pipeline mode)",
    )
    error: str | None = Field(default=None, description="Error if failed")
    version: int = Field(
        default=1,
        ge=1,
        description="Optimistic locking version (incremented on each save)",
    )

    def get_phase_execution(self, phase: PipelinePhase) -> PhaseExecution:
        """Get or create phase execution state."""
        if phase.value not in self.phases:
            self.phases[phase.value] = PhaseExecution(phase=phase)
        return self.phases[phase.value]

    def get_pending_decisions(self) -> list[HITLDecision]:
        """Get all pending HITL decisions."""
        return [d for d in self.decisions if d.status == DecisionStatus.PENDING]

    def add_decision(
        self,
        question: str,
        options: list[str] | None = None,
        decision_type: Literal["phase_gate", "choice", "feedback"] = "choice",
        questions: list[dict[str, str]] | None = None,
        phase: PipelinePhase | None = None,
        content_changed: bool | None = None,
    ) -> HITLDecision:
        """Add a new HITL decision request."""
        decision_id = f"decision-{len(self.decisions) + 1}"
        decision = HITLDecision(
            id=decision_id,
            question=question,
            options=options or [],
            decision_type=decision_type,
            questions=questions or [],
            phase=phase,
            content_changed=content_changed,
        )
        self.decisions.append(decision)
        self.updated_at = datetime.utcnow()
        return decision

    def resolve_decision(self, decision_id: str, resolution: str) -> HITLDecision | None:
        """Resolve a HITL decision."""
        for decision in self.decisions:
            if decision.id == decision_id and decision.status == DecisionStatus.PENDING:
                decision.status = DecisionStatus.RESOLVED
                decision.resolution = resolution
                decision.resolved_at = datetime.utcnow()
                self.updated_at = datetime.utcnow()
                return decision
        return None


class PipelineEvent(BaseModel):
    """Event emitted during pipeline execution."""

    pipeline_id: str = Field(..., description="Pipeline ID")
    event_type: str = Field(..., description="Event type")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="When event occurred")
    phase: PipelinePhase | None = Field(default=None, description="Phase if applicable")
    agent_role: AgentRole | None = Field(default=None, description="Agent if applicable")
    container_id: str | None = Field(default=None, description="Container if applicable")
    data: dict[str, Any] = Field(default_factory=dict, description="Event data")


class ProgressState(StrEnum):
    """State of a structured progress event."""

    WORKING = "working"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class ProgressEvent(BaseModel):
    """Structured progress event emitted by agents."""

    id: str = Field(..., description="Unique event ID")
    pipeline_id: str = Field(..., description="Pipeline ID")
    agent_role: str = Field(..., description="Agent role that emitted this event")
    step: str = Field(..., description="Current step description")
    state: ProgressState = Field(..., description="Progress state")
    detail: str = Field(default="", description="Optional detail text")
    blocker: str = Field(default="", description="Blocker description if state is blocked")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Event timestamp"
    )
