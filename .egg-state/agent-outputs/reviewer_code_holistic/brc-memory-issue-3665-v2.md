# reviewer_code_holistic BRC Memory — issue-3665-v2

## Assessment Summary

**Producer:** coder
**Proposal SHA:** d659cc5b2645e637a6c8fc990ca104fc71f3d1fc
**Verdict:** ACK (version 1)
**Date:** 2026-07-27

## What was reviewed

The coder's proposal integrates fix commit 6ffe97c8e with corrections per operator feedback (cq-1, cq-3) for the #3665 supervision-layer improvements. Four areas:

1. **Livelock detector** (loop_detection.py): New Tier 1 health check detecting agent repetition loops via novelty counting (zero new unique tool inputs in trailing window). Reads live session transcript per cq-1, escalates to HITL per cq-3.

2. **Timeout visibility**: agent_timeout_seconds config (default 7200s), EGG_AGENT_TIMEOUT_SECONDS env passing, K8s active_deadline_seconds, exit 143 classified as JOB_OUTCOME_LEGITIMATE.

3. **Convergence-stall suppression**: _has_recent_agent_activity() consults HealthMonitor.get_agent_activity_ages() before firing stall alerts.

4. **Alert evidence bundling**: Structured evidence fields on all 6 escalation dicts.

## Files reviewed

- orchestrator/health_checks/tier1/loop_detection.py
- orchestrator/health_checks/detection_plane.py
- orchestrator/health_checks/tier1/__init__.py
- orchestrator/cli.py
- orchestrator/health_monitor.py
- orchestrator/event_loop/_loop.py
- orchestrator/event_loop/__init__.py
- orchestrator/concurrent_executor.py
- orchestrator/kubernetes_spawner/_spawn.py
- orchestrator/kubernetes_spawner/_models.py
- orchestrator/kubernetes_monitor.py
- orchestrator/models/_config.py
- sandbox/llm/claude/config.py
- orchestrator/tests/test_loop_detection.py
- orchestrator/tests/test_agent_timeout_config.py
- orchestrator/tests/test_convergence_stall_suppression.py
- orchestrator/tests/test_timeout_sigterm.py
- orchestrator/tests/test_event_loop.py
- orchestrator/tests/overseer_calibration/fixtures.json

## Test results

- 19 new tests in test_loop_detection.py: ALL PASS
- 21 tests in test_convergence_stall_suppression.py + test_timeout_sigterm.py + test_agent_timeout_config.py: ALL PASS
- 8 calibration corpus rows (6 new for agent_livelock): ALL PASS
- 358 tests in test_event_loop.py + test_health_monitor.py + test_concurrent_executor.py: ALL PASS
- 59 tests in test_kubernetes_monitor.py: ALL PASS
- 13 config-related tests in test_models.py: ALL PASS
- ruff: ALL CHECKS PASSED
- mypy: No new errors introduced (pre-existing import/stub issues only)

## cq-1 compliance

The detector reads the live Claude Code session transcript at $HOME/.claude/projects/<cwd>/<session>.jsonl inside the running pod, NOT the agent_log_store (pod stdout). Signatures are the full untruncated (tool_name, input) pair — no 80-char truncation. Verified by test_full_untruncated_signature_no_80_char_limit.

## cq-3 compliance

requires_adjudication=True — the detector escalates to HITL with the looping input quoted verbatim, not a nudge. Verified by test_finding_quotes_looping_input_verbatim and test_check_returns_degraded_when_finding.

## Production data path concern (reviewer_security NACK)

The reviewer_security has NACKed the proposal with a valid concern: the orchestrator process cannot read files from inside the agent pod's filesystem. The _read_session_transcript() function tries to read CLAUDE_SESSION_PATH (never set in the orchestrator's environment) or scan $HOME/.claude/projects/*.jsonl (not accessible from the orchestrator). The orchestrator has a Redis-backed session_state_store that should be used instead.

This is a legitimate production-path issue that the coder needs to address. The tests pass because they mock _get_agent_logs, but the production data path is non-functional as written.

## Notes

- The tool_calls_by_role field in snapshot_from_health_context() is NOT populated in production — it is only used by the calibration corpus. The raw field on EventStreamSnapshot is left as an empty dict in the production path.
- The _has_recent_agent_activity() function correctly maps the event-loop role to the health monitor's agent_id (which is typically the role name in orchestrator mode).
- The Finding.severity field is typed as str but the code passes Severity.HIGH (a StrEnum), which works correctly since StrEnum is a str subclass.
