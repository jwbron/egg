"""Typed Impasse primitive for runtime escape-hatch (#2529).

When a producer agent discovers mid-execution that its assigned task is
structurally impossible — file restrictions block its role, the plan is
buggy, an external dependency is missing, etc. — it emits a typed
``Impasse`` instead of inventing a workaround. The orchestrator detects
the impasse post-phase and routes accordingly:

- Role-restriction impasse with a single eligible alternative role:
  delegate (mutate ``task.role``) and re-run the slice.
- Second impasse on the same task, or no eligible alternative role:
  escalate to HITL.

This module defines the schema only; the agent-side handler lives in
``sandbox/egg_agent_tools/handlers/sdlc.py`` and the routing helpers
live in ``orchestrator/impasse_routing.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ImpasseCategory(StrEnum):
    """Why a task is structurally impossible for the assigned role."""

    WRONG_ROLE = "wrong_role"
    """Role-restriction patterns block the assigned role from one or more
    files in ``task.files_affected``. The agent should populate
    ``suggested_role`` with the role that *can* write the blocked files.
    """

    PLAN_BUG = "plan_bug"
    """The task as written is internally inconsistent (e.g. acceptance
    criteria contradict each other, files reference paths that do not
    exist and no role could create them, the dependency in
    ``files_affected`` is the wrong artifact). Cannot be resolved by
    swapping role; needs HITL or a re-plan.
    """

    EXTERNAL_BLOCKER = "external_blocker"
    """Required external state is missing — upstream dependency not yet
    merged, an env var the task assumes is unset, a referenced ticket
    was closed without the work being done. Surface to HITL with the
    evidence so the operator can resolve the blocker.
    """

    UNKNOWN = "unknown"
    """The agent recognises the task is impossible but cannot classify
    the cause. Surface to HITL with the agent's reasoning verbatim.
    """


class Impasse(BaseModel):
    """A typed signal that a task is structurally impossible.

    Emitted by a producer via the ``mcp__sdlc__report_impasse`` tool and
    serialised under ``AgentOutput.impasse``. The orchestrator's
    impasse-routing helpers consume this post-phase to decide whether to
    delegate (for ``WRONG_ROLE`` with a single eligible alternative) or
    escalate to HITL.

    The agent emits this *instead of* inventing a workaround (e.g. the
    ``.github-staging/`` deletion-marker pattern that triggered the
    follow-on NACK in pipeline ``issue-2474-v2``). Once emitted, the
    agent should stop work and exit cleanly — its container will be
    terminated by the orchestrator after the phase completes.
    """

    model_config = ConfigDict(extra="forbid")

    category: ImpasseCategory = Field(
        ...,
        description="Why the task is impossible for the assigned role.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Human-readable explanation of what the agent observed. "
            "Surfaced verbatim in the HITL decision and structured logs."
        ),
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "Contract task ID this impasse applies to (e.g. "
            "``task-1-3``). When omitted, the orchestrator infers it "
            "from the agent's currently assigned task in the slice."
        ),
    )
    suggested_role: str | None = Field(
        default=None,
        description=(
            "For ``WRONG_ROLE`` impasses, the producer role that *can* "
            "write the blocked files. The orchestrator uses this for "
            "auto-delegation when ``delegation_attempts`` is below the "
            "limit. ``None`` for non-WRONG_ROLE categories or when no "
            "single role covers all files."
        ),
    )
    blocked_files: list[str] = Field(
        default_factory=list,
        description=(
            "Files the assigned role cannot write, when relevant. "
            "Populated by the agent or the gateway-side preflight; "
            "empty for non-restriction impasses."
        ),
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form structured evidence — error messages, links, "
            "tool outputs the agent collected before deciding the "
            "task was impossible. Surfaced in the HITL decision body."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Wall-clock time the impasse was reported.",
    )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip-safe dict for JSON serialisation."""
        return {
            "category": self.category.value,
            "reason": self.reason,
            "task_id": self.task_id,
            "suggested_role": self.suggested_role,
            "blocked_files": list(self.blocked_files),
            "evidence": dict(self.evidence),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Impasse:
        """Inverse of :meth:`to_dict`."""
        raw_ts = data.get("created_at")
        ts = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else datetime.now(UTC)
        return cls(
            category=ImpasseCategory(data["category"]),
            reason=data["reason"],
            task_id=data.get("task_id"),
            suggested_role=data.get("suggested_role"),
            blocked_files=list(data.get("blocked_files") or []),
            evidence=dict(data.get("evidence") or {}),
            created_at=ts,
        )


__all__ = ["Impasse", "ImpasseCategory"]
