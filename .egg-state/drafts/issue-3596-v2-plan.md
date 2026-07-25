# Plan: Make agent forward-progress state visible and make the system act on it

> Issue: #3596 | Phase: plan | Pipeline: issue-3596-v2
> Slice DAG **adopts the architect's design**
> (`.egg-state/agent-outputs/issue-3596-v2-architect-slices.yaml`): 6 slices,
> slice-1 the root (7 sub-tasks), slices 2-5 depend on slice-1, slice-5 deferred.
> Scope set by HITL: **cq-1 = opt-1** (four highest-leverage gaps from #3595),
> **cq-2 = opt-3** (split task-1: wire plane in 1a, enrich snapshot in 1b-1g).

## Goal

Operators cannot distinguish a working agent from a wedged one via `get_status` —
every agent reports `WORKING` with no forward-progress signal. The codebase has
27 deterministic detectors in the detection plane, but **none can fire** because
(1) the plane is never invoked from the runtime tick, and (2) the snapshot builder
populates only 5 of 13 top-level fields and 3 of 7 `RunningAgent` fields. This
plan wires the detection plane into the runtime tick, enriches the snapshot
builder with data from 8 existing data sources, adds a forward-progress detector,
fixes the peer-progress gate, and enriches `get_status` with per-agent progress
signals and active alerts.

## What already exists (verified — do NOT rebuild)

- `agent_log_store.py` (#3547) — Redis-backed pod log retention at Job removal, 24h TTL
- `GET /pipelines/{id}/health/alerts` — serves active health alerts
- `working_heartbeat.py` (#3341) — in-tool-loop WORKING heartbeat emitter
- `driver_heartbeat.py` (#3540) — driver thread liveness registry
- `HealthMonitor` — active in production (initialized in `_run_pipeline.py:272`, 12+ call sites)
- `ProgressStore` — in-memory progress event store
- `list_unpushed_commits` (`agent_salvage.py:425`) — unpushed commit enumeration
- `AgentExitInfo` (#2205) — frozen-at-exit snapshots with last_lines
- `commit_authorship_store.py` — durable commit authorship registry
- `evidence_rescue.py` (#3572) — patch-id rescue for unreachable commits

## What's missing (the visibility gap)

1. **Detection plane is never invoked** — `run_detection_plane()` exists on
   `HealthCheckRunner` but is never called from `_run_runtime_tick_checks`.
   `_run_overseer_detection_plane` at `_overseer.py:309` has zero call sites.
   All 27 registered detectors are starved.

2. **Snapshot builder is sparse** — `snapshot_from_health_context` populates
   only `phase_state` and `running_agents` (with just `role`, `state`,
   `lifecycle_owner`). Missing: `container_transitions`, `git_state`,
   `decision_state`, `cost_counters`, `gateway_error_counters`, `midturn_messages`,
   `raw.*`, and `RunningAgent` liveness fields (`last_tool_call_age_s`,
   `last_heartbeat_age_s`, `exit_code`, `exit_reason`).

3. **`role=str(cid)` defect** — `snapshot_from_health_context` builds
   `RunningAgent(role=str(cid), ...)` from `live_container_ids`, putting a
   container UUID in the `role` field. Any detector keying on role name matches
   the wrong thing.

4. **`get_status` doesn't surface progress** — `concurrent.agents` only carries
   `role`, `status`, `container_id`, `started_at`, `elapsed_seconds`. No commit
   count, heartbeat age, retry count, or progress event count. Active alerts
   are only available via a separate endpoint.

5. **Peer-progress gate is dependency-blind** — `_has_recent_peer_progress`
   defers on ANY peer's heartbeat (including the overseer's own), suppressing
   alerts about the agent the overseer watches.

6. **No continuous forward-progress detector** — no detector fires when an
   agent runs >N seconds with zero commits, zero progress events, and zero
   file modifications (the "exiting rc=0 doing nothing" case).

7. **`health_checks/README.md:88` falsely documents the plane as wired.**

## Detector audit (all 27 registered detectors are starved)

### Snapshot fields populated by `snapshot_from_health_context`:
- `snapshot_id`: YES
- `pipeline_id`: YES
- `phase`: YES
- `running_agents`: YES (but only `role`, `state`, `lifecycle_owner` — NOT `exit_code`, `exit_reason`, `last_tool_call_age_s`, `last_heartbeat_age_s`)
- `phase_state`: YES (but only `status`, `lifecycle_owner`, `event_loop_owner`, `started_age_s`, `awaiting_spawn` — NOT `expected_duration_s`, `drift_ratio`)
- `consensus`: NO
- `decision_state`: NO
- `container_transitions`: NO
- `gateway_error_counters`: NO
- `cost_counters`: NO
- `midturn_messages`: NO
- `git_state`: NO
- `raw`: NO (entirely unpopulated — no `runtime`, `llm`, `resources`, `pr_state`, `self_health` sections)

### Starved detectors (all 27):

**Reading unpopulated top-level fields:**
- `detect_brc_thrash`, `detect_incomplete_consensus_deferral`, `PhaseStallDetector` → `consensus`
- `detect_container_death`, `detect_container_oom_evicted`, `detect_container_restart_loop`, `detect_overseer_self_injection` → `container_transitions`
- `detect_cost_anomaly` → `cost_counters`
- `detect_approved_decision_orphaned`, `detect_auto_advance_wedge`, `detect_hitl_queue_backlog`, `detect_restarted_decision_replay` → `decision_state`
- `detect_gateway_error_spike`, `detect_gateway_repeated_denial`, `detect_gateway_token_expiry` → `gateway_error_counters`
- `detect_pushed_pr_not_updated`, `detect_worktree_corruption` → `git_state`
- `detect_disk_inode_pressure`, `detect_pr_external_mutation` → `raw`
- `detect_anthropic_5xx_sustained`, `detect_effective_model_drift`, `detect_llm_substrate_unreachable` → `raw.llm`
- `detect_agent_restart_propagation`, `detect_run_pipeline_thread_liveness` → `raw.runtime`
- `detect_overseer_self_health` → `raw.self_health`

**Reading unpopulated `RunningAgent` fields:**
- `detect_heartbeat_stall` → `last_tool_call_age_s`, `last_heartbeat_age_s`
- `detect_container_death` → `exit_code`
- `detect_overseer_self_injection` → `exit_reason`

**Reading unpopulated `phase_state` fields:**
- `detect_duration_drift` → `expected_duration_s`, `drift_ratio`

### Data sources needed for each field:
- `container_transitions`: `kubernetes_monitor` container event history
- `git_state`: worktree git log/rev-list/patch-id
- `decision_state`: contract pending decisions + decision queue
- `cost_counters`: cost_callback logs (no queryable store exists — deferred)
- `gateway_error_counters`: gateway error counters (no queryable store exists)
- `midturn_messages`: message store midturn messages
- `raw.runtime`: `driver_heartbeat` registry + `kubernetes_monitor` state
- `raw.llm`: litellm cost_callback logs (no queryable store)
- `raw.resources`: filesystem/disk usage
- `raw.pr_state`: PR status from GitHub API
- `raw.self_health`: overseer self_monitor metrics
- `RunningAgent.exit_code`/`exit_reason`: container exit info from `kubernetes_monitor`
- `RunningAgent.last_tool_call_age_s`: from `working_heartbeat` emitter or agent session
- `RunningAgent.last_heartbeat_age_s`: from `HealthMonitor._last_heartbeat`
- `phase_state.expected_duration_s`: from `PipelineConfig` phase budgets
- `phase_state.drift_ratio`: computed from `started_age_s` / `expected_duration_s`

## Implementation approach

**Option B**: targeted, high-leverage changes that activate existing dormant
infrastructure rather than rebuilding from scratch. The plan fixes gaps in order
of leverage: (1) wire the detection plane into the runtime tick, (2) enrich the
snapshot builder with data from existing sources, (3) add a forward-progress
detector, (4) fix the peer-progress gate, (5) enrich `get_status`, (6) record
sampling params (deferred).

## Ordering

```
slice-1 (1a: wire plane) → 1b, 1c, 1d, 1e, 1f, 1g (snapshot enrichment)
slice-1 (1c: git_state) → slice-2 (forward-progress detector needs git_state)
slice-1 (1e: liveness) → slice-2 (needs RunningAgent liveness fields)
slice-1 (1e: liveness) → slice-4 (status enrichment needs liveness fields)
slice-3 (peer-progress gate) is independent of the detection plane
slice-5 (sampling params) is fully independent and deferred
```

Execution order: slice-1 first (foundation), then slices 2-4 in parallel,
slice-5 anytime (deferred).

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Response payload size growth from status enrichment | Medium | Cap alerts at 10 entries; make git/diffstat fields best-effort with timeouts; cache where possible |
| Git subprocess cost on every status call | Medium | Cap commit count at 100; use `--oneline` format; run with 5s timeout; degrade to null on failure |
| Detection plane adds CPU load on runtime tick | Low | Detectors are pure functions, exception-isolated, only run on RUNTIME_TICK (5s default). Each is O(1) or O(n) in small bounded data |
| Peer-progress gate fix could re-introduce false positives | Medium | Gate falls back to "any peer" when dependency graph unavailable; conservative by default |
| Consumption breaker (task-5) has no cost counter store | Medium | Deferred to follow-up. Snapshot builder populates `cost_counters` from whatever store is created, but store creation is a separate task |
| Session transcripts pushed only on exit — never-exiting agent has no transcript | Low | Correctly deferred. Requires changes to agent session lifecycle. Track as separate follow-up issue |

## Deferred

- **Consumption breaker (task-5)**: No cost counter store exists — `cost_callback.py`
  logs to stdout, not a queryable store. The snapshot builder will populate
  `cost_counters` from whatever store is created, but store creation is a
  separate follow-up task.
- **Session transcript capture on pod exit**: Transcripts are pushed only on
  event-pod EXIT. An agent that never exits has no stored transcript.
  `agent_log_store` captures at Job removal and does not cover this. Requires
  changes to the agent session lifecycle.
- **Repetition-triggered context surgery**: Requires Claude Code session history
  rewriting support — needs investigation first.
- **Ground-truth verifier role for the review set**: Reviewer-graph topology
  change, not a visibility improvement.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.

```yaml
# yaml-tasks
pr:
  title: |-
    Wire detection plane + enrich status for agent visibility (#3596)
  description: |-
    Operators cannot distinguish a working agent from a wedged one via get_status
    — every agent reports WORKING with no forward-progress signal. The codebase has
    27 deterministic detectors in the detection plane, but none can fire because
    (1) the plane is never invoked from the runtime tick, and (2) the snapshot
    builder populates only 5 of 13 top-level fields and 3 of 7 RunningAgent fields.
    This plan wires the detection plane into the runtime tick, enriches the snapshot
    builder with data from 8 existing data sources, adds a forward-progress detector,
    fixes the peer-progress gate, and enriches get_status with per-agent progress
    signals and active alerts.
  test_plan: |-
    - Automated: per-slice test suites covering detection plane wiring, snapshot
      enrichment, forward-progress detector, peer-progress gate fix, and status
      endpoint enrichment. `make test` narrows to reachable suites.
    - Manual: verify /status response includes progress sub-object and alerts
      array; verify detection plane findings appear on the event bus.
  manual_steps: |-
    Pre-merge: none.
    Post-merge: watch the first real pipeline run to confirm detection plane
    findings appear in /health/alerts and get_status surfaces progress signals.
slices:
  - id: slice-1
    name: |-
      Wire detection plane into runtime tick + enrich snapshot builder
    goal: |-
      Foundation slice: wire the detection plane into _run_runtime_tick_checks (1a),
      then enrich snapshot_from_health_context with all data sources the 27
      starved detectors need. This is the critical foundation — without it, no
      other detector can fire.
    dependencies: []
    tasks:
      - id: TASK-1-1A
        description: |-
          Wire the detection plane into the runtime tick. Call
          HealthCheckRunner.run_detection_plane() from
          _run_runtime_tick_checks in kubernetes_monitor.py. The detection plane
          (DetectionPlane.default()) has 27 registered detectors but is never
          invoked in production. Guard against double-evaluation since
          _run_runtime_tick_checks fires from two call sites (_check_pod on
          container transitions + _reconciliation_sweep on periodic interval).
          Make evaluation idempotent per tick.
        acceptance: |-
          - run_detection_plane() is called from _run_runtime_tick_checks for running pipelines
          - Evaluation is idempotent per tick (no double-evaluation from two call sites)
          - Findings are emitted on the EventBus as DETECTION_FINDING events
          - Findings with requires_adjudication=True are routed to _escalate_finding_to_adjudicator
          - Routine findings (requires_adjudication=False) are executed by CorrectiveExecutor
          - Test: detection plane runs on RUNTIME_TICK and emits findings
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
          - orchestrator/health_checks/runner.py
      - id: TASK-1-1A-TEST
        description: |-
          Test for detection plane wiring into runtime tick.
        acceptance: |-
          - Test verifies run_detection_plane() is called from _run_runtime_tick_checks
          - Test verifies findings are emitted as DETECTION_FINDING events
          - Test verifies idempotent evaluation (no double-firing from two call sites)
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_runtime_wiring.py
      - id: TASK-1-1B
        description: |-
          Populate container_transitions in snapshot_from_health_context. Enrich
          to populate container_transitions from kubernetes_monitor's container
          event history. Each transition: {container, role, from, to, reason,
          exit_code, restart_count, transient, timestamp}. Best-effort: failures
          degrade to empty tuple, never crash.
        acceptance: |-
          - container_transitions populated with transition events from kubernetes_monitor
          - Each transition has: container, role, from, to, reason, exit_code, restart_count
          - Best-effort: failures degrade to empty tuple, never crash
          - Test: detect_container_death receives populated container_transitions
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-1-1B-TEST
        description: |-
          Test for container_transitions population in snapshot builder.
        acceptance: |-
          - Test verifies container_transitions are populated from kubernetes_monitor
          - Test verifies detect_container_death receives populated transitions
          - Test verifies best-effort degradation on failure
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_container_transitions.py
      - id: TASK-1-1C
        description: |-
          Populate git_state in snapshot_from_health_context. Enrich to populate
          git_state with: commit_count (git rev-list --count), last_commit_at
          (git log -1 --format=%aI), last_commit_sha, patch_id_matches, branch,
          is_ancestor_of_base, pr_head_sha, last_pushed_sha, pushed_age_s,
          pr_externally_mutated, fsck_errors, index_lock_present, lock_age_s.
          Best-effort: git failures degrade to empty dict, never crash.
        acceptance: |-
          - git_state populated with commit_count, last_commit_at, last_commit_sha, branch
          - git_state populated with patch_id_matches, is_ancestor_of_base for divergence detection
          - git_state populated with fsck_errors, index_lock_present, lock_age_s for corruption detection
          - Best-effort: git failures degrade to empty dict, never crash
          - Test: detect_worktree_corruption and detect_pushed_pr_not_updated receive populated git_state
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-1-1C-TEST
        description: |-
          Test for git_state population in snapshot builder.
        acceptance: |-
          - Test verifies git_state is populated with commit_count, last_commit_at, branch
          - Test verifies detect_worktree_corruption and detect_pushed_pr_not_updated receive populated git_state
          - Test verifies best-effort degradation on git failure
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_git_state.py
      - id: TASK-1-1D
        description: |-
          Populate decision_state in snapshot_from_health_context. Enrich to
          populate decision_state with: pending_hitl, open_decisions,
          approved_unapplied, oldest_open_age_s, replay_pending, replay_count,
          replayed_resolved_id, auto_advance_pending, auto_advance_age_s.
          Data source: contract store + decision queue.
        acceptance: |-
          - decision_state populated with pending_hitl, open_decisions from contract
          - decision_state populated with approved_unapplied, oldest_open_age_s from decision queue
          - decision_state populated with replay_pending, replay_count from session state
          - Test: detect_approved_decision_orphaned and detect_hitl_queue_backlog receive populated decision_state
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-1-1D-TEST
        description: |-
          Test for decision_state population in snapshot builder.
        acceptance: |-
          - Test verifies decision_state is populated from contract + decision queue
          - Test verifies detect_approved_decision_orphaned and detect_hitl_queue_backlog receive populated decision_state
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_decision_state.py
      - id: TASK-1-1E
        description: |-
          Populate RunningAgent liveness fields + fix role=str(cid) defect in
          snapshot_from_health_context. Populate last_tool_call_age_s (from
          working_heartbeat emitter or agent session), last_heartbeat_age_s (from
          HealthMonitor._last_heartbeat), exit_code, exit_reason (from container
          exit info). CRITICAL FIX: role=str(cid) currently puts a container UUID
          in the role field — map container IDs to agent roles via the pipeline's
          phase execution state (AgentExecution.container_id -> AgentExecution.role).
        acceptance: |-
          - RunningAgent.role is populated with the agent role name, not a container UUID
          - RunningAgent.last_heartbeat_age_s populated from HealthMonitor._last_heartbeat
          - RunningAgent.last_tool_call_age_s populated from working_heartbeat or agent session
          - RunningAgent.exit_code and exit_reason populated from container exit info
          - Test: detect_heartbeat_stall can fire when both age fields are stale
          - Test: detect_container_death reads correct role names, not container UUIDs
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-1-1E-TEST
        description: |-
          Test for RunningAgent liveness fields + role=str(cid) fix in snapshot builder.
        acceptance: |-
          - Test verifies RunningAgent.role is populated with agent role name, not container UUID
          - Test verifies last_heartbeat_age_s and last_tool_call_age_s are populated
          - Test verifies detect_heartbeat_stall can fire when both age fields are stale
          - Test verifies detect_container_death reads correct role names
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_liveness_fields.py
      - id: TASK-1-1F
        description: |-
          Populate phase_state.expected_duration_s + raw.runtime in
          snapshot_from_health_context. Populate phase_state.expected_duration_s
          from PipelineConfig phase budgets, and raw.runtime with:
          run_pipeline_thread_alive (from driver_heartbeat),
          thread_last_tick_age_s (from driver_heartbeat),
          restart_propagation (from event loop supervisor).
        acceptance: |-
          - phase_state.expected_duration_s populated from PipelineConfig
          - raw.runtime.run_pipeline_thread_alive populated from driver_heartbeat
          - raw.runtime.thread_last_tick_age_s populated from driver_heartbeat
          - raw.runtime.restart_propagation populated from event loop supervisor
          - Test: detect_duration_drift receives expected_duration_s
          - Test: detect_run_pipeline_thread_liveness receives runtime liveness fields
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-1-1F-TEST
        description: |-
          Test for phase_state.expected_duration_s + raw.runtime population in snapshot builder.
        acceptance: |-
          - Test verifies phase_state.expected_duration_s is populated from PipelineConfig
          - Test verifies raw.runtime fields are populated from driver_heartbeat
          - Test verifies detect_duration_drift and detect_run_pipeline_thread_liveness receive populated fields
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_phase_state.py
      - id: TASK-1-1G
        description: |-
          Correct health_checks/README.md:88. The README documents
          _run_overseer_detection_plane as the thing that "builds the snapshot,
          evaluates" — implying it's wired in production. Correct this line to
          reflect that the detection plane is NOT currently wired into the
          runtime tick, and that task-1a adds that wiring.
        acceptance: |-
          - README line 88 corrected to state the detection plane is not yet wired
          - README updated to describe the wiring path via _run_runtime_tick_checks
        role: documenter
        files:
          - orchestrator/health_checks/README.md

  - id: slice-2
    name: |-
      Add forward-progress detector (commit count + worktree activity rate)
    goal: |-
      New deterministic detector detect_forward_progress_stall that fires when
      an agent has been running >N seconds with zero commits, zero progress
      events, and zero file modifications. Depends on 1a (plane wired),
      1c (git_state), 1e (RunningAgent liveness).
    dependencies: slice-1
    tasks:
      - id: TASK-2-1
        description: |-
          Add a new deterministic detector detect_forward_progress_stall to the
          detection plane. The detector fires when an agent has been running
          >N seconds (configurable, default 600s) with zero new commits AND
          zero progress events AND zero file modifications. Reads commit_count
          and last_commit_at from git_state, progress_event_count from
          ProgressStore, and worktree file mtime from git_state. Directly
          addresses the issue's key diagnostic: "a hand-rolled loop counting
          commits on the agent's worktree."
        acceptance: |-
          - New detector detect_forward_progress_stall registered in DetectionPlane.default()
          - Detector fires when agent runs >600s with zero commits, zero progress events, and zero file modifications
          - Detector stays silent when agent is making any of: commits, progress events, or file modifications
          - Configurable threshold via PipelineConfig (orchestrator_forward_progress_stall_seconds)
          - requires_adjudication=True (stuck vs. legitimately slow is ambiguous)
          - Test: detector fires on zero-progress agent, stays silent on active agent
        role: coder
        files:
          - orchestrator/health_checks/tier1/forward_progress.py
      - id: TASK-2-1-TEST
        description: |-
          Test for forward-progress detector.
        acceptance: |-
          - Test verifies detector fires on zero-progress agent running >600s
          - Test verifies detector stays silent on active agent (commits/progress/file mods)
          - Test verifies configurable threshold via PipelineConfig
        role: tester
        files:
          - orchestrator/tests/test_forward_progress_detector.py

  - id: slice-3
    name: |-
      Fix peer-progress gate to be dependency-aware
    goal: |-
      Issue #3595 root cause 1: _has_recent_peer_progress defers on ANY peer's
      heartbeat, including the overseer's own, which suppresses alerts about
      the very agent the overseer watches. Fix: scope to only defer on peers
      the agent actually depends on (from BRC review_edges). HealthMonitor IS
      still active in production — this fix is NOT wasted.
    dependencies: []
    tasks:
      - id: TASK-3-1
        description: |-
          Fix _has_recent_peer_progress in HealthMonitor to only defer on
          peers that the agent actually depends on, per the BRC review graph's
          review_edges. Currently the gate defers on ANY peer's heartbeat,
          including the overseer's own, which suppresses alerts about the agent
          the overseer watches. The dependency structure exists in the consensus
          tracker's review_edges and in heartbeat metadata (waiting_on).
        acceptance: |-
          - _has_recent_peer_progress only defers on peers in the dependent set (from review_edges)
          - Overseer's own heartbeat no longer suppresses alerts about agents it watches
          - Gate still suppresses false positives on busy pipelines (peers A depends on are active)
          - Test: a wedged agent with an active overseer peer no longer gets suppressed
          - Test: a busy pipeline with active upstream peers still suppresses false positives
        role: coder
        files:
          - orchestrator/health_monitor.py
      - id: TASK-3-1-TEST
        description: |-
          Test for dependency-aware peer-progress gate fix.
        acceptance: |-
          - Test verifies gate only defers on peers in the dependent set
          - Test verifies overseer's own heartbeat no longer suppresses alerts
          - Test verifies busy pipeline with active upstream peers still suppresses false positives
        role: tester
        files:
          - orchestrator/tests/test_peer_progress_gate.py

  - id: slice-4
    name: |-
      Enrich get_status with forward-progress signals and active alerts
    goal: |-
      Add a progress sub-object to each entry in concurrent.agents with:
      last_heartbeat_age_s, last_progress_age_s, commit_count, last_commit_at,
      last_commit_sha, progress_event_count. Add an alerts array to the
      top-level status response (capped at 10) sourced from
      HealthMonitor.get_active_alerts(). Add phase timing fields.
      Depends on 1e for liveness fields (HealthMonitor, ProgressStore, git).
    dependencies: slice-1
    tasks:
      - id: TASK-4-1
        description: |-
          Enrich get_status with forward-progress signals and active alerts.
          Add a progress sub-object to each entry in concurrent.agents with:
          last_heartbeat_age_s, last_progress_age_s, commit_count, last_commit_at,
          last_commit_sha, progress_event_count. Add an alerts array to the
          top-level status response (capped at 10) sourced from
          HealthMonitor.get_active_alerts(). Add phase timing (phase_started_at,
          phase_elapsed_seconds) to the concurrent block. All fields are null
          when unmeasurable (per operator constraint: distinguish null from zero).
        acceptance: |-
          - concurrent.agents[].progress contains commit_count, last_commit_at, last_heartbeat_age_s, last_progress_age_s, progress_event_count
          - Top-level alerts array present with alert_type, agent_id, message, severity, timestamp
          - concurrent.phase_started_at and concurrent.phase_elapsed_seconds present
          - All progress fields are null when unmeasurable, never 0
          - Best-effort: git subprocess failures degrade to null, never crash the status endpoint
          - Test: /status response includes progress sub-object and alerts array
        role: coder
        files:
          - orchestrator/routes/pipelines/_status_view.py
          - orchestrator/routes/pipelines/_routes_status.py
      - id: TASK-4-1-TEST
        description: |-
          Test for get_status enrichment with forward-progress signals and alerts.
        acceptance: |-
          - Test verifies /status response includes progress sub-object with commit_count, last_commit_at, last_heartbeat_age_s, last_progress_age_s, progress_event_count
          - Test verifies top-level alerts array present with alert_type, agent_id, message, severity, timestamp
          - Test verifies all progress fields are null when unmeasurable, never 0
          - Test verifies best-effort degradation on git subprocess failure
        role: tester
        files:
          - orchestrator/tests/test_status_progress_enrichment.py

  - id: slice-5
    name: |-
      Record sampling params in cost_callback (DEFERRED)
    goal: |-
      Issue #3595 root cause 5: sampling configuration is unset and unrecorded.
      Extend cost_callback.py to log optional_params alongside the cost and
      cache stats it already emits. Pin explicit temperature/top_p per model.
      Fully independent and deferred — can be done in parallel or as a follow-up.
    dependencies: []
    tasks:
      - id: TASK-5-1
        description: |-
          Record sampling params in cost_callback. Extend cost_callback.py to
          log optional_params (temperature, top_p, top_k, presence_penalty,
          frequency_penalty, reasoning_effort) per call. Pin explicit
          temperature/top_p per model in litellm_settings.yaml. Log format is
          backward-compatible (new fields are additive).
        acceptance: |-
          - cost_callback logs optional_params (temperature, top_p, top_k, presence_penalty, frequency_penalty, reasoning_effort) per call
          - Per-model temperature/top_p pinned in litellm_settings.yaml
          - Log format is backward-compatible (new fields are additive)
          - Test: cost_callback output includes optional_params field
        role: coder
        files:
          - orchestrator/cost_callback.py
          - config/litellm/litellm_settings.yaml
      - id: TASK-5-1-TEST
        description: |-
          Test for sampling params logging in cost_callback.
        acceptance: |-
          - Test verifies cost_callback output includes optional_params field
          - Test verifies per-model temperature/top_p pinned in litellm_settings.yaml
          - Test verifies backward-compatible log format
        role: tester
        files:
          - orchestrator/tests/test_cost_callback_sampling_params.py
```


## HITL Resolution

The following was approved by a human reviewer at the plan phase gate:

Approved. The plan is well-scoped and its verification work is trusted: it independently quantified what the operator had only described (27 detectors, none able to fire; snapshot populating 5 of 13 fields and RunningAgent 3 of 7), and it correctly identified two stacked causes rather than one, which is why the wire-then-enrich sequencing is right.

Slice 3 is scoped correctly. Fixing `_has_recent_peer_progress` to defer only on peers the agent actually depends on, using the BRC review graph's dependency edges, is the right fix. Do NOT implement it as an overseer special-case; excluding one role would leave the general defect intact. The overseer is merely the most visible instance because it heartbeats every ~2 min against a 300s gate.

FOUR NOTES FOR IMPLEMENTATION.

1. Consider folding the noop-streak mis-park into slice 3. It is the same dependency-graph insight and is the single most reproducible defect in this investigation: 4/4 occurrences, prompt-independent AND phase-independent, including in this pipeline's own plan phase. `agent-invocation-noop-streak` parks a role for exiting cleanly with no BRC progress while it is legitimately blocked on an upstream producer, which is the normal opening state of every BRC phase. The `waiting_on` metadata naming the unsatisfied producer is already on every one of those heartbeats, so the fix is the same shape as slice 3's: consult the dependency graph before acting. Cheap to add while you are already in that code; if you judge it out of scope, say so explicitly rather than dropping it silently.

2. Slice 2's forward-progress detector must not key on commits alone. A real failure this run was a healthy agent doing implement-phase work during the plan phase: 300 tool calls, `pytest` 61x, `Edit` 23x, real commits, and no proposal for an hour. A detector that asks 'is it producing commits' scores that as HEALTHY. The distinguishing signal is absence of BRC progress (no proposal / no consensus action) despite activity, not absence of activity. Please make sure the predicate reflects that; otherwise the detector misses one of the three stall modes it is meant to catch.

3. There are three stall modes, not one, and they need different remedies: livelocked (restart with a fresh session), deadlocked on an unsatisfiable contract (operator answer), and working out-of-role (role correction). All three present identically from outside as healthy heartbeats + no proposal + sole blocker. If the detector can distinguish them even coarsely, that is worth more than detecting any one of them precisely, because applying the wrong remedy costs a phase.

4. Your deferrals are accepted and are now tracked, so they will not be lost: sampling-param recording is #3599, and the livelock detection/recovery motivation is #3598. Session-transcript-on-exit and the ground-truth verifier remain recorded in #3595 without their own issues. Your reasoning for deferring the consumption breaker (no queryable cost store exists; cost logging goes to stdout) is correct and worth keeping stated.

CONSTRAINTS STILL BINDING: null is not zero, on every new field. Do not rebuild `agent_log_store.py` or `GET /pipelines/{id}/health/alerts`. Guard slice-1a against double-evaluation, since `_run_runtime_tick_checks` fires from both `_check_pod` and `_reconciliation_sweep`. Fix `RunningAgent(role=str(cid))` in the same change as the snapshot enrichment, as planned.

Context: #3595 is the single source of truth for this run and carries the full evidence behind the gaps above.
