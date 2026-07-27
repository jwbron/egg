# Task Planner Proposal: Supervision Layer, Second Pass (#3665)

## Summary

The supervision layer was silent on seven livelocks and loud at healthy agents. Three root causes:
1. **No livelock detection** — agents in repetition loops (same tool call or short cycle repeated) were not detected by any in-product signal.
2. **Two-hour timeout invisibility** — agents killed at exactly 2 hours by `active_deadline_seconds` were counted as crashes against the fail-streak budget, with no visible timeout signal.
3. **False convergence-stall alerts** — busy agents with recent heartbeats/container activity were flagged as stalled because the event loop's convergence-stall check didn't consult the health monitor's per-agent activity data.

## What's Already Implemented (Verified in Tree)

### Already Shipped (per issue's "What has already landed" list)
- ✅ Terminating-Job adoption on event-loop respawn path (#3613)
- ✅ Worktree uncommitted work preservation on re-attach (#3644, #3647, #3652, #3654, #3656, #3660)
- ✅ Cancel stops the driver (#3645, #3649, #3655, #3657)
- ✅ Phase-gate approvals parse on first line (#3648)
- ✅ Never-heartbeated roles anchor at Job start (#3612)
- ✅ Simplifier's first propose gated on upstream producer (#3607)
- ✅ Green gate defaults to on (#3609), red escalates to HITL (#3628)
- ✅ Every routed call records decoding config (#3611, #3625)
- ✅ Re-reviews are blocking-only (#3661)

### Already Present in the Tree (Not to be Rebuilt)
- **`JOB_OUTCOME_LEGITIMATE`** constant exists in `orchestrator/event_loop/__init__.py:174` — the event loop already handles it in `_loop.py:92-95` via `supervisor.record_legitimate_outcome()`.
- **`detect_heartbeat_stall`** detector exists in `orchestrator/health_checks/tier1/consensus_stall.py:217` — but is **NOT registered** in `DetectionPlane.default()` and its required inputs (`last_tool_call_age_s`, `last_heartbeat_age_s`) are **never populated** by `snapshot_from_health_context`.
- **`agent_log_store`** module exists (`orchestrator/agent_log_store.py`) — captures pod logs before reaping, with Redis-backed storage and 24h TTL.
- **`get_agent_activity_ages`** method does NOT exist on HealthMonitor — needs to be added.
- **`_has_recent_agent_activity`** method does NOT exist on the event loop — needs to be added.
- **`agent_timeout_seconds`** config field does NOT exist on PipelineConfig — needs to be added.
- **`EGG_AGENT_TIMEOUT_SECONDS`** env var is NOT passed to the sandbox — needs to be wired.
- **`active_deadline_seconds`** is hardcoded to 14400 (4h) in `kubernetes_client.py:350` — needs to be configurable.
- **Exit code 143 (SIGTERM)** is only treated as clean during phase transition (`kubernetes_monitor.py:532`) — needs to also be clean during RUNNING phase (sandbox timeout).
- **`_failed_with_timeout_sigterm`** method does NOT exist on `_EventJobStatusView` — needs to be added.
- **`AgentLivelockCheck`** class does NOT exist — needs to be created.
- **`detect_agent_livelock`** function does NOT exist — needs to be created.

## Proposed Work

### Task 1: Agent Livelock / Repetition-Loop Detector (task-1-1)
**File:** `orchestrator/health_checks/tier1/loop_detection.py` (new)

Create a deterministic detector that analyzes agent log transcripts for zero new unique tool inputs over a trailing window. The empirical finding from the incident analysis: counting *unique tool inputs never issued before in the session* over a trailing window separates a loop from work cleanly — a working agent produces new ones, a loop of any length produces none.

Key design decisions:
- Read logs from `agent_log_store` (captures full pod stdout before reaping, unlike truncated pod logs).
- Parse Claude Code's `"> tool_name args"` emission lines to extract tool-call signatures.
- Track unique signatures per agent; fire when the unique ratio drops below 10% with ≥10 total calls.
- `requires_adjudication=False` — deterministic, no LLM needed.
- Register in `DetectionPlane.default()` via `_register_coverage_gap_detectors`.
- Export `AgentLivelockCheck` class (Tier 1 HealthCheck wrapper) and `detect_agent_livelock` function from `tier1/__init__.py`.
- Register in `cli.py` health check runner.

**Files to create/modify:**
- CREATE: `orchestrator/health_checks/tier1/loop_detection.py`
- MODIFY: `orchestrator/health_checks/detection_plane.py` (register detector)
- MODIFY: `orchestrator/health_checks/tier1/__init__.py` (export)
- MODIFY: `orchestrator/cli.py` (register check)

### Task 2: Two-Hour Timeout Visibility (task-1-2)
**Files:** `orchestrator/models/_config.py`, `orchestrator/kubernetes_spawner/_spawn.py`, `orchestrator/kubernetes_spawner/_models.py`, `orchestrator/kubernetes_monitor.py`, `orchestrator/concurrent_executor.py`

Make the 2-hour agent timeout visible and non-fatal:
1. Add `agent_timeout_seconds: int = Field(default=7200, ge=60)` to `PipelineConfig`.
2. Pass `EGG_AGENT_TIMEOUT_SECONDS` env to the sandbox in `concurrent_executor.py`.
3. Pass `active_deadline_seconds` from the env to the K8s Job spec in `_spawn.py`.
4. Classify exit code 143 (SIGTERM) as `JOB_OUTCOME_LEGITIMATE` in `_EventJobStatusView.outcome_for()` via a new `_failed_with_timeout_sigterm` method.
5. Update `_classify_exit` in `kubernetes_monitor.py` to treat 143 as clean during RUNNING phase (not just phase transitions).

**Files to modify:**
- MODIFY: `orchestrator/models/_config.py` (add config field)
- MODIFY: `orchestrator/kubernetes_spawner/_spawn.py` (pass active_deadline_seconds)
- MODIFY: `orchestrator/kubernetes_spawner/_models.py` (add _failed_with_timeout_sigterm, use JOB_OUTCOME_LEGITIMATE)
- MODIFY: `orchestrator/kubernetes_monitor.py` (update _classify_exit)
- MODIFY: `orchestrator/concurrent_executor.py` (pass EGG_AGENT_TIMEOUT_SECONDS env)

### Task 3: False Convergence-Stall Suppression (task-1-3)
**Files:** `orchestrator/health_monitor.py`, `orchestrator/event_loop/_loop.py`, `orchestrator/health_checks/detection_plane.py`

Suppress false convergence-stall alerts against agents with recent activity:
1. Add `get_agent_activity_ages()` to `HealthMonitor` — returns per-agent heartbeat/progress/activity ages.
2. Add `_has_recent_agent_activity()` to the event loop's convergence-stall check — consults the health monitor before firing.
3. Enrich `snapshot_from_health_context` to populate `last_tool_call_age_s` and `last_heartbeat_age_s` on `RunningAgent` entries from the health monitor, enabling the existing `detect_heartbeat_stall` detector to fire in the live path.

**Files to modify:**
- MODIFY: `orchestrator/health_monitor.py` (add get_agent_activity_ages)
- MODIFY: `orchestrator/event_loop/_loop.py` (add activity check to convergence-stall)
- MODIFY: `orchestrator/health_checks/detection_plane.py` (enrich snapshot builder)

### Task 4: Tests (task-1-4)
**Files:** `orchestrator/tests/`

Add test coverage for all three changes:
- `test_loop_detection.py` — unit tests for `detect_agent_livelock` with known-normal and known-bad fixtures.
- `test_event_loop_legitimate_outcome.py` — verify exit 143 is classified as legitimate, not abnormal.
- `test_convergence_stall_suppression.py` — verify activity-based suppression of false alerts.
- Add corpus rows to `fixtures.json` for the livelock detector.

## Candidate List (Deliverable — Not Obligated)

| # | Improvement | File/Symbol | Present? |
|---|------------|-------------|----------|
| 1 | **Tool-call signature tracking in pod logs** — The livelock detector parses `"> tool_name args"` lines, but Claude Code's log format may vary. A structured tool-call event emitter (like the existing `tool_use` logger in `client.py:808`) could write to a dedicated stream that's more reliable to parse. | `shared/egg_agent/client.py:808` (tool_use logging) | Partially present (logs exist, not structured for parsing) |
| 2 | **Per-agent timeout configuration** — Currently `agent_timeout_seconds` is a pipeline-level config. Different roles (coder vs. overseer) may need different timeouts. Consider role-scoped overrides. | `orchestrator/models/_config.py` | Absent |
| 3 | **Timeout warning emission** — The sandbox should emit a warning heartbeat at 90% of the timeout so the health monitor can surface "agent approaching timeout" before the SIGTERM fires. | `shared/egg_agent/client.py` (timeout handling) | Absent |
| 4 | **Livelock recovery action** — The detector fires `requires_adjudication=False`, so the bounded corrective vocabulary (slice-6) handles it. But what corrective action is available? A "nudge agent with loop description" or "respawn agent" action needs to be defined in the corrective executor. | `orchestrator/health_checks/runner.py` (corrective actions) | Absent |
| 5 | **Exit-code 143 provenance annotation** — When a 143 is classified as legitimate, the `exit_detail` should distinguish "sandbox timeout" from "orchestrator teardown SIGTERM" so operators can tell which path fired. | `orchestrator/kubernetes_spawner/_models.py:exit_detail_for` | Partially present (annotated as "likely sandbox timeout or orchestrator teardown") |
| 6 | **Heartbeat-stall detector registration** — `detect_heartbeat_stall` exists in `consensus_stall.py` but is not registered in `DetectionPlane.default()`. The snapshot enrichment (task 3) makes it usable, but it still needs explicit registration. | `orchestrator/health_checks/detection_plane.py:_register_coverage_gap_detectors` | Absent (detector exists, not registered) |
| 7 | **Agent log retention policy** — `agent_log_store` uses a 24h TTL. For long-running pipelines, logs from early agents may expire before incident response. Consider a pipeline-level retention override. | `orchestrator/agent_log_store.py:AGENT_LOG_TTL_SECONDS` | Present (24h default, no override) |
| 8 | **Livelock window tunability** — The 300s window and 10% unique-ratio threshold are hardcoded. These should be config-driven for different pipeline types. | `orchestrator/health_checks/tier1/loop_detection.py` (new) | Will be configurable via constructor params |
| 9 | **Convergence-stall suppression for reviewers** — The `_has_recent_agent_activity` check applies to all roles. Reviewers legitimately wait on producers; their activity pattern differs. Consider role-specific suppression logic. | `orchestrator/event_loop/_loop.py:_check_convergence_stall` | Absent (uniform suppression) |
| 10 | **Two-hour timeout config validation** — `agent_timeout_seconds` defaults to 7200 but the K8s `active_deadline_seconds` default is 14400. If an operator sets a custom timeout, both must stay in sync. Consider a validation that warns when they diverge. | `orchestrator/kubernetes_client.py:350` (hardcoded 14400) | Absent |
