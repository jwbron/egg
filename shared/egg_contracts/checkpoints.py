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


class Checkpoint(BaseModel):
    """
    An agent checkpoint capturing session context.

    Checkpoints are created when agents push commits and capture the full
    reasoning context: transcript, tool calls, files touched, and token usage.
    They are stored in the egg/checkpoints/v1 branch and linked to commits.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    id: str = Field(
        ..., pattern=r"^ckpt-[a-f0-9]{8,16}$", description="Unique checkpoint identifier"
    )
    commit_sha: str = Field(
        ...,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA this checkpoint is associated with",
    )
    session: SessionMetadata = Field(..., description="Session metadata")
    transcript: Transcript | None = Field(default=None, description="The conversation transcript")
    files_touched: list[FileOperation] = Field(
        default_factory=list, description="Files that were read, created, or edited"
    )
    tool_calls: list[ToolCall] = Field(
        default_factory=list, description="Tool invocations made during the session"
    )
    token_usage: TokenUsage | None = Field(default=None, description="Token usage for the session")
    issue_number: int | None = Field(
        default=None, ge=1, description="GitHub issue number if associated"
    )
    pipeline_phase: str | None = Field(
        default=None, description="SDLC pipeline phase when checkpoint was created"
    )
    branch: str | None = Field(default=None, description="Git branch where the commit was made")
    created_at: datetime = Field(..., description="When checkpoint was created")
    push_sha: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="The tip commit SHA of the push",
    )
    pr_number: int | None = Field(default=None, ge=1, description="GitHub PR number if associated")

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

    def get_tool_calls_by_name(self, name: str) -> list[ToolCall]:
        """Get all tool calls with the given name."""
        return [tc for tc in self.tool_calls if tc.name == name]

    def get_files_by_operation(self, operation: FileOperationType) -> list[FileOperation]:
        """Get all file operations of the given type."""
        return [fo for fo in self.files_touched if fo.operation == operation]

    def get_files_written(self) -> list[str]:
        """Get paths of all files that were written or created."""
        write_ops = {FileOperationType.WRITE, FileOperationType.CREATE, FileOperationType.EDIT}
        return [fo.path for fo in self.files_touched if fo.operation in write_ops]

    def get_files_read(self) -> list[str]:
        """Get paths of all files that were read."""
        return [fo.path for fo in self.files_touched if fo.operation == FileOperationType.READ]


class CheckpointSummary(BaseModel):
    """Summary of a checkpoint for the index."""

    id: str = Field(
        ..., pattern=r"^ckpt-[a-f0-9]{8,16}$", description="Unique checkpoint identifier"
    )
    commit_sha: str = Field(..., pattern=r"^[a-f0-9]{7,40}$", description="Git commit SHA")
    session_id: str = Field(..., description="Session ID")
    agent_role: str | None = Field(default=None, description="Agent role")
    issue_number: int | None = Field(default=None, ge=1, description="GitHub issue number")
    pr_number: int | None = Field(default=None, ge=1, description="GitHub PR number")
    branch: str | None = Field(default=None, description="Git branch")
    pipeline_phase: str | None = Field(default=None, description="Pipeline phase")
    created_at: datetime = Field(..., description="When checkpoint was created")
    message_count: int = Field(default=0, ge=0, description="Number of messages in transcript")
    tool_call_count: int = Field(default=0, ge=0, description="Number of tool calls")
    total_tokens: int = Field(default=0, ge=0, description="Total tokens used")

    @classmethod
    def from_checkpoint(cls, checkpoint: Checkpoint) -> "CheckpointSummary":
        """Create a summary from a full checkpoint."""
        return cls(
            id=checkpoint.id,
            commit_sha=checkpoint.commit_sha,
            session_id=checkpoint.session.session_id,
            agent_role=checkpoint.session.agent_role,
            issue_number=checkpoint.issue_number,
            pr_number=checkpoint.pr_number,
            branch=checkpoint.branch,
            pipeline_phase=checkpoint.pipeline_phase,
            created_at=checkpoint.created_at,
            message_count=checkpoint.transcript.message_count if checkpoint.transcript else 0,
            tool_call_count=len(checkpoint.tool_calls),
            total_tokens=checkpoint.token_usage.total_tokens if checkpoint.token_usage else 0,
        )


class CheckpointIndex(BaseModel):
    """
    Index of checkpoints for a repository.

    Stored at the root of the egg/checkpoints/v1 branch to enable
    fast lookup of checkpoints by commit SHA, issue number, or branch.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    checkpoints: list[CheckpointSummary] = Field(
        default_factory=list, description="List of checkpoint summaries"
    )
    last_updated: datetime = Field(..., description="When the index was last updated")

    def get_by_commit(self, commit_sha: str) -> CheckpointSummary | None:
        """Get checkpoint summary by commit SHA."""
        for cp in self.checkpoints:
            if cp.commit_sha == commit_sha or commit_sha.startswith(cp.commit_sha[:7]):
                return cp
        return None

    def get_by_issue(self, issue_number: int) -> list[CheckpointSummary]:
        """Get all checkpoint summaries for an issue."""
        return [cp for cp in self.checkpoints if cp.issue_number == issue_number]

    def get_by_branch(self, branch: str) -> list[CheckpointSummary]:
        """Get all checkpoint summaries for a branch."""
        return [cp for cp in self.checkpoints if cp.branch == branch]
