"""
Tier 1 (programmatic) health checks.

These are fast, deterministic checks that run on every lifecycle event.

Slice-8 (#2270 §5) adds the detection-plane *coverage-gap detectors* — pure
``snapshot -> Finding | None`` functions registered into the orchestrator
detection plane (see ``DetectionPlane.default``) and into the slice-1
calibration corpus by ``detector_key``.
"""

from health_checks.tier1.brc_thrashing import (
    detect_brc_thrash,
    detect_incomplete_consensus_deferral,
)
from health_checks.tier1.consensus_stall import ConsensusStallCheck
from health_checks.tier1.container_k8s import (
    detect_container_death,
    detect_container_oom_evicted,
    detect_container_restart_loop,
    detect_overseer_self_injection,
)
from health_checks.tier1.container_liveness import ContainerLivenessCheck
from health_checks.tier1.cost_budget import detect_cost_anomaly
from health_checks.tier1.decision_queue import (
    detect_approved_decision_orphaned,
    detect_auto_advance_wedge,
    detect_hitl_queue_backlog,
    detect_restarted_decision_replay,
)
from health_checks.tier1.gateway_health import (
    detect_gateway_error_spike,
    detect_gateway_repeated_denial,
    detect_gateway_token_expiry,
)
from health_checks.tier1.incomplete_consensus_stall import IncompleteConsensusStallCheck
from health_checks.tier1.llm_substrate import (
    detect_anthropic_5xx_sustained,
    detect_effective_model_drift,
    detect_llm_substrate_unreachable,
)
from health_checks.tier1.phase_output import PhaseOutputPresenceCheck
from health_checks.tier1.runtime_liveness import (
    detect_agent_restart_propagation,
    detect_duration_drift,
    detect_run_pipeline_thread_liveness,
)
from health_checks.tier1.startup_state import StartupStateCheck
from health_checks.tier1.state_consistency import StateConsistencyCheck
from health_checks.tier1.worktree_branch import (
    detect_disk_inode_pressure,
    detect_pr_external_mutation,
    detect_pushed_pr_not_updated,
    detect_worktree_corruption,
)

__all__ = [
    "ConsensusStallCheck",
    "ContainerLivenessCheck",
    "IncompleteConsensusStallCheck",
    "PhaseOutputPresenceCheck",
    "StartupStateCheck",
    "StateConsistencyCheck",
    # slice-8 §5 coverage-gap detectors
    "detect_agent_restart_propagation",
    "detect_anthropic_5xx_sustained",
    "detect_approved_decision_orphaned",
    "detect_auto_advance_wedge",
    "detect_brc_thrash",
    "detect_container_death",
    "detect_container_oom_evicted",
    "detect_container_restart_loop",
    "detect_cost_anomaly",
    "detect_disk_inode_pressure",
    "detect_duration_drift",
    "detect_effective_model_drift",
    "detect_gateway_error_spike",
    "detect_gateway_repeated_denial",
    "detect_gateway_token_expiry",
    "detect_hitl_queue_backlog",
    "detect_incomplete_consensus_deferral",
    "detect_llm_substrate_unreachable",
    "detect_overseer_self_injection",
    "detect_pr_external_mutation",
    "detect_pushed_pr_not_updated",
    "detect_restarted_decision_replay",
    "detect_run_pipeline_thread_liveness",
    "detect_worktree_corruption",
]
