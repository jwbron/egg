# Tester BRC Memory — Issue #3665 (v2)

## Pipeline: issue-3665-v2
## Phase: implement
## Slice: slice-1

## Summary of Assessment

### Event 1: Initial Review (coder v1, commit d659cc5b2)

**VERDICT: NACK** — Missing `detect_heartbeat_stall` registration in DetectionPlane.default()

The coder's v1 proposal integrated commit 6ffe97c8e with corrections per cq-1/cq-3.
The implementation was correct for all four priorities, but `detect_heartbeat_stall`
was NOT registered in `_register_coverage_gap_detectors` despite the commit message
claiming it was.

**NACK reason**: `detect_heartbeat_stall` function exists at `consensus_stall.py:217`
but is NOT registered in `_register_coverage_gap_detectors` (detection_plane.py).
The acceptance criteria for task-1-3 explicitly requires "detect_heartbeat_stall
registered and fires in live path." This is a one-line fix.

Also noted: ruff format check fails on 4 files (loop_detection.py, health_monitor.py,
test_loop_detection.py, _loop.py).

### Event 2: Re-review (coder v3, commit 940f6046bb56e3c88a4e6cfac420afd0dc72db88)

**VERDICT: ACK** — All three NACKs addressed

The coder's v3 proposal addresses all three NACKs from v1:

1. **Tester NACK (detect_heartbeat_stall not registered)**: ✅ Fixed
   - `detect_heartbeat_stall` now imported and registered in `_register_coverage_gap_detectors`
   - `detector_key`/`name` attributes added to the function in consensus_stall.py

2. **Reviewer_security NACK (broken production data path)**: ✅ Fixed
   - `_read_session_transcript` now uses `session_state_store.get()` (Redis-backed)
     instead of filesystem reading (CLAUDE_SESSION_PATH / /home/egg/.claude/projects/)
   - `tool_calls_by_role` populated in `snapshot_from_health_context()`

3. **Reviewer_concurrency NACK (multiple issues)**: ✅ Fixed
   - Switched from filesystem to session_state_store (same root cause)
   - Added `live_container_roles` property to map container IDs to role names
   - Added `_is_brc_idle()` consultation in `_has_recent_agent_activity()`
   - Reset stall timers when activity is detected
   - `grace_seconds` documented as enforced via `min_tool_calls`

**Additional v3 fix**: `tool_calls_by_role` lookup now checks both direct path
(production) and nested path (corpus).

### Tester Proposal (v1, no-op)

**VERDICT: CONFIRMED** — Verification-only proposal

The tester's proposal is a no-op (verification-only) since the tester role cannot
modify source files. All tests pass (163 tests across 5 test files + 9 calibration
corpus rows).

## Test Results

All tests pass:
- test_loop_detection.py: 19 tests (novelty metric, cq-1/cq-3, live transcript parsing)
- test_agent_timeout_config.py: 5 tests (agent_timeout_seconds config)
- test_convergence_stall_suppression.py: 6 tests (_has_recent_agent_activity)
- test_timeout_sigterm.py: 10 tests (exit 143 classification)
- test_event_loop.py: 119 tests (including first-propose gate)
- test_overseer_calibration.py: 9 relevant tests (6 agent_livelock + 4 heartbeat_stall)

Total: 163 tests pass (including calibration corpus).

## Code Review Notes

### Files Reviewed (v3)
- orchestrator/health_checks/tier1/loop_detection.py — Uses session_state_store, novelty metric, HITL escalation
- orchestrator/health_checks/tier1/consensus_stall.py — detect_heartbeat_stall registered with detector_key/name
- orchestrator/health_checks/detection_plane.py — detect_heartbeat_stall registered, tool_calls_by_role populated, live_container_roles used
- orchestrator/health_checks/context.py — live_container_roles property added
- orchestrator/event_loop/_loop.py — _is_brc_idle consultation, stall timer reset
- orchestrator/health_monitor.py — get_agent_activity_ages() + _build_agent_evidence
- orchestrator/concurrent_executor.py — _emit_supervision_alert accepts evidence kwarg
- orchestrator/kubernetes_spawner/_models.py — _failed_with_timeout_sigterm
- orchestrator/kubernetes_spawner/_spawn.py — active_deadline_seconds from EGG_AGENT_TIMEOUT_SECONDS
- orchestrator/kubernetes_monitor.py — exit 143 treated as clean
- orchestrator/models/_config.py — agent_timeout_seconds field
- orchestrator/health_checks/tier1/__init__.py — detect_agent_livelock exported
- orchestrator/cli.py — AgentLivelockCheck registered
- sandbox/llm/claude/config.py — Uses EGG_AGENT_TIMEOUT_SECONDS
- orchestrator/tests/test_loop_detection.py — 19 tests for novelty metric
- orchestrator/tests/test_agent_timeout_config.py — 5 tests for config
- orchestrator/tests/test_convergence_stall_suppression.py — 6 tests for suppression
- orchestrator/tests/test_timeout_sigterm.py — 10 tests for exit 143
- orchestrator/tests/test_event_loop.py — 119 tests (updated _NotifierSpy)
- orchestrator/tests/overseer_calibration/fixtures.json — 6 agent_livelock + 4 heartbeat_stall rows

### Remaining Issues
- ruff format check fails on 3 files (loop_detection.py, detection_plane.py, _loop.py) — minor line-wrapping issues, coder should run `ruff format`
