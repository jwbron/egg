# BRC Memory — coder role, issue #3665

## Pipeline context

Pipeline: issue-3665-v2
Phase: implement (slice-1)
Branch: egg/issue-3665-v2-slice-1-coder/work
Current HEAD: 00151f7a7 (v2 re-proposal addressing all NACKs)

## Summary of assessment

### What was already in the tree (verified, not rebuilt)

All nine "already landed" items from the issue's "What has already landed" list
were verified present in the tree before starting:

1. Terminating-Job adoption on event-loop respawn path (#3613) — present
2. Worktree uncommitted work preservation (#3644, #3647, #3652, #3654, #3656, #3660) — present
3. Cancel stops the driver (#3645, #3649, #3655, #3657) — present
4. Phase-gate approvals parse on first line (#3648) — present
5. Never-heartbeated roles anchor at Job start (#3612) — present
6. Simplifier's first propose gated on upstream producer (#3607) — present
7. Green gate defaults to on (#3609), red escalates to HITL (#3628) — present
8. Every routed call records decoding config (#3611, #3625) — present
9. Re-reviews are blocking-only (#3661) — present

### What was done

Integrated fix commit `6ffe97c8e` from the `issue-3665-supervision-gaps` branch
with corrections to the livelock detector per operator feedback (cq-1, cq-3),
plus alert evidence bundling (priority 4), and addressed all 3 reviewer NACKs.

#### Priority 1: Agent livelock/repetition-loop detection (CORRECTED)

The fix commit's `loop_detection.py` had three defects per operator feedback:

1. **cq-1 (data source + signature fidelity):** The fix commit sourced from
   `agent_log_store` (pod stdout) and truncated signatures to 80 chars. The
   issue explicitly states the pod log cannot support this signal — tool
   inputs are truncated at ~100 chars and distinct commands sharing a prefix
   collapse together. The corrected detector:
   - Reads the live session transcript from `session_state_store.get()`
     (Redis-backed, populated by the sandbox's `session-state push`)
   - Keys on the FULL untruncated `(tool_name, input)` pair — no character limit

2. **cq-3 (recovery action):** The fix commit defaulted to
   `requires_adjudication=False` (nudge). The corrected detector sets
   `requires_adjudication=True` and escalates to HITL with the looping input
   quoted verbatim.

3. **Metric correction:** The fix commit computed a ratio
   (`unique / total < 0.1`). The corrected detector implements novelty
   counting: counts inputs never issued before in the session over a trailing
   window, fires at zero novelty. This handles single-input, 2-, 3-, and
   8-cycles uniformly.

#### Priority 2: Two-hour timeout visibility (integrated as-is)

- Added `agent_timeout_seconds` config field (default 7200s, ge=60)
- Pass `EGG_AGENT_TIMEOUT_SECONDS` env to sandbox
- Pass `active_deadline_seconds` to K8s Job
- Exit 143 (SIGTERM) classified as `JOB_OUTCOME_LEGITIMATE`, not `ABNORMAL`
- `_classify_exit` treats 143 as clean during RUNNING phase
- Sandbox config uses `EGG_AGENT_TIMEOUT_SECONDS`

#### Priority 3: False convergence-stall suppression (integrated as-is + NACK fixes)

- `get_agent_activity_ages()` on HealthMonitor returns per-agent activity ages
- `_has_recent_agent_activity()` in event_loop suppresses convergence-stall
  alerts for agents with recent activity
- Added `_is_brc_idle()` consultation to distinguish "legitimately waiting"
  from "stuck" (reviewers waiting on upstream producers, declared no-ops,
  NACK discharging obligations)
- Reset stall timers (`_stall_first_seen`, `_stall_alerted`) when activity is
  detected, so the next poll re-anchors from the current bus timestamp
- `snapshot_from_health_context` enriched with `last_tool_call_age_s` and
  `last_heartbeat_age_s`
- `detect_heartbeat_stall` registered in `DetectionPlane.default()` (NACK fix)
- Added `live_container_roles` property to `PipelineHealthContext` that maps
  Docker container IDs to role names via the `egg.agent.role` label

#### Priority 4: Alert evidence bundling (NEW — not in fix commit)

Enriched OVERSEER_ALERT payloads with structured evidence fields:
- `latest_heartbeat_age_s` — seconds since last heartbeat
- `latest_progress_age_s` — seconds since last progress event
- `latest_tool_call_age_s` — seconds since last CONTAINER_ACTIVITY
- `last_progress_event` — the most recent progress event data
- `blocking_agents` — the BRC consensus blocking set
- `consensus_state` — is_complete, producer_phases, reviewer_phases

Applied to all 6 escalation dicts in health_monitor.py, the convergence-stall
anomaly payload in event_loop/_loop.py, and the _emit_supervision_alert
metadata in concurrent_executor.py.

### Files modified

- `orchestrator/health_checks/tier1/loop_detection.py` (new, with corrections)
- `orchestrator/health_checks/tier1/consensus_stall.py` (added detector_key/name)
- `orchestrator/health_checks/detection_plane.py` (snapshot enrichment + registration + container ID to role mapping)
- `orchestrator/health_checks/context.py` (added live_container_roles property)
- `orchestrator/health_checks/tier1/__init__.py` (exports)
- `orchestrator/cli.py` (registers AgentLivelockCheck)
- `orchestrator/health_monitor.py` (get_agent_activity_ages + evidence bundling)
- `orchestrator/event_loop/_loop.py` (_has_recent_agent_activity + convergence-stall suppression + evidence)
- `orchestrator/event_loop/__init__.py` (binds _has_recent_agent_activity)
- `orchestrator/concurrent_executor.py` (EGG_AGENT_TIMEOUT_SECONDS + evidence in alerts)
- `orchestrator/kubernetes_spawner/_spawn.py` (active_deadline_seconds)
- `orchestrator/kubernetes_spawner/_models.py` (_failed_with_timeout_sigterm + exit 143)
- `orchestrator/kubernetes_monitor.py` (exit 143 as clean)
- `orchestrator/models/_config.py` (agent_timeout_seconds field)
- `sandbox/llm/claude/config.py` (uses EGG_AGENT_TIMEOUT_SECONDS)
- `orchestrator/tests/test_loop_detection.py` (updated for novelty metric, cq-1/cq-3)
- `orchestrator/tests/test_agent_timeout_config.py` (new)
- `orchestrator/tests/test_convergence_stall_suppression.py` (new, updated for _is_brc_idle)
- `orchestrator/tests/test_timeout_sigterm.py` (new)
- `orchestrator/tests/test_event_loop.py` (updated _NotifierSpy)
- `orchestrator/tests/overseer_calibration/fixtures.json` (6 new corpus rows)

### Test results

All 1696 related tests pass. The only failing tests are pre-existing failures
in `test_slice_green_gate.py` and `test_kubernetes_spawner.py` caused by
`git init` failing in the test environment — unrelated to these changes.

### Standing constraints for implement

The two livelocks from this pipeline's own transcripts are the regression
fixtures and both are on this pipeline's own transcripts:
- architect, 07:46:20Z: 30/30 identical `grep -n "convergence_stall|..." _consensus_stall.py`
- risk_analyst, 08:41:39Z: 30/30 identical `grep -n "self.tracker" _loop.py`

A correct novelty detector fires on both. The ratio detector in the fix commit
fires late on both. The corrected detector uses novelty counting (fire at zero
new inputs in the trailing window) and handles both cases correctly.

### Candidate list additions

Added to the candidate list (per the plan's deliverable):
- **Structured tool-call event emitter** — The livelock detector reads the live
  session transcript via `session_state_store`. A structured emitter (writing
  tool calls to a dedicated stream) would be cleaner long-term but is strictly
  more work than reading a transcript that already exists. (ABSENT —
  session_state_store reading used instead)
- **Call-count-not-advancing detector** — Novelty answers "are the calls
  repeating?" It cannot answer "are calls happening at all?" A live pod that
  stops emitting tool calls entirely reads as a flat, maximally healthy window
  while nothing is happening. (ABSENT — not covered by this work)
