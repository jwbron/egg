# Issue #3665 — Implementation Plan: Operator Summary

## What this plan does

Three supervision failures are addressed, in three independent task groups
that can be built in any order (though tests depend on the implementation
groups):

1. **Agent livelock/repetition-loop detection** — A deterministic detector
   that analyzes agent log transcripts for zero new unique tool inputs over a
   trailing window.
2. **Two-hour timeout visibility** — Make the 2-hour agent timeout visible and
   non-fatal: configurable via PipelineConfig, passed to the K8s Job and
   sandbox, and classified as a legitimate outcome rather than a crash.
3. **False convergence-stall suppression** — Consult the health monitor's
   per-agent activity data before firing stall alerts, and enrich the
   detection-plane snapshot builder to populate tool-call/heartbeat ages.

## What's already implemented (verified)

All nine items in the issue's "already landed" list are present and verified:

1. Terminating-Job adoption (#3613) — `kubernetes_spawner/_events.py:110`
2. Worktree preservation (#3644+) — `kubernetes_spawner/_worktree.py`
3. Cancel stops driver (#3645+) — `event_loop/_loop.py:1011`
4. Phase-gate approvals (#3648) — `overseer/monitor/_anomaly_checks.py:126`
5. Never-heartbeated roles anchor at Job start (#3612) — `health_monitor.py:248-275`
6. Simplifier's first propose gated on upstream (#3607) — `event_loop/_loop.py:683`
7. Green gate defaults to on (#3609) — `models/_config.py:191`
8. Decoding config recorded (#3611, #3625) — `consensus_wrapper.py`
9. Re-reviews blocking-only (#3661) — `peer_consensus`

Additional items verified present in the tree:

- `JOB_OUTCOME_LEGITIMATE` constant exists at `event_loop/__init__.py:174` —
  the event loop already handles it in `_loop.py:92-95` via
  `supervisor.record_legitimate_outcome()`.
- `detect_heartbeat_stall` detector exists at
  `health_checks/tier1/consensus_stall.py:217` — but is NOT registered in the
  detection plane and its inputs are never populated.
- `agent_log_store` module exists (`orchestrator/agent_log_store.py`) —
  captures pod logs before reaping, with Redis-backed storage and 24h TTL.

## What does NOT exist yet (needs to be built)

- `get_agent_activity_ages()` method on HealthMonitor — needs to be added.
- `_has_recent_agent_activity()` method on the event loop — needs to be added.
- `agent_timeout_seconds` config field on PipelineConfig — needs to be added.
- `EGG_AGENT_TIMEOUT_SECONDS` env var passed to the sandbox — needs wiring.
- `active_deadline_seconds` is hardcoded to 14400 (4h) in `kubernetes_client.py:350`
  — needs to be configurable.
- Exit code 143 (SIGTERM) is only treated as clean during phase transition
  (`kubernetes_monitor.py:532`) — needs to also be clean during RUNNING phase.
- `_failed_with_timeout_sigterm` method on `_EventJobStatusView` — needs to be added.
- `AgentLivelockCheck` class — needs to be created.
- `detect_agent_livelock` function — needs to be created.

## The three task groups

### Task Group 1: Supervision Detectors and Snapshot Enrichment

**Goal:** Create the agent livelock detector, register it in the detection
plane, enrich the snapshot builder with tool-call/heartbeat ages, and register
`detect_heartbeat_stall` — all touching `detection_plane.py` together.

**Tasks:**

- **TASK-1-1** — Create `orchestrator/health_checks/tier1/loop_detection.py`
  with `detect_agent_livelock` function and `AgentLivelockCheck` class.
  Parse Claude Code tool-call lines from `agent_log_store` transcripts. Fire
  when unique tool-input ratio drops below 10% with ≥10 total calls.
  `requires_adjudication=False` (deterministic, no LLM needed).

- **TASK-1-3** — Add `get_agent_activity_ages()` to `HealthMonitor` returning
  per-agent heartbeat/progress/activity ages. Add
  `_has_recent_agent_activity()` to the event loop's convergence-stall check.
  Enrich `snapshot_from_health_context` in `detection_plane.py` to populate
  `last_tool_call_age_s` and `last_heartbeat_age_s` on `RunningAgent` entries.
  Register `detect_heartbeat_stall` in `DetectionPlane.default()`.

### Task Group 2: Two-Hour Timeout Visibility

**Goal:** Make the 2-hour agent timeout visible and non-fatal — configurable
via PipelineConfig, passed to the K8s Job and sandbox, and classified as a
legitimate outcome rather than a crash.

**Tasks:**

- **TASK-1-2** — Add `agent_timeout_seconds` field to `PipelineConfig`
  (default 7200, minimum 60). Pass `EGG_AGENT_TIMEOUT_SECONDS` env to the
  sandbox in `concurrent_executor.py`. Pass `active_deadline_seconds` to the
  K8s Job in `kubernetes_spawner/_spawn.py`. Add
  `_failed_with_timeout_sigterm` to `_EventJobStatusView` and classify exit
  code 143 (SIGTERM) as `JOB_OUTCOME_LEGITIMATE`. Update `_classify_exit` in
  `kubernetes_monitor.py` to treat 143 as clean during RUNNING phase.

### Task Group 4: Tests

**Goal:** Add test coverage for all three changes including corpus rows for
the livelock detector.

**Tasks:**

- **TASK-1-4** — Create `test_loop_detection.py` with known-normal and
  known-bad fixtures. Create `test_event_loop_legitimate_outcome.py` verifying
  exit 143 classification. Create
  `test_convergence_stall_suppression.py` verifying activity-based
  suppression. Add corpus rows to `fixtures.json` for the
  `agent_livelock` detector.

## Dependencies and ordering

- Task Group 1 and Task Group 2 are independent — can be built in parallel.
- Task Group 4 (Tests) depends on Task Group 2.
- The serialized chain order is: Group 1 → Group 2 (Group 1 first, then Group 2).
- All three groups touch `detection_plane.py` in Group 1, so they are
  coordinated to avoid merge conflicts.

## Open questions (HITL — not yet resolved)

Three decisions are registered on the SDLC contract and will be resolved by
the operator before or during implementation:

- **cq-1**: Livelock detector tool-call signature parsing — Should we parse
  the existing Claude Code log format as-is, or add a structured tool-call
  event emitter in the sandbox for more reliable parsing? The plan defaults
  to parsing existing logs (agent_log_store captures full stdout).

- **cq-2**: Two-hour timeout — Should the `agent_timeout_seconds` config be
  pipeline-level only (uniform 7200s default), or support per-role overrides?
  The plan defaults to pipeline-level.

- **cq-3**: Livelock recovery action — When the detector fires, should the
  corrective vocabulary nudge the agent with a loop description, or respawn
  the agent? The plan defaults to nudge (less disruptive).

## What was left out (and why)

- **The overseer poll cycle** — The overseer is NOT deprecated (verified:
  `start()` has no deprecation marker, `overseer_poll_interval_seconds` is
  live). However, the overseer's poll cycle is a separate detection path from
  the orchestrator-side detection plane. The three task groups above address
  the core supervision failures directly. The overseer's phase-duration
  affordance (#3577) and crash-respawn backoff (#3212) are listed as candidates
  in the analysis draft, not as priorities here.
- **The `detect_loop` / `classify_activity_pattern` LLM classifiers** — These
  are Haiku-tier classifiers that run only after an alert is raised. They are
  not the right tool for proactive loop detection. The issue explicitly states
  the empirical finding is about counting unique tool inputs, which is
  deterministic.
- **The 4-hour K8s `active_deadline_seconds`** — This is a K8s-level safety
  net, not an application-level timeout. It is correctly set higher than the
  2-hour agent timeout. The plan makes the agent timeout configurable (Task
  Group 2), but does not change the K8s deadline — that is a separate concern
  listed as a candidate in the analysis draft.

## Files to be created/modified

**New files:**
- `orchestrator/health_checks/tier1/loop_detection.py` (livelock detector)
- `orchestrator/tests/test_loop_detection.py`
- `orchestrator/tests/test_event_loop_legitimate_outcome.py`
- `orchestrator/tests/test_convergence_stall_suppression.py`

**Modified files:**
- `orchestrator/health_checks/detection_plane.py` (register detectors, enrich snapshot)
- `orchestrator/health_checks/tier1/__init__.py` (export new detector)
- `orchestrator/cli.py` (register health check)
- `orchestrator/health_monitor.py` (add `get_agent_activity_ages`)
- `orchestrator/event_loop/_loop.py` (add activity check to convergence-stall)
- `orchestrator/models/_config.py` (add `agent_timeout_seconds` field)
- `orchestrator/concurrent_executor.py` (pass `EGG_AGENT_TIMEOUT_SECONDS` env)
- `orchestrator/kubernetes_spawner/_spawn.py` (pass `active_deadline_seconds`)
- `orchestrator/kubernetes_spawner/_models.py` (add `_failed_with_timeout_sigterm`)
- `orchestrator/kubernetes_monitor.py` (update `_classify_exit`)
- `orchestrator/tests/overseer_calibration/fixtures.json` (corpus rows)
