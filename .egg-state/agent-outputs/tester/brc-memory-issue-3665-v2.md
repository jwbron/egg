# Tester BRC Memory — Issue #3665 (v2)

## Pipeline: issue-3665-v2
## Phase: implement
## Slice: slice-1

## Summary of Assessment

### Coder Proposal (v1, commit d659cc5b2)

**VERDICT: NACK** — Missing `detect_heartbeat_stall` registration in DetectionPlane.default()

The coder's proposal integrates commit 6ffe97c8e with corrections per cq-1/cq-3.
The implementation is correct for all four priorities:

1. **Livelock detector (loop_detection.py)**: ✅ Correct
   - Reads live session transcript at $HOME/.claude/projects/<cwd>/<session>.jsonl (NOT agent_log_store)
   - Keys on full untruncated (tool_name, input) pair (NOT 80-char truncation)
   - Implements novelty counting (fire at zero new inputs in trailing window)
   - Escalates to HITL with looping input quoted verbatim (requires_adjudication=True)
   - Registered in DetectionPlane.default() and tier1/__init__.py

2. **Timeout visibility**: ✅ Correct
   - agent_timeout_seconds config field (default 7200, ge=60)
   - EGG_AGENT_TIMEOUT_SECONDS env passing to sandbox
   - active_deadline_seconds on K8s Job
   - Exit 143 (SIGTERM) classified as JOB_OUTCOME_LEGITIMATE, not ABNORMAL
   - _classify_exit treats 143 as clean
   - Sandbox config uses EGG_AGENT_TIMEOUT_SECONDS

3. **Convergence-stall suppression**: ✅ Correct
   - get_agent_activity_ages() on HealthMonitor
   - _has_recent_agent_activity() in event_loop convergence-stall check
   - snapshot_from_health_context enrichment with last_tool_call_age_s/last_heartbeat_age_s
   - detect_heartbeat_stall registration: ❌ MISSING

4. **Alert evidence bundling (priority 4)**: ✅ Correct
   - _build_agent_evidence() / _build_agent_evidence_locked() on HealthMonitor
   - Enriched all 6 escalation dicts with evidence fields
   - Enriched convergence-stall anomaly payload with evidence
   - _emit_supervision_alert accepts optional evidence kwarg

**NACK reason**: `detect_heartbeat_stall` function exists at `consensus_stall.py:217` but is NOT registered in `_register_coverage_gap_detectors` (detection_plane.py). The acceptance criteria for task-1-3 explicitly requires "detect_heartbeat_stall registered and fires in live path." This is a one-line fix: add `detect_heartbeat_stall` to the `coverage_gap_detectors` tuple in `_register_coverage_gap_detectors`.

**Additional finding**: ruff format check fails on 4 files (loop_detection.py, health_monitor.py, test_loop_detection.py, _loop.py). The coder should run `ruff format` before re-proposing.

### Documenter Proposal (v1, no-op)

**VERDICT: No review edge** — No-op proposal is correct. The coder has not committed any documentation changes (verified via git show --stat). The documenter correctly identifies that there is no source diff to document.

### Tester Proposal (v1, no-op)

**VERDICT: Proposed** — Verification-only proposal. All tests pass (1696 tests across 5 test files + 9 calibration corpus rows). The tester role cannot modify source files, so the formatting fixes and detect_heartbeat_stall registration must be addressed by the coder.

## Test Results

All tests pass:
- test_loop_detection.py: 19 tests (novelty metric, cq-1/cq-3, live transcript parsing)
- test_agent_timeout_config.py: 5 tests (agent_timeout_seconds config)
- test_convergence_stall_suppression.py: 6 tests (_has_recent_agent_activity)
- test_timeout_sigterm.py: 10 tests (exit 143 classification)
- test_event_loop.py: 119 tests (including first-propose gate)
- test_overseer_calibration.py: 9 relevant tests (6 agent_livelock + 4 heartbeat_stall corpus rows)

Total: 1696 tests pass (including calibration corpus).

## Code Review Notes

### Files Reviewed
- orchestrator/health_checks/tier1/loop_detection.py — Corrected per cq-1/cq-3
- orchestrator/health_checks/detection_plane.py — detect_agent_livelock registered; detect_heartbeat_stall NOT registered
- orchestrator/health_monitor.py — get_agent_activity_ages() + _build_agent_evidence added
- orchestrator/event_loop/_loop.py — _has_recent_agent_activity() + evidence enrichment
- orchestrator/concurrent_executor.py — _emit_supervision_alert accepts evidence kwarg
- orchestrator/kubernetes_spawner/_models.py — _failed_with_timeout_sigterm added
- orchestrator/kubernetes_spawner/_spawn.py — active_deadline_seconds from EGG_AGENT_TIMEOUT_SECONDS
- orchestrator/kubernetes_monitor.py — exit 143 treated as clean
- orchestrator/models/_config.py — agent_timeout_seconds field added
- orchestrator/health_checks/tier1/__init__.py — detect_agent_livelock exported
- orchestrator/cli.py — AgentLivelockCheck registered
- sandbox/llm/claude/config.py — Uses EGG_AGENT_TIMEOUT_SECONDS
- orchestrator/tests/test_loop_detection.py — 19 tests for novelty metric
- orchestrator/tests/test_agent_timeout_config.py — 5 tests for config
- orchestrator/tests/test_convergence_stall_suppression.py — 6 tests for suppression
- orchestrator/tests/test_timeout_sigterm.py — 10 tests for exit 143
- orchestrator/tests/test_event_loop.py — 119 tests (updated _NotifierSpy)
- orchestrator/tests/overseer_calibration/fixtures.json — 6 new agent_livelock rows + 4 heartbeat_stall rows

### Issues Found
1. **NACK**: `detect_heartbeat_stall` not registered in `_register_coverage_gap_detectors` (detection_plane.py)
2. **Formatting**: ruff format check fails on 4 files — coder should run `ruff format`
