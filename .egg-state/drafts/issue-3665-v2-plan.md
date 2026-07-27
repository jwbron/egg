# Task Planner Proposal: Supervision Layer, Second Pass (#3665)

## Summary

The supervision layer was silent on seven livelocks and loud at healthy agents. Three root causes:
1. **No livelock detection** — agents in repetition loops (same tool call or short cycle repeated) were not detected by any in-product signal.
2. **Two-hour timeout invisibility** — agents killed at exactly 2 hours by `active_deadline_seconds` were counted as crashes against the fail-streak budget, with no visible timeout signal.
3. **False convergence-stall alerts** — busy agents with recent heartbeats/container activity were flagged as stalled because the event loop's convergence-stall check didn't consult the health monitor's per-agent activity data.

## What's Already Implemented (Verified in Tree)

### Already Shipped (per issue's "What has already landed" list)
- ✅ Terminating-Job adoption on event-loop respawn path (#3613)
- ✅ Worktree uncommitted work preservation on re-attach (#3644, #3647, #3652, #3654, #3656, #3660)
- ✅ Cancel stops the driver (#3645, #3649, #3655, #3657)
- ✅ Phase-gate approvals parse on first line (#3648)
- ✅ Never-heartbeated roles anchor at Job start (#3612)
- ✅ Simplifier's first propose is gated on its upstream producer (#3607)
- ✅ Green gate defaults to on (#3609), red escalates to HITL (#3628)
- ✅ Every routed call records decoding config (#3611, #3625)
- ✅ Re-reviews are blocking-only (#3661)

### Already Present in the Tree (Not to be Rebuilt)
- **`JOB_OUTCOME_LEGITIMATE`** constant exists in `orchestrator/event_loop/__init__.py:174` — the event loop already handles it in `_loop.py:92-95` via `supervisor.record_legitimate_outcome()`.
- **`detect_heartbeat_stall`** detector exists in `orchestrator/health_checks/tier1/consensus_stall.py:217` — but is **NOT registered** in `DetectionPlane.default()` and its required inputs (`last_tool_call_age_s`, `last_heartbeat_age_s`) are **never populated** by `snapshot_from_health_context`.
- **`agent_log_store`** module exists (`orchestrator/agent_log_store.py`) — captures pod logs before reaping, with Redis-backed storage and 24h TTL.
- **`agent_timeout_seconds`** config field does NOT exist on PipelineConfig — needs to be added.
- **`active_deadline_seconds`** is hardcoded to 14400 (4h) in `kubernetes_client.py:350` — needs to be configurable.
- **Exit code 143 (SIGTERM)** is only treated as clean during phase transition (`kubernetes_monitor.py:532`) — needs to also be clean during RUNNING phase (sandbox timeout).
- **`AgentLivelockCheck`** class does NOT exist — needs to be created.
- **`detect_agent_livelock`** function does NOT exist — needs to be created.

### Already Implemented in Commit 6ffe97c8e (issue-3665-supervision-gaps branch)
Commit `6ffe97c8e` on the `issue-3665-supervision-gaps` branch already implements all three supervision fixes (17 files, 1072 insertions). The plan integrates this commit rather than reimplementing it. The coder's task is to VERIFY the integration is correct and complete, and address any gaps identified by reviewers.

## Proposed Work

### Task 1: Integrate Supervision-Layer Fixes from Commit 6ffe97c8e (task-1-1, task-1-3)
**Source:** Commit `6ffe97c8e` on `issue-3665-supervision-gaps` branch

Integrate the three supervision-layer fixes that already exist in commit `6ffe97c8e`:

1. **Agent livelock/repetition-loop detection** — `orchestrator/health_checks/tier1/loop_detection.py` (new, 317 lines):
   - `detect_agent_livelock` function: parses Claude Code tool-call lines from `agent_log_store` transcripts, fires when unique tool-input ratio drops below 10% with ≥10 total calls. `requires_adjudication=False`.
   - `AgentLivelockCheck` class: Tier 1 HealthCheck wrapper, registered in `cli.py`.
   - Registered in `DetectionPlane.default()` via `_register_coverage_gap_detectors`.
   - Exported from `tier1/__init__.py`.

2. **Two-hour timeout visibility** — modifies 5 files:
   - `orchestrator/models/_config.py`: adds `agent_timeout_seconds: int = Field(default=7200, ge=60)`.
   - `orchestrator/concurrent_executor.py`: passes `EGG_AGENT_TIMEOUT_SECONDS` env to sandbox.
   - `orchestrator/kubernetes_spawner/_spawn.py`: passes `active_deadline_seconds` to K8s Job.
   - `orchestrator/kubernetes_spawner/_models.py`: adds `_failed_with_timeout_sigterm`, classifies exit 143 as `JOB_OUTCOME_LEGITIMATE`.
   - `orchestrator/kubernetes_monitor.py`: updates `_classify_exit` to treat 143 as clean during RUNNING phase.
   - `sandbox/llm/claude/config.py`: uses `EGG_AGENT_TIMEOUT_SECONDS` for the ClaudeConfig timeout.

3. **False convergence-stall suppression** — modifies 3 files:
   - `orchestrator/health_monitor.py`: adds `get_agent_activity_ages()` returning per-agent heartbeat/progress/activity ages.
   - `orchestrator/event_loop/_loop.py`: adds `_has_recent_agent_activity()` to convergence-stall check, suppresses alert when agent has recent activity.
   - `orchestrator/health_checks/detection_plane.py`: enriches `snapshot_from_health_context` to populate `last_tool_call_age_s` and `last_heartbeat_age_s` on `RunningAgent` entries; registers `detect_heartbeat_stall` in `DetectionPlane.default()`.

**Files to verify/integrate:**
- `orchestrator/health_checks/tier1/loop_detection.py` (new)
- `orchestrator/health_checks/detection_plane.py` (modified)
- `orchestrator/health_checks/tier1/__init__.py` (modified)
- `orchestrator/cli.py` (modified)
- `orchestrator/models/_config.py` (modified)
- `orchestrator/concurrent_executor.py` (modified)
- `orchestrator/kubernetes_spawner/_spawn.py` (modified)
- `orchestrator/kubernetes_spawner/_models.py` (modified)
- `orchestrator/kubernetes_monitor.py` (modified)
- `orchestrator/health_monitor.py` (modified)
- `orchestrator/event_loop/_loop.py` (modified)
- `orchestrator/event_loop/__init__.py` (modified)
- `sandbox/llm/claude/config.py` (modified)

### Task 2: Verify Test Coverage (task-1-4)
**Source:** Commit `6ffe97c8e` includes 4 test files

Verify the test coverage from commit `6ffe97c8e`:
- `orchestrator/tests/test_loop_detection.py` (210 lines) — unit tests for `detect_agent_livelock`.
- `orchestrator/tests/test_agent_timeout_config.py` (36 lines) — tests for `agent_timeout_seconds` config.
- `orchestrator/tests/test_convergence_stall_suppression.py` (127 lines) — tests for activity-based suppression.
- `orchestrator/tests/test_timeout_sigterm.py` (148 lines) — tests for exit 143 classification.

**Files to verify:**
- `orchestrator/tests/test_loop_detection.py` (new)
- `orchestrator/tests/test_agent_timeout_config.py` (new)
- `orchestrator/tests/test_convergence_stall_suppression.py` (new)
- `orchestrator/tests/test_timeout_sigterm.py` (new)

## Candidate List (Deliverable — Not Obligated)

| # | Improvement | File/Symbol | Present? |
|---|------------|-------------|----------|
| 1 | **Tool-call signature tracking in pod logs** — The livelock detector parses `"> tool_name args"` lines, but Claude Code's log format may vary. A structured tool-call event emitter (like the existing `tool_use` logger in `client.py:808`) could write to a dedicated stream that's more reliable to parse. | `shared/egg_agent/client.py:808` (tool_use logging) | Partially present (logs exist, not structured for parsing) |
| 2 | **Per-agent timeout configuration** — `agent_timeout_seconds` is pipeline-level. Different roles (coder vs. overseer) may need different timeouts. Consider role-scoped overrides. | `orchestrator/models/_config.py` | Absent (pipeline-level only) |
| 3 | **Timeout warning emission** — The sandbox should emit a warning heartbeat at 90% of the timeout so the health monitor can surface "agent approaching timeout" before the SIGTERM fires. | `shared/egg_agent/client.py` (timeout handling) | Absent |
| 4 | **Livelock recovery action** — The detector fires `requires_adjudication=False`, so the bounded corrective vocabulary (slice-6) handles it. But what corrective action is available? A "nudge agent with loop description" or "respawn agent" action needs to be defined in the corrective executor. | `orchestrator/health_checks/runner.py` (corrective actions) | Absent |
| 5 | **Exit-code 143 provenance annotation** — When a 143 is classified as legitimate, the `exit_detail` should distinguish "sandbox timeout" from "orchestrator teardown SIGTERM" so operators can tell which path fired. | `orchestrator/kubernetes_spawner/_models.py:exit_detail_for` | Partially present (annotated as "likely sandbox timeout or orchestrator teardown") |
| 6 | **Heartbeat-stall detector registration** — `detect_heartbeat_stall` exists in `consensus_stall.py` but is not registered in `DetectionPlane.default()`. The snapshot enrichment (task 3) makes it usable, but it still needs explicit registration. | `orchestrator/health_checks/detection_plane.py:_register_coverage_gap_detectors` | Addressed by commit 6ffe97c8e (registers detect_heartbeat_stall) |
| 7 | **Agent log retention policy** — `agent_log_store` uses a 24h TTL. For long-running pipelines, logs from early agents may expire before incident response. Consider a pipeline-level retention override. | `orchestrator/agent_log_store.py:AGENT_LOG_TTL_SECONDS` | Present (24h default, no override) |
| 8 | **Livelock window tunability** — The 300s window and 10% unique-ratio threshold are hardcoded. These should be config-driven for different pipeline types. | `orchestrator/health_checks/tier1/loop_detection.py` | Present (configurable via constructor params) |
| 9 | **Convergence-stall suppression for reviewers** — The `_has_recent_agent_activity` check applies to all roles. Reviewers legitimately wait on producers; their activity pattern differs. Consider role-specific suppression logic. | `orchestrator/event_loop/_loop.py:_check_convergence_stall` | Absent (uniform suppression) |
| 10 | **Two-hour timeout config validation** — `agent_timeout_seconds` defaults to 7200 but the K8s `active_deadline_seconds` default is 14400. If an operator sets a custom timeout, both must stay in sync. Consider a validation that warns when they diverge. | `orchestrator/kubernetes_client.py:350` (hardcoded 14400) | Absent |

## Open Questions

The following decisions are registered on the SDLC contract (cq-1, cq-2, cq-3) and will be resolved by the operator before or during implementation:

- **cq-1**: Livelock detector tool-call signature parsing: Should we parse the existing Claude Code log format as-is, or add a structured tool-call event emitter in the sandbox for more reliable parsing? The plan defaults to parsing existing logs (agent_log_store captures full stdout).
- **cq-2**: Two-hour timeout: Should the agent_timeout_seconds config be pipeline-level only (uniform 7200s default), or support per-role overrides? The plan defaults to pipeline-level.
- **cq-3**: Livelock recovery action: When the detector fires, should the corrective vocabulary nudge the agent with a loop description, or respawn the agent? The plan defaults to nudge (less disruptive).

```yaml
# yaml-tasks
pr:
  title: "Supervision layer second pass: integrate livelock detection, timeout visibility, false-stall suppression"
  description: |
    Integrates commit 6ffe97c8e from the issue-3665-supervision-gaps branch,
    which implements all three supervision fixes: (1) agent livelock/repetition-loop
    detection via a deterministic detector that analyzes agent log transcripts for
    zero new unique tool inputs over a trailing window; (2) two-hour timeout
    visibility — add agent_timeout_seconds config, pass it through the spawn path,
    classify exit 143 (SIGTERM) as a legitimate outcome rather than a crash;
    (3) false convergence-stall suppression — consult health monitor per-agent
    activity data before firing stall alerts, and enrich the detection-plane
    snapshot builder to populate tool-call/heartbeat ages.
phases:
  - id: 1
    name: Integrate Supervision-Layer Fixes
    goal: Integrate commit 6ffe97c8e which implements all three supervision fixes — livelock detection, two-hour timeout visibility, and false convergence-stall suppression. Verify the integration is correct and complete.
    tasks:
      - id: TASK-1-1
        description: Integrate orchestrator/health_checks/tier1/loop_detection.py (detect_agent_livelock + AgentLivelockCheck). Verify registration in DetectionPlane.default(), tier1/__init__.py, and cli.py. Verify the detector parses Claude Code tool-call lines from agent_log_store transcripts and fires on zero new unique tool inputs.
        acceptance: loop_detection.py exists and is correct; detect_agent_livelock returns Finding on livelock, None on normal; AgentLivelockCheck implements HealthCheck protocol; registered in DetectionPlane.default() and tier1/__init__.py; registered in cli.py health check runner; test_loop_detection.py passes
        files:
          - orchestrator/health_checks/tier1/loop_detection.py
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/health_checks/tier1/__init__.py
          - orchestrator/cli.py
      - id: TASK-1-3
        description: Integrate health_monitor.py get_agent_activity_ages(), event_loop/_loop.py _has_recent_agent_activity(), and detection_plane.py snapshot enrichment for last_tool_call_age_s/last_heartbeat_age_s. Verify detect_heartbeat_stall registration in DetectionPlane.default().
        acceptance: get_agent_activity_ages returns correct ages; convergence-stall check consults activity before firing; snapshot builder populates tool-call/heartbeat ages; detect_heartbeat_stall registered and fires in live path; test_convergence_stall_suppression.py passes
        files:
          - orchestrator/health_monitor.py
          - orchestrator/event_loop/_loop.py
          - orchestrator/health_checks/detection_plane.py
    dependencies: []
  - id: 2
    name: Two-Hour Timeout Visibility
    goal: Integrate agent_timeout_seconds config, EGG_AGENT_TIMEOUT_SECONDS env passing, active_deadline_seconds on K8s Job, and exit 143 classification as JOB_OUTCOME_LEGITIMATE.
    tasks:
      - id: TASK-1-2
        description: Integrate agent_timeout_seconds field in PipelineConfig. Pass EGG_AGENT_TIMEOUT_SECONDS to sandbox in concurrent_executor.py. Pass active_deadline_seconds to K8s Job in kubernetes_spawner/_spawn.py. Add _failed_with_timeout_sigterm to _EventJobStatusView and classify exit 143 as JOB_OUTCOME_LEGITIMATE. Update _classify_exit in kubernetes_monitor.py to treat 143 as clean during RUNNING phase.
        acceptance: agent_timeout_seconds config field exists and serializes; EGG_AGENT_TIMEOUT_SECONDS passed to sandbox; active_deadline_seconds passed to K8s Job; exit 143 classified as JOB_OUTCOME_LEGITIMATE not ABNORMAL; _classify_exit treats 143 as clean; test_agent_timeout_config.py and test_timeout_sigterm.py pass
        files:
          - orchestrator/models/_config.py
          - orchestrator/concurrent_executor.py
          - orchestrator/kubernetes_spawner/_spawn.py
          - orchestrator/kubernetes_spawner/_models.py
          - orchestrator/kubernetes_monitor.py
          - sandbox/llm/claude/config.py
    dependencies: []
  - id: 4
    name: Tests
    goal: Verify test coverage for all three changes
    tasks:
      - id: TASK-1-4
        description: Verify test_loop_detection.py, test_agent_timeout_config.py, test_convergence_stall_suppression.py, and test_timeout_sigterm.py all pass. These tests were included in commit 6ffe97c8e.
        acceptance: All four test files exist and pass; test coverage is complete for all three supervision fixes
        files:
          - orchestrator/tests/test_loop_detection.py
          - orchestrator/tests/test_agent_timeout_config.py
          - orchestrator/tests/test_convergence_stall_suppression.py
          - orchestrator/tests/test_timeout_sigterm.py
    dependencies: ["2"]
    serialized_chain_order: ["1", "2"]
```
