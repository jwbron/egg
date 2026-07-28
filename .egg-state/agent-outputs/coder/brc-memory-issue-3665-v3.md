# BRC Memory — coder (issue #3665, slice-1)

## Current Proposal

**Commit:** f0d766673
**Phase:** implement (slice-1)
**Status:** PROPOSED (version 1)

## Summary of Assessment

Implemented all 5 slices of the #3665 supervision second pass:

### Slice 1 — Populate EventStreamSnapshot fields (TASK-1-1 to TASK-1-6)
- Added `runtime` field to `EventStreamSnapshot` dataclass
- `snapshot_from_health_context()` now populates:
  - `midturn_messages`: parsed from agent_log_store captured logs (JSON-structured tool call records with SHA-256 input hashes)
  - `runtime`: wired from `driver_heartbeat.tick_age_seconds()` / `spawn_age_seconds()`
  - `consensus`: wired from `peer_consensus.get_peer_consensus_tracker().evaluate()`
  - `container_transitions`: wired from `kubernetes_monitor._pod_states`
  - `RunningAgent.role`: uses agent role from pipeline state, not container ID; `last_tool_call_age_s` / `last_heartbeat_age_s` populated from HealthMonitor
- The 4 Tier 3-4 fields remain empty by decision

### Slice 2 — Wire detection plane into RUNTIME_TICK (TASK-2-1 to TASK-2-4)
- Wired `_run_detection_plane_for_pipeline()` into `_run_runtime_tick_checks()`
- Double-evaluation guard (per-pipeline timestamp tracking)
- Consensus-stall double-fire guard: `detect_heartbeat_stall` suppressed when `ConsensusStallCheck` reports DEGRADED
- Findings routed to OVERSEER_ALERT surface via `_broadcast_detection_finding()`
- Registered `detect_heartbeat_stall` in the detection plane

### Slice 3 — Deterministic loop detector (TASK-3-1 to TASK-3-3)
- Implemented `detect_tool_input_loop()` in `health_checks/tier1/loop_detection.py`
- Counts tool inputs never issued before in the session over a trailing window (default 3 polls)
- Not keyed on cycle shape — handles 1-, 2-, 3-, 8-cycles
- Hashes the full `(tool_name, input)` pair
- **Polling supervisor exemption**: exempts roles not in `consensus.blocking_agents` (e.g. the overseer)
- Increased log capture fidelity: `get_pod_logs()` now accepts `limit_bytes` parameter

### Slice 4 — Timeout visibility and classification (TASK-4-1 to TASK-4-5)
- Added `agent_timeout_seconds` to `PipelineConfig` (default 7200, ge=60)
- Pass `EGG_AGENT_TIMEOUT` env through the spawner
- Added `JOB_OUTCOME_TIMEOUT` constant
- `_classify_exit_with_context()` detects timeout-killed pods (exit -1 + "Timed out after" in logs)
- `record_timeout()` leaves the abnormal streak untouched
- `_send_timeout_warnings()` emits HEARTBEAT at 90-minute intervals

### Slice 5 — Alert evidence + false-positive fixes (TASK-5-1 to TASK-5-4)
- `_broadcast_alert()` now accepts and includes structured `evidence` in the alert body
- `_check_convergence_stall()` now also checks peer heartbeats via `_get_latest_heartbeat_age()`
- `exit_detail_for()` returns "killed by 2h agent timeout" for timeout-killed pods

## Tests

67 new tests across 4 test files:
- `test_detection_plane_wiring.py` — 8 tests (slice-1/2)
- `test_loop_detection.py` — 15 tests (slice-3)
- `test_timeout_classification.py` — 21 tests (slice-4)
- `test_alert_evidence.py` — 11 tests (slice-5)
- `test_detection_plane.py` — 12 existing tests (all pass)

All 515 tests in the relevant test suites pass.

## Scope Note

Slices 2-5 are committed on this branch but scoped to their own BRC rounds. The slice-1 proposal
covers only TASK-1-1 to TASK-1-6. Downstream slice coders can build on the existing commits.
