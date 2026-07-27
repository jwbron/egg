# Task Planner Proposal: Supervision Layer, Second Pass (#3665) — v3

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
- ✅ Phase-gate approvals parse on their first line (#3648)
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

### Already Implemented in Commit 68b185ca (issue-3665-supervision-gaps branch)
Commit `68b185ca` (tip of `issue-3665-supervision-gaps` branch, verified at integration time) implements all three supervision fixes (17 files, 1072 insertions). The plan integrates this commit, with corrections to the livelock detector per operator feedback (cq-1, cq-3).

**Corrections needed to the livelock detector per operator feedback:**
- **cq-1**: The commit sources from `agent_log_store` (pod stdout) and truncates signatures to 80 chars. The issue explicitly says the pod log cannot support this signal. The detector must read the live session transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl` inside the running pod, and key on the FULL untruncated `(tool_name, input)` pair.
- **cq-3**: The commit defaults to nudge recovery. The operator resolved cq-3 as respawn: a two-step process (1) post a terminating message to the bus, then (2) respawn with a fresh session. The detector must escalate to HITL with the looping input quoted verbatim.
- **Metric correction**: The commit uses a ratio (unique/total) but the issue specifies counting "inputs never issued before in the session" over a trailing window, firing at zero novelty.

## Proposed Work

### Task 1: Integrate Supervision-Layer Fixes with Corrections (task-1-1, task-1-3)
**Source:** Commit `68b185ca` on `issue-3665-supervision-gaps` branch (verified at integration time)

Integrate the three supervision-layer fixes, with corrections to the livelock detector per operator feedback:

1. **Agent livelock/repetition-loop detection** — `orchestrator/health_checks/tier1/loop_detection.py` (new, 317 lines):
   - **CORRECTION per cq-1**: Read the live session transcript at `$HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl` inside the running pod, NOT `agent_log_store` (pod stdout). Key on the FULL untruncated `(tool_name, input)` pair — no character limit.
   - **CORRECTION per issue**: Implement novelty metric — count inputs never issued before IN THE SESSION over a trailing window, fire at zero. NOT a ratio (unique/total).
   - **CORRECTION per cq-3**: Recovery is a two-step process: (1) post a terminating message to the bus, then (2) respawn with a fresh session. The detector escalates to HITL with the looping input quoted verbatim, since it cannot know the answer to the agent's question.
   - `requires_adjudication=False` (deterministic detection), but the corrective action escalates to HITL.
   - Registered in `DetectionPlane.default()` via `_register_coverage_gap_detectors`.
   - Exported from `tier1/__init__.py`.
   - Registered in `cli.py` health check runner.

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
- `orchestrator/health_checks/tier1/loop_detection.py` (new, with corrections)
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

### Task 2: Two-Hour Timeout Visibility (task-1-2)
Same as task 1 above — see files list.

### Task 3: Alert Evidence Bundling (task-1-4) — RESTORED
**Priority 4 from the issue's "What to propose" section.**

Enrich OVERSEER_ALERT payloads with structured evidence so operators can act without hand-investigation:
- `latest_heartbeat_age_s` — seconds since last heartbeat
- `latest_tool_call_age_s` — seconds since last tool call
- `last_progress_event` — the most recent progress event data
- `blocking_agents` — the BRC consensus blocking set
- `consensus_state` — the current BRC consensus matrix state

The overseer already fetches container logs separately at `_poll.py:78-85`, so most of the data is in hand. This is what makes the other three fixes usable: a livelock alert that does not carry the repeated input and the ages is an alert an operator has to investigate by hand.

**Files to modify:**
- `orchestrator/health_monitor.py` — enrich escalation dicts with evidence fields
- `orchestrator/event_loop/_loop.py` — enrich convergence-stall anomaly payloads
- `orchestrator/overseer/monitor/_alerting.py` — enrich OVERSEER_ALERT payloads

### Task 4: Tests (task-1-5)
Verify test coverage from commit `68b185ca` and update tests for the corrected livelock detector:
- `orchestrator/tests/test_loop_detection.py` — update to test novelty metric (not ratio), live session transcript parsing (not agent_log_store), and HITL escalation (not nudge).
- `orchestrator/tests/test_agent_timeout_config.py` — verify agent_timeout_seconds config.
- `orchestrator/tests/test_convergence_stall_suppression.py` — verify activity-based suppression.
- `orchestrator/tests/test_timeout_sigterm.py` — verify exit 143 classification.

## What Was Left Out (and Why)

- **Per-agent timeout configuration** (candidate #2): The operator resolved cq-2 as pipeline-level only. Per-role overrides are a real follow-up but not in scope for this work.
- **Timeout warning emission** (candidate #3): The operator's cq-2 resolution notes the agent must be able to SEE the deadline. `EGG_AGENT_TIMEOUT_SECONDS` reaching the sandbox is necessary but not sufficient; the remaining budget must reach the agent's prompt or a tool it can call. This is a follow-up — the current work makes the timeout visible and non-fatal, which is the core ask.
- **Agent log retention policy** (candidate #7): The livelock detector now reads from the live session transcript, not `agent_log_store`, so the 24h TTL is no longer a concern for detection.
- **Convergence-stall suppression for reviewers** (candidate #9): The `_has_recent_agent_activity` check applies to all roles. Reviewers legitimately wait on producers; their activity pattern differs. This is a follow-up — the current work suppresses false alerts against busy agents, which is the core ask.
- **Two-hour timeout config validation** (candidate #10): The K8s `active_deadline_seconds` default (14400) remains as the outer safety net per cq-2 resolution. No validation needed — the operator explicitly chose this.

## Candidate List (Deliverable — Not Obligated)

| # | Improvement | File/Symbol | Present? |
|---|------------|-------------|----------|
| 1 | **Structured tool-call event emitter** — The livelock detector reads the live session transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl`. A structured emitter (writing tool calls to a dedicated stream) would be cleaner long-term but is strictly more work than reading a transcript that already exists. | `shared/egg_agent/client.py:808` (tool_use logging) | Absent (transcript reading used instead) |
| 2 | **Per-agent timeout configuration** — `agent_timeout_seconds` is pipeline-level. Different roles (coder vs. overseer) may need different timeouts. Consider role-scoped overrides. | `orchestrator/models/_config.py` | Absent (pipeline-level only, per cq-2) |
| 3 | **Timeout warning emission** — The sandbox should emit a warning heartbeat at 90% of the timeout so the health monitor can surface "agent approaching timeout" before the SIGTERM fires. The agent must also be able to SEE the deadline in its prompt or a tool. | `shared/egg_agent/client.py` (timeout handling) | Absent |
| 4 | **Livelock recovery action** — The detector fires and escalates to HITL with the looping input quoted verbatim. An operator supplies the terminating answer, then the agent is respawned with a fresh session. A fully autonomous recovery path (without operator input) is not shipped because the detector cannot know the answer to the agent's question. | `orchestrator/health_checks/runner.py` (corrective actions) | Absent (HITL escalation shipped) |
| 5 | **Exit-code 143 provenance annotation** — When a 143 is classified as legitimate, the `exit_detail` should distinguish "sandbox timeout" from "orchestrator teardown SIGTERM" so operators can tell which path fired. | `orchestrator/kubernetes_spawner/_models.py:exit_detail_for` | Partially present (annotated as "likely sandbox timeout or orchestrator teardown") |
| 6 | **Heartbeat-stall detector registration** — `detect_heartbeat_stall` exists in `consensus_stall.py` but is not registered in `DetectionPlane.default()`. The snapshot enrichment makes it usable. | `orchestrator/health_checks/detection_plane.py:_register_coverage_gap_detectors` | Addressed by commit 68b185ca (registers detect_heartbeat_stall) |
| 7 | **Agent log retention policy** — `agent_log_store` uses a 24h TTL. The livelock detector now reads from the live session transcript, so this is no longer a concern for detection. | `orchestrator/agent_log_store.py:AGENT_LOG_TTL_SECONDS` | Present (24h default, not used by livelock detector) |
| 8 | **Livelock window tunability** — The 300s window is configurable via constructor params on `AgentLivelockCheck`. | `orchestrator/health_checks/tier1/loop_detection.py` | Present (configurable via constructor params) |
| 9 | **Convergence-stall suppression for reviewers** — The `_has_recent_agent_activity` check applies to all roles. Reviewers legitimately wait on producers; their activity pattern differs. Consider role-specific suppression logic. | `orchestrator/event_loop/_loop.py:_check_convergence_stall` | Absent (uniform suppression) |
| 10 | **Two-hour timeout config validation** — `agent_timeout_seconds` defaults to 7200 but the K8s `active_deadline_seconds` default is 14400. Per cq-2, the 4h K8s deadline is kept as the outer safety net. | `orchestrator/kubernetes_client.py:350` (hardcoded 14400) | Present (kept as outer safety net per cq-2) |

## Open Questions

The following decisions are registered on the SDLC contract (cq-1, cq-2, cq-3) and have been resolved:

- **cq-1** (resolved): The livelock detector must NOT source from `agent_log_store` and must NOT truncate the signature. Read the live session transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl` inside the running pod, and key on the FULL untruncated `(tool_name, input)` pair.
- **cq-2** (resolved): Pipeline-level only. Ship the uniform 7200s default and make it configurable; do not build per-role overrides. The agent must be able to SEE the deadline. Keep the 4h K8s `active_deadline_seconds` as the outer safety net.
- **cq-3** (resolved): Recovery is a two-step process: (1) post a terminating message to the bus, then (2) respawn with a fresh session. The detector escalates to HITL with the looping input quoted verbatim, since it cannot know the answer to the agent's question.

```yaml
# yaml-tasks
pr:
  title: "Supervision layer second pass: integrate fix commit 68b185ca with livelock corrections + restore priority 4"
  description: |
    Integrates commit 68b185ca from the issue-3665-supervision-gaps branch,
    with corrections to the livelock detector per operator feedback (cq-1,
    cq-3): reads live session transcripts instead of pod logs, uses novelty
    metric instead of ratio, and escalates to HITL with respawn instead of
    nudge. Also restores priority 4 (alert evidence bundling) that was
    dropped in v2.
phases:
  - id: 1
    name: Integrate Supervision-Layer Fixes with Corrections
    goal: Integrate commit 68b185ca which implements all three supervision fixes, with corrections to the livelock detector per cq-1/cq-3 (live session transcript, novelty metric, HITL escalation with respawn)
    tasks:
      - id: TASK-1-1
        description: "Integrate orchestrator/health_checks/tier1/loop_detection.py with CORRECTIONS per cq-1/cq-3: read live session transcript at $HOME/.claude/projects/<cwd>/<session>.jsonl (NOT agent_log_store), key on FULL untruncated (tool_name, input) pair (NOT 80-char truncation), implement novelty metric (count of inputs never issued before in session, fire at zero — NOT ratio), escalate to HITL with looping input quoted (NOT nudge). Verify registration in DetectionPlane.default(), tier1/__init__.py, and cli.py."
        acceptance: "loop_detection.py reads live session transcript; keys on full untruncated (tool_name, input); implements novelty metric (zero new inputs = livelock); escalates to HITL with looping input quoted; registered in DetectionPlane.default() and tier1/__init__.py; registered in cli.py; test_loop_detection.py passes"
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
        description: Integrate agent_timeout_seconds field in PipelineConfig (default 7200, ge=60). Pass EGG_AGENT_TIMEOUT_SECONDS to sandbox in concurrent_executor.py. Pass active_deadline_seconds to K8s Job in kubernetes_spawner/_spawn.py. Add _failed_with_timeout_sigterm to _EventJobStatusView and classify exit 143 as JOB_OUTCOME_LEGITIMATE. Update _classify_exit in kubernetes_monitor.py to treat 143 as clean during RUNNING phase. Update sandbox/llm/claude/config.py to use EGG_AGENT_TIMEOUT_SECONDS.
        acceptance: agent_timeout_seconds config field exists and serializes; EGG_AGENT_TIMEOUT_SECONDS passed to sandbox; active_deadline_seconds passed to K8s Job; exit 143 classified as JOB_OUTCOME_LEGITIMATE not ABNORMAL; _classify_exit treats 143 as clean; sandbox config uses EGG_AGENT_TIMEOUT_SECONDS; test_agent_timeout_config.py and test_timeout_sigterm.py pass
        files:
          - orchestrator/models/_config.py
          - orchestrator/concurrent_executor.py
          - orchestrator/kubernetes_spawner/_spawn.py
          - orchestrator/kubernetes_spawner/_models.py
          - orchestrator/kubernetes_monitor.py
          - sandbox/llm/claude/config.py
    dependencies: []
  - id: 5
    name: Alert Evidence Bundling
    goal: Restore priority 4 — enrich OVERSEER_ALERT payloads with structured evidence (heartbeat/tool-call ages, progress events, blocking agents, consensus state) so operators can act without hand-investigation.
    tasks:
      - id: TASK-1-4
        description: Enrich OVERSEER_ALERT payloads with latest_heartbeat_age_s, latest_tool_call_age_s, last_progress_event, blocking_agents, consensus_state. The overseer already fetches container logs at _poll.py:78-85, so most data is in hand. Modify health_monitor.py escalation dicts, event_loop/_loop.py convergence-stall anomaly payloads, and overseer/monitor/_alerting.py OVERSEER_ALERT payloads.
        acceptance: OVERSEER_ALERT payloads carry structured evidence fields; convergence-stall alerts include agent activity ages; livelock alerts include the looping input; unit tests pass
        files:
          - orchestrator/health_monitor.py
          - orchestrator/event_loop/_loop.py
          - orchestrator/overseer/monitor/_alerting.py
    dependencies: ["1"]
  - id: 6
    name: Tests
    goal: Verify test coverage for all changes, with corrected livelock detector tests
    tasks:
      - id: TASK-1-5
        description: Update test_loop_detection.py to test novelty metric (not ratio), live session transcript parsing (not agent_log_store), and HITL escalation (not nudge). Verify test_agent_timeout_config.py, test_convergence_stall_suppression.py, and test_timeout_sigterm.py all pass.
        acceptance: All test files exist and pass; livelock detector tests verify novelty metric and live transcript parsing; timeout tests verify exit 143 classification; convergence-stall tests verify activity-based suppression
        files:
          - orchestrator/tests/test_loop_detection.py
          - orchestrator/tests/test_agent_timeout_config.py
          - orchestrator/tests/test_convergence_stall_suppression.py
          - orchestrator/tests/test_timeout_sigterm.py
    dependencies: ["2"]
    serialized_chain_order: ["1", "2", "5"]
```
