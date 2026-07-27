## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta — #3189 anchors are authoritative -->
-

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### coder

- producer: coder
- last_reviewed_commit_sha: d659cc5b2645e637a6c8fc990ca104fc71f3d1fc
- prior_verdict: NACK
- prior_nack_reasons: CRITICAL BLOCKERS — the livelock detector cannot function in production:

1. `_read_session_transcript` (loop_detection.py:161-162) checks `os.environ.get("CLAUDE_SESSION_PATH")` which is NEVER set anywhere in the codebase. The actual mechanism uses `EGG_SESSION_STATE_FILE` (a pointer file containing session_id JSON) and `CLAUDE_CONFIG_DIR`. The fallback filesystem scan (lines 175-196) is unreliable: it doesn't use the session-state pointer file, doesn't use `CLAUDE_CONFIG_DIR`, and doesn't use `EGG_REPO_PATH` for slug computation. In production with multiple sessions, it will read the wrong transcript or miss the correct one entirely. The detector runs in the orchestrator process (via HealthCheckRunner), not inside the agent pod, so it cannot read the pod's local filesystem at all.

2. `snapshot_from_health_context` (detection_plane.py:551-559) sets `RunningAgent.role=str(cid)` where `cid` is a Docker container ID, not a role name. But `detect_agent_livelock` uses this `role` to look up `tool_calls_by_role[role]` (corpus keys are role names like "coder") and to call `_get_agent_logs(pipeline_id, role)`. In production, the detector needs to map container IDs to role names to fetch the correct transcript from the session-state store (via `get_session_state_store().get(pipeline_id, slice_id, role)`), but no such mapping exists.

3. The `_has_recent_agent_activity` suppression (loop_detection.py:1020-1061) only checks for recent heartbeat/progress/container activity. It does NOT address the 5 false-positive cases explicitly called out in the issue: (a) producers legitimately podless between events, (b) reviewers waiting on upstream producers, (c) declared no-op leaving review edges pending forever, (d) NACK discharging obligations like ACK, (e) two states not visible in the status payload. The convergence-stall check needs to consult BRC consensus state (blocking_agents, producer/reviewer phases) to distinguish "legitimately waiting" from "stuck".

4. `grace_seconds` parameter (loop_detection.py:222) is accepted but never used — the docstring claims it checks agent age against the grace period, but no such check exists in the function body.

5. The convergence-stall suppression (loop_detection.py:935-943) does a `continue` to skip the alert for this poll cycle, but does NOT reset `_stall_first_seen[role]` or `_stall_alerted[role]`. On the next poll cycle, if the agent's activity ages out, the stall timer continues from the original (stale) `bus_timestamp` anchor, potentially firing an alert that's still based on the original event.

These are not minor issues — the detector's core data path (finding and reading the transcript) is broken. The fix requires: (a) using the session-state store to fetch transcripts by (pipeline_id, slice_id, role), (b) mapping container IDs to role names in the snapshot, and (c) consulting BRC consensus state in the convergence-stall suppression.
- prior_conditional_obligation: -
- enrichment_sha: d659cc5b2645e637a6c8fc990ca104fc71f3d1fc
- summary_of_assessment: CRITICAL BLOCKERS — the livelock detector cannot function in production: 1. `_read_session_transcript` (loop_detection.py:161-162) checks `os.environ.get("CLAUDE_SESSION_PATH")` which is NEVER set anywhere in the codebase. The actual mechanism uses `EGG_SESSION_STATE_FILE` (a pointer file containing session_id JSON) and `CLAUDE_CONFIG_DIR`. The fallback filesystem scan (lines 175-196) is unreliable: it doesn't use the session-state pointer file, doesn't use `CLAUDE_CONFIG_DIR`, and doesn't use `EGG_REPO_PATH` for slug computation. In production with multiple sessions, it will read the wrong transcript or miss the correct one entirely. The detector runs in the orchestrator process (via HealthCheckRunner), not inside the agent pod, so it cannot read the pod's local filesystem at all. 2. `snapshot_from_health_context` (detection_plane.py:551-559) sets `RunningAgent.role=str(cid)` where `cid` is a Docker container ID, not a role name. But `detect_agent_livelock` uses this `role` to look u…

## Decision log

- 2026-07-27T10:25:36Z nack coder: CRITICAL BLOCKERS — the livelock detector cannot function in production: [orchestrator/health_checks/tier1/loop_detection.py, orchestrator/health_checks/detection_plane.py, orchestrator/event_loop/_loop.py, orchestrator/health_monitor.py, orchestrator/models/_config.py, orchestrator/concurrent_executor.py, orchestrator/kubernetes_monitor.py, orchestrator/kubernetes_spawner/_models.py, orchestrator/kubernetes_spawner/_spawn.py, sandbox/llm/claude/config.py, orchestrator/health_checks/context.py, orchestrator/agent_log_store.py, orchestrator/routes/session_state.py, sandbox/egg_lib/session_state_sync.py, orchestrator/tests/test_loop_detection.py, orchestrator/tests/test_convergence_stall_suppression.py, orchestrator/tests/test_timeout_sigterm.py, orchestrator/tests/test_agent_timeout_config.py, orchestrator/tests/overseer_calibration/fixtures.json]
