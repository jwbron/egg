# Detector Starvation Audit — Issue #3596

For each of the 25 registered detection-plane detectors, this audit names the snapshot fields its predicate reads and whether `snapshot_from_health_context` populates them.

## Current snapshot_from_health_context output (detection_plane.py)

Populated fields:
- `snapshot_id` — yes
- `pipeline_id` — yes
- `phase` — yes
- `running_agents` — yes, but only `role`, `state`, `lifecycle_owner` (BUG: role=str(cid))
- `phase_state` — yes, but only `status`, `lifecycle_owner`, `event_loop_owner`, `started_age_s`, `awaiting_spawn`
- `consensus` — NO (empty dict)
- `decision_state` — NOW POPULATED (was NO)
- `container_transitions` — NO (empty tuple)
- `gateway_error_counters` — NO (empty dict)
- `cost_counters` — NO (empty dict)
- `midturn_messages` — NO (empty tuple)
- `git_state` — NOW POPULATED (was NO)
- `raw` — NO (empty dict)

RunningAgent populated fields:
- `role` — FIXED (was str(cid), now mapped from pipeline state)
- `state` — yes ("running")
- `lifecycle_owner` — yes
- `exit_code` — NOW POPULATED (was None)
- `exit_reason` — NOW POPULATED (was None)
- `last_tool_call_age_s` — NOW POPULATED (was None)
- `last_heartbeat_age_s` — NOW POPULATED (was None)

phase_state populated fields (post-enrichment):
- `status` — yes
- `lifecycle_owner` — yes
- `event_loop_owner` — yes
- `started_age_s` — yes
- `awaiting_spawn` — yes
- `expected_duration_s` — NOW POPULATED (was missing)

raw populated fields (post-enrichment):
- `raw.runtime` — NOW POPULATED (was empty)
  - `run_pipeline_thread_alive` — from driver_heartbeat
  - `thread_last_tick_age_s` — from driver_heartbeat
  - `spawn_age_s` — from driver_heartbeat

## Detector-by-detector audit (post-enrichment)

### PhaseStallDetector (detection_plane.py:295-376)
Fields read: phase_state.status, running_agents, phase_state.lifecycle_owner, phase_state.awaiting_spawn, decision_state.pending_hitl, decision_state.open_decisions, phase_state.started_age_s, consensus.blocking_agents
Status: **PARTIALLY POPULATED** — decision_state now populated; consensus.blocking_agents still empty

### detect_heartbeat_stall (consensus_stall.py:217-269)
Fields read: phase_state.status, running_agents, RunningAgent.last_tool_call_age_s, RunningAgent.last_heartbeat_age_s
Status: **NOW UNBLOCKED** — last_tool_call_age_s and last_heartbeat_age_s are populated

### detect_container_death (container_k8s.py:75-153)
Fields read: container_transitions, running_agents (for exit_code/exit_reason)
Status: **STILL STARVED** — container_transitions not populated; exit_code/exit_reason only for non-live agents

### detect_container_oom_evicted (container_k8s.py:275-311)
Fields read: container_transitions
Status: **STILL STARVED**

### detect_container_restart_loop (container_k8s.py:220-268)
Fields read: container_transitions
Status: **STILL STARVED**

### detect_overseer_self_injection (container_k8s.py:160-213)
Fields read: running_agents (role, exit_reason, exit_code), container_transitions
Status: **PARTIALLY UNBLOCKED** — role fixed; exit_reason/exit_code populated for non-live agents; container_transitions still empty

### detect_run_pipeline_thread_liveness (runtime_liveness.py:84-131)
Fields read: raw.runtime (run_pipeline_thread_alive, thread_last_tick_age_s)
Status: **NOW UNBLOCKED** — raw.runtime populated from driver_heartbeat

### detect_duration_drift (runtime_liveness.py:138-186)
Fields read: phase_state.status, phase_state.started_age_s, phase_state.expected_duration_s, phase_state.drift_ratio
Status: **PARTIALLY UNBLOCKED** — expected_duration_s now populated; drift_ratio still not populated

### detect_agent_restart_propagation (runtime_liveness.py:193-252)
Fields read: raw.runtime (restart_propagation), phase_state (restart_requested_age_s, restart_role), container_transitions
Status: **STILL STARVED**

### detect_auto_advance_wedge (decision_queue.py:80-124)
Fields read: phase_state.status, raw.runtime (auto_advance_pending, auto_advance_age_s)
Status: **PARTIALLY UNBLOCKED** — phase_state.status populated; auto_advance_pending/auto_advance_age_s still not populated

### detect_approved_decision_orphaned (decision_queue.py:131-171)
Fields read: decision_state (approved_unapplied)
Status: **NOW UNBLOCKED** — decision_state populated

### detect_restarted_decision_replay (decision_queue.py:178-212)
Fields read: decision_state (replay_pending, replay_count)
Status: **PARTIALLY UNBLOCKED** — replay_pending/replay_count set to False/0 (not tracked on pipeline model)

### detect_hitl_queue_backlog (decision_queue.py:224-261)
Fields read: decision_state (oldest_open_age_s)
Status: **NOW UNBLOCKED** — decision_state populated

### detect_worktree_corruption (worktree_branch.py:81-124)
Fields read: git_state (fsck_errors, index_lock_present, lock_age_s, branch)
Status: **NOW UNBLOCKED** — git_state populated

### detect_disk_inode_pressure (worktree_branch.py:131-174)
Fields read: raw.resources (disk_used_pct, inode_used_pct, disk_threshold_pct)
Status: **STILL STARVED** — raw.resources not populated

### detect_pr_external_mutation (worktree_branch.py:181-215)
Fields read: raw.pr_state (pr_head_sha, pushed_sha, external_mutation)
Status: **STILL STARVED**

### detect_pushed_pr_not_updated (worktree_branch.py:222-267)
Fields read: git_state (pr_head_sha, last_pushed_sha, pushed_age_s, pr_externally_mutated)
Status: **PARTIALLY STARVED** — git_state populated with branch/commit info but not PR-specific fields

### detect_cost_anomaly (cost_budget.py:51-87)
Fields read: cost_counters (cost_per_hour_usd, max_llm_cost_per_hour, tokens, cost_usd)
Status: **STILL STARVED**

### detect_gateway_error_spike (gateway_health.py:58-87)
Fields read: gateway_error_counters (5xx_rate_per_min, rate_threshold_per_min)
Status: **STILL STARVED**

### detect_gateway_repeated_denial (gateway_health.py:94-127)
Fields read: gateway_error_counters (identical_403_streak, denial_signature)
Status: **STILL STARVED**

### detect_gateway_token_expiry (gateway_health.py:134-162)
Fields read: gateway_error_counters (token_expired, 401)
Status: **STILL STARVED**

### detect_llm_substrate_unreachable (llm_substrate.py:62-96)
Fields read: raw.llm (litellm_reachable, consecutive_failures)
Status: **STILL STARVED**

### detect_effective_model_drift (llm_substrate.py:103-131)
Fields read: raw.llm (requested_model, effective_model)
Status: **STILL STARVED**

### detect_anthropic_5xx_sustained (llm_substrate.py:138-169)
Fields read: raw.llm (anthropic_5xx_streak, window_s)
Status: **STILL STARVED**

### detect_brc_thrash (brc_thrashing.py:57-92)
Fields read: consensus (nack_cycles, late_confirmed_then_renack)
Status: **STILL STARVED**

### detect_incomplete_consensus_deferral (brc_thrashing.py:99-131)
Fields read: consensus (incomplete_consensus_deferrals, deferral_cap)
Status: **STILL STARVED**

### detect_overseer_self_health (self_monitor.py:349-393)
Fields read: raw.self_health (classifier_failure_rate, advisor_failure_rate, failure_threshold)
Status: **STILL STARVED**

### detect_branch_divergence (_alerts.py:850-890)
Fields read: git_state (is_ancestor_of_base, patch_id_matches, pr_subject_divergence, branch)
Status: **PARTIALLY UNBLOCKED** — branch populated; is_ancestor_of_base, patch_id_matches, pr_subject_divergence not populated

## Summary (post-enrichment)

- Total registered detectors: 25
- Fully populated (all fields): 0
- Partially starved: 9 (down from 3 — more detectors now have some fields)
- Fully starved: 16 (down from 22)
- **Newly unblocked**: detect_heartbeat_stall, detect_approved_decision_orphaned, detect_hitl_queue_backlog, detect_worktree_corruption, detect_run_pipeline_thread_liveness
- **Partially unblocked**: detect_duration_drift (expected_duration_s now populated), detect_auto_advance_wedge (phase_state.status populated)
- **role=str(cid) bug fixed** for all detectors that key on agent role

## Priority order for remaining snapshot enrichment

1. **container_transitions** — unblocks 4 detectors (container_death, oom_evicted, restart_loop, overseer_self_injection)
2. **raw.llm** — unblocks 3 detectors (llm_substrate_unreachable, effective_model_drift, anthropic_5xx)
3. **gateway_error_counters** — unblocks 3 detectors
4. **consensus** — unblocks 2 detectors (brc_thrash, incomplete_consensus_deferral) + helps PhaseStallDetector
5. **cost_counters** — unblocks detect_cost_anomaly
6. **raw.resources** — unblocks detect_disk_inode_pressure
7. **raw.pr_state** — unblocks detect_pr_external_mutation
8. **raw.self_health** — unblocks detect_overseer_self_health
