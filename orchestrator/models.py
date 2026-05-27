"""
Pydantic models for orchestrator pipeline state.

These models represent the orchestrator's view of pipeline execution,
including container state, HITL decisions, and agent coordination.
"""

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from slice_id_validation import SLICE_ID_PATTERN

# Phase-aware fallback defaults for consensus timeout. Calibrated against
# producer/reviewer fan-out and iteration profile per phase — see #2263.
PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN: dict[str, int] = {
    "refine": 30,
    "plan": 60,
    "implement": 90,
}


def resolve_consensus_timeout_minutes(config: PipelineConfig, phase: str) -> int:
    """Resolve the consensus timeout (minutes) for *phase*.

    Resolution order, highest priority first:

    1. The phase-specific override field (``consensus_timeout_minutes_<phase>``).
    2. The legacy global field (``consensus_timeout_minutes``), if explicitly
       set — preserves the AC clause that pipelines passing only the global
       continue to behave identically across all three phases.
    3. The phase-aware default from :data:`PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN`,
       falling back to the ``refine`` default for any unknown phase string
       (the smallest calibrated budget — safe upper bound for unrecognized
       phases that may have shorter NACK loops than ``implement``).
    """
    override: int | None = getattr(config, f"consensus_timeout_minutes_{phase}", None)
    if override is not None:
        return override
    if config.consensus_timeout_minutes is not None:
        return config.consensus_timeout_minutes
    return PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN.get(
        phase, PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN["refine"]
    )


class PipelineStatus(StrEnum):
    """Overall status of a pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PipelineMode(StrEnum):
    """Pipeline execution mode."""

    ISSUE = "issue"  # Standard issue-driven SDLC pipeline


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


# Single source of truth for "which container statuses count as live"
# (#2420). Both ``routes/pipelines._count_live_pods_for_pipeline`` and
# ``startup_reconciliation.reconcile_stale_containers`` import this so
# the two label-scoped pod checks can't drift — drift would reintroduce
# the #2411 false-positive class (live pipelines marked FAILED at startup
# while the start_pipeline guard still treats their pods as live).
#
# Pending / Creating / Running are the only statuses that map to a pod
# whose work is still in flight. Terminal phases (Failed / Succeeded →
# ``ContainerStatus.FAILED`` / ``EXITED``) are deliberately excluded:
# k8s keeps such pod objects around for the ``ttlSecondsAfterFinished``
# window (600s in our Job specs) after the container exits, so a naive
# "any pod with this label" check would treat a recently-finished pod as
# live and mask a wedged pipeline whose work has actually stopped. The
# guard's contract is "is there anything still doing work?" — terminal
# pod objects within the TTL window do not count.
LIVE_POD_STATUSES: tuple[ContainerStatus, ...] = (
    ContainerStatus.PENDING,
    ContainerStatus.CREATING,
    ContainerStatus.RUNNING,
)


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
    consensus_timeout_minutes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Global consensus timeout in minutes before publishing an "
            "OVERSEER_ALERT. When set, applies to every phase and overrides "
            "phase-aware defaults. When None (the default), each phase uses "
            "its calibrated default from PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN "
            "unless a per-phase field below is set. (#2263, #2264)"
        ),
    )
    consensus_timeout_minutes_refine: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-phase consensus timeout for refine. When set, wins over the legacy "
            "global and the phase-aware default. (#2263)"
        ),
    )
    consensus_timeout_minutes_plan: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-phase consensus timeout for plan. When set, wins over the legacy "
            "global and the phase-aware default. (#2263)"
        ),
    )
    consensus_timeout_minutes_implement: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-phase consensus timeout for implement. When set, wins over the "
            "legacy global and the phase-aware default. (#2263)"
        ),
    )
    post_consensus_iteration_budget_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            "After consensus_timeout_minutes elapses, the post-timeout poll loop "
            "waits up to this many seconds without producer progress before "
            "force-killing remaining containers (issue #2245). The clock "
            "rebaselines whenever a producer issues a new CONSENSUS_PROPOSE "
            "(initial propose or NACK→re-propose), so a healthy multi-iteration "
            "BRC cycle no longer counts iteration time against a single fixed "
            "budget."
        ),
    )
    post_consensus_max_total_seconds: int = Field(
        default=14400,
        ge=60,
        description=(
            "Hard ceiling on the post-consensus-timeout wait, in seconds. "
            "Caps the total time spent in the post-timeout poll loop even when "
            "producer progress keeps rebaselining the per-iteration budget — "
            "prevents an unbounded loop if propose events keep arriving but "
            "consensus never converges. Default 4 hours."
        ),
    )
    brc_consensus_progress_gate_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "Defer the consensus-timeout OVERSEER_ALERT while any BRC progress signal "
            "(CONSENSUS_PROPOSE/ACK/NACK or container heartbeat) has fired within this "
            "many seconds. 0 disables the gate. (#2243, #2264)"
        ),
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
    orchestrator_implement_heartbeat_timeout_seconds: int = Field(
        default=600,
        ge=10,
        description="Seconds without heartbeat/progress before auto-nudge during implement phase",
    )
    orchestrator_error_repeat_threshold: int = Field(
        default=3, ge=1, description="Identical error count before escalation"
    )
    orchestrator_message_rate_limit: int = Field(
        default=20, ge=1, description="Max messages per minute before auto-throttle"
    )
    orchestrator_activity_quiet_seconds: int = Field(
        default=120,
        ge=0,
        description=(
            "Seconds since the last CONTAINER_ACTIVITY event below which an "
            "agent is considered alive — suppresses heartbeat/progress stall "
            "alerts even when bus-level HEARTBEATs are absent (issue #2190). "
            "Set to 0 to disable the gate entirely (operator escape hatch)."
        ),
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
    overseer_max_turns: int = Field(
        default=2000,
        ge=100,
        le=10000,
        description="Max Agent SDK turns for the overseer agent per phase",
    )
    overseer_max_respawns: int = Field(
        default=3,
        ge=0,
        le=50,
        description="Max times to respawn the overseer if it exits mid-pipeline",
    )
    overseer_rerun_min_work_seconds: int = Field(
        default=60,
        ge=1,
        description="Min work seconds after request_changes before flagging re-run anomaly",
    )
    overseer_hitl_propagation_timeout_seconds: int = Field(
        default=300,
        ge=10,
        description="Seconds before raising HITL propagation failure alert",
    )
    post_proposal_grace_seconds: int = Field(
        default=300,
        ge=30,
        description="Grace period after CONSENSUS_PROPOSE before flagging blocking reviewers as stalled",
    )
    auto_repropose_debounce_seconds: int = Field(
        default=60,
        ge=1,
        description="Debounce window between consecutive auto re-proposals on producer push (seconds)",
    )
    max_auto_repropose: int = Field(
        default=5,
        ge=0,
        description="Maximum automatic re-proposals per producer per review cycle (0 to disable)",
    )
    orchestrator_post_ack_confirmation_timeout_seconds: int = Field(
        default=180,
        ge=30,
        description="Timeout for producers to send CONFIRMED after being fully ACKed",
    )
    orchestrator_plan_post_ack_confirmation_timeout_seconds: int = Field(
        default=300,
        ge=30,
        description=(
            "Plan-phase post-ACK confirm timeout. Plan-phase reconciliation "
            "(resolved decisions, feedback bodies, slice-DAG sanity) "
            "legitimately exceeds the default 180s on heavy pipelines, so "
            "plan uses a higher threshold than refine/implement. (#2242)"
        ),
    )
    orchestrator_alert_progress_gate_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "Defer heartbeat-stall and progress-stall alerts while any peer "
            "agent or the BRC bus has emitted a signal within this many "
            "seconds. Mirrors brc_consensus_progress_gate_seconds but for "
            "per-agent tripwires (#2242). 0 disables the gate."
        ),
    )
    active_agent_stall_extension_seconds: int = Field(
        default=120,
        ge=30,
        description="If a blocking agent has progress events within this window, suppress stall alerts",
    )
    # Advisor-strategy knobs (issue #1962). The advisor is invoked from the
    # overseer when Haiku flags an anomaly AND a Tier-1 health alert has
    # tripped. See sandbox/agent-config/rules/overseer.md and
    # docs/guides/pipeline-health-monitoring.md for context.
    overseer_advisor_model: str = Field(
        default="opus",
        description=(
            "Opus-class model used as the advisor when the Haiku-classify "
            "loop intersects a Tier-1 health alert. Uses the 'opus' alias "
            "for automatic version adoption. NOTE: per decision-19, no "
            "per-phase invocation cap is enforced; the existing "
            "max_llm_cost_per_hour envelope is the only budget control "
            "until the follow-up advisor-budget issue lands."
        ),
    )
    overseer_advisor_recent_log_bytes_cap: int = Field(
        default=256_000,
        ge=0,
        description=(
            "Byte cap for the ``recent_log_lines`` block in the advisor "
            "prompt (issue #2120). When the joined block exceeds the "
            "cap, the prompt-builder drops oldest lines first so the "
            "most-recent lines (highest signal) survive, and prepends a "
            "marker so the advisor knows truncation happened. 256 KiB "
            "default sits well under the opus context window with "
            "headroom for the rest of the prompt; bump for log-heavy "
            "anomalies, set to 0 to disable (not recommended — leaves "
            "the prompt-builder open to pathological log payloads)."
        ),
    )
    overseer_auto_file_issues_mode: Literal["shadow", "live"] = Field(
        default="shadow",
        description=(
            "Auto-issue filing rollout mode. 'shadow' (default): the advisor's "
            "decision='file_issue' surfaces as an OVERSEER_ALERT + HITL "
            "decision; the human gates the actual filing. 'live': the same "
            "HITL flow still runs but the CLI verb is willing to call gh "
            "once approval lands. Full disable continues to be expressed via "
            "overseer_enabled=False (per decision-10)."
        ),
    )
    overseer_owns_host_detection: bool = Field(
        default=False,
        description=(
            "Calibration-window flag. While False (the default), /sdlc keeps "
            "running its host-side stall / silent-agent / NACK / long-run / "
            "rescue detectors so the overseer's new detectors can be "
            "calibrated side-by-side with no behavior regression. The "
            "follow-up cleanup PR flips the default to True and deletes the "
            "now-dormant /sdlc detection blocks."
        ),
    )
    overseer_stuck_phase_transition_seconds: int = Field(
        default=180,
        ge=10,
        description=(
            "Wall-clock seconds for the orchestrator-side "
            "stuck-phase-transition trigger. Bumped from ~60s to 180s per "
            "feedback-1.Q2 — legitimate refiner work on a complex multi-"
            "thread issue can take 5-10+ minutes."
        ),
    )
    overseer_agent_stall_seconds: int = Field(
        default=180,
        ge=10,
        description=(
            "Per-agent elapsed-time threshold for the migrated detect_agent_stall "
            "detector (issue #1962 host migration). Distinct from "
            "overseer_stuck_phase_transition_seconds (which fires on the "
            "orchestrator-level signal) so operators can tune them "
            "independently. Default matches stuck-phase-transition for "
            "release-time parity."
        ),
    )
    overseer_silent_agent_threshold_seconds: int = Field(
        default=600,
        ge=10,
        description=(
            "Threshold (10 min default) for detect_agent_silent. Lifted out "
            "of /sdlc into PipelineConfig so the host migration in Phase 6 "
            "of #1962 can read it via the pipelines-status endpoint."
        ),
    )
    overseer_long_running_phase_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            "Threshold (60 min default) for detect_phase_long_running on "
            "implement phase. Lifted out of /sdlc into PipelineConfig."
        ),
    )
    overseer_nack_unresolved_seconds: int = Field(
        default=180,
        ge=10,
        description=(
            "Threshold (3 min default) for detect_nack_unresolved. Lifted "
            "out of /sdlc into PipelineConfig."
        ),
    )
    start_phase: str | None = Field(
        default=None,
        description="Phase to start execution from, skipping earlier phases. "
        "Valid values: 'plan', 'implement'. When set, the pipeline starts "
        "at this phase instead of 'refine'.",
    )
    spawn_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Maximum retry attempts for transient gateway worktree-creation "
            "failures during agent spawn. Total attempts = spawn_max_retries + 1. "
            "0 disables retry. See #1839."
        ),
    )
    spawn_retry_initial_backoff_seconds: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Initial backoff seconds between spawn retries. Subsequent attempts "
            "scale by 2.5x (e.g. 2s, 5s, 12.5s). See #1839."
        ),
    )
    phase_spawn_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Phase-level retry attempts when at least one role's spawn fails "
            "with a transient error (e.g. gateway restart). Per-role retries "
            "(see spawn_max_retries) run first; this second budget bridges "
            "longer outages like a ~30s gateway cold start. 0 disables "
            "phase-level retry. See #1879."
        ),
    )
    phase_spawn_retry_initial_backoff_seconds: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Initial backoff seconds before the first phase-level spawn retry. "
            "Subsequent attempts scale by 3x (e.g. 30s, 90s). See #1879."
        ),
    )
    agent_models: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-role model overrides keyed by AgentRole value (e.g. "
            "{'refiner': 'qwen3-coder-30b'}). Keys are restricted to the "
            "SDLC phase producer and reviewer roles the resolver honors "
            "(agent_roles.MODEL_OVERRIDE_ROLES) — utility/interface roles "
            "such as overseer or autofixer are rejected at construction "
            "time because their spawn paths never consult the resolver. "
            "The value is the upstream-side model name: a Claude alias "
            "(opus / opus[1m] / sonnet / sonnet[1m] / haiku / claude-*) "
            "routes through the Anthropic upstream, anything else routes "
            "through the in-cluster LiteLLM proxy with the recognised "
            "alias 'opus' presented to Claude Code (cq-5 mitigation). When a "
            "role is absent from this mapping the resolver falls back to the "
            "repository-level default_agent_model setting and then to the "
            "built-in 'opus' default. See #2769."
        ),
    )

    @field_validator("agent_models")
    @classmethod
    def _validate_agent_models_roles(cls, v: dict[str, str]) -> dict[str, str]:
        """Reject ``agent_models`` keys the per-agent model resolver never honors.

        ``resolve_agent_model`` is consulted only by the spawn/restart
        paths that cover the SDLC phase producers and reviewers
        (``MODEL_OVERRIDE_ROLES``). Utility roles (autofixer,
        conflict_resolver) and interface roles (overseer, inspector) spawn
        through paths that never call the resolver, so an override naming
        one of them would be silently dropped at spawn. Rejecting both
        typos and these unhonored-but-real roles at PipelineConfig
        construction time surfaces the misconfiguration immediately
        instead of letting it silently no-op. See #2769 task-2-1.
        """
        if not v:
            return v
        # Lazy import to avoid a circular dependency with shared.egg_contracts
        # when PipelineConfig is imported during package init.
        from egg_contracts.agent_roles import MODEL_OVERRIDE_ROLES

        valid = {role.value for role in MODEL_OVERRIDE_ROLES}
        invalid = sorted(role for role in v if role not in valid)
        if invalid:
            raise ValueError(
                f"Invalid agent_models role keys: {invalid}. agent_models is "
                f"honored only for SDLC phase producer and reviewer roles: "
                f"{sorted(valid)}"
            )
        return v

    @model_validator(mode="after")
    def _validate_post_consensus_budgets(self) -> PipelineConfig:
        """Reject configs where the absolute cap is below the per-iteration budget.

        Without this, a misconfigured pipeline (e.g. ``iteration_budget=7200``
        with ``max_total=3600``) silently makes the per-iteration logic
        unreachable — the absolute cap would always fire first. See #2245.
        """
        if self.post_consensus_max_total_seconds < self.post_consensus_iteration_budget_seconds:
            raise ValueError(
                "post_consensus_max_total_seconds "
                f"({self.post_consensus_max_total_seconds}) must be >= "
                "post_consensus_iteration_budget_seconds "
                f"({self.post_consensus_iteration_budget_seconds})"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _alias_post_propose_grace(cls, data: Any) -> Any:
        """Accept orchestrator_post_propose_grace_seconds as alias for post_proposal_grace_seconds.

        The original plan added orchestrator_post_propose_grace_seconds as a
        separate field, but review identified it as a duplicate of the existing
        post_proposal_grace_seconds. This validator preserves backward
        compatibility for callers using the old name.
        """
        if isinstance(data, dict) and "orchestrator_post_propose_grace_seconds" in data:
            data.setdefault(
                "post_proposal_grace_seconds",
                data.pop("orchestrator_post_propose_grace_seconds"),
            )
        return data

    @property
    def orchestrator_post_propose_grace_seconds(self) -> int:
        """Alias for post_proposal_grace_seconds (backward compatibility)."""
        return self.post_proposal_grace_seconds

    @field_validator("start_phase")
    @classmethod
    def validate_start_phase(cls, v: str | None) -> str | None:
        if v is not None:
            valid = {"plan", "implement"}
            if v not in valid:
                raise ValueError(f"Invalid start_phase: {v!r}. Must be one of {sorted(valid)}")
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
    base_branch: str | None = Field(
        default=None,
        description="Base branch for PR creation. When None, auto-detected from repo's default branch.",
    )
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
        default_factory=lambda: datetime.now(UTC), description="When pipeline was created"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Last update time"
    )
    contract_synced: bool = Field(default=True, description="Whether state is synced with contract")
    network_mode: str | None = Field(
        default=None,
        description="Network mode for spawned containers: 'public', 'private', or None (auto from pipeline mode)",
    )
    mode: PipelineMode = Field(
        default=PipelineMode.ISSUE,
        description="Pipeline execution mode (currently only 'issue' for standard SDLC)",
    )
    pr_number: int | None = Field(
        default=None,
        ge=1,
        description="Number of the PR opened by this pipeline's implement "
        "phase (issue #1557 reverse-index in-flight detection). None until "
        "the pipeline reaches the PR stage.",
    )
    pr_head_sha: str | None = Field(
        default=None,
        description="Head commit SHA of the PR opened by this pipeline, "
        "captured during PR finalization. None until the PR stage.",
    )

    @field_validator("pr_head_sha")
    @classmethod
    def _validate_pr_head_sha(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            return None
        if v is not None and not re.fullmatch(r"[0-9a-f]{7,40}", v):
            raise ValueError("pr_head_sha must be a 7-40 char hex string")
        return v

    has_contract: bool = Field(
        default=True,
        description="Whether this pipeline has an upstream SDLC contract "
        "(plan/refine artifacts, .egg-state/contracts/<issue>.json). "
        "Controls whether the implement-phase reviewer roster includes "
        "reviewer_contract. Default True for standard issue-mode pipelines.",
    )
    error: str | None = Field(default=None, description="Error if failed")
    analysis: str | None = Field(
        default=None,
        max_length=200_000,
        description="Pre-generated analysis for short flow pipelines (written to drafts on first run)",
    )
    plan: str | None = Field(
        default=None,
        max_length=200_000,
        description="Pre-generated plan with yaml-tasks appendix for short flow pipelines "
        "(written to drafts and parsed into contract on first run)",
    )
    source_branch: str | None = Field(
        default=None,
        description="Source branch to read prior-run artifacts from (plan, analysis). "
        "When set, the orchestrator reads drafts from this branch via git show "
        "instead of requiring inline content.",
    )
    source_artifact_prefix: str | None = Field(
        default=None,
        description="Explicit prefix for draft filenames on the source branch "
        "(e.g. 'issue-1570-v3'). Overrides the default pipeline_id-based "
        "prefix resolution when reading artifacts from source_branch.",
    )
    run_epoch: datetime | None = Field(
        default=None,
        description="Thread ownership epoch — bumped on restart_phase and start_pipeline "
        "recovery so lingering _run_pipeline threads detect the change and exit. "
        "Separate from created_at which is user-facing.",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Optimistic locking version (incremented on each save)",
    )
    jira_ticket: str | None = Field(
        default=None,
        description="Optional Atlassian Jira ticket key (e.g. 'ENG-1234') the "
        "pipeline is working against. Advisory only — exported to the sandbox "
        'as EGG_JIRA_TICKET so agents can call `jira ticket get "$EGG_JIRA_TICKET"` '
        "without hard-coding a key. The gateway does NOT use this for policy "
        "gating; only the project allowlist in config/context-filters.yaml "
        "can authorise a Jira call (issue #1556 refine decision #9).",
    )
    # Jira-epic SDLC support (issue #1557). When ``is_epic`` is true the
    # orchestrator schedules an APPLY phase after every HITL approval so
    # the APPLIER role can drive Jira mutations (epic-Description writes,
    # child creates, link creates, ``Won't Do`` transitions). ``pipeline_
    # mode`` distinguishes fresh-epic (no children yet) from reassess
    # (existing children to classify). Both default to falsy values so
    # contracts written before #1557 load with stable shape.
    is_epic: bool = Field(
        default=False,
        description=(
            "True when ``jira_ticket`` resolves to a Jira issue with "
            "``issuetype.name == 'Epic'`` (or the operator passed "
            "``mode='fresh' | 'reassess'`` to ``submit_task``). The "
            "orchestrator inspects this flag to decide whether to "
            "insert the APPLY phase between PLAN and IMPLEMENT — "
            "non-epic pipelines continue to advance PLAN → IMPLEMENT "
            "directly. Persisted alongside ``jira_ticket`` and round-"
            "trips through the state-store."
        ),
    )
    pipeline_mode: Literal["fresh", "reassess"] | None = Field(
        default=None,
        description=(
            "Epic-mode sub-classification (issue #1557). ``'fresh'`` "
            "when the epic has no children yet (the planner produces "
            "all-net-new ``jira_action='create'`` tasks); ``'reassess'`` "
            "when the epic already has children to classify "
            "(Done/In-flight/Updatable) and the planner emits a mix of "
            "``edit`` / ``create`` / ``wontdo`` / ``split-of`` / "
            "``consolidate-into`` actions. ``None`` for non-epic "
            "pipelines and for epic pipelines where the operator "
            "explicitly disabled APPLY (e.g. dry-run inspections)."
        ),
    )
    pr_url: str | None = Field(
        default=None,
        description=(
            "Full URL of the implement-phase PR opened by this pipeline "
            "(issue #1557 slice-2 — reverse-index in-flight detection). "
            "Populated alongside ``pr_number`` when the implement phase "
            "opens a PR; consumed by the reassess sweep's in-flight "
            "classifier so existing children with an open PR aren't "
            "re-mutated without operator confirmation. ``None`` for "
            "pipelines that haven't reached the PR stage yet."
        ),
    )

    @field_validator("jira_ticket")
    @classmethod
    def _validate_jira_ticket(cls, v: str | None) -> str | None:
        """Permit either None or a standard Atlassian ticket key."""
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("jira_ticket must be a string")
        trimmed = v.strip()
        if trimmed == "":
            return None
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", trimmed):
            raise ValueError("jira_ticket must match '<PROJECT>-<number>' (e.g. 'ENG-1234')")
        return trimmed

    @field_validator("pr_url")
    @classmethod
    def _validate_pr_url(cls, v: str | None) -> str | None:
        """Permit either None or a non-empty HTTPS URL string.

        Kept deliberately permissive: the orchestrator stamps whatever
        the GitHub API returns for the PR's ``html_url``, and we don't
        want a regex tightening to break older contracts that captured
        a slightly different shape (e.g. http→https redirect).
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("pr_url must be a string")
        trimmed = v.strip()
        if trimmed == "":
            return None
        if not (trimmed.startswith("http://") or trimmed.startswith("https://")):
            raise ValueError("pr_url must be an http(s) URL")
        return trimmed

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
        self.updated_at = datetime.now(UTC)
        return decision

    def resolve_decision(self, decision_id: str, resolution: str) -> HITLDecision | None:
        """Resolve a HITL decision."""
        for decision in self.decisions:
            if decision.id == decision_id and decision.status == DecisionStatus.PENDING:
                decision.status = DecisionStatus.RESOLVED
                decision.resolution = resolution
                decision.resolved_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)
                return decision
        return None


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
