"""Orchestrator types and enums.

Provides typed data classes for orchestrator communication.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DeploymentMode(StrEnum):
    """Deployment mode for egg.

    Determines how the sandbox communicates with orchestration components:
    - LOCAL: Interactive mode, direct gateway communication
    - REMOTE_SINGLE: Single sandbox spawned by remote orchestrator
    - DISTRIBUTED: Multiple sandboxes coordinated by orchestrator
    """

    LOCAL = "local"
    REMOTE_SINGLE = "remote-single"
    DISTRIBUTED = "distributed"

    @classmethod
    def from_env(cls) -> "DeploymentMode":
        """Detect deployment mode from environment."""
        import os

        from .constants import ENV_ORCHESTRATOR_MODE

        mode_str = os.environ.get(ENV_ORCHESTRATOR_MODE, "local").lower()
        try:
            return cls(mode_str)
        except ValueError:
            return cls.LOCAL


class SignalType(StrEnum):
    """Signal types for sandbox-to-orchestrator communication.

    Used to report execution state back to the orchestrator:
    - COMPLETE: Agent finished execution successfully
    - PROGRESS: Progress update during execution
    - ERROR: Error occurred (may be recoverable)
    - HEARTBEAT: Keep-alive signal for monitoring
    """

    COMPLETE = "complete"
    PROGRESS = "progress"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class AgentRole(StrEnum):
    """Agent roles matching orchestrator.models.AgentRole."""

    CODER = "coder"
    REVIEWER = "reviewer"
    CHECKER = "checker"
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


@dataclass
class CompletionData:
    """Data for completion signal.

    Attributes:
        agent_role: Role of the completing agent
        commit: Optional commit SHA if changes were made
        files_changed: List of changed files
        handoff_data: Data to pass to dependent agents
        metrics: Execution metrics (tokens, duration, etc.)
    """

    agent_role: str
    commit: str | None = None
    files_changed: list[str] = field(default_factory=list)
    handoff_data: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        return {
            "signal_type": SignalType.COMPLETE.value,
            "agent_role": self.agent_role,
            "commit": self.commit,
            "files_changed": self.files_changed,
            "handoff_data": self.handoff_data,
            "metrics": self.metrics,
        }


@dataclass
class ProgressData:
    """Data for progress signal.

    Attributes:
        agent_role: Role of the agent
        progress_percent: Completion percentage (0-100)
        current_task: Description of current task
        message: Optional status message
    """

    agent_role: str
    progress_percent: int
    current_task: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        """Validate progress_percent is within bounds."""
        if not 0 <= self.progress_percent <= 100:
            raise ValueError(f"progress_percent must be 0-100, got {self.progress_percent}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        return {
            "signal_type": SignalType.PROGRESS.value,
            "agent_role": self.agent_role,
            "progress_percent": self.progress_percent,
            "current_task": self.current_task,
            "message": self.message,
        }


@dataclass
class ErrorData:
    """Data for error signal.

    Attributes:
        agent_role: Role of the agent that encountered the error
        error: Error message
        recoverable: Whether the error is recoverable
        traceback: Optional traceback string
    """

    agent_role: str
    error: str
    recoverable: bool = False
    traceback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        result: dict[str, Any] = {
            "signal_type": SignalType.ERROR.value,
            "agent_role": self.agent_role,
            "error": self.error,
            "recoverable": self.recoverable,
        }
        if self.traceback:
            result["traceback"] = self.traceback
        return result


@dataclass
class HeartbeatData:
    """Data for heartbeat signal.

    Attributes:
        agent_role: Role of the agent
        container_id: Docker container ID
    """

    agent_role: str
    container_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        result: dict[str, Any] = {
            "signal_type": SignalType.HEARTBEAT.value,
            "agent_role": self.agent_role,
        }
        if self.container_id:
            result["container_id"] = self.container_id
        return result


@dataclass
class SignalPayload:
    """Generic signal payload for orchestrator API.

    Attributes:
        signal_type: Type of signal
        agent_role: Role of the sending agent
        data: Signal-specific data
    """

    signal_type: SignalType
    agent_role: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        return {
            "signal_type": self.signal_type.value,
            "agent_role": self.agent_role,
            **self.data,
        }


@dataclass
class SignalResponse:
    """Response from orchestrator signal API.

    Attributes:
        success: Whether the signal was processed successfully
        message: Response message
        data: Additional response data
    """

    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalResponse":
        """Create from API response dictionary."""
        return cls(
            success=data.get("success", False),
            message=data.get("message", ""),
            data=data.get("data", {}),
        )


__all__ = [
    "AgentRole",
    "CompletionData",
    "DeploymentMode",
    "ErrorData",
    "HeartbeatData",
    "ProgressData",
    "SignalPayload",
    "SignalResponse",
    "SignalType",
]
