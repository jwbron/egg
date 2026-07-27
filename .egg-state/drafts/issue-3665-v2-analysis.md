# Analysis: Supervision Layer — Second Pass (#3665)

## Problem Statement

The supervision layer was silent on seven livelocks (repetition loops) and loud at healthy agents (false-positive alerts). Session boundaries (timeouts, clean exits without verdicts) are read as failures. Alerts lack the evidence operators need to act.

## Codebase Investigation

### Architecture Overview

The supervision layer has two parallel detection paths:

1. **Orchestrator-side detection plane** (`orchestrator/health_checks/`)
   - Deterministic, in-process detectors over `EventStreamSnapshot`
   - Runs on RUNTIME_TICK via `kubernetes_monitor._run_runtime_tick_checks`
   - Replaces the standing-pod overseer (#2270 Option C)
   - Detectors: `PhaseStallDetector`, `DriverLivenessCheck`, `ConsensusStallCheck`, `IncompleteConsensusStallCheck`, `ContainerLivenessCheck`, `detect_brc_thrash`, `detect_container_restart_loop`, `detect_duration_drift`, `detect_heartbeat_stall`, etc.

2. **Event-loop convergence-stall check** (`orchestrator/event_loop/_loop.py:_check_convergence_stall`)
   - Runs on every poll tick (5s default)
   - Raises `stuck-phase-transition` anomaly when a role's actionable event has been pending longer than `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) without BRC-bus activity

3. **Health monitor tripwires** (`orchestrator/health_monitor.py`)
   - Heartbeat timeout, progress stall, container exit, repeated errors, message rate
   - Subscribes to EventBus events
   - Suppressed for roles with no active one-shot Job (`_orchestrator_skip_tripwire`)

4. **Overseer (deprecated standing-pod)** (`orchestrator/overseer/monitor/`)
   - `start()` is deprecated (#2270 slice-4)
   - `adjudicate()` is the on-demand replacement

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

## Candidate List

See the full refiner proposal at `.egg-state/agent-outputs/refiner/refiner-proposal-issue-3665.md` for the ranked candidate list with file-and-symbol citations.
