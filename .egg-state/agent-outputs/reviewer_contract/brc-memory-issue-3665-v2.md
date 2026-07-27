## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta — #3189 anchors are authoritative -->
-

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### coder

- producer: coder
- last_reviewed_commit_sha: 00151f7a778eaaec86c250368a288e21b99317de
- prior_verdict: NACK
- prior_nack_reasons: Bug in _extract_tool_calls_by_role: the function iterates over live_ids (container IDs) and uses them directly as role names when calling session_state_store.get() and as keys in the result dict. However, session_state_store.get() expects a role name, not a container ID. The live_roles mapping (container_id -> role_name) is available in snapshot_from_health_context but is NOT passed to _extract_tool_calls_by_role. This means: (1) store.get() is called with container IDs instead of role names, so no transcripts will be found in production; (2) the tool_calls_by_role dict is keyed by container IDs, but detect_agent_livelock looks up by role name (from RunningAgent.role which is correctly mapped via live_roles.get(cid, cid)). The two will never match, so the corpus path is also broken. Fix: pass live_roles to _extract_tool_calls_by_role and iterate over role names, using them as both the store.get() key and the result dict key.
- prior_conditional_obligation: -
- enrichment_sha: 00151f7a778eaaec86c250368a288e21b99317de
- summary_of_assessment: Bug in _extract_tool_calls_by_role: the function iterates over live_ids (container IDs) and uses them directly as role names when calling session_state_store.get() and as keys in the result dict. However, session_state_store.get() expects a role name, not a container ID. The live_roles mapping (container_id -> role_name) is available in snapshot_from_health_context but is NOT passed to _extract_tool_calls_by_role. This means: (1) store.get() is called with container IDs instead of role names, so no transcripts will be found in production; (2) the tool_calls_by_role dict is keyed by container IDs, but detect_agent_livelock looks up by role name (from RunningAgent.role which is correctly mapped via live_roles.get(cid, cid)). The two will never match, so the corpus path is also broken. Fix: pass live_roles to _extract_tool_calls_by_role and iterate over role names, using them as both the store.get() key and the result dict key.

## Decision log

- 2026-07-27T11:20:31Z nack coder: Bug in _extract_tool_calls_by_role: the function iterates over live_ids (container IDs) and uses them directly as role names when calling session_state_store.get() and as keys in the result dict. However, session_state_store.get() expects a role name, not a container ID. The live_roles mapping (container_id -> role_name) is available in snapshot_from_health_context but is NOT passed to _extract_tool_calls_by_role. This means: (1) store.get() is called with container IDs instead of role names, so no transcripts will be found in production; (2) the tool_calls_by_role dict is keyed by container IDs, but detect_agent_livelock looks up by role name (from RunningAgent.role which is correctly mapped via live_roles.get(cid, cid)). The two will never match, so the corpus path is also broken. Fix: pass live_roles to _extract_tool_calls_by_role and iterate over role names, using them as both the store.get() key and the result dict key. [orchestrator/health_checks/detection_plane.py, orchestrator/health_checks/tier1/loop_detection.py, orchestrator/health_checks/tier1/consensus_stall.py, orchestrator/health_checks/context.py, orchestrator/event_loop/_loop.py, orchestrator/tests/test_loop_detection.py, orchestrator/tests/test_convergence_stall_suppression.py, orchestrator/tests/test_event_loop.py, orchestrator/tests/test_overseer_calibration.py]
