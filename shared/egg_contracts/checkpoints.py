"""
Pydantic models for agent checkpoint data.

These models match the JSON schema defined in .egg/schemas/checkpoint.schema.json
and provide validation and type safety for checkpoint operations.

Checkpoints capture agent session context as first-class versioned data in Git,
including transcripts, tool calls, files touched, and token usage.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class FileOperationType(StrEnum):
    """Types of file operations tracked in checkpoints."""

    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    CREATE = "create"
    DELETE = "delete"
    GLOB = "glob"
    GREP = "grep"


class MessageRole(StrEnum):
    """Roles for messages in the transcript."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"


class SessionMetadata(BaseModel):
    """Metadata about the agent session."""

    session_id: str = Field(..., description="Claude Code session ID or container session ID")
    container_id: str | None = Field(
        default=None, description="Docker container ID if running in egg sandbox"
    )
    agent_role: str | None = Field(
        default=None,
        description="Agent role (e.g., coder, tester, documenter, integrator)",
    )
    started_at: datetime = Field(..., description="When session started")
    ended_at: datetime | None = Field(default=None, description="When session ended")
    duration_seconds: float | None = Field(
        default=None, ge=0, description="Session duration in seconds"
    )
    model: str | None = Field(
        default=None, description="Model used for the session (e.g., claude-opus-4-5-20251101)"
    )
    claude_code_version: str | None = Field(
        default=None, description="Claude Code version if available"
    )


class Message(BaseModel):
    """A single message in the transcript."""

    role: MessageRole = Field(..., description="Message role")
    content: str | None = Field(default=None, description="Message content")
    content_summary: str | None = Field(
        default=None, description="Summary if content was too large to include"
    )
    timestamp: datetime = Field(..., description="When message was sent")
    uuid: str | None = Field(default=None, description="Message UUID from Claude Code")


class Transcript(BaseModel):
    """The conversation transcript."""

    messages: list[Message] = Field(default_factory=list, description="Conversation messages")
    message_count: int = Field(default=0, ge=0, description="Total number of messages")
    truncated: bool = Field(
        default=False, description="Whether the transcript was truncated due to size"
    )
    truncation_reason: str | None = Field(
        default=None, description="Reason for truncation if truncated"
    )


class FileOperation(BaseModel):
    """A file operation during the session."""

    path: str = Field(..., description="File path (relative to repo root)")
    operation: FileOperationType = Field(..., description="Type of operation")
    timestamp: datetime | None = Field(default=None, description="When the operation occurred")


class ToolCall(BaseModel):
    """A tool invocation during the session."""

    name: str = Field(..., description="Tool name (e.g., Bash, Read, Write, Edit, Grep, Glob)")
    tool_use_id: str | None = Field(default=None, description="Tool use ID from Claude API")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Tool parameters (may be redacted)"
    )
    result_summary: str | None = Field(
        default=None, description="Summary of tool result (may be truncated/redacted)"
    )
    success: bool | None = Field(default=None, description="Whether the tool call succeeded")
    duration_ms: float | None = Field(
        default=None, ge=0, description="Tool execution duration in milliseconds"
    )
    timestamp: datetime = Field(..., description="When the tool call was made")


class TokenUsage(BaseModel):
    """Token usage for the session."""

    input_tokens: int = Field(default=0, ge=0, description="Total input tokens")
    output_tokens: int = Field(default=0, ge=0, description="Total output tokens")
    cache_read_tokens: int = Field(default=0, ge=0, description="Tokens read from cache")
    cache_creation_tokens: int = Field(default=0, ge=0, description="Tokens written to cache")
    total_tokens: int = Field(default=0, ge=0, description="Total tokens (input + output)")
    estimated_cost_usd: float | None = Field(
        default=None, ge=0, description="Estimated cost in USD"
    )

    def calculate_total(self) -> int:
        """Calculate and return total tokens."""
        return self.input_tokens + self.output_tokens


# ==============================================================================
# Checkpoint v2 Models
#
# V2 checkpoints capture every agent session (not just push events) and support
# rich multi-dimensional querying. Stored on the egg/checkpoints/v2 branch.
# ==============================================================================


class TriggerType(StrEnum):
    """What triggered checkpoint creation."""

    COMMIT = "commit"
    SESSION_END = "session_end"


class SessionStatus(StrEnum):
    """Terminal state of the session."""

    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


class AgentType(StrEnum):
    """Agent role/type for classification."""

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"
    REVIEWER = "reviewer"
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    REFINER = "refiner"
    CHECKER = "checker"
    UNKNOWN = "unknown"


class CheckpointV2(BaseModel):
    """
    V2 checkpoint with rich metadata for querying.

    Unlike v1, commit_sha is optional (session-end checkpoints may not have
    commits) and trigger_type/session_id are required at top level for
    direct indexing.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="2.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    id: str = Field(
        ..., pattern=r"^ckpt-[a-f0-9]{8,16}$", description="Unique checkpoint identifier"
    )

    # Trigger context
    trigger_type: TriggerType = Field(..., description="What created this checkpoint")
    session_status: SessionStatus | None = Field(
        default=None, description="Terminal session state (only for SESSION_END triggers)"
    )

    # Git context (optional for session-end checkpoints)
    commit_sha: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA (optional in v2)",
    )
    push_sha: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="The tip commit SHA of the push",
    )
    branch: str | None = Field(default=None, description="Git branch")

    # Workflow correlation
    session_id: str = Field(..., description="Session/container ID for direct indexing")
    issue_number: int | None = Field(
        default=None, ge=1, description="GitHub issue number if associated"
    )
    pr_number: int | None = Field(default=None, ge=1, description="GitHub PR number if associated")

    # Agent classification
    agent_type: AgentType = Field(
        default=AgentType.UNKNOWN, description="Agent role classification"
    )
    pipeline_phase: str | None = Field(
        default=None, description="SDLC pipeline phase when checkpoint was created"
    )
    pipeline_id: str | None = Field(
        default=None, description="Pipeline run ID for multi-agent workflow correlation"
    )

    # Session details
    session: SessionMetadata = Field(..., description="Session metadata")
    transcript: Transcript | None = Field(default=None, description="The conversation transcript")
    files_touched: list[FileOperation] = Field(
        default_factory=list, description="Files that were read, created, or edited"
    )
    tool_calls: list[ToolCall] = Field(
        default_factory=list, description="Tool invocations made during the session"
    )
    token_usage: TokenUsage | None = Field(default=None, description="Token usage for the session")

    # Timestamps
    created_at: datetime = Field(..., description="When checkpoint was created")
    session_started_at: datetime = Field(..., description="When session started")
    session_ended_at: datetime | None = Field(default=None, description="When session ended")

    @field_validator("pipeline_phase")
    @classmethod
    def validate_pipeline_phase(cls, v: str | None) -> str | None:
        if v is None:
            return None
        valid_phases = {"refine", "plan", "implement", "pr"}
        if v not in valid_phases:
            msg = f"pipeline_phase must be one of {valid_phases}"
            raise ValueError(msg)
        return v

    @field_validator("commit_sha", "push_sha", mode="before")
    @classmethod
    def validate_sha(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        return str(v)


class CheckpointSummaryV2(BaseModel):
    """Summary with all queryable fields for the v2 index."""

    id: str = Field(
        ..., pattern=r"^ckpt-[a-f0-9]{8,16}$", description="Unique checkpoint identifier"
    )
    trigger_type: TriggerType = Field(..., description="What created this checkpoint")
    session_status: SessionStatus | None = Field(default=None, description="Terminal session state")

    # All queryable fields
    session_id: str = Field(..., description="Session/container ID")
    commit_sha: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{7,40}$", description="Git commit SHA"
    )
    issue_number: int | None = Field(default=None, ge=1, description="GitHub issue number")
    pr_number: int | None = Field(default=None, ge=1, description="GitHub PR number")
    branch: str | None = Field(default=None, description="Git branch")
    agent_type: AgentType = Field(
        default=AgentType.UNKNOWN, description="Agent role classification"
    )
    pipeline_phase: str | None = Field(default=None, description="Pipeline phase")
    pipeline_id: str | None = Field(default=None, description="Pipeline run ID")

    # Metrics
    created_at: datetime = Field(..., description="When checkpoint was created")
    message_count: int = Field(default=0, ge=0, description="Number of messages in transcript")
    tool_call_count: int = Field(default=0, ge=0, description="Number of tool calls")
    total_tokens: int = Field(default=0, ge=0, description="Total tokens used")
    files_touched_count: int = Field(default=0, ge=0, description="Number of files touched")

    @classmethod
    def from_checkpoint(cls, checkpoint: "CheckpointV2") -> "CheckpointSummaryV2":
        """Create a summary from a full v2 checkpoint."""
        return cls(
            id=checkpoint.id,
            trigger_type=checkpoint.trigger_type,
            session_status=checkpoint.session_status,
            session_id=checkpoint.session_id,
            commit_sha=checkpoint.commit_sha,
            issue_number=checkpoint.issue_number,
            pr_number=checkpoint.pr_number,
            branch=checkpoint.branch,
            agent_type=checkpoint.agent_type,
            pipeline_phase=checkpoint.pipeline_phase,
            pipeline_id=checkpoint.pipeline_id,
            created_at=checkpoint.created_at,
            message_count=checkpoint.transcript.message_count if checkpoint.transcript else 0,
            tool_call_count=len(checkpoint.tool_calls),
            total_tokens=checkpoint.token_usage.total_tokens if checkpoint.token_usage else 0,
            files_touched_count=len(checkpoint.files_touched),
        )


class CheckpointIndexV2(BaseModel):
    """
    Multi-dimensional index for fast checkpoint lookups.

    Supports queries like:
    - "All checkpoints for issue #530"
    - "All checkpoints for PR #42"
    - "All checkpoints by session abc123"
    - "All checkpoints for commit deadbeef"
    - "All coder agent checkpoints in implement phase"
    - "All failed sessions"

    Stored at the root of the egg/checkpoints/v2 branch.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="2.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    last_updated: datetime = Field(..., description="When the index was last updated")

    # Primary index: all checkpoint summaries
    checkpoints: list[CheckpointSummaryV2] = Field(
        default_factory=list, description="List of checkpoint summaries"
    )

    # Secondary indices for fast lookups (populated on write)
    by_session: dict[str, list[str]] = Field(
        default_factory=dict, description="session_id -> [checkpoint_ids]"
    )
    by_issue: dict[str, list[str]] = Field(
        default_factory=dict, description="issue_number (as str) -> [checkpoint_ids]"
    )
    by_pr: dict[str, list[str]] = Field(
        default_factory=dict, description="pr_number (as str) -> [checkpoint_ids]"
    )
    by_commit: dict[str, str] = Field(
        default_factory=dict, description="commit_sha -> checkpoint_id (1:1)"
    )
    by_agent_type: dict[str, list[str]] = Field(
        default_factory=dict, description="agent_type -> [checkpoint_ids]"
    )
    by_phase: dict[str, list[str]] = Field(
        default_factory=dict, description="pipeline_phase -> [checkpoint_ids]"
    )
    by_trigger: dict[str, list[str]] = Field(
        default_factory=dict, description="trigger_type -> [checkpoint_ids]"
    )
    by_status: dict[str, list[str]] = Field(
        default_factory=dict, description="session_status -> [checkpoint_ids]"
    )
    by_pipeline: dict[str, list[str]] = Field(
        default_factory=dict, description="pipeline_id -> [checkpoint_ids]"
    )

    def get_by_session(self, session_id: str) -> list[str]:
        """Get checkpoint IDs for a session."""
        return self.by_session.get(session_id, [])

    def get_by_issue(self, issue_number: int) -> list[str]:
        """Get checkpoint IDs for an issue."""
        return self.by_issue.get(str(issue_number), [])

    def get_by_pr(self, pr_number: int) -> list[str]:
        """Get checkpoint IDs for a PR."""
        return self.by_pr.get(str(pr_number), [])

    def get_by_commit(self, commit_sha: str) -> str | None:
        """Get checkpoint ID for a commit (1:1 mapping)."""
        return self.by_commit.get(commit_sha)

    def get_by_agent_type(self, agent_type: AgentType) -> list[str]:
        """Get checkpoint IDs for an agent type."""
        return self.by_agent_type.get(agent_type.value, [])

    def get_by_phase(self, phase: str) -> list[str]:
        """Get checkpoint IDs for a pipeline phase."""
        return self.by_phase.get(phase, [])

    def get_by_trigger(self, trigger_type: TriggerType) -> list[str]:
        """Get checkpoint IDs for a trigger type."""
        return self.by_trigger.get(trigger_type.value, [])

    def get_by_status(self, status: SessionStatus) -> list[str]:
        """Get checkpoint IDs for a session status."""
        return self.by_status.get(status.value, [])

    def get_by_pipeline(self, pipeline_id: str) -> list[str]:
        """Get checkpoint IDs for a pipeline run."""
        return self.by_pipeline.get(pipeline_id, [])
