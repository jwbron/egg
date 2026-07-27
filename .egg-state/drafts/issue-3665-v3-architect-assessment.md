# Architect Assessment — Issue #3665 Plan

## Verdict: ACK

The task_planner's plan (commit 6092b5a7a, v1) is architecturally sound. All technical
claims verified against the live tree at `main @ 1cd0c8ad7`.

## Verified findings

### 1. Detection plane is fully built but unwired (CONFIRMED)

- `snapshot_from_health_context()` at `detection_plane.py:511` populates only 5 of 13
  `EventStreamSnapshot` fields: `snapshot_id`, `pipeline_id`, `phase`, `running_agents`,
  `phase_state`. The other 8 (`consensus`, `container_transitions`, `gateway_error_counters`,
  `cost_counters`, `midturn_messages`, `git_state`, `raw`) are left empty.
- `RunningAgent(role=str(cid))` at line 536 uses the container ID as the role — a bug.
  The `role` field should carry the agent role (e.g. "coder", "reviewer_plan"), not the
  container ID.
- `_run_overseer_detection_plane()` at `routes/pipelines/_overseer.py:309` has zero call
  sites. The import at `__init__.py:1277` is an import, not a call.
- `HealthCheckRunner.run_detection_plane()` at `runner.py:159` exists but is never invoked.
- The `ConsensusStallCheck` class (registered, runs every tick) and the bare
  `detect_heartbeat_stall()` function (unregistered, never called) coexist in the same
  file (`consensus_stall.py:217`). This is the precise trap that produced operator errors
  in earlier runs — two layers in one file, opposite fates.

### 2. Timeout classification bug (CONFIRMED)

- `ClaudeConfig.timeout = 7200` at `sandbox/llm/claude/config.py:23` — hardcoded, no
  `PipelineConfig.agent_timeout_seconds` field exists.
- `asyncio.timeout(timeout)` at `client.py:765`; `TimeoutError` handler at lines 903-921
  returns `returncode=-1`.
- `_classify_exit()` at `kubernetes_monitor.py:1148` treats exit code -1 as FAILED
  (only 0 and 143 are clean). No `JOB_OUTCOME_TIMEOUT` constant exists.
- `record_abort()` at `_supervisor.py:145` increments the failure streak — a timeout-killed
  pod consumes a retry budget it should not.

### 3. Loop detection gap (CONFIRMED)

- `detect_tool_input_loop()` does NOT exist — no `loop_detection.py` file in `tier1/`.
- `detect_loop()` and `classify_activity_pattern()` in `overseer/classifier.py:224/298`
  use LLM (Haiku) calls — expensive, not for the hot path.
- `midturn_messages` field at `detection_plane.py:126` is never populated.
- `read_job_log_snapshot()` at `kubernetes_client.py:455` truncates at ~100 chars per line.

### 4. Convergence-stall false positive (CONFIRMED)

- `_check_convergence_stall()` at `event_loop/_loop.py:859` uses
  `tracker.get_latest_progress_timestamp()` with the convergence stall budget.
- `_has_recent_peer_progress()` at `health_monitor.py:388` uses the same
  `tracker.get_latest_progress_timestamp()` but with `orchestrator_alert_progress_gate_seconds`
  (300s, defined at `models/_config.py:336`).
- Both use the same timestamp source but different gate windows — the false positive
  described in the issue is real: a coder with recent peer heartbeats gets flagged by
  the convergence-stall alert while the health monitor concurrently logs the agent as alive.

### 5. Linear slice structure is correct (CONFIRMED)

Slices share overlapping files (`kubernetes_monitor.py`, `detection_plane.py`,
`health_monitor.py`), so they must be a linear chain per #3046. The dependency ordering
(slice-1 → slice-2 → slice-3 → slice-4 → slice-5) is correct.

### 6. Double-evaluation concern is valid (CONFIRMED)

`_run_runtime_tick_checks()` is called from both `_check_pod` (line 219, on container
state changes) and `_reconciliation_sweep` (line 621, on periodic interval). The plan
correctly identifies the need for a double-evaluation guard.

## Architectural concerns and refinements

### Concern 1: Slice-2 TASK-2-1 — hot-loop wiring risk

The plan correctly flags slice-2 as high-risk. The `_run_runtime_tick_checks()` method
currently runs `HealthCheckRunner.run()` (the tiered checks), not the detection plane.
Wiring `_run_overseer_detection_plane()` into this path adds a new code path on every
runtime tick. The plan's risk note about exception isolation is critical — the detection
plane's `evaluate()` method is already exception-isolated per-detector, but the snapshot
building and finding routing must also be guarded.

**Recommendation:** The double-evaluation guard should be a simple per-pipeline, per-trigger
dedupe key (e.g. `sha256(pipeline_id, "detection_plane", tick_epoch)`) stored in the
`KubernetesMonitor` instance, not a global lock. This avoids cross-pipeline contention.

### Concern 2: Slice-1 TASK-1-5 — midturn_messages population

The plan notes that `midturn_messages` population depends on parsing agent logs, which are
truncated at ~100 chars in the k8s log API. The plan correctly sequences TASK-3-2 (log
fidelity) to address this. However, the `agent_log_store` only captures logs at pod removal
(`MAX_LOG_BYTES=1MB` at `agent_log_store.py:51`). For live pods, the loop detector will
need access to the pod's log stream.

**Recommendation:** Consider whether the loop detector should read from the agent's
`midturn_messages` field on the BRC tracker (if it exists) rather than parsing logs.
The BRC tracker already records tool calls as they happen — if that data is available
in the snapshot context, it would be more reliable than log parsing.

### Concern 3: Slice-4 TASK-4-3 — timeout classification via error message

The plan proposes detecting timeouts via the agent result's error message
("Timed out after {timeout} seconds"). This is fragile — the message format could change.
A more robust approach would be to check the pod's `active_deadline_seconds` or the
agent's own timeout configuration.

**Recommendation:** The error message check is acceptable as a first pass, but consider
also checking `container_info` for timeout-related annotations or the pod's
`active_deadline_seconds` field. The `agent_result` object (if available in the
reconciliation context) should carry a structured timeout flag.

### Concern 4: Slice-5 TASK-5-2 — timestamp unification

The plan correctly identifies that both `_check_convergence_stall()` and
`_has_recent_peer_progress()` use `get_latest_progress_timestamp()` but with different
gate windows. The fix should unify the gate window, not the timestamp source. The
timestamp source is already unified — the divergence is in the gate window
(`orchestrator_alert_progress_gate_seconds` vs the convergence stall budget).

**Recommendation:** The fix should make `_check_convergence_stall()` respect
`orchestrator_alert_progress_gate_seconds` as a deferral window, similar to how
`_has_recent_peer_progress()` does. This is a smaller change than unifying timestamp
sources.

## Scope assessment

The plan correctly limits scope to Tier 1 and Tier 2 items from the 30-item candidate
list. Tier 3-5 are properly excluded as "input to the gate, not a work queue." The
non-goals (don't rebuild overseer, don't remove HealthMonitor tripwires, don't add LLM
to hot path, don't change timeout default) are all correct.

## Risk assessment

The risk assessment is accurate:
- Slice-2 is correctly flagged as high-risk (hot-loop path).
- The double-evaluation guard is critical.
- The timeout classification must distinguish timeout-killed pods from real crashes.
- The midturn_messages population depends on log parsing fidelity.

## Conclusion

The plan is well-structured, properly scoped, and technically accurate. All grounded
anchors were verified against the live tree. The architectural concerns above are
refinements, not blockers. ACK.
