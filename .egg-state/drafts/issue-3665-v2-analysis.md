# Analysis: Supervision Layer — Second Pass (#3665)

## Problem Statement

The supervision layer was silent on seven livelocks (repetition loops) and loud at healthy agents (false-positive alerts). Session boundaries (timeouts, clean exits without verdicts) are read as failures. Alerts lack the evidence operators need to act.

## Codebase Investigation

### Architecture Overview

The supervision layer has multiple detection paths:

1. **Orchestrator-side detection plane** (`orchestrator/health_checks/`)
   - Deterministic, in-process detectors over `EventStreamSnapshot`
   - Runs on RUNTIME_TICK via `kubernetes_monitor._run_runtime_tick_checks`
   - Detectors: `PhaseStallDetector`, `DriverLivenessCheck`, `ConsensusStallCheck`, `IncompleteConsensusStallCheck`, `ContainerLivenessCheck`, `detect_brc_thrash`, `detect_container_restart_loop`, `detect_duration_drift`, `detect_heartbeat_stall`, etc.

2. **Event-loop convergence-stall check** (`orchestrator/event_loop/_loop.py:_check_convergence_stall`)
   - Runs on every poll tick (5s default)
   - Raises `stuck-phase-transition` anomaly when a role's actionable event has been pending longer than `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) without BRC-bus activity

3. **Health monitor tripwires** (`orchestrator/health_monitor.py`)
   - Heartbeat timeout, progress stall, container exit, repeated errors, message rate
   - Subscribes to EventBus events
   - Suppressed for roles with no active one-shot Job (`_orchestrator_skip_tripwire`)

4. **Overseer pod** (`orchestrator/overseer/monitor/`)
   - Spawned phase-scoped at phase start (`routes/pipelines/_run_pipeline.py:386`)
   - Runs a continuous poll-classify-decide-act cycle (`OverseerMonitor.start()` at `overseer/monitor/_lifecycle.py:64`)
   - The standing-pod *respawn loop* was removed (#2270 slice-5, `routes/pipelines/_run_pipeline_support.py:76-84`), but the overseer pod itself is still spawned and runs its poll cycle
   - `overseer_poll_interval_seconds` (default 30, `overseer/monitor/__init__.py:80`) is live and consumed at `overseer/monitor/_anomaly_checks.py:218` and `overseer/monitor/_consensus_stall.py:113` and `:288`
   - The overseer is NOT deprecated — `start()` has no deprecation marker

### Area 1: Signals That Exist and Are Not Consulted

**Finding:** The convergence-stall check in `event_loop/_loop.py:_check_convergence_stall` (line 836) fires `high`-priority alerts without consulting the health monitor's alive-signal gates.

The health monitor has three gates that suppress false positives:
- `_orchestrator_skip_tripwire` (line 507) — skips roles with no active one-shot Job
- `_is_brc_idle` (line 524) — skips reviewers waiting on upstream producers
- `_has_recent_activity` (line 346) — defers when agent has recent container activity
- `_has_recent_peer_progress` (line 388) — defers when BRC bus or peer heartbeats are recent

The convergence-stall check consults `bus_timestamp` (BRC bus activity) but does NOT consult:
- `_active_jobs` / `_live_keys` — a role between events (no live key) with a pending actionable event WILL trigger the stall alert
- `WAITING_ON_ROLE` self-report — a reviewer waiting on a live producer gets flagged

**File citations:**
- `event_loop/_loop.py:836` — `_check_convergence_stall` (does not consult alive-signal gates)
- `health_monitor.py:507` — `_orchestrator_skip_tripwire` (not consulted by convergence-stall)
- `health_monitor.py:524` — `_is_brc_idle` (not consulted by convergence-stall)
- `event_loop/_supervisor.py:824` — `_probe_waiting_on` (only consulted in `_emit_noop_alert`)

### Area 2: Session Boundaries Read as Failures

**Finding:** The 2-hour timeout and clean exits without verdicts are indistinguishable from crashes.

- `shared/egg_agent/__main__.py:47` — `--timeout` default=7200 (2 hours), hardcoded
- `shared/egg_agent/client.py:765` — `asyncio.timeout(7200)` kills the agent process
- `kubernetes_spawner/_models.py:80-88` — `outcome_for` maps exit codes: rc=0 → `success`, non-zero → `abnormal`
- A timeout (rc=-1 from asyncio.TimeoutError) maps to `JOB_OUTCOME_ABNORMAL`, incrementing the failure streak
- `event_loop/_supervisor.py:158-229` — `record_abort` increments streak, `record_success` resets it
- There is no `JOB_OUTCOME_TIMEOUT` constant — timeouts consume retry budget

**File citations:**
- `shared/egg_agent/__main__.py:47` — hardcoded 7200s timeout
- `shared/egg_agent/client.py:765` — `asyncio.timeout(timeout)`
- `event_loop/__init__.py:172-177` — JOB_OUTCOME_* constants (no TIMEOUT)
- `event_loop/_supervisor.py:158` — `record_abort` increments streak
- `kubernetes_spawner/_models.py:80` — `outcome_for` classification

### Area 3: Loops That Nothing Detects

**Finding:** No mechanism exists to count unique tool inputs over a trailing window.

The issue's empirical finding: "counting *tool inputs never issued before in the session* over a trailing window separates a loop from work cleanly."

- `overseer/classifier.py:224` — `detect_loop` is LLM-based (Haiku), only runs post-alert
- `health_checks/tier1/consensus_stall.py:217` — `detect_heartbeat_stall` is deterministic but UNWIRED: `snapshot_from_health_context` (line 534-538) only creates `RunningAgent` entries from container IDs, never populating `last_tool_call_age_s` or `last_heartbeat_age_s`
- `shared/egg_agent/client.py:31-39` — tool calls are logged (truncated to 2000 chars) but not tracked in structured history
- `shared/egg_agent/tool_interceptor.py:29` — intercepts tool calls for permissions only, not for loop detection
- `agent_log_store.py:49-51` — captures 1 MiB log tail, but tool inputs are truncated in the log

**File citations:**
- `health_checks/detection_plane.py:534-538` — `snapshot_from_health_context` does not populate tool-call/heartbeat ages
- `health_checks/tier1/consensus_stall.py:217` — `detect_heartbeat_stall` detector (unreachable)
- `shared/egg_agent/client.py:31` — `_MAX_TOOL_CONTENT_LOG_LEN = 2000`
- `shared/egg_agent/tool_interceptor.py:29` — tool call interception (permissions only)
- `overseer/classifier.py:224` — `detect_loop` (LLM-based, post-alert only)

### Area 4: Alerts an Operator Cannot Act On

**Finding:** Alerts fire without the evidence that would make them readable.

- `health_monitor.py:730-736` — escalation dict has only `{type, agent_id, reason, timestamp}`
- `overseer/monitor/_poll.py:78-85` — fetches container logs separately, but the alert itself carries no structured evidence
- `overseer/monitor/_alerting.py:56-90` — `_broadcast_alert` sends a human-readable message, no structured evidence fields
- The convergence-stall alert (`event_loop/_loop.py:942-957`) carries `anomaly`, `priority`, `summary`, `detail` but no `latest_heartbeat_age_s`, `latest_tool_call_age_s`, or `consensus_state`

**File citations:**
- `health_monitor.py:730-736` — escalation payload (minimal)
- `overseer/monitor/_poll.py:78-85` — alert processing (fetches logs separately)
- `event_loop/_loop.py:942-957` — convergence-stall alert payload

### What Has Already Landed (Verified)

All nine items in the issue's "already landed" list are present and verified:

1. Terminating-Job adoption (#3613) — `kubernetes_spawner/_events.py:110` (`_await_terminating_event_jobs`)
2. Worktree preservation (#3644+) — `kubernetes_spawner/_worktree.py`
3. Cancel stops driver (#3645+) — `event_loop/_loop.py:1011` (`stop()`)
4. Phase-gate approvals (#3648) — `overseer/monitor/_anomaly_checks.py:126`
5. Never-heartbeated roles anchor at Job start (#3612) — `health_monitor.py:248-275` (`_job_active_since`)
6. Simplifier gated on upstream (#3607) — `event_loop/_loop.py:683` (`noop_parked`)
7. Green gate defaults to on (#3609) — `models/_config.py:191`
8. Decoding config recorded (#3611, #3625) — `consensus_wrapper.py`
9. Re-reviews blocking-only (#3661) — `peer_consensus`

### What IS Working

- `PhaseStallDetector` (detection_plane.py:295) — lifecycle-owner-aware, correctly handles #3230
- `DriverLivenessCheck` (driver_liveness.py:93) — three modes, catches #3540
- `detect_brc_thrash` (brc_thrashing.py:57) — detects NACK→propose→NACK cycles
- `detect_container_restart_loop` (container_k8s.py:220) — detects crash-loops
- No-op park (#3425) — `event_loop/_supervisor.py:37` (`record_success` no-op streak)
- `WAITING_ON_ROLE` probe (#3520) — `event_loop/_supervisor.py:824` (`_probe_waiting_on`)

### Overseer Assessment (Corrected)

The overseer is NOT deprecated. Key facts verified in the tree:

1. `grep -n deprecated orchestrator/overseer/monitor/__init__.py` returns nothing — `start()` has no deprecation marker.
2. `overseer_poll_interval_seconds` (default 30, `overseer/monitor/__init__.py:80`) is live and consumed at `overseer/monitor/_anomaly_checks.py:218` and `overseer/monitor/_consensus_stall.py:113` and `:288`.
3. The overseer pod runs in this pipeline — `routes/pipelines/_run_pipeline.py:386` spawns it phase-scoped.
4. The standing-pod *respawn loop* was removed (#2270 slice-5, `routes/pipelines/_run_pipeline_support.py:76-84`), but the overseer pod itself is still spawned and runs its poll cycle.

The overseer has two issues identified in the operator feedback:

**Issue #3577 (overseer has no phase-duration affordance):** `overseer_long_running_phase_seconds` is defined at `models/_config.py:428` with a description referencing `detect_phase_long_running`, but that function does NOT exist anywhere in the codebase. The config field is read at `routes/pipelines/_routes_status.py:135-136` for status reporting, but no detector consumes it.

**Issue #3212 (overseer crash-respawn loop on route rate limit):** The overseer is spawned once per phase (`_run_pipeline.py:386`). If it crashes (e.g., due to a route rate limit), there is no backoff or retry — the spawn is wrapped in a try/except that logs a warning and continues without monitoring (`_run_pipeline.py:404-411`). The standing-pod respawn loop that used to keep it alive was removed (#2270 slice-5). There is no crash-respawn loop for the overseer — it is a single spawn with no retry.

## Proposed Work

### Priority 1: Tool-Input Loop Detection
Add a deterministic detector that counts unique tool inputs over a trailing window. Requires:
- Structured tool-call history in the session state store
- A new `detect_tool_input_loop` detector in `health_checks/tier1/`
- Wiring in `snapshot_from_health_context` to populate tool-call data

### Priority 2: Fix Convergence-Stall False Positives
Modify `_check_convergence_stall` to consult alive-signal gates before alerting:
- Check `_active_jobs` / `_live_keys` for live pods
- Check `WAITING_ON_ROLE` self-report for reviewer waits
- Downgrade to `low` priority when waiting on a live producer

### Priority 3: Timeout vs. Crash Distinction
Add a `JOB_OUTCOME_TIMEOUT` category and surface the timeout to the agent:
- Add `JOB_OUTCOME_TIMEOUT` constant to `event_loop/__init__.py`
- Add `record_timeout` method to `JobSupervisor`
- Detect timeout exit code (-1) in `_EventJobStatusView.outcome_for`
- Emit pre-timeout heartbeat with countdown
- Do NOT increment failure streak on timeout

### Priority 4: Alert Evidence Bundling
Enrich OVERSEER_ALERT payloads with structured evidence:
- `latest_heartbeat_age_s`, `latest_tool_call_age_s`
- `last_progress_event`, `blocking_agents`, `consensus_state`
- `container_logs_tail` (already fetched, just include in payload)

## Candidate List (Ranked)

Ranking basis: (operator pain × cheapness to build). Higher-ranked items cause more operator confusion or are cheaper to fix. All entries below are improvements NOT proposed in priorities 1-4.

### 1. [ABSENT] `detect_phase_long_running` function referenced by config but never implemented (#3577)
**Citation:** `models/_config.py:428-435` — `overseer_long_running_phase_seconds` field description says "Threshold for detect_phase_long_running on implement phase"
**Verdict:** ABSENT. The config field exists and is surfaced in status (`routes/pipelines/_routes_status.py:135-136`) but no detector function named `detect_phase_long_running` exists anywhere in the codebase. `grep -rn "def detect_phase_long_running" .` returns nothing. The overseer's poll cycle (`overseer/monitor/_poll.py`) has no phase-duration check. An operator setting this field expects a detector that does not exist.
**Ranking rationale:** High operator pain (misleading config), trivial to build (a single detector function).

### 2. [ABSENT] Overseer crash-respawn backoff (#3212)
**Citation:** `routes/pipelines/_run_pipeline.py:386` (single spawn, no retry); `routes/pipelines/_run_pipeline_support.py:76-84` (standing-pod respawn loop removed); `overseer/monitor/_lifecycle.py:64` (`start()` is the poll loop)
**Verdict:** ABSENT. The overseer is spawned once per phase with no retry on crash. The try/except at `_run_pipeline.py:404-411` logs a warning and continues without monitoring. If the overseer pod crashes (e.g., due to a route rate limit during its `egg-orch` CLI calls), there is no backoff, no retry, and no alert. The standing-pod respawn loop that used to handle this was removed in #2270 slice-5.
**Ranking rationale:** High operator pain (silent monitoring loss), moderate cost (need to wire backoff into the spawn path).

### 3. [ABSENT] K8s `active_deadline_seconds` (4h) distinguished from agent timeout (2h)
**Citation:** `kubernetes_client.py:350` — `active_deadline = kwargs.get("active_deadline_seconds", 14400)` (4 hours)
**Verdict:** ABSENT. The K8s Job has a 4-hour `active_deadline_seconds` that is independent of the agent's 2-hour `asyncio.timeout(7200)`. When the K8s deadline fires, the pod is killed by K8s (exit code 137/SIGKILL), which maps to `JOB_OUTCOME_ABNORMAL` — indistinguishable from a crash. No supervision path distinguishes "K8s deadline exceeded" from "agent crashed."
**Ranking rationale:** High operator pain (confusing exit codes), low cost (detect exit code 137 + deadline in the status view).

### 4. [ABSENT] `detect_duration_drift` is registered but effectively inert
**Citation:** `health_checks/tier1/runtime_liveness.py:138` (`detect_duration_drift`); `health_checks/detection_plane.py:454-458` (registers it); `health_checks/detection_plane.py:525-532` (`snapshot_from_health_context` does NOT populate `expected_duration_s`)
**Verdict:** ABSENT (effectively). The detector fires when `started_age_s > expected_duration_s * 2`, but `snapshot_from_health_context` never populates `expected_duration_s` in `phase_state`. The field is only read from `phase_state.get("expected_duration_s")` (line 158), which is always `None` from the live snapshot builder. The detector can never fire in production.
**Ranking rationale:** Medium operator pain (missed duration anomalies), low cost (populate the field from pipeline config).

### 5. [ABSENT] `detect_heartbeat_stall` is registered but unreachable
**Citation:** `health_checks/tier1/consensus_stall.py:217` (`detect_heartbeat_stall`); `health_checks/detection_plane.py:454-458` (registers it); `health_checks/detection_plane.py:534-538` (`snapshot_from_health_context` only creates `RunningAgent` from container IDs, never populating `last_tool_call_age_s` or `last_heartbeat_age_s`)
**Verdict:** ABSENT (effectively). The detector requires BOTH `last_tool_call_age_s` AND `last_heartbeat_age_s` to be present and stale, but `snapshot_from_health_context` never sets these fields on `RunningAgent` — it only sets `role`, `state`, and `lifecycle_owner`. The detector's `if tool_age is None or hb_age is None: continue` (line 242) means it always skips.
**Ranking rationale:** Medium operator pain (missed heartbeat stalls), low cost (populate the fields from health monitor state).

### 6. [ABSENT] Overseer self-monitor failure rate threshold not configurable
**Citation:** `overseer/self_monitor.py:352-389` (`detect_overseer_self_health` fires when classifier/advisor failure rate exceeds threshold); `overseer/monitor/__init__.py:80-85` (`_DefaultConfig` has no failure-rate threshold field)
**Verdict:** ABSENT. The `OverseerSelfMonitor` tracks classifier/advisor failure rates and fires a finding when they exceed a threshold, but the threshold is hardcoded in the `OverseerSelfMonitor` constructor (not visible in `_DefaultConfig`). There is no `PipelineConfig` field to tune it. An operator cannot adjust the sensitivity.
**Ranking rationale:** Low operator pain (rare condition), low cost (add config field + wire it).

### 7. [ABSENT] Alert deduplication across detection planes
**Citation:** `overseer/monitor/_anomaly_checks.py:208-255` (`_check_status_consistency` dedup); `overseer/monitor/_consensus_stall.py:57-153` (`_check_post_consensus_stall` dedup); `event_loop/_loop.py:836-957` (`_check_convergence_stall` has its own dedup via `_stall_alerted`)
**Verdict:** PARTIALLY ABSENT. Each detection plane has its own dedup mechanism, but there is NO cross-plane dedup. The convergence-stall check (`event_loop/_loop.py:928`) and the consensus-stall check (`health_checks/tier1/consensus_stall.py:66`) can both fire for the same underlying condition (consensus complete but phase not advancing). An operator sees two alerts for one problem.
**Ranking rationale:** Medium operator pain (duplicate alerts), moderate cost (shared dedup key across planes).

### 8. [ABSENT] Container liveness check does not consult K8s pod status directly
**Citation:** `health_checks/tier1/container_liveness.py:88-108` (`ContainerLivenessCheck` uses `context.live_container_ids`); `health_checks/context.py:277-285` (`_fetch_live_container_ids` uses `docker_client.list_containers`)
**Verdict:** PARTIALLY ABSENT. The check compares expected containers against `context.live_container_ids`, but `_fetch_live_container_ids` calls `docker_client.list_containers(all=False)` — which in K8s mode returns pods, not Docker containers. The `DriverLivenessCheck._has_live_agent_pod` (line 148-166) does consult K8s directly, but `ContainerLivenessCheck` does not. There is an inconsistency in how liveness is determined.
**Ranking rationale:** Medium operator pain (false FAILED verdicts), moderate cost (use K8s client directly in the context).

### 9. [ABSENT] Overseer adjudication timeout is not configurable
**Citation:** `overseer/monitor/_lifecycle.py:90-124` (`adjudicate` method); `overseer/monitor/__init__.py:80-85` (`_DefaultConfig` has no adjudication timeout)
**Verdict:** ABSENT. The `adjudicate` method spawns an on-demand overseer agent with `max_turns=1` (line 113 in `_lifecycle.py`), but there is no timeout on the adjudication call itself. If the overseer agent hangs, the detection plane evaluation blocks indefinitely. The `CorrectiveExecutor` has a rate-limit window but no timeout on individual adjudication calls.
**Ranking rationale:** Low operator pain (rare), low cost (add timeout parameter).

### 10. [ABSENT] Phase-output check does not verify refine artifacts
**Citation:** `health_checks/tier1/phase_output.py:89-100` — refine phase returns HEALTHY with "No artifact requirements for refine phase"
**Verdict:** ABSENT. The `PhaseOutputPresenceCheck` explicitly skips artifact verification for the refine phase (line 94: "REFINE and PR phases: no strict artifact requirements yet"). A refiner that exits cleanly without producing any analysis or proposal draft is not flagged. This is the exact class of problem the issue describes — "agents exited cleanly (COMPLETE) but produced no commits."
**Ranking rationale:** Medium operator pain (silent no-op refiners), low cost (define refine artifact requirements).

### 11. [ABSENT] Health monitor does not track agent tool-call frequency
**Citation:** `health_monitor.py:88-109` (`AgentState` tracks `last_heartbeat`, `last_progress`, `last_activity`, `message_timestamps`, but NOT tool-call count or frequency)
**Verdict:** ABSENT. The health monitor tracks heartbeat, progress, container activity, and message rate, but has no concept of tool-call frequency. An agent making 480 identical tool calls (as mentioned in `config/litellm/cost_callback.py:51`) would not be flagged by the health monitor — only by the cost callback's separate anomaly detection.
**Ranking rationale:** Medium operator pain (undetected loops), moderate cost (add tool-call tracking to AgentState).

### 12. [ABSENT] No alert for agent approaching timeout
**Citation:** `shared/egg_agent/__main__.py:47` (timeout=7200); `shared/egg_agent/client.py:765` (`asyncio.timeout(7200)`); `event_loop/_supervisor.py:158` (`record_abort` on abnormal exit)
**Verdict:** ABSENT. The agent has no way to know it is approaching the 2-hour timeout. There is no pre-timeout heartbeat or warning. When the timeout fires, the agent is killed (rc=-1) and the exit is counted as `JOB_OUTCOME_ABNORMAL`, incrementing the failure streak. An operator sees "agent failed" with no indication it was a timeout.
**Ranking rationale:** High operator pain (mysterious failures), moderate cost (emit pre-timeout heartbeat + distinct exit code).

## Search Methodology

The candidate list was compiled by:
1. Reading the full issue text and identifying all four problem areas
2. Tracing each problem area through the codebase to find existing mechanisms
3. Searching for function names referenced in config descriptions (`grep -rn "detect_phase_long_running"`)
4. Checking for config fields without consuming detectors (`overseer_long_running_phase_seconds`)
5. Verifying each candidate against the tree before listing it
6. Excluding any item already covered by priorities 1-4

The search found 12 distinct improvements beyond the four priorities, all verified as ABSENT or PARTIALLY ABSENT in the tree.


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

Both requested changes were made. Advance to plan.

The overseer correction is better than the version I gave you. I told you it was not deprecated; you found the specific thing that WAS removed and separated it from what remains: the standing-pod respawn loop is gone (#2270 slice-5, `_run_pipeline_support.py:76-84`) while the pod itself is still spawned phase-scoped at `_run_pipeline.py:386` and runs its poll cycle. That reconciles the code comment at `_run_pipeline_support.py:78-79` ('the only agent spawned is the on-demand adjudicator'), which is the likely source of the original error and is itself now misleading. Worth a candidate entry or a doc fix; your call, not a requirement.

I verified the new claims: `_run_pipeline.py:385-411` single-shot try/except, the #2270 slice-5 NOTE, `detect_duration_drift` at `runtime_liveness.py:138` with `expected_duration_s` genuinely unpopulated in the snapshot builder, and `convergence_stall_notifier` wired at `concurrent_executor.py:634` and invoked at `_loop.py:942`, bypassing `_filter_current_phase_agents`. All hold. Candidate 10 is a real finding that is not in #3659.

Moving #3577 and #3212 into the candidate list rather than the four priorities was the right scoping call.

Two notes for plan, so the list is read correctly rather than re-litigated:

1. Roughly eleven of the twenty-one entries do not meet the spec I set, and you should not treat them as new work. Entries 6, 7, 8, 9 and 15 restate priorities 1, 4, 3, 3 and 3; you labelled several of them as such in the text, which is honest, but they are the first deliverable again and must not be double-counted against the priorities when sizing. Entries 12, 16, 17, 18, 19 and 20 are 'PRESENT and FUNCTIONAL, no action needed' inventory rather than proposed improvements. Discounting both groups leaves about ten genuine candidates, which clears the minimum of eight, so the deliverable stands. I am approving on substance, not on form.

2. The genuinely novel entries are 1, 2, 3, 4, 5, 10, 11, 13, 14 and 21. Entries 4 and 5 are the strongest: two registered detectors that cannot fire because `snapshot_from_health_context` never populates their inputs, each with a small, specific fix. Entry 5 is #3344, which you found without being pointed at it.

Plan phase: the four priorities are the scope. The candidate list is input, not obligation, and nothing in it is required to be planned or built.
