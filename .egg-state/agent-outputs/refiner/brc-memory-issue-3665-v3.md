# BRC Memory — Refiner, Issue #3665

## Pipeline identity
- Issue: #3665 — Supervision, second pass: the layer was silent on seven livelocks and loud at healthy agents
- Role: refiner
- Phase: refine

## Code model (verified against tree)

### Architecture: two parallel supervision layers

**Layer 1 — HealthMonitor (deterministic tripwires)**
- `orchestrator/health_monitor.py` — `HealthMonitor` class. Subscribes to `EventBus` events
  (PROGRESS_EMITTED, ERROR, CONTAINER_STOPPED, MESSAGE_SENT, CONTAINER_ACTIVITY).
  Runs `check_heartbeats()`, `check_progress()`, `_check_infra_errors()`, `check_brc_progress()`.
- Tripwires are gated by `_orchestrator_skip_tripwire()` (only checks roles with active one-shot Jobs,
  #3064/#3164) and `_is_brc_idle()` (suppresses reviewer-only agents waiting on upstream producers).
- The alive-signal gate `_has_recent_peer_progress()` (#2242) defers alerts when BRC bus activity
  or peer heartbeats are recent.
- **NOT wired into the event-loop path.** The `OverseerMonitor` poll cycle (`_poll_cycle` in
  `overseer/monitor/_poll.py`) queries `egg-orch` CLI subcommands, but `_poll_cycle` has no
  production construction site — the docstring says so explicitly (#3312 slice-8).
- HealthMonitor IS wired into the `kubernetes_monitor.py` RUNTIME_TICK sweep via
  `set_health_check_runner()` — but that runs `HealthCheckRunner.run()`, NOT the detection plane.

**Layer 2 — DetectionPlane (deterministic detectors, #2270)**
- `orchestrator/health_checks/detection_plane.py` — `DetectionPlane` class, `default_detection_plane()`.
- `snapshot_from_health_context(context)` builds an `EventStreamSnapshot` — but ONLY populates:
  `running_agents` (from `context.live_container_ids`), `phase_state`, `pipeline_id`, `phase`.
  Does NOT populate: `container_transitions`, `midturn_messages`, `cost_counters`,
  `gateway_error_counters`, `git_state`, `decision_state`, `consensus`, `runtime`.
- The plane is registered with detectors in `_register_coverage_gap_detectors()` but
  **`_run_overseer_detection_plane()` in `routes/pipelines/_overseer.py` is never called
  from any production code path.** It is imported in `__init__.py:1277` but has zero callers.
- The `HealthCheckRunner.run_detection_plane()` method exists but is never invoked.

**Layer 3 — OverseerMonitor (LLM-based, legacy)**
- `orchestrator/overseer/monitor/__init__.py` — `OverseerMonitor` class.
- `_poll_cycle()` is the main loop but has no production construction site.
- `_classify_stall()` in `overseer/classifier.py` uses Haiku to classify stalls.
- The overseer agent IS spawned per-phase in `_run_pipeline.py:386` via `_spawn_overseer_agent()`.

### Key finding: the detection plane is dead code in production

The #2270 detection plane (the "structural replacement for the respawning overseer watcher pod")
is fully implemented but never invoked. `snapshot_from_health_context()` only populates 5 of the
13 snapshot fields (`snapshot_id`, `pipeline_id`, `phase`, `running_agents`, `phase_state`).
The `DetectionPlane.evaluate()` method exists but `_run_overseer_detection_plane()` has zero
call sites outside its definition (the import in `__init__.py:1277` is not a call site).

### The 2-hour timeout

- `sandbox/llm/claude/config.py:23` — `ClaudeConfig.timeout = 7200` (2 hours)
- `shared/egg_agent/client.py:223` — `run_agent_async(..., timeout: int = 7200)`
- `sandbox/egg_lib/runtime.py:961` — `timeout_seconds = timeout_minutes * 60` (default 30 min)
- The k8s `active_deadline_seconds` defaults to 14400 (4 hours) in `kubernetes_client.py:350`
- The 2-hour timeout is the `ClaudeConfig.timeout` / `run_agent_async` default, applied via
  `asyncio.timeout(timeout)` at `client.py:765`. When it fires, the agent gets `TimeoutError`
  at `client.py:903` and returns `AgentResult(success=False, returncode=-1, error="Timed out...")`.
- This timeout is NOT visible to the agent — it's a server-side `asyncio.timeout` wrapper.
- The agent's exit code (-1) is NOT 0 or 143, so it's classified as abnormal by the supervisor
  (`_classify_exit` in `kubernetes_monitor.py:1164`), incrementing the failure streak.

### Repetition loop detection

- `overseer/classifier.py:224` — `detect_loop()` uses an LLM (Haiku) to classify whether
  recent_actions show a repetitive pattern. This is NOT deterministic and requires the overseer
  agent to be running and classifying.
- `overseer/classifier.py:298` — `classify_activity_pattern()` also uses an LLM to classify
  into productive/thrashing/spinning/improper_tool_use.
- The issue's empirical finding: "counting tool inputs never issued before in the session over a
  trailing window separates a loop from work cleanly." This signal is NOT computed anywhere in
  the orchestrator or health monitor. The agent's tool calls are logged in pod logs (via
  `agent_log_store.py`) but there's no deterministic counter for unique tool inputs.
- `agent_log_store.py` captures logs at pod removal (#3547) but the logs are truncated at 100
  characters per line in the k8s log path (`kubernetes_client.py:455` `read_job_log_snapshot`
  with `tail_lines=2000`), and the issue notes "tool inputs are truncated at about 100 characters."

### Session boundaries read as failures

- `kubernetes_monitor.py:1148` — `_classify_exit()` classifies exit codes: 0 and 143
  are clean; everything else is FAILED.
- An agent killed by the 2-hour timeout exits with code -1 (from `client.py:920`), which is
  NOT 0 or 143, so it's classified as FAILED and increments the failure streak.
- `event_loop/_supervisor.py:154-181` — `record_abort()` increments the streak and can
  escalate to `agent-invocation-fail-streak` at `SUPERVISION_FAILURE_STREAK_ALERT` (10).
- An agent that exits cleanly (rc=0) without emitting a verdict is handled by
  `record_success()` which counts toward the no-op streak (#3425), but a timeout-kill
  (rc=-1) goes through `record_abort()` instead.

### Signals that exist but are not consulted

1. **`driver_heartbeat.record_spawn()` / `record_tick()`** (`driver_heartbeat.py`) —
   tracks spawn and tick ages per pipeline. Read by `DriverLivenessCheck`
   (`health_checks/tier1/driver_liveness.py`) but that check is registered in `cli.py:352`
   and runs via `kubernetes_monitor._run_runtime_tick_checks()`. The detection plane's
   `detect_run_pipeline_thread_liveness` reads `runtime.run_pipeline_thread_alive` and
   `runtime.thread_last_tick_age_s` from `snapshot.raw.runtime` — but `snapshot_from_health_context()`
   does NOT populate `raw.runtime`. So this detector is unreachable.

2. **`peer_consensus` tracker** — has `get_latest_progress_timestamp()`, `get_latest_proposal_timestamp()`,
   `get_fully_acked_producers()`, `consensus_state_fingerprint()`. Read by `HealthMonitor._has_recent_peer_progress()`
   and `_check_incomplete_consensus_stall()` in `_consensus_stall.py`. But the detection plane's
   `consensus` field is never populated by `snapshot_from_health_context()`.

3. **`agent_log_store`** — captures pod logs at removal (#3547). The overseer's
   `_query_container_logs()` in `_queries.py:240` fetches logs via `egg-orch container logs`,
   which falls back to the store when the pod is gone (`routes/containers.py:511`). But this
   is only called when the overseer poll cycle runs — which has no production construction site.

4. **`HealthMonitor.check_brc_progress()`** — detects fully-ACKed producers that haven't
   confirmed. This IS wired into `check_tripwires()` and IS called from the kubernetes_monitor
   RUNTIME_TICK path. But it's part of HealthMonitor, not the detection plane.

### False-positive sources (the five states the operator rewrote the rule for)

1. **Producers legitimately podless between events** — handled by `_orchestrator_skip_tripwire()`
   (#3064/#3164): only roles with active one-shot Jobs are checked.
2. **Reviewers waiting on upstream producers** — handled by `_is_brc_idle()` in `health_monitor.py:524`.
3. **Declared no-op leaves review edges pending forever** — `noop_parked()` in `event_loop/_supervisor.py:665`
   handles this with fingerprint/Brc-probe release.
4. **NACK is a verdict that discharges the obligation** — handled in `peer_consensus/_state.py`
   (`_un_confirm_stale_reviewers`, `_invalidate_pre_proposal_acks`).
5. **Two states not visible in the status payload** — the `consensus` field in the detection
   plane snapshot is never populated, and `container_transitions` is never populated.

## Already-landed items (verified via git log)

All 9 items from the issue are confirmed in the tree:
- #3613: Terminating-Job adoption — `kubernetes_spawner/_events.py` `_job_is_terminating()`, `_await_terminating_event_jobs()`
- #3644/#3647/#3652/#3654/#3656/#3660: Worktree preservation — `kubernetes_spawner/_worktree.py`, `_spawn.py` `reuse_worktree_id`
- #3645/#3649/#3655/#3657: Cancel stops driver — `routes/pipelines/_run_concurrent.py` `_phase_bail_reason_impl()`
- #3648: Phase-gate approvals parse first line — `routes/pipelines/_hitl_rerun.py`
- #3612: Never-heartbeated roles anchor at Job start — `health_monitor.py` `_job_active_since`, `_never_seen_escalated`
- #3607: Simplifier's first propose gated on upstream — `event_loop/_loop.py` `_check_convergence_stall`
- #3609/#3628: Green gate defaults on, red escalates to HITL — `slice_green_gate.py`, `routes/pipelines/_run_concurrent.py`
- #3611/#3625: Decoding config recorded — `agent_model_resolution.py`, `kubernetes_spawner/_spawn.py`
- #3661: Re-reviews blocking-only — `peer_consensus/_confirm.py` `handle_re_propose`

## Summary of assessment

The supervision layer has three parallel mechanisms, but the newest and most promising one
(the #2270 detection plane) is **completely unwired** in production. The `snapshot_from_health_context()`
builder populates only 3 of 12 snapshot fields, and `_run_overseer_detection_plane()` has zero
call sites. The HealthMonitor tripwires ARE wired (via kubernetes_monitor RUNTIME_TICK) but
operate on a different data path than the detection plane. The overseer agent IS spawned per-phase
but its LLM-based classification is expensive and the poll cycle that feeds it has no production
construction site.

The 2-hour timeout is the `ClaudeConfig.timeout` default in `run_agent_async`, applied via
`asyncio.timeout()`. It kills the agent with exit code -1, which the supervisor counts as an
abnormal termination (not a clean exit), incrementing the failure streak. The agent never sees
the timeout coming because it's a server-side asyncio wrapper, not a signal sent to the agent.

Repetition loops are not detected because there is no deterministic counter for unique tool inputs.
The `detect_loop()` and `classify_activity_pattern()` functions in `overseer/classifier.py` use
an LLM (Haiku) but require the overseer agent to be running and classifying — and the overseer's
poll cycle has no production construction site.
