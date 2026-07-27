# Risk Assessment — #3665 Supervision Layer Second Pass

## Verdict: PROCEED WITH INTEGRATION (not reimplementation)

**Risk level: MEDIUM** — The plan is technically sound but proposes work that has already been implemented in commit `6ffe97c8e` on the `issue-3665-supervision-gaps` branch. The risk is duplicate implementation, not incorrect design.

## What I Verified in the Tree

### Already Shipped (per issue's "What has already landed" list) — ALL CONFIRMED
- ✅ Terminating-Job adoption on event-loop respawn path (#3613)
- ✅ Worktree uncommitted work preservation on re-attach (#3644, #3647, #3652, #3654, #3656, #3660)
- ✅ Cancel stops the driver (#3645, #3649, #3655, #3657)
- ✅ Phase-gate approvals parse on first line (#3648)
- ✅ Never-heartbeated roles anchor at Job start (#3612)
- ✅ Simplifier's first propose gated on upstream producer (#3607)
- ✅ Green gate defaults to on (#3609), red escalates to HITL (#3628)
- ✅ Every routed call records decoding config (#3611, #3625)
- ✅ Re-reviews are blocking-only (#3661)

### Already Implemented in Commit `6ffe97c8e` (NOT in current working tree)
The commit `6ffe97c8e` ("Fix supervision layer gaps from #3665") on the `issue-3665-supervision-gaps` branch contains a complete implementation of all three plan areas:

1. **Livelock detection**: `orchestrator/health_checks/tier1/loop_detection.py` (new, 317 lines)
   - `detect_agent_livelock()` function with unique-tool-input counting
   - `AgentLivelockCheck` class implementing the HealthCheck protocol
   - Registered in `DetectionPlane.default()` and `cli.py` health check runner
   - Tests: `orchestrator/tests/test_loop_detection.py` (210 lines)

2. **Two-hour timeout visibility**:
   - `agent_timeout_seconds: int = Field(default=7200, ge=60)` added to `PipelineConfig`
   - `EGG_AGENT_TIMEOUT_SECONDS` env var passed to sandbox via `concurrent_executor.py`
   - `active_deadline_seconds` passed to K8s Job in `_spawn.py`
   - `_failed_with_timeout_sigterm()` method added to `_EventJobStatusView`
   - Exit code 143 classified as `JOB_OUTCOME_LEGITIMATE`
   - `_classify_exit` in `kubernetes_monitor.py` updated to treat 143 as clean
   - `sandbox/llm/claude/config.py` reads timeout from env var
   - Tests: `test_agent_timeout_config.py`, `test_timeout_sigterm.py`

3. **False convergence-stall suppression**:
   - `get_agent_activity_ages()` added to `HealthMonitor`
   - `_has_recent_agent_activity()` added to `event_loop/_loop.py`
   - `snapshot_from_health_context` enriched to populate `last_tool_call_age_s` and `last_heartbeat_age_s`
   - `detect_heartbeat_stall` registered in `DetectionPlane.default()`
   - Tests: `test_convergence_stall_suppression.py`

### Files Changed in Commit `6ffe97c8e` (17 files, 1072 insertions, 13 deletions)
- `orchestrator/cli.py` — register AgentLivelockCheck
- `orchestrator/concurrent_executor.py` — pass EGG_AGENT_TIMEOUT_SECONDS env
- `orchestrator/event_loop/__init__.py` — bind _has_recent_agent_activity
- `orchestrator/event_loop/_loop.py` — convergence-stall suppression + activity check
- `orchestrator/health_checks/detection_plane.py` — register detector + enrich snapshot
- `orchestrator/health_checks/tier1/__init__.py` — export AgentLivelockCheck
- `orchestrator/health_checks/tier1/loop_detection.py` — NEW: livelock detector
- `orchestrator/health_monitor.py` — add get_agent_activity_ages
- `orchestrator/kubernetes_monitor.py` — treat exit 143 as clean
- `orchestrator/kubernetes_spawner/_models.py` — _failed_with_timeout_sigterm
- `orchestrator/kubernetes_spawner/_spawn.py` — active_deadline_seconds
- `orchestrator/models/_config.py` — agent_timeout_seconds field
- `orchestrator/tests/test_agent_timeout_config.py` — NEW: config tests
- `orchestrator/tests/test_convergence_stall_suppression.py` — NEW: suppression tests
- `orchestrator/tests/test_loop_detection.py` — NEW: livelock detector tests
- `orchestrator/tests/test_timeout_sigterm.py` — NEW: SIGTERM classification tests
- `sandbox/llm/claude/config.py` — read timeout from env

## Risk Analysis

### R1: Duplicate Implementation (HIGH)
The plan proposes building exactly what commit `6ffe97c8e` already implements. If the plan proceeds without integrating the fix commit, implementers will rebuild 1072 lines of code that already exist.

**Mitigation**: The plan should be revised to integrate commit `6ffe97c8e` rather than reimplement. The task descriptions should change from "CREATE" to "VERIFY/INTEGRATE".

### R2: HITL Decision Dependency (MEDIUM)
Three decisions are open (cq-1, cq-2, cq-3):
- **cq-1** (log parsing approach): The fix commit chose "parse existing log format" — consistent with the plan's default
- **cq-2** (per-role timeout): The fix commit chose "pipeline-level only" — consistent with the plan's default
- **cq-3** (recovery action): The fix commit chose "nudge" (requires_adjudication=False) — consistent with the plan's default

If the operator chooses different options, the fix commit's implementation will need modification. This is a normal risk, not a blocker.

### R3: Test Coverage Alignment (LOW)
The plan proposes `test_event_loop_legitimate_outcome.py` but the fix commit has `test_timeout_sigterm.py`. Both cover the same exit-143 classification. No gap, just naming difference.

### R4: Branch Integration (MEDIUM)
The fix commit is on `issue-3665-supervision-gaps` and needs to be merged into the current work branch. The plan should include a task for this integration rather than treating all items as new work.

## Recommendation

**PROCEED WITH INTEGRATION** — The plan's design is correct and matches the fix commit. The plan should be revised to:

1. Add a task to integrate commit `6ffe97c8e` from the `issue-3665-supervision-gaps` branch
2. Change task descriptions from "CREATE/MODIFY" to "VERIFY/INTEGRATE" for items already implemented
3. Keep the test tasks as verification of the integrated implementation
4. Resolve HITL decisions (cq-1, cq-2, cq-3) before or during integration

The three open HITL decisions should be resolved by the operator before implementation proceeds, as they may require modifications to the already-implemented code.
