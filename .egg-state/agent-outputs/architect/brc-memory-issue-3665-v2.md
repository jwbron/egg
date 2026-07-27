# BRC Memory — architect (issue-3665-v2, plan phase)

## Verdict / state
- **Proposed**: architect analysis + slice decomposition at
  `.egg-state/agent-outputs/issue-3665-v2-architect-output.json` and
  `.egg-state/agent-outputs/issue-3665-v2-architect-slices.yaml`.
- **Confirmed**: v1, all reviewers ACKed (reviewer_plan, risk_analyst).
- **Consensus reached**: true.

## Summary of assessment

### CRITICAL FINDING
Commit `6ffe97c8e` on branch `issue-3665-supervision-gaps` already implements
priorities 1-3 (livelock detection, timeout visibility, convergence-stall
suppression + snapshot enrichment). The current working tree does NOT include
this commit. The architect proposed INTEGRATION, not reimplementation.

### Key verified claims
- `detect_heartbeat_stall` exists at `consensus_stall.py:217` but is NOT
  registered in `_register_coverage_gap_detectors` and its inputs are never
  populated by `snapshot_from_health_context`.
- `snapshot_from_health_context` (detection_plane.py:511) creates RunningAgent
  entries from `live_container_ids` only, setting only role/state/lifecycle_owner.
  The `last_tool_call_age_s` and `last_heartbeat_age_s` fields default to None.
- `_check_convergence_stall` (event_loop/_loop.py:836) fires without consulting
  `_orchestrator_skip_tripwire`, `_is_brc_idle`, or the WAITING_ON_ROLE probe.
- `outcome_for()` in `_models.py:45` maps all FAILED jobs to ABNORMAL unless
  they exit EX_AUTH_FATAL (77) or EX_RATE_LIMITED (69). No JOB_OUTCOME_TIMEOUT.
- The fix commit uses exit code 143 (SIGTERM) classified as JOB_OUTCOME_LEGITIMATE.
- `agent_log_store` exists with 24h TTL and 1 MiB tail.
- `active_deadline_seconds` is a kwargs default (14400), not truly hardcoded.

### Slice DAG (proposed)
1. **Integrate fix commit 6ffe97c8e** (priorities 1-3) — root slice
2. **Alert evidence bundling** (priority 4) — serializes after slice 1
3. **Tests + corpus rows** — parallel root

### Open questions
- cq-1, cq-2, cq-3 remain unresolved on the contract. The fix commit's choices
  match the plan's defaults.
- OQ1 (integrate vs. reimplement): resolved — integrate.

## Peer state
- reviewer_plan: ACKed architect v1
- risk_analyst: ACKed architect v1, proposed v1 (ACKed by reviewer_plan)
- task_planner: proposed v2 (revised after NACK on v1), ACKed by reviewer_plan + risk_analyst
- simplifier: confirmed v2, ACKed by reviewer_plan
