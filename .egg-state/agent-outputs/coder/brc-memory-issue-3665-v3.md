# BRC Memory — coder (issue #3665, slice-1)

## Current Proposal

**Commit:** c2131679a (v3)
**Phase:** implement (slice-1)
**Status:** PROPOSED (version 3) — waiting on reviewer re-review

## Summary of Assessment

Implemented all 5 slices of the #3665 supervision second pass:

### Slice 1 — Populate EventStreamSnapshot fields (TASK-1-1 to TASK-1-6)
- Added `runtime` field to `EventStreamSnapshot` dataclass
- `snapshot_from_health_context()` now populates:
  - `midturn_messages`: parsed from agent_log_store captured logs (JSON-structured tool call records with SHA-256 input hashes, plus fallback regex parser)
  - `runtime`: wired from `driver_heartbeat.tick_age_seconds()` / `spawn_age_seconds()` with correct field names (`thread_last_tick_age_s`, `run_pipeline_thread_alive`)
  - `raw["runtime"]`: also populated so the `_runtime()` helper in runtime_liveness.py can read it
  - `consensus`: wired from `peer_consensus.get_peer_consensus_tracker().evaluate()` with augmented fields (`nack_cycles`, `late_confirmed_then_renack`, `incomplete_consensus_deferrals`, `deferral_cap`)
  - `container_transitions`: wired from `kubernetes_monitor._pod_states` with correct format (`to`, `reason`, `transient`, `container`, `restart_count`, `to_state`, `recovered`)
  - `RunningAgent.role`: uses agent role from pipeline state, not container ID; `last_tool_call_age_s` / `last_heartbeat_age_s` populated from HealthMonitor
- The 4 Tier 3-4 fields remain empty by decision
- All field reads are lock-protected (health_monitor._lock, monitor._lock)

### Slice 2 — Wire detection plane into RUNTIME_TICK (TASK-2-1 to TASK-2-4)
- Wired `_run_detection_plane_for_pipeline()` into `_run_runtime_tick_checks()`
- Double-evaluation guard (per-pipeline timestamp tracking, under self._lock)
- Consensus-stall double-fire guard: `detect_heartbeat_stall` suppressed when `ConsensusStallCheck` reports DEGRADED
- Findings routed to OVERSEER_ALERT surface via `_broadcast_detection_finding()`
- Registered `detect_heartbeat_stall` and `detect_tool_input_loop` in the detection plane

### Slice 3 — Deterministic loop detector (TASK-3-1 to TASK-3-3)
- Implemented `detect_tool_input_loop()` in `health_checks/tier1/loop_detection.py`
- Counts tool inputs never issued before in the session over a trailing window (default 3 polls)
- Not keyed on cycle shape — handles 1-, 2-, 3-, 8-cycles
- Hashes the full `(tool_name, input)` pair
- **Polling supervisor exemption**: exempts roles not in `consensus.blocking_agents` (e.g. the overseer)
- Thread-safe `ToolInputLoopTracker` with `threading.Lock`
- Increased log capture fidelity: `get_pod_logs()` now accepts `limit_bytes` parameter

### Slice 4 — Timeout visibility and classification (TASK-4-1 to TASK-4-5)
- Added `agent_timeout_seconds` to `PipelineConfig` (default 7200, ge=60)
- Pass `EGG_AGENT_TIMEOUT` env through the spawner
- Added `JOB_OUTCOME_TIMEOUT` constant
- `_classify_exit_with_context()` detects timeout-killed pods (exit -1 + "Timed out after" in logs)
- `record_timeout()` leaves the abnormal streak untouched
- `_send_timeout_warnings()` emits HEARTBEAT at 90-minute intervals (under self._lock)

### Slice 5 — Alert evidence + false-positive fixes (TASK-5-1 to TASK-5-4)
- `_broadcast_alert()` now accepts and includes structured `evidence` in the alert body
- `_check_convergence_stall()` now also checks peer heartbeats via `_get_latest_heartbeat_age()`
- `exit_detail_for()` returns "killed by 2h agent timeout" for timeout-killed pods

## NACKs Addressed

### reviewer_security (v1 → resolved in v2)
1. ✅ Populate raw['runtime'] with correct field names (thread_last_tick_age_s, run_pipeline_thread_alive)
2. ✅ Fix container_transitions format to match container_k8s.py detectors
3. ✅ Augment consensus section with nack_cycles, late_confirmed_then_renack, etc.
4. ✅ Add fallback regex parser for midturn_messages
5. ✅ Re-added slice-1 tests (TestMidturnMessages, TestRuntimeSection, etc.)
6. ✅ Calibration corpus field compatibility (runtime is a new field, corpus uses raw)

### reviewer_concurrency (v1 → resolved in v2, v2 → resolved in v3)
1. ✅ TOCTOU race on `_detection_plane_last_tick` — now under self._lock
2. ✅ `ToolInputLoopTracker` lock added
3. ✅ `_build_running_agents` — health_monitor._lock now covers attribute reads (v3 fix)
4. ✅ `_build_container_transitions` — monitor._lock acquired
5. ✅ `peer_consensus.evaluate()` verified to already acquire RLock at _queries.py:137

## Tests

67 new tests across 4 test files + 12 existing tests:
- `test_detection_plane.py` — 12 existing tests (all pass)
- `test_detection_plane_wiring.py` — 23 tests (slice-1 + slice-2)
- `test_loop_detection.py` — 15 tests (slice-3)
- `test_timeout_classification.py` — 21 tests (slice-4)
- `test_alert_evidence.py` — 11 tests (slice-5)
- Plus all existing event_loop, kubernetes_monitor, models, health_check tests

All 530 tests pass.

## enrichement_sha
c2131679a
