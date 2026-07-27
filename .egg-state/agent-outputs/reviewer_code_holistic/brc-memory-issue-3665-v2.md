## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta — #3189 anchors are authoritative -->
-

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### coder

- producer: coder
- last_reviewed_commit_sha: 00151f7a778eaaec86c250368a288e21b99317de
- prior_verdict: NACK
- prior_nack_reasons: Bug in _extract_tool_calls_by_role (detection_plane.py): the function iterates over live_ids (container IDs) and passes them as the role parameter to session_state_store.get(), but the store keys records by role name (e.g., "coder"), not container ID. This means the livelock detector will never find any transcripts in production — the corpus path (tool_calls_by_role) is also dead code in production since _extract_tool_calls_by_role returns an empty dict. The fix is to pass live_roles (container ID → role name mapping) to _extract_tool_calls_by_role and iterate over role names, not container IDs. The tests pass because they mock _get_agent_logs and use tool_calls_by_role in the snapshot's raw field, but neither path is exercised with real session_state_store data in production.
- prior_conditional_obligation: -
- enrichment_sha: 00151f7a778eaaec86c250368a288e21b99317de
- summary_of_assessment: Bug in _extract_tool_calls_by_role (detection_plane.py): the function iterates over live_ids (container IDs) and passes them as the role parameter to session_state_store.get(), but the store keys records by role name (e.g., "coder"), not container ID. This means the livelock detector will never find any transcripts in production — the corpus path (tool_calls_by_role) is also dead code in production since _extract_tool_calls_by_role returns an empty dict. The fix is to pass live_roles (container ID → role name mapping) to _extract_tool_calls_by_role and iterate over role names, not container IDs. The tests pass because they mock _get_agent_logs and use tool_calls_by_role in the snapshot's raw field, but neither path is exercised with real session_state_store data in production.

## Decision log

- 2026-07-27T11:22:08Z nack coder: Bug in _extract_tool_calls_by_role (detection_plane.py): the function iterates over live_ids (container IDs) and passes them as the role parameter to session_state_store.get(), but the store keys records by role name (e.g., "coder"), not container ID. This means the livelock detector will never find any transcripts in production — the corpus path (tool_calls_by_role) is also dead code in production since _extract_tool_calls_by_role returns an empty dict. The fix is to pass live_roles (container ID → role name mapping) to _extract_tool_calls_by_role and iterate over role names, not container IDs. The tests pass because they mock _get_agent_logs and use tool_calls_by_role in the snapshot's raw field, but neither path is exercised with real session_state_store data in production. [orchestrator/health_checks/detection_plane.py, orchestrator/health_checks/tier1/loop_detection.py, orchestrator/health_checks/tier1/consensus_stall.py, orchestrator/health_checks/context.py, orchestrator/event_loop/_loop.py, orchestrator/health_monitor.py, orchestrator/concurrent_executor.py, orchestrator/kubernetes_spawner/_spawn.py, orchestrator/kubernetes_spawner/_models.py, orchestrator/kubernetes_monitor.py, orchestrator/models/_config.py, sandbox/llm/claude/config.py, orchestrator/cli.py, orchestrator/event_loop/__init__.py, orchestrator/tests/test_loop_detection.py, orchestrator/tests/test_convergence_stall_suppression.py, orchestrator/tests/test_timeout_sigterm.py, orchestrator/tests/test_agent_timeout_config.py, orchestrator/tests/test_event_loop.py, orchestrator/tests/overseer_calibration/fixtures.json]
