# BRC Memory — reviewer_security — issue-3665-v3

## Verdict on coder proposal (commit f0d766673, v1)

**NACK** — 7 blocking issues. The proposal populates snapshot fields but the data
is inaccessible to the detectors that need it, in the wrong format, or targets
a log format that doesn't exist. The detection plane will be blind to all five
detector categories.

### Blocking issues

1. **`runtime` field not accessible to `detect_run_pipeline_thread_liveness`**
   - `_runtime()` in `runtime_liveness.py:68-72` reads from `snapshot.raw["runtime"]`
   - `snapshot_from_health_context` sets `runtime` as a top-level field but does NOT
     populate `raw` (defaults to `{}`)
   - Driver liveness detector will never see runtime data → dead/hung orchestrator
     thread goes undetected (security-relevant false negative)

2. **Field name mismatch in `runtime` section**
   - `_build_runtime_section` returns `{"tick_age_s", "spawn_age_s"}`
   - `detect_run_pipeline_thread_liveness` expects `run_pipeline_thread_alive` and
     `thread_last_tick_age_s` (runtime_liveness.py:105-106)
   - `detect_agent_restart_propagation` expects `restart_propagation.deadline_exceeded`
     etc. (runtime_liveness.py:211-216)
   - Detectors will find `None` and stay silent

3. **`container_transitions` format mismatch**
   - `_build_container_transitions` produces `{pod_id, status, pipeline_id, phase}`
     from `_pod_states` (a `dict[str, ContainerStatus]` current-state map, NOT a
     transition log)
   - Detectors in `container_k8s.py` expect `{to, reason, transient, container,
     restart_count, to_state}` (container_k8s.py:95-102, 190-192, 242-245, 287-289)
   - `detect_overseer_self_injection` expects `container` field (container_k8s.py:192)
     → prompt injection attack detection will be blind (security-relevant)

4. **`midturn_messages` parsing targets non-existent log format**
   - `_parse_tool_calls_from_logs` expects JSON lines with `message == "Tool call"`
     and `extra.event_type == "tool_use"` (detection_plane.py:753)
   - `egg_agent/client.py` does NOT emit any such log lines
   - Only tool-related logging: `event_type="tool_intercepted"` (blocked tools) and
     `event_type="system"` (system events)
   - `detect_tool_input_loop` — the primary deliverable for issue #3665 — will
     never find any tool call records → all 7 livelocks go undetected
     (PRIMARY security-relevant false negative)

5. **`consensus` field missing expected keys for BRC thrash detection**
   - `tracker.evaluate()` returns: `is_complete`, `blocking_agents`,
     `has_unresolved_nacks`, `unresolved_nacks`, `pre_merge_conditions`, `agents`,
     `approval_matrix`, `review_graph`, `protocol` (peer_consensus/_queries.py:179-190)
   - `detect_brc_thrash` expects `nack_cycles` and `late_confirmed_then_renack`
     (brc_thrashing.py:71-72)
   - `detect_incomplete_consensus_deferral` expects `incomplete_consensus_deferrals`
     and `deferral_cap` (brc_thrashing.py:109-112)
   - BRC thrashing detection will be blind

6. **Test file mismatch**
   - Diff describes 15 tests (TestMidturnMessages, TestRuntimeSection, etc.)
   - Actual file on disk has 8 different tests (TestDetectionPlaneInvocation,
     TestDoubleEvaluationGuard, TestConsensusStallDoubleFireGuard,
     TestFindingRouting)
   - Proposal's tests were replaced by slice-2 tests
   - NO tests verify field population → wiring issues undetected

7. **Calibration corpus missing `runtime` field**
   - `corpus.py:182-207` `EventStreamSnapshot` has no `runtime` field
   - Production type in `detection_plane.py:129` has `runtime`
   - Calibration corpus cannot test `detect_run_pipeline_thread_liveness`

### What was verified as correct

- `list_records(pipeline_id, include_logs=True)` — keyword-only param, call is correct
- `get_agent_log_store()`, `get_kubernetes_monitor()`, `get_health_monitor()`,
  `get_peer_consensus_tracker()` — all exist with correct signatures
- `AgentLogStore.put()` stores records with `job_name`, `agent_role`, `logs` fields
- `_pod_states` is `dict[str, ContainerStatus]` — current state map, not transition log
- `AgentState` has `last_heartbeat` and `last_progress` as float timestamps
- `RunningAgent` dataclass has `last_tool_call_age_s` and `last_heartbeat_age_s` fields
- `EventStreamSnapshot` has all 13 fields including `runtime` (added by proposal)
- `detect_phase_stall` reads `consensus["blocking_agents"]` — this IS in tracker output ✓
- `detect_heartbeat_stall` reads `agent.last_tool_call_age_s` and `agent.last_heartbeat_age_s`
  from `RunningAgent` — this IS populated by `_build_running_agents` ✓
- `detect_tool_input_loop` reads `midturn_messages` directly from snapshot (not `raw`) ✓
  and reads `consensus["blocking_agents"]` which IS in tracker output ✓

## Summary of assessment

The proposal adds field-population code to `snapshot_from_health_context` but
fails to ensure the data is consumable by the detectors that were already
written to read those fields. The root causes are:

- `raw` dict not populated (runtime section invisible to `_runtime()` helper)
- Field names don't match between builder and consumer
- `container_transitions` records use current-state format instead of transition format
- `midturn_messages` parsing targets a log format that doesn't exist in the codebase
- `consensus` section missing keys that BRC thrash detectors expect
- Tests were replaced, so no tests verify the wiring

The detection plane will compile and run without errors, but every detector
will silently stay silent because it cannot find the data it expects.
