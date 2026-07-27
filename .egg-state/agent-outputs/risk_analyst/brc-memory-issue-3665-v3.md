# BRC Memory — risk_analyst — pipeline issue-3665-v3 (plan phase)

## Producer state
- **Reviewed**: task_planner proposal (v3, commit b898df9733) — the 5-slice linear plan for issue #3665.
- **Verdict**: **PROCEED** — plan addresses all 4 iteration feedback items from the operator.
- Reviewed as both producer (risk assessment) and reviewer (plan review).

## Iteration feedback addressed (v1 → v3)

1. **AC-1 unsatisfiable (BLOCKING)** — FIXED: AC-1 now says "populates the 5 in-scope `EventStreamSnapshot` fields" and explicitly names the 4 excluded fields (`decision_state`, `gateway_error_counters`, `cost_counters`, `git_state`) that remain empty by decision. TASK-1-6 updated to match.

2. **Dropped consensus-stall double-fire constraint** — FIXED: TASK-2-2 added to slice-2: "Prevent consensus-stall double-firing" — guards against `ConsensusStallCheck` (runs every tick) and the detection plane's consensus-stall detector firing twice. Acceptance criterion #3 added.

3. **Tier 3 work without decision** — FIXED: "Scope decisions" section added with explicit justification for TASK-3-2 (candidate #20, log fidelity — hard dependency for loop detector) and TASK-2-3 (candidate #19, alert routing — core to issue's "alerts an operator cannot act on" problem). Non-goals updated to reflect the two registered exceptions.

4. **midturn_messages sequenced last** — FIXED: TASK-1-1 is now `midturn_messages` (was TASK-1-5), per the refine gate's carry-into-plan note.

## Risk assessment summary

| Slice | Risk | Verdict | Notes |
|-------|------|---------|-------|
| slice-1 (snapshot fields) | Medium | Accept | 5 in-scope fields only (4 Tier 3-4 excluded by decision). Exception-isolation documented in `snapshot_from_health_context()` docstring. |
| slice-2 (detection plane wiring) | **High** | Accept with mitigation | New code path on hot loop. Double-evaluation guard critical — `_run_runtime_tick_checks()` called from both `_check_pod` (line 219) and `_reconciliation_sweep` (line 621). Must be exception-isolated. TASK-2-2 adds consensus-stall double-fire guard. |
| slice-3 (loop detector) | Medium | Accept | Depends on slice-1's `midturn_messages` population. Log truncation fix (TASK-3-2) correctly sequenced. |
| slice-4 (timeout) | Medium | Accept | Exit code -1 disambiguation via error message is correct. `JOB_OUTCOME_TIMEOUT` does not exist yet — confirmed. |
| slice-5 (alerts) | Medium | Accept | TASK-5-3 depends on TASK-4-3 (soft dependency, correctly noted). |

## Key risk mitigations verified
1. **Double-evaluation guard (slice-2)**: `_run_runtime_tick_checks()` called from two sites — plan correctly identifies this.
2. **Consensus-stall double-fire guard (slice-2 TASK-2-2)**: `ConsensusStallCheck` at `consensus_stall.py:51` runs every tick; `detect_heartbeat_stall()` at line 217 is dormant. The guard must suppress the detection plane's detector, not the registered check.
3. **Exception isolation**: `snapshot_from_health_context()` is defensive; `DetectionPlane.evaluate()` exception-isolates each detector.
4. **Tracker may be None (TASK-1-2)**: `get_peer_consensus_tracker()` may return None — snapshot builder must handle gracefully.
5. **Log truncation (TASK-3-2)**: `read_job_log_snapshot()` truncates at ~100 chars — correctly identified as prerequisite.
6. **Exit code disambiguation (TASK-4-3)**: Both timeout-kills and crashes produce exit -1; plan's error-message check is correct.
7. **JOB_OUTCOME_TIMEOUT (TASK-4-3)**: Does not exist yet — must not interfere with EX_AUTH_FATAL / EX_RATE_LIMITED chain (#3364).

## What's correctly excluded
- No LLM classification on hot path
- No change to 2-hour timeout default
- No rebuild of overseer agent
- No removal of HealthMonitor tripwires
- Tier 3-5 are input to gate, not work queue — EXCEPT two registered scope decisions (TASK-3-2 log fidelity, TASK-2-3 alert routing)

## Ordering
Linear chain (slice-1 → slice-2 → slice-3 → slice-4 → slice-5) is correct per #3046: slices share overlapping files.

## Recommendation
**PROCEED** with the plan as-is. The task_planner addressed all 4 iteration feedback items. The risk assessment is accurate, technical claims verified, mitigations appropriate.
