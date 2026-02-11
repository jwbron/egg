"""
Pydantic models for orchestrator pipeline state.

These models represent the orchestrator's view of pipeline execution,
including container state, HITL decisions, and agent coordination.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PipelinePhase(StrEnum):
    """Current phase in the SDLC pipeline."""

    REFINE = "refine"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR = "pr"


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
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AgentRole(StrEnum):
    """Agent roles in multi-agent execution."""

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"


class ContainerInfo(BaseModel):
    """Information about a sandbox container."""

    container_id: str = Field(..., description="Docker container ID")
    container_name: str = Field(..., description="Container name")
    status: ContainerStatus = Field(
        default=ContainerStatus.PENDING, description="Container status"
    )
    started_at: datetime | None = Field(default=None, description="When container started")
    exited_at: datetime | None = Field(default=None, description="When container exited")
    exit_code: int | None = Field(default=None, description="Container exit code")
    agent_role: AgentRole | None = Field(
        default=None, description="Agent role if multi-agent execution"
    )
    session_token: str | None = Field(
        default=None, description="Session token for gateway auth"
    )


class AgentExecution(BaseModel):
    """State of a single agent execution."""

    role: AgentRole = Field(..., description="Agent role")
    status: AgentExecutionStatus = Field(
        default=AgentExecutionStatus.PENDING, description="Execution status"
    )
    container_id: str | None = Field(default=None, description="Container ID if running")
    started_at: datetime | None = Field(default=None, description="When started")
    completed_at: datetime | None = Field(default=None, description="When completed")
    commit: str | None = Field(default=None, description="Commit SHA if changes made")
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Handoff data for dependent agents"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, ge=0, description="Number of retries")


class HITLDecision(BaseModel):
    """A human-in-the-loop decision request."""

    id: str = Field(..., description="Unique decision ID")
    question: str = Field(..., min_length=1, description="Question for human")
    context: str = Field(default="", description="Additional context")
    options: list[str] = Field(
        default_factory=list, description="Available options (empty for free-form)"
    )
    status: DecisionStatus = Field(
        default=DecisionStatus.PENDING, description="Decision status"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When created"
    )
    resolved_at: datetime | None = Field(default=None, description="When resolved")
    resolution: str | None = Field(default=None, description="Human's response")
    timeout_seconds: int = Field(
        default=3600, ge=60, description="Timeout in seconds"
    )


class PhaseExecution(BaseModel):
    """State of a single phase execution."""

    phase: PipelinePhase = Field(..., description="Phase being executed")
    status: PipelineStatus = Field(
        default=PipelineStatus.PENDING, description="Phase status"
    )
    started_at: datetime | None = Field(default=None, description="When started")
    completed_at: datetime | None = Field(default=None, description="When completed")
    containers: list[ContainerInfo] = Field(
        default_factory=list, description="Containers spawned for this phase"
    )
    agents: list[AgentExecution] = Field(
        default_factory=list, description="Agent executions (implement phase)"
    )
    review_cycles: int = Field(default=0, ge=0, description="Review cycles completed")
    artifacts: dict[str, str] = Field(
        default_factory=dict, description="Produced artifacts (file paths)"
    )
    error: str | None = Field(default=None, description="Error if failed")


class PipelineConfig(BaseModel):
    """Configuration for pipeline execution."""

    auto_create_pr: bool = Field(
        default=True, description="Auto-create PR on implementation complete"
    )
    multi_agent: bool = Field(
        default=True, description="Use multi-agent execution in implement phase"
    )
    parallel_agents: bool = Field(
        default=True, description="Run independent agents in parallel"
    )
    max_review_cycles: int = Field(
        default=3, ge=1, description="Max review cycles per phase"
    )
    decision_timeout: int = Field(
        default=3600, ge=60, description="HITL decision timeout in seconds"
    )


class Pipeline(BaseModel):
    """Complete state of an SDLC pipeline execution.

    This is the root model stored in .egg-state/pipelines/{id}.json.
    It tracks all state needed to orchestrate a pipeline from issue to PR.
    """

    id: str = Field(..., description="Unique pipeline ID (e.g., 'issue-496')")
    issue_number: int = Field(..., ge=1, description="GitHub issue number")
    repo: str = Field(..., description="Repository in owner/name format")
    branch: str = Field(..., description="Work branch name")
    status: PipelineStatus = Field(
        default=PipelineStatus.PENDING, description="Overall pipeline status"
    )
    current_phase: PipelinePhase = Field(
        default=PipelinePhase.REFINE, description="Current phase"
    )
    config: PipelineConfig = Field(
        default_factory=PipelineConfig, description="Pipeline configuration"
    )
    phases: dict[str, PhaseExecution] = Field(
        default_factory=dict, description="Phase execution state by phase name"
    )
    decisions: list[HITLDecision] = Field(
        default_factory=list, description="HITL decisions"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When pipeline was created"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, description="Last update time"
    )
    contract_synced: bool = Field(
        default=True, description="Whether state is synced with contract"
    )
    error: str | None = Field(default=None, description="Error if failed")
    version: int = Field(
        default=1, ge=1, description="Optimistic locking version (incremented on each save)"
    )

    def get_phase_execution(self, phase: PipelinePhase) -> PhaseExecution:
        """Get or create phase execution state."""
        if phase.value not in self.phases:
            self.phases[phase.value] = PhaseExecution(phase=phase)
        return self.phases[phase.value]

    def get_pending_decisions(self) -> list[HITLDecision]:
        """Get all pending HITL decisions."""
        return [d for d in self.decisions if d.status == DecisionStatus.PENDING]

    def add_decision(self, question: str, options: list[str] | None = None) -> HITLDecision:
        """Add a new HITL decision request."""
        decision_id = f"decision-{len(self.decisions) + 1}"
        decision = HITLDecision(
            id=decision_id,
            question=question,
            options=options or [],
            timeout_seconds=self.config.decision_timeout,
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
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="When event occurred"
    )
    phase: PipelinePhase | None = Field(default=None, description="Phase if applicable")
    agent_role: AgentRole | None = Field(default=None, description="Agent if applicable")
    container_id: str | None = Field(default=None, description="Container if applicable")
    data: dict[str, Any] = Field(default_factory=dict, description="Event data")
