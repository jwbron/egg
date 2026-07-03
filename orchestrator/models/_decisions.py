"""HITL-decision and operator-directive models.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312).
Every symbol re-exports through the ``models`` barrel (stable public API).
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._enums import DecisionStatus


class HITLDecision(BaseModel):
    """A human-in-the-loop decision request."""

    model_config = ConfigDict(validate_assignment=True)

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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When created"
    )
    resolved_at: datetime | None = Field(default=None, description="When resolved")
    resolution: str | None = Field(default=None, description="Human's response")
    phase: PipelinePhase | None = Field(
        default=None, description="Pipeline phase when decision was created"
    )
    content_changed: bool | None = Field(
        default=None,
        description="Whether the phase output changed compared to the previous decision's context (literal string comparison, not semantic)",
    )

    @field_validator("resolution", mode="before")
    @classmethod
    def _serialize_resolution(cls, v: Any) -> str | None:
        """Ensure resolution is always stored as a JSON string, not a dict (#1635)."""
        if isinstance(v, dict | list):
            return json.dumps(v)
        return v


class OperatorDirective(BaseModel):
    """An operator-issued directive recorded at an HITL phase-gate kickback.

    Replaces the single ``PhaseExecution.hitl_feedback`` string with a
    chronologically accumulated record. Each kickback on a phase appends
    one ``OperatorDirective``; the list is never cleared, so iteration
    N+1's prompt can render all prior directives in order with explicit
    precedence prose. See issue #2795.
    """

    iteration_n: int = Field(
        ..., ge=0, description="Zero-based index of the iteration this directive kicked back"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the operator issued the directive",
    )
    feedback_text: str = Field(..., description="Operator-provided feedback text")


class IterationSummary(BaseModel):
    """Frozen snapshot of a kicked-back iteration's BRC outcome.

    Captured before ``_clear_concurrent_state`` wipes the consensus
    tracker so reviewers in iteration N+1 can see what tripped the rubric
    last round. See issue #2795.
    """

    iteration_n: int = Field(..., ge=0, description="Zero-based iteration index")
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the iteration's consensus closed",
    )
    final_proposal_commit: dict[str, str] = Field(
        default_factory=dict,
        description="Map of producer role to final proposal commit SHA, if any",
    )
    verdict_matrix: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-edge BRC verdict at iteration close, keyed by "
            "'reviewer_role->producer_role' → ApprovalState value "
            "(e.g. 'acked', 'nacked')"
        ),
    )
    nack_reasons: list[str] = Field(
        default_factory=list,
        description="Collected NACK rationales, prefixed by reviewer role",
    )
    artifacts_snapshot: dict[str, str] = Field(
        default_factory=dict,
        description="Snapshot of PhaseExecution.artifacts at iteration close",
    )
