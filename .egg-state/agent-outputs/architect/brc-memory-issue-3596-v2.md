# BRC memory — architect (issue-3596)

## Issue summary
GitHub issue #3596: "Operator cannot tell a working agent from a wedged one."
Operators ran a 48-hour pipeline that hit four different failures, all of which
`get_status` reported identically as `running` / `WORKING` with no pending
decisions. The only reliable diagnostic was a hand-rolled commit-counting loop.
The issue asks to make forward-progress state visible and make the system act on
it, in four areas: forward progress, events/alerts, clocks/per-invocation
accounting, post-mortem durability.

## Codebase audit (what already exists)

### Forward progress tracking — PARTIAL / FRAGMENTED
- `orchestrator/driver_heartbeat.py`: process-local monotonic-clock stamps for
  driver-loop ticks (`record_tick`) and successful spawns (`record_spawn`).
  Read by `DriverLivenessCheck` (Tier 1, RUNTIME_TICK). Detects dead/hung/no-progress
  drivers (#3540). BUT: spawn stamps only fire on *new* spawns — a single long-running
  one-shot agent in event-pump mode never re-stamps the spawn clock for its whole
  runtime (#3230), so the "no progress" mode relies on the live-pod check.
- `orchestrator/health_monitor.py` HealthMonitor: tracks per-agent `last_heartbeat`,
  `last_progress`, `last_activity` (CONTAINER_ACTIVITY). Tripwires: heartbeat timeout,
  progress stall. Has alive-signal gates (#2190 focal-agent activity, #2242 peer progress).
  BUT: `last_progress` is only updated when the agent emits a structured PROGRESS_EMITTED
  event — which requires the agent to call `mcp__progress__emit`. An agent making
  tool calls but never emitting progress events is invisible.
- `shared/egg_agent/working_heartbeat.py`: in-tool-loop WORKING heartbeat emitter
  wired as PostToolUse hook. Fires every 120s by default. This restores liveness
  for agents making continuous tool calls. EXISTS and is wired.
- `orchestrator/routes/pipelines/_pod_liveness.py` `_live_event_agents`: reconstructs
  running-agent view from live Job labels (#3230). Populates `concurrent.agents` in
  `/status`. EXISTS.
- `orchestrator/health_checks/tier1/phase_output.py` `PhaseOutputPresenceCheck`: checks
  for commits at phase boundaries (WAVE_COMPLETE/PHASE_COMPLETE) via `git rev-list --count`.
  EXISTS but only at phase boundaries, NOT continuously.

### Events and alerts — PARTIAL
- `orchestrator/events.py` EventBus: pub/sub with bounded history (100 events).
  Many event types including CONTAINER_ACTIVITY, PROGRESS_EMITTED, OVERSEER_ALERT.
- `orchestrator/health_monitor.py`: six tripwire rules (heartbeat timeout, container
  exit, repeated errors, message volume spike, progress stall, infra error).
  Active alerts stored in bounded deque (maxlen=200).
- `orchestrator/overseer/monitor/`: legacy standing-pod overseer (deprecated, #2270).
  Now only used for on-demand adjudication via `adjudicate()` method.
- `orchestrator/routes/pipelines/_overseer.py` `_run_overseer_detection_plane`:
  evaluates detection plane + escalates findings needing adjudication. EXISTS.
- `orchestrator/health_checks/detection_plane.py`: DetectionPlane with deterministic
  detectors. `default_detection_plane()` registers PhaseStallDetector + all slice-8
  coverage-gap detectors. `snapshot_from_health_context()` builds the snapshot.
  **BUT: the detection plane is NEVER actually invoked in the runtime tick path!**
  `run_detection_plane` exists on HealthCheckRunner but is not called from
  `_run_runtime_tick_checks` in kubernetes_monitor.py.

### Clocks and per-invocation accounting — PARTIAL
- `orchestrator/models/_config.py`: phase-aware timeouts (consensus, heartbeat,
  post-ACK confirmation, etc.). Config exists.
- `orchestrator/models/_execution.py`: AgentExecution tracks started_at/completed_at,
  PhaseExecution tracks cycle_timings, agent_exits (frozen-at-exit snapshots).
- `orchestrator/event_loop/_loop.py`: spawn timing (spawn_requested_at,
  spawn_dispatch_seconds). Dedupe keys.
- `orchestrator/overseer/self_monitor.py`: OverseerSelfMonitor tracks poll cycle
  duration, message volume, LLM costs per model, classifier/advisor failure rates.
  **BUT: only tracks the overseer's own health, not individual agent progress.**

### Post-mortem durability — PARTIAL
- `orchestrator/agent_log_store.py`: Redis-backed store for agent pod logs captured
  at removal. TTL 24h. EXISTS.
- `orchestrator/session_state_store.py`: Redis-backed BRC warm-resume data. TTL 6h.
- `orchestrator/overseer/self_monitor.py` `_resolve_oversight_dir`: writes oversight
  events to `.egg-state/oversight/{pipeline_id}-oversight.jsonl`. EXISTS.
- `orchestrator/models/_execution.py` AgentExitInfo: frozen-at-exit snapshot with
  last 200 lines of logs, stored in PhaseExecution.agent_exits. EXISTS.
- `orchestrator/evidence_rescue.py`: patch-id rescue for unreachable commit SHAs.

## Key gaps (what's MISSING)

### Gap 1: No continuous forward-progress metric
The codebase has NO continuous commit-counting or worktree-activity monitoring.
- `PhaseOutputPresenceCheck` only checks at phase boundaries.
- `DriverLivenessCheck` checks spawn stamps, but a single long-running agent
  doesn't re-stamp.
- The `working_heartbeat.py` emitter fires WORKING heartbeats but doesn't track
  whether the agent is actually producing commits or files.
- There is NO detector that says "this agent has been running for N minutes but
  has made zero commits and zero file modifications."

### Gap 2: Detection plane not wired into runtime tick
`DetectionPlane` and its 25+ detectors exist but `run_detection_plane()` is never
called from the kubernetes monitor's RUNTIME_TICK sweep. The snapshot builder
`snapshot_from_health_context()` only populates `phase_state` and `running_agents`
— it does NOT populate `container_transitions`, `git_state`, `cost_counters`,
`gateway_error_counters`, `midturn_messages`, or `decision_state`. So even if the
plane were wired in, most detectors would be dormant.

### Gap 3: Status endpoint doesn't expose progress granularity
`/api/v1/pipelines/<id>/status` returns `status`, `current_phase`, `pending_decisions`,
and `concurrent.agents` (role + status + elapsed_seconds). It does NOT expose:
- Commit count on the worktree branch
- File modification activity (mtime of worktree files)
- Last commit timestamp
- Progress event count / last progress timestamp
- Container activity timestamp
- Any notion of "is this agent actually producing output"

### Gap 4: No worktree activity rate metric
There is no mechanism tracking how frequently files are being modified in the
worktree as a liveness/progress signal. An agent could be making tool calls but
not writing any files, and this wouldn't be detected.

## Proposal

### Area 1: Forward progress — continuous commit + worktree activity tracking
Add a new Tier 1 detector `detect_forward_progress_stall` that:
- Tracks commit count on the pipeline branch over time (sampled at runtime ticks)
- Tracks worktree file modification activity (newest mtime in worktree)
- Fires when an agent has been running for >N seconds with zero new commits
  and zero file modifications
- Populates `git_state` and `phase_state` fields in the snapshot builder

### Area 2: Wire detection plane into runtime tick
- Call `run_detection_plane()` from `_run_runtime_tick_checks` in kubernetes_monitor
- Enrich `snapshot_from_health_context()` to populate:
  - `container_transitions` from container event history
  - `git_state` with commit count, last commit timestamp, patch-id info
  - `phase_state` with started_age_s, expected_duration_s
  - `decision_state` with pending HITL decisions
- This activates the 25+ dormant detectors

### Area 3: Status endpoint enrichment
Add to `/status` response:
- `concurrent.agents[].commits_made` (count of commits by this agent)
- `concurrent.agents[].last_commit_at` (timestamp of last commit)
- `concurrent.agents[].last_file_activity` (newest mtime in worktree)
- `concurrent.agents[].progress_events` (count of progress events emitted)
- `concurrent.agents[].last_progress_at` (timestamp of last progress event)
- `concurrent.agents[].last_activity_at` (timestamp of last CONTAINER_ACTIVITY)

### Area 4: Post-mortem durability — progress event persistence
- Back the in-memory `ProgressStore` with Redis (like `agent_log_store`)
- Persist commit-count samples and worktree mtime samples to Redis
- Ensure oversight log captures all detection-plane findings

## Ordering
1. Wire detection plane into runtime tick + enrich snapshot builder (unlocks
   all 25+ existing detectors, including the new forward-progress one)
2. Add forward-progress detector (continuous commit + worktree activity tracking)
3. Enrich status endpoint with progress granularity
4. Persist progress events to Redis for post-mortem durability

## What I checked
- `orchestrator/health_monitor.py` — HealthMonitor, check_heartbeats, check_progress
- `orchestrator/driver_heartbeat.py` — driver heartbeat registry
- `orchestrator/progress_store.py` — in-memory progress store (NOT Redis-backed)
- `orchestrator/health_checks/detection_plane.py` — DetectionPlane, snapshot builder
- `orchestrator/health_checks/runner.py` — HealthCheckRunner (has run_detection_plane but never called)
- `orchestrator/kubernetes_monitor.py` — _run_runtime_tick_checks (does NOT call detection plane)
- `orchestrator/routes/pipelines/_pod_liveness.py` — _live_event_agents
- `orchestrator/routes/pipelines/_routes_status.py` — /status endpoint
- `orchestrator/routes/pipelines/_status_view.py` — _get_concurrent_status
- `orchestrator/routes/pipelines/_alerts.py` — branch divergence tick
- `orchestrator/routes/pipelines/_overseer.py` — _run_overseer_detection_plane
- `orchestrator/overseer/monitor/` — overseer monitor (deprecated standing-pod, on-demand adjudication only)
- `orchestrator/overseer/self_monitor.py` — overseer self-monitoring
- `orchestrator/agent_log_store.py` — Redis-backed agent log store
- `orchestrator/session_state_store.py` — Redis-backed session state
- `orchestrator/models/_config.py` — PipelineConfig thresholds
- `orchestrator/models/_execution.py` — AgentExecution, PhaseExecution, AgentExitInfo
- `orchestrator/models/_enums.py` — PipelineStatus, AgentExecutionStatus, ContainerStatus
- `shared/egg_agent/working_heartbeat.py` — in-tool-loop heartbeat emitter
- `orchestrator/routes/commit_authorship.py` — CONTAINER_ACTIVITY event publisher
- `orchestrator/events.py` — EventBus, EventType enum
- `orchestrator/health_checks/tier1/` — all tier1 detectors (phase_output, driver_liveness, etc.)
- `orchestrator/health_checks/tier1/runtime_liveness.py` — runtime thread liveness, duration drift
- `orchestrator/health_checks/tier1/container_k8s.py` — container death, OOM, restart loop
- `orchestrator/health_checks/tier1/worktree_branch.py` — worktree corruption, disk pressure
- GitHub issue #3596 (live) and #3595 (incident analysis, partially superseded)

## Detector audit (all 27 registered detectors are starved)

### Snapshot fields populated by snapshot_from_health_context:
- snapshot_id: YES
- pipeline_id: YES
- phase: YES
- running_agents: YES (but only role, state, lifecycle_owner — NOT exit_code, exit_reason, last_tool_call_age_s, last_heartbeat_age_s)
- phase_state: YES (but only status, lifecycle_owner, event_loop_owner, started_age_s, awaiting_spawn — NOT expected_duration_s, drift_ratio)
- consensus: NO
- decision_state: NO
- container_transitions: NO
- gateway_error_counters: NO
- cost_counters: NO
- midturn_messages: NO
- git_state: NO
- raw: NO (entirely unpopulated — no runtime, llm, resources, pr_state, self_health sections)

### Audit results: ALL 27 detectors are starved (cannot fire in production)

Detectors reading unpopulated top-level fields:
- detect_brc_thrash, detect_incomplete_consensus_deferral → consensus (NOT populated)
- detect_container_death, detect_container_oom_evicted, detect_container_restart_loop,
  detect_overseer_self_injection → container_transitions (NOT populated)
- detect_cost_anomaly → cost_counters (NOT populated)
- detect_approved_decision_orphaned, detect_auto_advance_wedge, detect_hitl_queue_backlog,
  detect_restarted_decision_replay → decision_state (NOT populated)
- detect_gateway_error_spike, detect_gateway_repeated_denial, detect_gateway_token_expiry
  → gateway_error_counters (NOT populated)
- detect_pushed_pr_not_updated, detect_worktree_corruption → git_state (NOT populated)
- detect_disk_inode_pressure, detect_pr_external_mutation → raw (NOT populated)
- detect_anthropic_5xx_sustained, detect_effective_model_drift, detect_llm_substrate_unreachable
  → raw.llm (NOT populated)
- detect_agent_restart_propagation, detect_run_pipeline_thread_liveness → raw.runtime (NOT populated)
- detect_overseer_self_health → raw.self_health (NOT populated)

Detectors reading unpopulated RunningAgent fields:
- detect_heartbeat_stall → last_tool_call_age_s, last_heartbeat_age_s (NOT populated)
- detect_container_death → exit_code (NOT populated)
- detect_overseer_self_injection → exit_reason (NOT populated)

Detectors reading unpopulated phase_state fields:
- detect_duration_drift → expected_duration_s, drift_ratio (NOT populated)

Detectors reading unpopulated consensus fields:
- PhaseStallDetector → consensus.blocking_agents (NOT populated)

### Data sources needed for each field:
- container_transitions: kubernetes_monitor container event history
- git_state: worktree git log/rev-list/patch-id
- decision_state: contract pending decisions + decision queue
- cost_counters: cost_callback logs (no queryable store exists — R6)
- gateway_error_counters: gateway error counters (no queryable store exists)
- midturn_messages: message store midturn messages
- raw.runtime: driver_heartbeat registry + kubernetes_monitor state
- raw.llm: litellm cost_callback logs (no queryable store)
- raw.resources: filesystem/disk usage
- raw.pr_state: PR status from GitHub API
- raw.self_health: overseer self_monitor metrics
- RunningAgent.exit_code/exit_reason: container exit info from kubernetes_monitor
- RunningAgent.last_tool_call_age_s: from working_heartbeat emitter or agent session
- RunningAgent.last_heartbeat_age_s: from HealthMonitor._last_heartbeat
- phase_state.expected_duration_s: from PipelineConfig phase budgets
- phase_state.drift_ratio: computed from started_age_s / expected_duration_s

## NACK resolution (reviewer_plan v1)

The proposal was NACKed with 7 points. All are addressed:

R1 (HIGH): cq-2 was registered and resolved by the operator. The operator confirmed
the detection plane is NOT wired in production and retracted the cq-1 premise.
Task-1 should wire the plane into _run_runtime_tick_checks.

R3 (HIGH): Task-1 split into sub-tasks by data source:
- 1a: Wire detection plane into _run_runtime_tick_checks
- 1b: Populate container_transitions from kubernetes_monitor
- 1c: Populate git_state from worktree git operations
- 1d: Populate decision_state from contract + decision queue
- 1e: Populate RunningAgent liveness fields from HealthMonitor + progress store
- 1f: Populate phase_state.expected_duration_s from PipelineConfig
- 1g: Populate raw.runtime from driver_heartbeat + kubernetes_monitor
- 1h: Fix role=str(cid) defect (map container IDs to agent roles)
- 1i: Correct health_checks/README.md:88 (docs claim plane is wired)

R5 (MEDIUM): Full detector audit above. ALL 27 detectors are starved.

R6 (MEDIUM): Task-5 (consumption breaker) requires a cost counter store.
Deferred to a follow-up — no queryable cost store exists. The snapshot builder
should populate cost_counters from whatever store is created, but the store
creation is a separate task.

R7 (MEDIUM): HealthMonitor IS still active (initialized in _run_pipeline.py:272,
used across 12+ call sites). The peer-progress gate fix in HealthMonitor is NOT
wasted effort. The detection plane is the future, but HealthMonitor is the
present. Both need the fix: HealthMonitor for immediate effect, detection plane
for the future.

## Revised proposal (v2) — re-proposed after NACK from reviewer_plan

### Slice 1: Wire detection plane + enrich snapshot builder (9 sub-tasks)
- 1a: Wire detection plane into _run_runtime_tick_checks (idempotent per tick)
- 1b: Populate container_transitions from kubernetes_monitor
- 1c: Populate git_state from worktree git operations
- 1d: Populate decision_state from contract + decision queue
- 1e: Populate RunningAgent liveness fields + fix role=str(cid) defect
- 1f: Populate phase_state.expected_duration_s + raw.runtime from driver_heartbeat
- 1g: Correct health_checks/README.md:88 (docs claim plane is wired)

### Slice 2: Forward-progress detector
New detector: detect_forward_progress_stall — fires when agent runs >N seconds
with zero commits, zero progress events, zero file modifications.

### Slice 3: Peer-progress gate fix
Fix _has_recent_peer_progress to only defer on peers the agent depends on
(from BRC review_edges). HealthMonitor IS still active (12+ call sites).

### Slice 4: Status endpoint enrichment
Add progress sub-object to concurrent.agents + alerts array to top-level.

### Slice 5: Consumption breaker (DEFERRED)
No cost counter store exists — cost_callback logs to stdout, not queryable.
Deferred as implementation constraint, not operator decision.

### Slice 6: Record sampling params
Extend cost_callback.py to log optional_params. Pin temperature/top_p per model.

## Verdict
The codebase has substantial observability infrastructure but it is entirely
dormant — the detection plane is never invoked, and the snapshot builder
populates only 5 of 13 top-level fields (and only 3 of 7 RunningAgent fields).
ALL 27 registered detection-plane detectors are starved. The fix is to (1) wire
the detection plane into the runtime tick, (2) enrich the snapshot builder with
data from the existing data sources, (3) add a forward-progress detector,
(4) fix the peer-progress gate, (5) enrich the status endpoint, and (6) record
sampling params. Task-5 (consumption breaker) is deferred pending a cost counter
store.

## Proposal status
- v1 proposed, NACKed by reviewer_plan with 7 points
- cq-2 registered by risk_analyst, resolved by operator: detection plane is NOT
  wired in production, cq-1 premise retracted
- v2 re-proposed with all 7 NACK points addressed:
  - R1: cq-2 resolved, task-1a wires the plane
  - R3: task-1 split into 9 sub-tasks (1a-1g)
  - R5: full detector audit included (all 27 starved)
  - R6: task-5 deferred (no cost counter store)
  - R7: HealthMonitor confirmed active (12+ call sites)
- Commit SHA: ac390d79aee3179c818527fdf27b8a522e7ff52f
- Awaiting ACK from reviewer_plan and risk_analyst
