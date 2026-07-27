# BRC Memory — coder role, issue #3665

## Pipeline context

Pipeline: issue-3665-v2
Phase: implement (slice-1)
Branch: egg/issue-3665-v2-slice-1-coder/work
Current HEAD: 940f6046b (v3 re-proposal addressing all NACKs)

## Summary of assessment

### What was already in the tree (verified, not rebuilt)

All nine "already landed" items from the issue's "What has already landed" list
were verified present in the tree before starting.

### What was done

Integrated fix commit `6ffe97c8e` from the `issue-3665-supervision-gaps` branch
with corrections to the livelock detector per operator feedback (cq-1, cq-3),
plus alert evidence bundling (priority 4). Addressed all reviewer NACKs across
3 proposal versions.

#### Priority 1: Agent livelock/repetition-loop detection (CORRECTED)

1. **cq-1 (data source + signature fidelity):** Reads the live session transcript
   from `session_state_store.get()` (Redis-backed, populated by the sandbox's
   `session-state push`), NOT `agent_log_store` (pod stdout). Keys on the FULL
   untruncated `(tool_name, input)` pair — no character limit.

2. **cq-3 (recovery action):** Sets `requires_adjudication=True` — escalates to
   HITL with the looping input quoted verbatim. The operator posts a terminating
   message to the bus, then the agent is respawned with a fresh session.

3. **Metric correction:** Implements novelty counting: counts inputs never issued
   before in the session over a trailing window, fires at zero novelty. Handles
   single-input, 2-, 3-, and 8-cycles uniformly.

#### Priority 2: Two-hour timeout visibility (integrated as-is)

- `agent_timeout_seconds` config field (default 7200s, ge=60)
- `EGG_AGENT_TIMEOUT_SECONDS` env to sandbox
- `active_deadline_seconds` on K8s Job
- Exit 143 (SIGTERM) classified as `JOB_OUTCOME_LEGITIMATE`, not `ABNORMAL`

#### Priority 3: False convergence-stall suppression (integrated as-is + NACK fixes)

- `get_agent_activity_ages()` on HealthMonitor
- `_has_recent_agent_activity()` in event_loop with `_is_brc_idle()` consultation
- Reset stall timers when activity is detected
- `snapshot_from_health_context` enriched with age fields + `tool_calls_by_role`
- `detect_heartbeat_stall` registered in `DetectionPlane.default()`
- `live_container_roles` property on `PipelineHealthContext` maps container IDs
  to role names via the `egg.agent.role` label

#### Priority 4: Alert evidence bundling (NEW)

Enriched all 6 escalation dicts with evidence fields, convergence-stall anomaly
payload, and `_emit_supervision_alert` metadata.

### NACK resolution history

- **v1 NACKs (tester, reviewer_security, reviewer_concurrency):** Fixed by
  registering `detect_heartbeat_stall`, using `session_state_store` instead of
  filesystem, adding `live_container_roles` mapping, consulting `_is_brc_idle`
  in convergence-stall suppression, resetting stall timers.

- **v2 NACKs (reviewer_code_holistic, reviewer_contract, reviewer_security):**
  Fixed by passing `role_names` (not container IDs) to
  `_extract_tool_calls_by_role`, and fixing the `tool_calls_by_role` lookup
  path in `detect_agent_livelock` (was looking at `raw["raw"]["tool_calls_by_role"]`
  but the data is at `raw["tool_calls_by_role"]`).

### Test results

All 1696 related tests pass.

### Standing constraints

The two livelocks from this pipeline's own transcripts are regression fixtures:
- architect, 07:46:20Z: 30/30 identical `grep -n "convergence_stall|..." _consensus_stall.py`
- risk_analyst, 08:41:39Z: 30/30 identical `grep -n "self.tracker" _loop.py`

A correct novelty detector fires on both. The corrected detector uses novelty
counting and handles both cases correctly.
