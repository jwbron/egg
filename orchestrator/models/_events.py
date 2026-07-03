"""Pipeline and structured-progress event models.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312).
Every symbol re-exports through the ``models`` barrel (stable public API).
"""

from datetime import UTC, datetime
from typing import Any

from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, Field

from ._enums import AgentRole, ProgressState


class PipelineEvent(BaseModel):
    """Event emitted during pipeline execution."""

    pipeline_id: str = Field(..., description="Pipeline ID")
    event_type: str = Field(..., description="Event type")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When event occurred"
    )
    phase: PipelinePhase | None = Field(default=None, description="Phase if applicable")
    agent_role: AgentRole | None = Field(default=None, description="Agent if applicable")
    container_id: str | None = Field(default=None, description="Container if applicable")
    data: dict[str, Any] = Field(default_factory=dict, description="Event data")


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
