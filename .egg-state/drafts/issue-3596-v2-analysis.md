# Analysis: Operator visibility into agent state (issue #3596)

## Problem

Operators ran a pipeline for ~48 hours and hit four genuinely different failures, all indistinguishable via `get_status` (always `running`, every agent `WORKING`, no pending decisions). The only reliable diagnostic was a hand-rolled loop counting commits on the agent's worktree. The issue asks for visibility into:

1. **Forward progress** — is an agent actually getting anywhere, and would we notice if it started going backwards?
2. **Events and alerts** — destructive/exceptional things that never reach anyone watching
3. **Clocks and per-invocation accounting** — deadlines that matter are invisible until they pass; a cycle that did nothing is indistinguishable from one that did real work
4. **Post-mortem durability** — evidence routinely dies with the pod

The issue explicitly states: "This belongs in the orchestrator and overseer, which run continuously and can see inside the pod. It does not belong in the `/sdlc` skill."

## What already exists (verified in the tree)

### Forward progress tracking

- **`HealthMonitor`** (`orchestrator/health_monitor.py`): Per-agent tracking of `last_heartbeat`, `last_progress`, `last_activity` (from `CONTAINER_ACTIVITY` events). Tripwire rules: heartbeat timeout, progress stall, repeated errors, message rate spikes, infra errors, BRC confirmation timeout. Phase-aware thresholds (600s implement vs 120s standard). BRC-idle suppression for reviewers waiting on upstream producers.
- **`CONTAINER_ACTIVITY` event** (`orchestrator/events.py:122`): Fires on successful commit registrations from the gateway commit observer. Used to suppress heartbeat/progress stall alerts against agents legitimately blocked in long tool calls (#2190).
- **`detect_heartbeat_stall`** (`health_checks/tier1/consensus_stall.py`): Calibration detector that fires when BOTH `last_tool_call_age_s` and `last_heartbeat_age_s` are past the stall window (300s). An agent making recent tool calls is busy, not stalled.
- **`AgentExecution.commit`** (`models/_execution.py:168`): Records commit SHA when changes are made.
- **`PhaseExecution.cycle_timings`** (`models/_execution.py:239`): Per-cycle timing records with `started_at`/`completed_at`/`commit_sha`.
- **`AgentExecution.retry_count`** (`models/_execution.py:173`): Tracks restart count.
- **`list_unpushed_commits`** (`agent_salvage.py:425`): Enumerates commits on a worktree's local branch not in the anchor, with `--shortstat` for file counts.
- **`detect_uncommitted_changes`** (`kubernetes_spawner/_concurrent.py:21`): Checks agent worktrees for uncommitted changes after Job exit.

### Events and alerts

- **`OVERSEER_ALERT` message type**: Dedicated alert channel visible to human-facing surfaces. Hard-coded `to_role="all"` so `/sdlc` skill and `get_status` enrichment see it.
- **`HealthMonitor.get_active_alerts()`**: Returns structured alert dicts with `alert_type`, `agent_id`, `message`, `severity`, `timestamp`.
- **`GET /pipelines/<id>/health/alerts`**: REST endpoint for active health alerts.
- **`agent_log_store.py`**: Redis-backed store for one-shot agent pod logs captured at removal (#3547). 24h TTL. Captures `job_name`, `agent_role`, `slice_id`, `exit_code`, `logs`.
- **Tier-1 health check detectors** (`health_checks/tier1/`): 25+ deterministic detectors covering container death, restart loops, OOM evictions, overseer self-injection, branch divergence, worktree corruption, disk pressure, consensus stalls, driver liveness, cost anomalies, LLM substrate issues, gateway errors, decision queue issues.
- **Overseer alert body format** (`egg_overseer/issue_template.py`): Canonical template with timeline, classification, actions taken, evidence, remediation.

### Clocks and per-invocation accounting

- **`PhaseExecution.started_at`/`completed_at`** (`models/_execution.py:228-230`): Phase timing.
- **`AgentExecution.started_at`/`completed_at`** (`models/_execution.py:154-155`): Agent timing.
- **`PhaseExecution.work_started_at`** (`models/_execution.py:229`): When first agent spawned.
- **Configurable thresholds** (from `PipelineConfig`): `overseer_agent_stall_seconds` (180s), `overseer_silent_agent_threshold_seconds` (600s), `overseer_long_running_phase_seconds` (3600s), `overseer_stuck_phase_transition_seconds` (180s), `overseer_nack_unresolved_seconds` (180s), `overseer_phase_desync_alert_seconds` (300s), `orchestrator_implement_heartbeat_timeout_seconds` (600s), `orchestrator_heartbeat_timeout_seconds` (120s), `orchestrator_post_ack_confirmation_timeout_seconds` (180s), `orchestrator_plan_post_ack_confirmation_timeout_seconds` (300s), `orchestrator_activity_quiet_seconds`, `orchestrator_alert_progress_gate_seconds`, `orchestrator_error_repeat_threshold`, `orchestrator_message_rate_limit`.
- **`EGG_BRC_IDLE_BUDGET_MIN`** (default 30 min): Idle/no-progress safety budget in the BRC event loop.
- **`PhaseExecution.phase_start_sha`** (`models/_execution.py:263`): Branch tip SHA at phase start.
- **`PhaseExecution.agent_exits`** (`models/_execution.py:267`): Frozen-at-exit snapshots with `last_lines` (issue #2205).
- **`agent-timing.json`** (`.egg-state/oversight/`): Per-agent timing state for the overseer, with `phase_entered_at`, `first_seen_at`, `has_any_messages`, `alerted_anomalies`.

### Post-mortem durability

- **`agent_log_store.py`**: Redis-backed, 24h TTL, captures pod logs at Job removal before deletion. Keyed `agent-logs:{pipeline_id}:{job_name}`.
- **`AgentExitInfo`** (`models/_execution.py:191`): Frozen-at-exit snapshot with `role`, `exit_code`, `last_lines`, `terminated_at`, `container_id`.
- **`PhaseExecution.agent_exits`**: List of `AgentExitInfo` on the phase execution record.
- **`evidence_rescue.py`**: Patch-id rescue for unreachable commits (#3572) — resolves cited-but-unreachable SHAs via patch-id matching against integration branch.
- **`commit_authorship_store.py`**: Durable commit authorship registry with patch-ids.
- **`agent_salvage.py`**: Salvage of unpushed commits before worktree deletion, including uncommitted working-tree state (#2807).
- **`filed-issues.jsonl`** (`.egg-state/oversight/`): Append-only JSONL for filed diagnostic issues, with `fcntl.LOCK_EX` flock.
- **`agent-timing.json`** (`.egg-state/oversight/`): Per-agent timing state, `fcntl.LOCK_EX` guarded.
- **Overseer oversight log** (`overseer/monitor/_lifecycle.py`): JSONL oversight events at `.egg-state/oversight/{pipeline_id}-oversight.jsonl`.
- **Health summary** (`overseer/monitor/_lifecycle.py`): Written to `.egg-state/oversight/{pipeline_id}-health-summary.md` at pipeline completion.

### What `get_status` currently returns

From `_get_concurrent_status` (`routes/pipelines/_status_view.py`):
- `enabled`, `max_concurrent_agents`
- `messages`: `{total, by_type}` — message counts by type
- `consensus`: agents (with `producer_phase`, `reviewer_phase`, `confirmed`), `is_complete`, `blocking_agents`, `has_unresolved_nacks`, `unresolved_nacks`, `proposal_versions`, `review_edges`, `zero_proposal_producers`, `protocol`
- `agents`: `[{role, status, container_id, started_at, elapsed_seconds}]` — from phase execution or live Job labels (#3230)
- `slice_admit`: `{cap, admitted, admitted_keys}`
- `config`: overseer_* knobs
- `slice_consensus`: per-slice consensus state

**Notably absent from `get_status`:**
- Active health alerts (only available via separate `GET /pipelines/<id>/health/alerts`)
- Per-agent commit count or last commit info
- Per-agent heartbeat/progress age
- Per-agent retry count
- Per-agent exit info (from `PhaseExecution.agent_exits`)
- Phase timing (started_at, elapsed)
- Idle budget status

### What `snapshot_from_health_context` currently populates

From `health_checks/detection_plane.py:511`:
- `running_agents`: only `role`, `state`, `lifecycle_owner` — does NOT populate `last_tool_call_age_s`, `last_heartbeat_age_s`, `exit_code`, `exit_reason`
- `phase_state`: `status`, `lifecycle_owner`, `event_loop_owner`, `started_age_s`, `awaiting_spawn`
- `git_state`: empty (not populated)
- `container_transitions`: empty (not populated)
- `decision_state`: empty (not populated)
- `cost_counters`: empty (not populated)
- `gateway_error_counters`: empty (not populated)
- `midturn_messages`: empty (not populated)

The `detect_heartbeat_stall` detector reads `last_tool_call_age_s` and `last_heartbeat_age_s` from `RunningAgent`, but `snapshot_from_health_context` never populates them — so the detector can never fire in production.

## What's MISSING — the visibility gap

The infrastructure exists but **the data is not surfaced in `get_status`** in a way operators can read at a glance. The issue's core complaint is that `get_status` reports `running` + `WORKING` for every agent, with no way to distinguish legitimate work from wedged agents.

### Gap 1: Forward progress not visible in `get_status`

The `concurrent.agents` array only carries `role`, `status`, `container_id`, `started_at`, `elapsed_seconds`. It does NOT include:
- **Commit count** — how many commits has this agent made since spawn? (The issue's key diagnostic was "a hand-rolled loop counting commits on the agent's worktree")
- **Time since last commit** — when was the last commit pushed?
- **Heartbeat/progress age** — `HealthMonitor` tracks this but it's not in the status payload
- **Retry count** — `AgentExecution.retry_count` exists but isn't surfaced
- **Worktree diffstat** — how many files/lines changed?

### Gap 2: Active alerts not in `get_status`

`get_status` returns `pending_decisions` count but NOT active health alerts. An operator must call a separate endpoint (`GET /pipelines/<id>/health/alerts`) or run `egg-orch health alerts`. The issue says "Alert volume is currently a number, not something you can read."

### Gap 3: Per-invocation accounting not visible

No per-agent "time in current state" or "time since last progress event" in the status payload. The idle budget (`EGG_BRC_IDLE_BUDGET_MIN`) is documented but not visible. Phase timing exists on `PhaseExecution` but isn't surfaced in `get_status` beyond `elapsed_seconds` on agents.

### Gap 4: Post-mortem evidence not consolidated in status

`agent_log_store` captures logs with 24h TTL, but there's no way to see "what happened to this agent" from `get_status`. `AgentExitInfo` exists on `PhaseExecution.agent_exits` but is only populated when the container monitor detects an exit — if the pod is killed before the monitor sees it, the evidence is lost. The `agent_log_store` is best-effort Redis and may not survive a Redis failure.

### Gap 5: `snapshot_from_health_context` doesn't populate liveness fields

The `RunningAgent` dataclass has `last_tool_call_age_s` and `last_heartbeat_age_s` fields, and `detect_heartbeat_stall` reads them, but `snapshot_from_health_context` never populates them. This means the heartbeat-stall detector is dead code in production — it can never fire because the snapshot is always empty for those fields.

## Proposed approach

### 1. Enrich `get_status` with forward-progress signals

Add a `progress` sub-object to each entry in `concurrent.agents`:

```json
{
  "role": "coder",
  "status": "running",
  "container_id": "...",
  "started_at": "...",
  "elapsed_seconds": 3600,
  "retry_count": 0,
  "progress": {
    "last_heartbeat_age_s": 42,
    "last_progress_age_s": 120,
    "commit_count": 12,
    "last_commit_at": "...",
    "last_commit_sha": "abc1234",
    "last_commit_subject": "Fix auth bug in login flow",
    "progress_event_count": 142,
    "files_changed": 23
  }
}
```

Implementation:
- Populate `last_heartbeat_age_s` / `last_progress_age_s` from `HealthMonitor._last_heartbeat` and `AgentState.last_progress`
- Count commits on the agent's worktree branch (reuse `list_unpushed_commits` logic from `agent_salvage.py`)
- Count progress events from `ProgressStore.get_events()`
- Count changed files from worktree `git diff --stat` against the phase-start SHA

### 2. Surface active alerts in `get_status`

Add an `alerts` array to the top-level status response, sourced from `HealthMonitor.get_active_alerts()` (capped at 10 entries).

### 3. Add per-invocation accounting to status

Add `phase_started_at`, `phase_elapsed_seconds`, `idle_budget_minutes`, `idle_budget_remaining_s` to the `concurrent` block.

### 4. Consolidate post-mortem evidence

Surface `PhaseExecution.agent_exits` entries in the agent entries for completed/failed agents, and add a `log_store_records` field pointing to `agent_log_store` records for the agent's recent Job.

### 5. Populate `snapshot_from_health_context` with liveness fields

Fill `last_tool_call_age_s` / `last_heartbeat_age_s` on `RunningAgent` from `HealthMonitor` and the progress store, so `detect_heartbeat_stall` actually fires.

### 6. Forward-progress detector

Add a deterministic detector to the detection plane that fires when an agent has been running >N seconds with zero commits and zero progress events — the "exiting rc=0 doing nothing against an empty worktree mount" case.

## Risks

- **Response payload size**: Adding commit counts, diffstats, and alerts to every status call could grow the payload. Mitigation: cap alerts at 10, make commit/diffstat fields best-effort (skip on failure), cache where possible.
- **Git subprocess cost**: Counting commits and diffstat requires `git log` / `git diff` calls on the worktree. Mitigation: cap commit count at a reasonable number (e.g., 100), use `--oneline` format, run with a timeout.
- **HealthMonitor coupling**: Reading from `HealthMonitor._last_heartbeat` requires importing the health monitor singleton. Mitigation: defensive imports with fallback to `None`.
- **agent_log_store dependency**: Reading from Redis adds a dependency. Mitigation: best-effort, degrade to empty on failure.
