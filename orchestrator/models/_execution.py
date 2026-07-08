"""Review, container, agent, and phase execution models.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312).
Every symbol re-exports through the ``models`` barrel (stable public API).
"""

from datetime import datetime
from typing import Any, NamedTuple

from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, Field, field_validator, model_validator
from slice_id_validation import SLICE_ID_PATTERN

from ._decisions import IterationSummary, OperatorDirective
from ._enums import (
    AgentExecutionStatus,
    AgentRole,
    ContainerStatus,
    PipelineStatus,
)


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

    # Kubernetes-native fields (optional, populated when running on k8s)
    pod_name: str | None = Field(default=None, description="Kubernetes pod name")
    namespace: str | None = Field(default=None, description="Kubernetes namespace")
    job_name: str | None = Field(default=None, description="Kubernetes Job name")

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
    container_info: ContainerInfo | None = Field(
        default=None,
        description=(
            "Full ContainerInfo from the spawner, carrying backend-specific "
            "fields (e.g. pod_name, namespace, job_name on Kubernetes). "
            "Optional for backward compatibility with older state files."
        ),
    )
    slice_id: str | None = Field(
        default=None,
        description=(
            "Slice scope (e.g. ``slice-2``) when the agent runs as part of a "
            "per-slice team in a multi-slice phase (#2137). ``None`` for "
            "pipeline-level (non-sliced) agents. Distinguishes concurrent "
            "same-role agents in the same ``phase_exec.agents`` list so "
            "consumers that walk by role match on ``(role, slice_id)`` "
            "rather than role alone (#2422)."
        ),
    )

    @field_validator("slice_id")
    @classmethod
    def _validate_slice_id(cls, v: str | None) -> str | None:
        """Defense-in-depth: reject non-canonical ``slice_id`` values.

        Production write paths populate this field from validated values
        produced by ``extract_slice_id`` / ``concurrent_executor._slice_id``,
        which already enforce ``SLICE_ID_PATTERN``. This validator closes
        the gap for hand-built fixtures, migration tools, or any future
        caller that constructs ``AgentExecution`` directly — a non-canonical
        value would silently break the ``(role, slice_id)`` walks that
        consumers rely on.
        """
        if v is None:
            return None
        if not SLICE_ID_PATTERN.fullmatch(v):
            raise ValueError(f"Invalid slice_id {v!r}: must match 'slice-<N>'")
        return v

    started_at: datetime | None = Field(default=None, description="When started")
    completed_at: datetime | None = Field(default=None, description="When completed")
    resolved_model: str | None = Field(
        default=None,
        description=(
            "The Claude-Code-facing model alias the agent was spawned "
            "with (``AgentModelDecision.claude_code_alias``, e.g. "
            "``opus`` or ``deepseek-v4-pro[1m]``). Recorded at spawn / "
            "restart time so operators can confirm a live "
            "``agent_models`` swap took effect from ``get_status`` / "
            "``list_containers`` instead of grepping pod logs (#3174). "
            "``None`` on records persisted before the field existed."
        ),
    )
    commit: str | None = Field(default=None, description="Commit SHA if changes made")
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Handoff data for dependent agents"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, ge=0, description="Number of retries")
    conflicts: list[str] = Field(
        default_factory=list, description="Files with unresolved merge conflicts"
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


class AgentExitInfo(BaseModel):
    """Frozen-at-exit snapshot preserved across phase failure cleanup.

    Captured when a container exits during concurrent BRC execution, so
    operators can triage which role failed and what it said last even after
    container cleanup. Field overlap with `AgentExecution` (role,
    container_id) and `ContainerInfo` (exit_code, exited_at) is intentional:
    those live structures may be mutated or removed during cleanup, while
    this snapshot is immutable history. Only `last_lines` is genuinely new.
    See issue #2205.
    """

    role: AgentRole = Field(..., description="Agent role that exited")
    exit_code: int | None = Field(
        ...,
        description=(
            "Container exit code. None when the pod-phase race surfaces "
            "an exit before container_statuses[0].state.terminated is "
            "populated (matches ContainerInfo.exit_code)."
        ),
    )
    last_lines: list[str] = Field(
        default_factory=list,
        description="Tail of container stdout/stderr (up to 200 lines)",
    )
    terminated_at: datetime = Field(..., description="When the container exit was observed")
    container_id: str | None = Field(
        default=None,
        description="Container ID at time of exit (may be unresolvable post-cleanup)",
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
    operator_directives: list[OperatorDirective] = Field(
        default_factory=list,
        description=(
            "Chronologically accumulated operator directives from HITL "
            "phase-gate kickbacks. Never cleared — replaces the single "
            "hitl_feedback string so iteration N+1 prompts can render "
            "every prior directive with precedence prose. See #2795."
        ),
    )
    iteration_history: list[IterationSummary] = Field(
        default_factory=list,
        description=(
            "One entry per kicked-back iteration. Captured before "
            "_clear_concurrent_state wipes the BRC tracker so future "
            "iterations can see prior verdicts/NACK reasons. See #2795."
        ),
    )
    phase_start_sha: str | None = Field(
        default=None,
        description="Branch tip SHA at phase start, for completion signal verification",
    )
    agent_exits: list[AgentExitInfo] = Field(
        default_factory=list,
        description=(
            "Frozen-at-exit snapshots from concurrent BRC execution. Populated "
            "by _record_container_exit and never mutated afterwards — use this "
            "for post-mortem triage. The live agents/containers lists are the "
            "source of truth while the phase is running. See issue #2205."
        ),
    )
    decision_ledger: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Decision-ledger summary captured at the phase's HITL gate "
            "(#3526): registered cq-N ids, whether the phase attested an "
            "explicit-none ledger, and the considered-candidate entries. "
            "Persisted so decisions-surfaced-per-phase is queryable from "
            "pipeline state over time; a behavioral decline in decision "
            "surfacing must be visible in data, not operator feel."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_hitl_feedback(cls, data: Any) -> Any:
        """Migrate persisted ``hitl_feedback`` strings to ``operator_directives``.

        Before #2795 ``PhaseExecution`` carried a single ``hitl_feedback: str``
        that the inline HITL handler and AWAITING_HUMAN recovery path each
        wrote and consumed. #2795 replaces it with the chronological
        ``operator_directives`` list. Pydantic's default ``extra='ignore'``
        would silently drop a surviving ``hitl_feedback`` value on the first
        load after deploy — for a pipeline paused at a HITL gate with the
        operator's directive already on disk, that means the directive is
        lost with no log, warning, or error.

        Translate any non-empty ``hitl_feedback`` into a synthetic
        ``OperatorDirective`` so the directive survives the deploy, and emit
        a one-shot deprecation log so operators see the migration happen.
        """
        if not isinstance(data, dict):
            return data
        # Normalise an explicit ``null`` for ``operator_directives`` to an
        # empty list before any early return: Pydantic's
        # ``default_factory=list`` does not prevent a writer from emitting a
        # literal ``null``, and the non-Optional ``list[OperatorDirective]``
        # field validation would then reject the record entirely. Doing
        # this here covers both the no-legacy-feedback fast path and the
        # migration branch below.
        if "operator_directives" in data and data["operator_directives"] is None:
            data["operator_directives"] = []
        legacy = data.pop("hitl_feedback", None)
        if not legacy:
            return data

        directives = list(data.get("operator_directives") or [])
        # Synthesize the directive at the iteration index implied by the
        # cycle counter (the inline handler's old behaviour wrote
        # hitl_feedback after incrementing hitl_review_cycles). On
        # collision with an existing entry, pick one past the current
        # maximum so the floor is uniqueness regardless of how sparse
        # the existing indices are (a ``len(directives)`` fallback can
        # itself collide when entries are sparse — e.g. ``[0, 2]`` with
        # primary collision at ``1`` would fall back to ``2``).
        hitl_cycles = data.get("hitl_review_cycles", 0) or 0
        iteration_n = max(hitl_cycles - 1, 0)
        # The migration only sees raw JSON-loaded data, so entries are
        # expected to be ``dict`` with an int ``iteration_n``. Any
        # malformed entry (non-dict or non-int index) is silently skipped
        # here from collision detection — downstream Pydantic field
        # validation will reject it with a precise error rather than the
        # migration trying to second-guess the shape.
        existing_indices = [
            d.get("iteration_n")
            for d in directives
            if isinstance(d, dict) and isinstance(d.get("iteration_n"), int)
        ]
        if iteration_n in existing_indices:
            iteration_n = max(existing_indices) + 1
        directives.append(
            {
                "iteration_n": iteration_n,
                "feedback_text": legacy,
            }
        )
        data["operator_directives"] = directives
        import logging as _logging

        _logging.getLogger("orchestrator.models").warning(
            "Migrated legacy PhaseExecution.hitl_feedback to operator_directives "
            "(phase=%s, iteration_n=%s); the hitl_feedback field is removed in #2795.",
            data.get("phase"),
            iteration_n,
        )
        return data
