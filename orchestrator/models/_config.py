"""Pipeline configuration model + consensus-timeout resolution.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312).
Every symbol re-exports through the ``models`` barrel (stable public API).
"""

from typing import Any, Literal

from agent_model_resolution import OVERSEER_TIER_MODELS
from egg_contracts.models import PipelinePhase
from pydantic import BaseModel, Field, field_validator, model_validator

# Phase-aware fallback defaults for consensus timeout. Originally calibrated
# against producer/reviewer fan-out and iteration profile per phase (#2263),
# then widened 3-4x after the wall hard-failed verified-correct work (#3490):
# the clock starts at slice start, so initial build time on a large slice eats
# the review/iteration budget, and a single legitimate step on a giant slice
# (e.g. `make test-all`) can run 30-60+ minutes (#3341). The wall is a
# last-resort backstop against a genuinely wedged slice, not a pace-setter;
# the #2243 progress gate already defers it while agents show live activity.
PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN: dict[str, int] = {
    "refine": 90,
    "plan": 180,
    "implement": 360,
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
        description=(
            "HITL converge-before-advance round count at which a non-fatal "
            "overseer non-convergence alert is emitted (#3392). No longer a "
            "force-advance budget: the loop is human-gated every round and "
            "never force-advances."
        ),
    )
    hitl_gates: bool = Field(
        default=True,
        description=(
            "Pause for human approval after non-gated phases. For refine and "
            "plan this flag selects the gate *mode* rather than disabling the "
            "gate outright (#3392): when True (the default) those phases run "
            "the converge-before-advance loop, resolving decisions with a human "
            "each round; when False they advance autonomously after surfacing a "
            "non-blocking gate event (the converge loop requires a human, so it "
            "cannot run unattended — blocking on it would hang the pipeline "
            "indefinitely). For phases outside refine/plan the flag toggles the "
            "post-phase approval pause as before."
        ),
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
    max_parallel_slices: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-pipeline cap on how many implement-phase slices run concurrently "
            "in a slice-DAG wave. Set at pipeline creation. Under on-demand "
            "spawning (#3164) an in-flight slice costs at most one short-lived "
            "one-shot pod per BRC event rather than a resident ~8-container "
            "cohort, so this remains the primary implement-phase host-load knob "
            "but at a far lower per-slice cost. When None (the default), the "
            "orchestrator falls back to the EGG_ORCH_MAX_PARALLEL_SLICES env var, "
            "whose default is 4. The process-wide "
            "EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES cap still applies across all "
            "pipelines."
        ),
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
    agent_timeout_seconds: int = Field(
        default=7200,
        ge=60,
        description=(
            "Maximum runtime for a single agent container, in seconds. "
            "Passed as ``active_deadline_seconds`` to the Kubernetes Job "
            "spec and as ``EGG_AGENT_TIMEOUT_SECONDS`` to the sandbox "
            "container so the agent can surface the deadline. "
            "Default 7200 (2 hours). (#3665)"
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
        # Default is the routine-tier model; sourced from the single tier
        # table so it can never drift from the resolver's default detection
        # (`_overseer_decision_override` / the deprecation shims below).
        default=OVERSEER_TIER_MODELS["routine"],
        description=(
            "Deprecated and runtime-inert (#2270 §1, folds #2813). It does NOT "
            "drive the overseer's runtime model: the spawn base model resolves "
            "through the per-agent resolver (resolve_agent_model(OVERSEER) -> "
            "opus by default), and the overseer's decision work is tiered via "
            "resolve_overseer_model — classify on haiku, routine corrective "
            "decisions on sonnet, adversarial/high-stakes adjudication on opus "
            "(overseer/monitor.py no longer reads this field as of slice-9). "
            "The ONLY residual effect is a documented back-compat override: a "
            "non-default value still feeds resolve_overseer_model's adversarial "
            "tier when agent_models['overseer'] is unset. Set "
            "agent_models['overseer'] instead; this field will be removed in a "
            "future release (PipelineConfig uses extra='ignore', so a persisted "
            "config still carrying it loads cleanly once dropped)."
        ),
    )

    @field_validator("overseer_decision_maker_model")
    @classmethod
    def _warn_overseer_decision_maker_model_deprecated(cls, v: str) -> str:
        """Surface a deprecation notice when the deprecated field is set (#2270 §1).

        The field validator runs only when the value is *provided* at
        construction (Pydantic does not validate omitted defaults), so a
        default ``PipelineConfig()`` stays silent while an explicit
        non-default value logs a one-line deprecation warning. The value is
        still honoured for back-compat — ``resolve_overseer_model`` maps it
        through to the adversarial/decision tier ONLY when
        ``agent_models['overseer']`` is unset — so this is a notice, not a
        rejection. As of slice-9 the field is runtime-inert: the overseer's
        live decision/adjudication path (overseer/monitor.py) no longer reads
        it; only the back-compat spawn override survives.
        """
        if v and v != OVERSEER_TIER_MODELS["routine"]:
            import logging as _logging

            _logging.getLogger("orchestrator.models").warning(
                "PipelineConfig.overseer_decision_maker_model=%r is deprecated "
                "(#2270 §1, folds #2813): the overseer model now resolves via "
                "resolve_overseer_model / resolve_agent_model(OVERSEER) -> opus "
                "by default. Set agent_models['overseer'] instead; this field "
                "is runtime-inert as of slice-9 (only a back-compat spawn "
                "override survives) and will be removed in a future release.",
                v,
            )
        return v

    overseer_max_turns: int = Field(
        default=2000,
        ge=100,
        le=10000,
        description="Max Agent SDK turns for the overseer agent per phase",
    )
    # overseer_max_respawns removed in #2270 slice-5: the standing-pod overseer
    # respawn loop was retired (orchestrator-side detection plane + on-demand
    # adjudicator is the replacement; any surviving restart need goes through the
    # general agent-restart machinery). PipelineConfig uses extra='ignore', so a
    # persisted config still carrying this key loads cleanly.
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
    overseer_phase_desync_alert_seconds: int = Field(
        default=300,
        ge=10,
        description=(
            "Seconds a pipeline-record vs contract current_phase mismatch may "
            "persist while status=running before the deterministic desync "
            "alert fires (#3521)"
        ),
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
            "Auto-issue filing rollout mode — the guarded shadow->enforce gate "
            "(#2270 §6). 'shadow' (default): the overseer's 'issue' corrective "
            "action and the advisor's decision='file_issue' surface as an "
            "OVERSEER_ALERT + HITL decision; the human gates the actual filing "
            "and overseer/monitor.py does NOT call gh. 'live' (enforce): the "
            "same HITL flow still runs and the monitor's 'issue' action files "
            "the diagnostic (through the two-tier dedup ledger) / the CLI verb "
            "is willing to call gh once approval lands. Default stays 'shadow'; "
            "flip to 'live' only after telemetry validates the detectors' "
            "precision. Full disable continues to be expressed via "
            "overseer_enabled=False (per decision-10)."
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
            "{'refiner': 'qwen3-max'}). Keys are restricted to the "
            "SDLC phase producer and reviewer roles the resolver honors "
            "(agent_roles.MODEL_OVERRIDE_ROLES) — utility/interface roles "
            "such as overseer or autofixer are rejected at construction "
            "time because their spawn paths never consult the resolver. "
            "The value is the upstream-side model name: a Claude alias "
            "(opus / opus[1m] / sonnet / sonnet[1m] / haiku / fable / "
            "fable[1m] / claude-*) "
            "routes through the Anthropic upstream, anything else routes "
            "through the in-cluster LiteLLM proxy with the "
            "ANTHROPIC_CUSTOM_MODEL_OPTION env-var registration set on "
            "the agent's sandbox so Claude Code opts into 1M-context "
            "compaction math (#2832, superseding the original cq-5 "
            "recognized-alias mitigation). When a role is absent from "
            "this mapping the resolver falls back to the "
            "repository-level default_agent_model setting and then to the "
            "built-in default ('fable' for refine/plan phase roles, "
            "'opus' otherwise). See #2769."
        ),
    )

    @field_validator("agent_models")
    @classmethod
    def _validate_agent_models_roles(cls, v: dict[str, str]) -> dict[str, str]:
        """Reject ``agent_models`` keys the per-agent model resolver never honors.

        ``resolve_agent_model`` is consulted only by the spawn/restart
        paths that cover the SDLC phase producers and reviewers
        (``MODEL_OVERRIDE_ROLES``). Utility roles (autofixer,
        conflict_resolver) and interface roles (overseer) spawn
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
