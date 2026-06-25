"""Pydantic models for agent anchor data.

These models match the JSON schema defined in .egg/schemas/agent-anchor.schema.json
and provide validation and type safety for anchor operations.

Agent anchors capture working state at natural milestones for post-compaction
state recovery during long-running agent sessions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .constants import (
    ANCHOR_MAX_DECISIONS,
    ANCHOR_MAX_ERRORS,
    ANCHOR_MAX_FILES,
    ANCHOR_MAX_KEY_CONTEXT,
    ANCHOR_MAX_PROGRESS_ITEMS,
)

logger = logging.getLogger(__name__)


class AnchorStatus(StrEnum):
    """Agent status within the anchor lifecycle."""

    INITIALIZING = "initializing"
    WORKING = "working"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"
    FAILED = "failed"


class BRCPhase(StrEnum):
    """Broadcast-Review-Converge protocol phases."""

    ORIENT = "orient"
    WORKING = "working"
    PROPOSED = "proposed"
    REVIEWING = "reviewing"
    CONFIRMED = "confirmed"


class ProgressState(StrEnum):
    """State of a progress step."""

    PENDING = "pending"
    WORKING = "working"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class ReviewVerdict(StrEnum):
    """Latest verdict on a reviewer->producer review edge (#3189).

    Derived mechanically from the BRC message record, never transcribed by
    an agent. ``CONDITIONAL_ACK`` is an ACK that carries a
    ``pre_merge_condition`` pre-merge obligation (#1998).
    """

    ACK = "ack"
    NACK = "nack"
    CONDITIONAL_ACK = "conditional_ack"


class AnchorMeta(BaseModel):
    """Anchor metadata for versioning and ordering."""

    schema_version: str = Field(default="1.0", description="Schema version for migrations")
    created_at: datetime = Field(..., description="ISO 8601 timestamp of creation")
    updated_at: datetime = Field(..., description="ISO 8601 timestamp of last update")
    sequence: int = Field(..., ge=0, description="Monotonically incrementing sequence number")


class TaskInfo(BaseModel):
    """Information about the agent's current task."""

    id: str = Field(..., description="Task identifier")
    description: str = Field(..., description="Task description")
    phase: str = Field(..., description="Pipeline phase")


class ProgressItem(BaseModel):
    """A progress milestone tracked by the agent."""

    step: str = Field(..., description="Step description")
    state: ProgressState = Field(..., description="Step state")
    detail: str | None = Field(default=None, description="Optional detail about the step")
    timestamp: datetime = Field(..., description="When this progress was recorded")


class Decision(BaseModel):
    """A HITL decision point encountered by the agent."""

    id: str = Field(..., description="Decision identifier")
    question: str = Field(..., description="The decision question")
    answer: str | None = Field(default=None, description="The answer if resolved")
    decided_by: str | None = Field(default=None, description="Who decided")
    timestamp: datetime = Field(..., description="When this decision was recorded")


class ReviewEdgeVerdict(BaseModel):
    """Latest verdict on a single reviewer->producer edge (#3189).

    ``version`` is the producer proposal version the verdict applies to;
    ``reviewed_sha`` is that proposal's ``proposal_commit_sha`` — the SHA the
    reviewer actually reviewed — so a git-log delta against the producer's
    current HEAD reveals exactly what changed since the last review.
    """

    reviewer: str = Field(..., description="Reviewer role")
    producer: str = Field(..., description="Producer role")
    verdict: ReviewVerdict = Field(..., description="Latest verdict on the edge")
    version: int = Field(..., ge=0, description="Producer proposal version reviewed")
    reviewed_sha: str = Field(default="", description="proposal_commit_sha reviewed")


class OpenNack(BaseModel):
    """An unresolved NACK against the producer's CURRENT proposal version (#3189).

    A NACK whose ``version`` is older than the producer's current proposal
    version is historical — superseded by a re-propose — and is NOT listed
    here. This is the obligation a threshold reseed must never drop.
    """

    reviewer: str = Field(..., description="Reviewer role that NACKed")
    producer: str = Field(..., description="Producer role NACKed")
    version: int = Field(..., ge=0, description="Producer proposal version NACKed")
    reason: str = Field(default="", description="Blocking reason cited in the NACK")


class ConditionalAckObligation(BaseModel):
    """A pre-merge obligation attached to a conditional ACK (#1998, #3189).

    Carries ``resolved`` so the protected root distinguishes an obligation
    still owed to the merger from one already satisfied in-cycle (#2338).
    Only obligations against the producer's current proposal version are
    listed — a re-propose clears prior obligations.
    """

    reviewer: str = Field(..., description="Reviewer role that conditionally ACKed")
    producer: str = Field(..., description="Producer role the obligation is on")
    version: int = Field(..., ge=0, description="Producer proposal version")
    condition: str = Field(..., description="The pre_merge_condition text")
    resolved: bool = Field(default=False, description="Whether satisfied in-cycle")


class BRCDerivedAnchors(BaseModel):
    """The four #3189 deterministic BRC anchors derived from the message record.

    Every field is computed MECHANICALLY from the CONSENSUS_PROPOSE / ACK /
    NACK / OBLIGATION_RESOLVED message record (see
    :func:`egg_anchor.brc_derive.derive_brc_anchors`) — no agent-authored
    content enters this layer, so it cannot drift from the record. These are
    the authoritative anchors a threshold reseed must preserve: dropping them
    would re-review settled SHAs or lose NACK obligations.
    """

    last_reviewed_sha: dict[str, str] = Field(
        default_factory=dict,
        description=("producer -> commit SHA of the latest proposal any reviewer has verdicted on"),
    )
    latest_verdicts: list[ReviewEdgeVerdict] = Field(
        default_factory=list,
        description="Latest verdict per reviewer->producer edge",
    )
    open_nacks: list[OpenNack] = Field(
        default_factory=list,
        description="Current-version NACKs not yet resolved by a re-propose",
    )
    conditional_ack_obligations: list[ConditionalAckObligation] = Field(
        default_factory=list,
        description="Live pre-merge obligations with resolved/unresolved status",
    )


class BRCState(BaseModel):
    """Broadcast-Review-Converge protocol state."""

    phase: BRCPhase = Field(default=BRCPhase.ORIENT, description="BRC protocol phase")
    proposed_at: datetime | None = Field(default=None, description="When the proposal was made")
    acks: list[str] = Field(default_factory=list, description="Agent IDs that acknowledged")
    nacks: list[str] = Field(default_factory=list, description="Agent IDs that rejected")
    last_message_id: str | None = Field(default=None, description="Last message ID processed")
    # Additive #3189 layer (default None on legacy anchors). Mechanically
    # derived from the BRC message record; NEVER replaces acks/nacks/
    # last_message_id above — those keep their original agent-id-list meaning.
    derived: BRCDerivedAnchors | None = Field(
        default=None,
        description=(
            "Mechanically-derived #3189 anchors: last-reviewed SHA per producer, "
            "latest verdict per edge, open NACKs, conditional-ACK obligations. "
            "Additive and optional — None on legacy anchors."
        ),
    )


class KeyContext(BaseModel):
    """A key-value context item for state recovery."""

    label: str = Field(..., max_length=50, description="Context label")
    value: str = Field(..., max_length=500, description="Context value")


class ErrorEncountered(BaseModel):
    """An error encountered during agent execution."""

    error: str = Field(..., max_length=200, description="Error description")
    resolution: str | None = Field(
        default=None, max_length=200, description="How the error was resolved"
    )
    timestamp: datetime = Field(..., description="When the error occurred")


class AgentAnchor(BaseModel):
    """Complete agent anchor document for post-compaction state recovery.

    Captures the agent's working state at natural milestones so that
    context can be restored after compaction events.
    """

    meta: AnchorMeta = Field(..., alias="_meta", description="Anchor metadata")
    agent_id: str = Field(..., description="Unique identifier for the agent")
    role: str = Field(..., description="Agent role (e.g., coder, tester)")
    team: list[str] = Field(default_factory=list, description="Other agents in the pipeline")
    task: TaskInfo = Field(..., description="Current task information")
    status: AnchorStatus = Field(..., description="Current agent status")
    pipeline_id: str = Field(..., description="Pipeline identifier")
    progress: list[ProgressItem] = Field(default_factory=list, description="Progress milestones")
    decisions: list[Decision] = Field(
        default_factory=list, description="HITL decisions encountered"
    )
    brc_state: BRCState = Field(default_factory=BRCState, description="BRC protocol state")
    key_context: list[KeyContext] = Field(default_factory=list, description="Key context items")
    errors_encountered: list[ErrorEncountered] = Field(
        default_factory=list, description="Errors encountered"
    )
    files_modified: list[str] = Field(
        default_factory=list, description="Files modified by the agent"
    )

    model_config = {"populate_by_name": True}

    @field_validator("progress")
    @classmethod
    def validate_progress_count(cls, v: list[ProgressItem]) -> list[ProgressItem]:
        """Enforce maximum progress items."""
        if len(v) > ANCHOR_MAX_PROGRESS_ITEMS:
            msg = f"progress must have at most {ANCHOR_MAX_PROGRESS_ITEMS} items"
            raise ValueError(msg)
        return v

    @field_validator("decisions")
    @classmethod
    def validate_decisions_count(cls, v: list[Decision]) -> list[Decision]:
        """Enforce maximum decisions."""
        if len(v) > ANCHOR_MAX_DECISIONS:
            msg = f"decisions must have at most {ANCHOR_MAX_DECISIONS} items"
            raise ValueError(msg)
        return v

    @field_validator("key_context")
    @classmethod
    def validate_key_context_count(cls, v: list[KeyContext]) -> list[KeyContext]:
        """Enforce maximum key context items."""
        if len(v) > ANCHOR_MAX_KEY_CONTEXT:
            msg = f"key_context must have at most {ANCHOR_MAX_KEY_CONTEXT} items"
            raise ValueError(msg)
        return v

    @field_validator("errors_encountered")
    @classmethod
    def validate_errors_count(cls, v: list[ErrorEncountered]) -> list[ErrorEncountered]:
        """Enforce maximum error items."""
        if len(v) > ANCHOR_MAX_ERRORS:
            msg = f"errors_encountered must have at most {ANCHOR_MAX_ERRORS} items"
            raise ValueError(msg)
        return v

    @field_validator("files_modified")
    @classmethod
    def validate_files_count(cls, v: list[str]) -> list[str]:
        """Enforce maximum files modified."""
        if len(v) > ANCHOR_MAX_FILES:
            msg = f"files_modified must have at most {ANCHOR_MAX_FILES} items"
            raise ValueError(msg)
        return v

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict with ISO datetime strings.

        Uses the JSON schema field names (e.g., _meta) and converts
        datetime objects to ISO 8601 strings.
        """

        def _serialize_datetime(dt: datetime) -> str:
            """Convert datetime to ISO 8601 string."""
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.isoformat()

        def _serialize_value(v: Any) -> Any:
            """Recursively serialize values."""
            if isinstance(v, datetime):
                return _serialize_datetime(v)
            if isinstance(v, StrEnum):
                return v.value
            if isinstance(v, BaseModel):
                return _serialize_model(v)
            if isinstance(v, list):
                return [_serialize_value(item) for item in v]
            if isinstance(v, dict):
                return {k: _serialize_value(val) for k, val in v.items()}
            return v

        def _serialize_model(model: BaseModel) -> dict[str, Any]:
            """Serialize a Pydantic model to dict.

            Omits None values for optional fields (where default is None)
            to match JSON Schema expectations (optional fields are omitted,
            not set to null).
            """
            result = {}
            for field_name, field_info in model.model_fields.items():
                alias = field_info.alias or field_name
                value = getattr(model, field_name)
                if value is None and field_info.default is None:
                    # Skip optional fields that are None
                    continue
                result[alias] = _serialize_value(value)
            return result

        return _serialize_model(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentAnchor:
        """Deserialize from a dict (e.g., loaded from JSON)."""
        return cls.model_validate(data)
