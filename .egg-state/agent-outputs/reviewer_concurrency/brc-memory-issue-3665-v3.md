## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta — #3189 anchors are authoritative -->
-

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### coder

- producer: coder
- last_reviewed_commit_sha: d8a20145f
- prior_verdict: NACK
- prior_nack_reasons: 4 of 5 concurrency fixes are correct, but Fix 3 (`_build_running_agents` in `detection_plane.py:922-933`) is incomplete. The lock protects the `_agents.get(role)` dict lookup but is released BEFORE reading `agent_state.last_heartbeat` and `agent_state.last_progress` (the attribute reads at lines 324-326 in the diff happen OUTSIDE the lock). Since `HealthMonitor` mutates `AgentState` fields under its lock, reading them outside the lock is still a data race. The producer must either: (a) move the attribute reads inside the lock, or (b) copy the needed values inside the lock. The other 4 fixes (TOCTOU race on `_detection_plane_last_tick`, `ToolInputLoopTracker` lock, `_pod_states` lock, `_timeout_warning_last_sent` lock) are all correct. Issue #5 (peer_consensus.evaluate()) was verified to already be thread-safe via RLock.
- prior_conditional_obligation: -
- enrichment_sha: d8a20145f
- summary_of_assessment: 4 of 5 concurrency fixes are correct, but Fix 3 (`_build_running_agents` in `detection_plane.py:922-933`) is incomplete. The lock protects the `_agents.get(role)` dict lookup but is released BEFORE reading `agent_state.last_heartbeat` and `agent_state.last_progress` (the attribute reads at lines 324-326 in the diff happen OUTSIDE the lock). Since `HealthMonitor` mutates `AgentState` fields under its lock, reading them outside the lock is still a data race. The producer must either: (a) move the attribute reads inside the lock, or (b) copy the needed values inside the lock. The other 4 fixes (TOCTOU race on `_detection_plane_last_tick`, `ToolInputLoopTracker` lock, `_pod_states` lock, `_timeout_warning_last_sent` lock) are all correct. Issue #5 (peer_consensus.evaluate()) was verified to already be thread-safe via RLock.

## Decision log

- 2026-07-28T02:30:51Z nack coder: 4 of 5 concurrency fixes are correct, but Fix 3 (`_build_running_agents` in `detection_plane.py:922-933`) is incomplete. The lock protects the `_agents.get(role)` dict lookup but is released BEFORE reading `agent_state.last_heartbeat` and `agent_state.last_progress` (the attribute reads at lines 324-326 in the diff happen OUTSIDE the lock). Since `HealthMonitor` mutates `AgentState` fields under its lock, reading them outside the lock is still a data race. The producer must either: (a) move the attribute reads inside the lock, or (b) copy the needed values inside the lock. The other 4 fixes (TOCTOU race on `_detection_plane_last_tick`, `ToolInputLoopTracker` lock, `_pod_states` lock, `_timeout_warning_last_sent` lock) are all correct. Issue #5 (peer_consensus.evaluate()) was verified to already be thread-safe via RLock. [orchestrator/health_checks/detection_plane.py, orchestrator/health_checks/tier1/consensus_stall.py, orchestrator/health_checks/tier1/loop_detection.py, orchestrator/kubernetes_monitor.py, orchestrator/tests/test_detection_plane_wiring.py, orchestrator/tests/test_loop_detection.py, orchestrator/tests/test_timeout_classification.py, orchestrator/tests/test_alert_evidence.py, orchestrator/peer_consensus/_queries.py, orchestrator/health_monitor.py]
