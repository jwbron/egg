# BRC Memory — Issue #3596 (coder)

## Pipeline Context
- Issue: #3596 — Operator cannot tell a working agent from a wedged one
- Role: coder
- Phase: implement (per BRC event)

## Operator's cq-1 Resolution (binding)
The operator resolved cq-1 with a detailed response:
1. The detection plane IS already wired and IS evaluated in production — do NOT add a new tick or invocation path.
2. The actual defect is that `snapshot_from_health_context` populates only 5 of 13 top-level fields and 3 of 7 RunningAgent fields.
3. Fix `role=str(cid)` defect — container IDs are mapped to agent roles via pipeline state.
4. Scope: four narrow-and-deep items (snapshot enrichment, forward-progress detector, peer-progress gate fix, status enrichment).
5. Null is not zero — any new progress field must be null when unmeasurable.
6. Session transcripts remain a real gap — agent_log_store captures at Job removal, not at event-pod EXIT.
7. Do not rebuild agent_log_store.py or the /health/alerts endpoint.

## Work Completed

### Task 1: Detector Starvation Audit
- Audited all 25 registered detection-plane detectors
- Found: 0 fully populated, 3 partially starved, 22 fully starved
- The `role=str(cid)` bug affects ALL detectors that key on agent role
- Audit saved to: `.egg-state/agent-outputs/coder/detector-audit-issue-3596.md`

### Task 2: Fix role=str(cid) defect
- Fixed in `snapshot_from_health_context` — now maps container IDs to agent roles via `AgentExecution.container_id` → `AgentExecution.role`
- Falls back to `str(cid)` when no mapping is found

### Task 3: Populate RunningAgent liveness fields
- `last_tool_call_age_s` — populated from ProgressStore (last ProgressEvent timestamp per agent)
- `last_heartbeat_age_s` — populated from HealthMonitor._last_heartbeat (only when singleton tracks the right pipeline)
- `exit_code`, `exit_reason` — populated from pipeline's AgentExecution
- All fields are null when unmeasurable, never 0

### Task 4: Populate git_state
- `agent_commit_counts` — per-agent commit counts via `git rev-list --count origin/<base>..<worktree_HEAD>`
- `agent_last_commit_age_s` — per-agent age of last commit (for forward-progress stall detection)
- `commit_count`, `last_commit_sha`, `last_commit_at`, `branch` — branch-level git state
- `fsck_errors`, `index_lock_present`, `lock_age_s` — worktree corruption detection
- Best-effort: git failures degrade to empty dict

### Task 5: Populate container_transitions
- Currently returns empty tuple — kubernetes_monitor doesn't track transition history
- Documented in code that this needs a future enhancement to track from→to transitions

### Task 6: Populate decision_state
- `pending_hitl`, `open_decisions` — from pipeline.decisions
- `approved_unapplied`, `oldest_open_age_s` — from resolved decisions
- `replay_pending`, `replay_count` — not tracked on pipeline model (set to False/0)

### Task 11: Populate phase_state.expected_duration_s + raw.runtime
- `phase_state.expected_duration_s` — populated from pipeline config or phase-specific defaults (refine=600s, plan=900s, apply=300s, implement=3600s)
- `raw.runtime.run_pipeline_thread_alive` — from driver_heartbeat.tick_age_seconds
- `raw.runtime.thread_last_tick_age_s` — from driver_heartbeat.tick_age_seconds
- `raw.runtime.spawn_age_s` — from driver_heartbeat.spawn_age_seconds

### Task 7: Forward-progress detector
- New tier-1 detector in `health_checks/tier1/forward_progress.py`
- Three firing modes: stall (last commit >600s ago), reset (commit count decreased), no-commits-at-completion
- **Stateless** design: reads `git_state.agent_last_commit_age_s`, `git_state.agent_commit_counts`, `git_state.agent_prev_commit_counts` from the snapshot
- Registered in `DetectionPlane.default()` and `health_checks/tier1/__init__.py`
- 3 calibration corpus fixtures added (stall__bad, stall__normal, reset__bad)

### Tests
- 15 unit tests for forward-progress detector (all pass)
- 15 unit tests for snapshot enrichment (all pass)
- 3 calibration corpus fixtures for forward-progress (all pass)
- All 115 existing detection plane + calibration tests still pass
- All 860 health check + detection tests pass
- mypy: no issues found
- ruff: all checks passed
- bandit: only low-severity issues (same patterns as existing code)

## Deferred Design Choices (to raise as contract decisions)
1. Commit counting scope: worktree branch only, or also track patch-id-equivalent across rebases?
2. Tool-call proxy: progress store vs. new working_heartbeat emitter?
3. Alert payload shape: structured evidence fields vs. free-form string with metadata?
4. Log TTL configurability: 24h sufficient or per-pipeline?

## Remaining Work (not yet started)
- Peer-progress gate fix — depends on understanding the current gate implementation in health_monitor.py
- Status endpoint enrichment — depends on understanding the /status route in routes/pipelines/__init__.py
- Container transitions tracking — kubernetes_monitor doesn't currently track transition history; needs enhancement to populate container_transitions for container death/restart loop detectors
- Deferred design choices (4 items) — to raise as contract decisions when reached
