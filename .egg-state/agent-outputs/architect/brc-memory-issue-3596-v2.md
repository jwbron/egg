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

## Verdict
The codebase has substantial observability infrastructure but it is fragmented
and the detection plane — the centerpiece of #2270's "orchestrator-side
overseership" — is not actually wired into the runtime tick. The four
forward-progress signals (driver heartbeat, agent progress events, working
heartbeat, commit counting) exist but none provides continuous commit-rate
or worktree-activity monitoring. The fix is to (1) wire the detection plane
into the runtime tick and enrich the snapshot builder, (2) add a forward-progress
detector, (3) enrich the status endpoint, and (4) persist progress events.
