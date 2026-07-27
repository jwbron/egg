# BRC Memory — Refiner for Issue #3665

## Role: refiner
## Phase: refine
## Pipeline: issue-3665-v2

## Summary of Assessment

The supervision layer has two parallel detection paths:

1. **Orchestrator-side detection plane** (`health_checks/`) — deterministic, in-process detectors over an `EventStreamSnapshot`. Runs on RUNTIME_TICK via `kubernetes_monitor._run_runtime_tick_checks`. This is the #2270 Option C replacement for the standing-pod overseer.

2. **Event-loop convergence-stall check** (`event_loop/_loop.py:_check_convergence_stall`) — runs on every poll tick, raises `stuck-phase-transition` anomaly when a role's actionable event has been pending longer than `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) without BRC-bus activity.

3. **Health monitor tripwires** (`health_monitor.py`) — heartbeat timeout, progress stall, container exit, repeated errors, message rate spike. Subscribes to EventBus events. Suppressed for roles with no active one-shot Job (`_orchestrator_skip_tripwire`).

4. **Overseer (deprecated standing-pod)** (`overseer/monitor/`) — still exists but `start()` is deprecated (#2270 slice-4). The on-demand `adjudicate()` method is the replacement.

## Key Findings

### Loop Detection (Area 3 — "Loops that nothing detects")
- **ABSENT**: No mechanism counts unique tool inputs over a trailing window. The issue's empirical finding is not implemented.
- `overseer/classifier.py:detect_loop` exists but is LLM-based and only runs post-alert.
- `health_checks/tier1/consensus_stall.py:detect_heartbeat_stall` exists but is UNWIRED — `snapshot_from_health_context` never populates `last_tool_call_age_s` / `last_heartbeat_age_s` on `RunningAgent`.
- `shared/egg_agent/client.py` truncates tool-call logs to 2000 chars but does not track unique inputs.

### False Positives (Area 1 + Area 4 — "Signals that exist and are not consulted" / "Alerts an operator cannot act on")
- **BUG**: `event_loop/_loop.py:_check_convergence_stall` fires `high`-priority alerts WITHOUT consulting:
  - `_active_jobs` / `_live_keys` (podless-between-events false positive)
  - `WAITING_ON_ROLE` self-report (reviewer-waiting-on-producer false positive)
- The health monitor's alive-signal gates (`_has_recent_activity`, `_has_recent_peer_progress`, `_is_brc_idle`) are NOT consulted by the convergence-stall check.
- Alert payloads lack structured evidence (heartbeat age, tool-call age, consensus state, container logs).

### Session Boundaries (Area 2 — "Session boundaries read as failures")
- **ABSENT**: 2-hour timeout (`shared/egg_agent/__main__.py:47`, default=7200) is invisible to the agent.
- Timeout exit (rc=-1) maps to `JOB_OUTCOME_ABNORMAL`, incrementing the failure streak and consuming retry budget.
- No `JOB_OUTCOME_TIMEOUT` category exists.
- Clean exits without verdicts are indistinguishable from crashes.

### What IS Working
- `PhaseStallDetector` (detection_plane.py:295) — correctly handles #3230 false-stall with lifecycle-owner awareness.
- `DriverLivenessCheck` (driver_liveness.py:93) — three modes (dead/hung/no-progress), catches #3540.
- `detect_brc_thrash` (brc_thrashing.py:57) — detects NACK→propose→NACK cycles.
- `detect_container_restart_loop` (container_k8s.py:220) — detects crash-loops.
- No-op park mechanism (#3425) — handles "declared no-op" correctly.
- `WAITING_ON_ROLE` probe (#3520) — exists but only consulted in `_emit_noop_alert`.

## Proposed Work (4 priorities)

1. **Tool-input loop detection** — Structured tool-call history + deterministic detector
2. **Fix convergence-stall false positives** — Consult alive-signal gates before alerting
3. **Timeout vs. crash distinction** — New `JOB_OUTCOME_TIMEOUT`, agent-visible warning
4. **Alert evidence bundling** — Enrich OVERSEER_ALERT payloads with structured evidence

## Files Checked

- `orchestrator/health_checks/` — detection plane, tier1 detectors, context, runner, types
- `orchestrator/event_loop/` — _loop.py, _supervisor.py, __init__.py
- `orchestrator/health_monitor.py` — tripwire checks, alive-signal gates
- `orchestrator/overseer/` — monitor package, classifier, corrective, decision_maker
- `orchestrator/consensus_wrapper.py` — event-pump template, idle budget
- `orchestrator/kubernetes_monitor.py` — RUNTIME_TICK checks, driver liveness
- `orchestrator/kubernetes_spawner/` — job spawning, status view, events
- `orchestrator/kubernetes_client.py` — create_container (active_deadline=14400)
- `orchestrator/models/_config.py` — PipelineConfig, consensus timeout
- `orchestrator/routes/pipelines/_run_concurrent.py` — consensus timeout handling
- `orchestrator/routes/pipelines/_alerts.py` — _check_brc_progress_gate
- `shared/egg_agent/__main__.py` — --timeout default=7200
- `shared/egg_agent/client.py` — run_agent_async, asyncio.timeout(7200)
- `shared/egg_agent/tool_interceptor.py` — tool call interception (permissions only)
- `orchestrator/agent_log_store.py` — pod log capture (Redis, 1MiB tail)
