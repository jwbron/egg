# BRC Memory — risk_analyst — pipeline issue-3665-v3 (plan phase)

## Producer state
- **Reviewed**: task_planner proposal (v1, commit 6092b5a7a) — the 5-slice linear plan for issue #3665.
- **Verdict**: **PROCEED** — plan is well-grounded, properly scoped, and risk-rated.
- Reviewed as both producer (risk assessment) and reviewer (plan review).

## Risk assessment summary

| Slice | Risk | Verdict | Notes |
|-------|------|---------|-------|
| slice-1 (snapshot fields) | Medium | Accept | Prerequisite for all downstream work. Exception-isolation documented in `snapshot_from_health_context()` docstring. |
| slice-2 (detection plane wiring) | **High** | Accept with mitigation | New code path on hot loop. Double-evaluation guard critical — `_run_runtime_tick_checks()` called from both `_check_pod` (line 219) and `_reconciliation_sweep` (line 621). Must be exception-isolated. |
| slice-3 (loop detector) | Medium | Accept | Depends on slice-1's `midturn_messages` population. Log truncation fix (TASK-3-2) correctly sequenced. |
| slice-4 (timeout) | Medium | Accept | Exit code -1 disambiguation via error message is correct. `JOB_OUTCOME_TIMEOUT` does not exist yet — confirmed. |
| slice-5 (alerts) | Medium | Accept | TASK-5-3 depends on TASK-4-3 (soft dependency, correctly noted). |

## Key risk mitigations verified
1. **Double-evaluation guard (slice-2)**: `_run_runtime_tick_checks()` called from two sites — plan correctly identifies this.
2. **Exception isolation**: `snapshot_from_health_context()` is defensive; `DetectionPlane.evaluate()` exception-isolates each detector.
3. **Tracker may be None (TASK-1-2)**: `get_peer_consensus_tracker()` may return None — snapshot builder must handle gracefully.
4. **Log truncation (TASK-3-2)**: `read_job_log_snapshot()` truncates at ~100 chars — correctly identified as prerequisite.
5. **Exit code disambiguation (TASK-4-3)**: Both timeout-kills and crashes produce exit -1; plan's error-message check is correct.

## What's correctly excluded
- No LLM classification on hot path
- No change to 2-hour timeout default
- No rebuild of overseer agent
- No removal of HealthMonitor tripwires
- Tier 3-5 are input to gate, not work queue

## Ordering
Linear chain (slice-1 → slice-2 → slice-3 → slice-4 → slice-5) is correct per #3046: slices share overlapping files.

## Acceptance criteria
All 10 criteria are measurable and testable. Verified each maps to specific tasks.

## Recommendation
**PROCEED** with the plan as-is. The risk assessment is accurate, technical claims verified, mitigations appropriate.
