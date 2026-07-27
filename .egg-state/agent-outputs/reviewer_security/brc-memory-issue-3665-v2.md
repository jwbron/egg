## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta — #3189 anchors are authoritative -->
-

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### coder

- producer: coder
- last_reviewed_commit_sha: d659cc5b2645e637a6c8fc990ca104fc71f3d1fc
- prior_verdict: NACK
- prior_nack_reasons: The livelock detector's production data path is broken: `_read_session_transcript()` tries to read from `CLAUDE_SESSION_PATH` (never set by the sandbox) or scan `$HOME/.claude/projects/*.jsonl` (the orchestrator cannot access the agent pod's filesystem). The orchestrator already has the transcript via `session_state_store` (Redis-backed, populated by `session-state push`). The detector should use `get_session_state_store().get(pipeline_id, slice_id, role)` instead. Additionally, `tool_calls_by_role` in the snapshot's `raw` field is never populated by `snapshot_from_health_context()`, making the corpus path dead code. The tests mock `_get_agent_logs` so they pass, but the production path is untested and non-functional. The timeout config, SIGTERM classification, convergence-stall suppression, and evidence bundling changes are sound.
- prior_conditional_obligation: -
- enrichment_sha: d659cc5b2645e637a6c8fc990ca104fc71f3d1fc
- summary_of_assessment: The livelock detector's production data path is broken: `_read_session_transcript()` tries to read from `CLAUDE_SESSION_PATH` (never set by the sandbox) or scan `$HOME/.claude/projects/*.jsonl` (the orchestrator cannot access the agent pod's filesystem). The orchestrator already has the transcript via `session_state_store` (Redis-backed, populated by `session-state push`). The detector should use `get_session_state_store().get(pipeline_id, slice_id, role)` instead. Additionally, `tool_calls_by_role` in the snapshot's `raw` field is never populated by `snapshot_from_health_context()`, making the corpus path dead code. The tests mock `_get_agent_logs` so they pass, but the production path is untested and non-functional. The timeout config, SIGTERM classification, convergence-stall suppression, and evidence bundling changes are sound.

## Decision log

- 2026-07-27T10:20:35Z nack coder: The livelock detector's production data path is broken: `_read_session_transcript()` tries to read from `CLAUDE_SESSION_PATH` (never set by the sandbox) or scan `$HOME/.claude/projects/*.jsonl` (the orchestrator cannot access the agent pod's filesystem). The orchestrator already has the transcript via `session_state_store` (Redis-backed, populated by `session-state push`). The detector should use `get_session_state_store().get(pipeline_id, slice_id, role)` instead. Additionally, `tool_calls_by_role` in the snapshot's `raw` field is never populated by `snapshot_from_health_context()`, making the corpus path dead code. The tests mock `_get_agent_logs` so they pass, but the production path is untested and non-functional. The timeout config, SIGTERM classification, convergence-stall suppression, and evidence bundling changes are sound. [orchestrator/health_checks/tier1/loop_detection.py, orchestrator/health_checks/detection_plane.py, orchestrator/health_monitor.py, orchestrator/event_loop/_loop.py, orchestrator/concurrent_executor.py, orchestrator/kubernetes_spawner/_models.py, orchestrator/kubernetes_spawner/_spawn.py, orchestrator/kubernetes_monitor.py, orchestrator/models/_config.py, sandbox/llm/claude/config.py, orchestrator/tests/test_loop_detection.py, orchestrator/tests/test_convergence_stall_suppression.py, orchestrator/tests/test_timeout_sigterm.py, orchestrator/tests/test_agent_timeout_config.py, orchestrator/session_state_store.py, orchestrator/routes/session_state.py, sandbox/egg_lib/session_state_sync.py, sandbox/egg_lib/cli_session_state.py, orchestrator/mcp_tools/_health.py]
