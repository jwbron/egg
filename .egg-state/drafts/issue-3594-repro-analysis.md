# Refiner Analysis — issue #3594: Operator-visibility gaps

## Pipeline context

- **Pipeline ID**: `issue-3594-repro`
- **Phase**: refine
- **Issue**: #3594 — "Operator-visibility gaps: forward-progress, event feed, and post-mortem surfaces"
- **Parent issue**: #3364 (operator-visibility backlog, broken out from the four-PR plan that all landed via #3524)
- **Reproduction purpose**: This is a reproduction run for #3595 investigation. The previous run's refiner livelocked (119 identical Bash calls, zero proposals, 94 min to consensus timeout). This run captures the refiner's live session transcript to determine whether thinking/reasoning blocks are persisted across turns.

## Issue summary

Issue #3594 tracks the operator-visibility backlog from #3364's two comments. The common denominator: **every recovery was cheap once the state was visible; the expensive part was always discovering the state.** Over ~48h the pipeline hit four distinct pathologies and `get_status` reported the same thing for all of them — `running`, all agents `WORKING`, no pending decisions.

The issue proposes four groups of visibility gaps:

1. **Forward-progress surface** — per-role forward-progress in `get_status`, progress-regression detection, NACKed-but-not-re-proposing watchdog
2. **Event/alert feed on the status surface** — `recent_events` list, `OVERSEER_ALERT` as a feed, surfacing `pipeline.error`, agent-side worktree health
3. **Clocks and per-invocation accounting** — consensus-window countdown, per-invocation outcome summaries
4. **Post-mortem durability** — per-arm termination causes queryable at any time, session-state TTL expiry is silent, agent invocation logs die with the pod

## Grounded codebase verification

All gaps were re-confirmed absent from the current tree against `main` (commit `9c079a4f8`).

### Group 1 — Forward-progress surface

**1.1 Per-role forward-progress in `get_status`** — VERIFIED ABSENT.

The status payload is built in `orchestrator/routes/pipelines/_routes_status.py:52-110`. The `data` dict (lines 52-58) contains only:
- `id`, `status`, `current_phase`, `pending_decisions`, `updated_at`
- `pending_decision` (first pending decision details, lines 62-70)
- `pr_url`, `pr_number` (lines 74-78)
- `concurrent` (from `_get_concurrent_status`, lines 85-87)
- `slice_admit` (lines 101-106)
- `config` (overseer-relevant config subset, lines 116-145)

No `commits_ahead_of_base`, `last_commit_ts`, or `last_commit_subject` field exists anywhere in the payload. The `_get_concurrent_status` function in `_status_view.py:98-324` provides agent lifecycle info (`role`, `status`, `container_id`, `started_at`, `elapsed_seconds`) but no git-forward-progress fields.

**1.2 Progress-regression detection in the overseer** — VERIFIED ABSENT.

The anomaly checks in `orchestrator/overseer/monitor/_anomaly_checks.py` contain exactly these checks:
- `_check_orchestrator_reachability` (L68)
- `_check_rerun_anomaly` (L126)
- `_check_status_consistency` (L193)
- `_check_hitl_resolution_propagation` (L243)
- `_check_contract_phase_desync` (L306)
- `_check_cross_phase_consistency` (L411)

No check monitors `commits_ahead` decreasing or resetting to 0. There is no `progress_regression` detector anywhere in the overseer monitor sub-package.

**1.3 "NACKed but not re-proposing" watchdog** — VERIFIED ABSENT.

The consensus-stall detectors in `orchestrator/overseer/monitor/_consensus_stall.py` contain:
- `_check_post_consensus_stall` (L47) — fires when consensus is complete but phase hasn't transitioned
- `_check_incomplete_consensus_stall` (L210) — fires when consensus is incomplete with stuck blocking agents

Neither covers "NACKed but not re-proposing." There is no `nack_without_repropose` or similar detector. The `_check_incomplete_consensus_stall` function does handle the case where blocking agents have not confirmed, but it does not distinguish between a producer that was NACKed and one that simply hasn't proposed yet.

### Group 2 — Event / alert feed on the status surface

**2.1 `recent_events` list in the status payload** — VERIFIED ABSENT.

The status payload (`_routes_status.py:52-110`) contains no `recent_events` field. The `concurrent` block from `_get_concurrent_status` includes `messages` (aggregate counts by type) and `consensus` (BRC state), but no list of recent warnings or anomalies. The overseer's anomaly checks log to `orchestrator/logs` and emit `OVERSEER_ALERT` messages, but these are not surfaced in the `get_status` payload.

**2.2 `OVERSEER_ALERT` as a feed, not a counter** — VERIFIED ABSENT.

The `OVERSEER_ALERT` message type is defined in `orchestrator/message_store.py:105` as `OVERSEER_ALERT = "OVERSEER_ALERT"`. The message store (L167-194) has schema fields for `anomaly_name`, `priority`, `severity`, `dedup_key`, and `schema_version` on OVERSEER_ALERT messages, but the status payload does not include a feed of these messages. The `_get_concurrent_status` function's `messages` field only provides aggregate counts (`total` and `by_type`), not the message contents or severity.

**2.3 Surface the stored `pipeline.error` string** — VERIFIED ABSENT.

The `pipeline.error` field IS set in various places (e.g., `_populate.py:598`, `_alerts.py:136`, `_run_phase_blocks.py:183`), but it is NOT included in the status payload. The `_get_pipeline_status_body` function in `_routes_status.py:52-58` does not read or expose `pipeline.error`. An operator querying `get_status` sees only `status: "failed"` with no error reason.

**2.4 Agent-side worktree health** — VERIFIED ABSENT.

The `_live_event_agents` function in `_pod_liveness.py:58-122` reconstructs the running-pod cohort from live Job labels, providing `role`, `status`, `container_id`, `started_at`, and `elapsed_seconds`. However, it does not include any worktree mount health probe results. The "STILL UNMOUNTED" detection mentioned in the issue was from the previous run's overseer and is not present in the current codebase — there is no mount health probe in the overseer monitor or the status payload. The overseer rules in `sandbox/agent-config/rules/overseer.md` reference `_is_brc_idle` in `orchestrator/health_monitor.py:177-218` for reviewer suppression, but no mount probe.

### Group 3 — Clocks and per-invocation accounting

**3.1 Consensus-window countdown** — VERIFIED ABSENT.

The status payload contains no `consensus_window_started_at`, `timeout_minutes`, or `expires_at` fields. The BRC tracker state (from `peer_consensus`) is surfaced via `_consensus_block` in `_status_view.py:45-95`, which includes `agents`, `is_complete`, `blocking_agents`, `has_unresolved_nacks`, `unresolved_nacks`, `proposal_versions`, `review_edges`, `zero_proposal_producers`, and `protocol` — but no timing/clock information. The consensus window start time and timeout are internal to the orchestrator's BRC implementation and are not exposed.

**3.2 Per-invocation outcome summaries** — VERIFIED ABSENT.

The status payload does not include any per-invocation summary with `{role, duration, commits_made, consensus_action_taken, cost_usd}`. The `AgentResult` dataclass in `shared/egg_agent/result.py:7-54` captures `success`, `stdout`, `stderr`, `returncode`, `error`, `metadata`, `cost_usd`, `num_turns`, `duration_ms`, `session_id`, `window_occupancy`, and `token_usage` — but these are per-invocation results, not aggregated into the status payload. The `cost_callback.py` in `config/litellm/` logs per-call and session-level cost/token stats to stdout, but these are not queryable via `get_status`.

### Group 4 — Post-mortem durability

**4.1 Per-arm termination causes queryable at any time** — VERIFIED PARTIALLY.

The `_exit_history` mechanism in `orchestrator/event_loop/_supervisor.py:533-541` (`_record_exit`) and `_format_exit_history` (L544-555) records per-key termination history. The `exhausted_report` method (L558-581) surfaces this in the `exit_history` and `exit_history_text` fields. However, this data is only surfaced when an arm is **exhausted** (streak >= threshold) and is embedded in the OVERSEER_ALERT at that transition. It is NOT queryable at any time via a dedicated route or status field. The issue notes this data exists in memory (`_format_exit_history` / `exit_history_text`, `orchestrator/event_loop/_supervisor.py:548`, `orchestrator/concurrent_executor.py:1387`) but is only rendered into log lines and escalation decisions.

**4.2 Session-state TTL expiry is silent** — VERIFIED ABSENT.

The `SESSION_STATE_TTL_SECONDS = 6 * 60 * 60` constant is defined in `orchestrator/session_state_store.py:54`. The `SessionStateStore.put` method (L117-165) uses `setex` with this TTL, but there is no event emitted on expiry. The Redis TTL lapses silently — there is no expiry notification, no log line, and no alert when a session state record expires while its slice is still active.

**4.3 Agent invocation logs die with the pod** — VERIFIED ABSENT.

There is no retention or archive path for agent invocation logs in the spawner or MCP log tooling. The `cost_callback.py` logs to stdout (captured by egg's log stream), but these are ephemeral pod logs. Once the pod is garbage-collected by kubelet, the logs are lost. The `get_container_logs` MCP tool works for live pods, but there is no archive for completed one-shots.

## Incident #3595 analysis

Issue #3595 documents a specific incident where the refiner livelocked: 119 identical `Bash` calls (`grep -rn "repo_path\|worktree_path\|WORKTREE_BASE_DIR" orchestrator/...`), zero `mcp__*` tool calls, zero proposals, 94 minutes to consensus timeout.

Key findings from #3595 that are relevant to #3594:

1. **Context grew unbounded** because compaction is keyed to the advertised window (`agent_model_resolution.py:148`), not to the model's actual tool-call fidelity knee. `laguna-s-2.1` is a genuine 1M-context model, so it gets the `[1m]` compaction profile, but tool-call fidelity degrades well before 1M tokens. There is no way to express "1M window, but compact at ~150k to preserve agentic tool-use reliability."

2. **The peer-progress gate is dependency-blind** (`health_monitor.py:318-435`). `_has_recent_peer_progress` defers agent A's alert on the basis of *any* peer's heartbeat, with no reference to whether that peer's progress says anything about A. The overseer's own liveness heartbeat permanently satisfied the gate for the agent it was supposed to watch.

3. **The noop-streak detector parked the correctly-behaving agent** (`_supervisor.py:36-98`). The simplifier correctly reported `WAITING_ON_ROLE: refiner` and declined to fabricate an artifact. The detector keyed on "invocation exited with no BRC progress," which cannot distinguish *stuck* from *correctly blocked on an upstream producer*, despite `WAITING_ON_ROLE` + `waiting_on` being present on every heartbeat.

4. **Detection latency**: The only detector that correctly identified the situation, `protracted-phase-no-progress`, fired at 84 minutes and at `[low]` priority. This detector does not exist in the current codebase — it was part of the previous run's overseer.

5. **No consumption breaker**: Every call logged `cost: null` / `cost_estimated: null` (`cost_callback.py:401-405`), so no dollar-denominated breaker was possible. Token and call counts were available on every call but nothing was watching them.

6. **Sampling configuration is unset, unrecorded, and partly unexpressible**: egg specifies no sampling params anywhere. `cost_callback.py` does not capture `optional_params`. The Anthropic Messages API surface has no repetition, frequency, or presence penalty field.

7. **The refiner's `token_usage` field does NOT include `reasoning_tokens`**: `_usage_components` in `shared/egg_agent/client.py:54-74` extracts `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `output_tokens` — but NOT `reasoning_tokens` (which lives at `completion_tokens_details.reasoning_tokens`). The `cost_callback.py` does capture `reasoning_tokens` (L204-205), but the agent-side `AgentResult.token_usage` does not. This is directly relevant to the reproduction purpose: determining whether thinking/reasoning blocks are persisted across turns.

## Proposed work

### Refine-phase deliverables

The refiner's job in this phase is to produce a grounded analysis (this document) that identifies the specific code paths and proposes a plan for the implement phase. The analysis above confirms all four groups of gaps from #3594 are genuinely absent from the current tree.

**HITL decisions needed (refine phase):**

1. **cq-1: Scope of Group 1 (forward-progress surface).** Three sub-items. Recommend implementing all three: (a) per-role forward-progress fields in `get_status`, (b) progress-regression detection in the overseer, (c) NACKed-but-not-re-proposing watchdog. These are the highest-leverage items per #3595.

2. **cq-2: Scope of Group 2 (event/alert feed).** Four sub-items. Recommend implementing all four, with 2.1 (`recent_events` list) as the highest priority — it closes most of the gap cheaply.

3. **cq-3: Scope of Group 3 (clocks and per-invocation accounting).** Two sub-items. Recommend implementing both, with 3.2 (per-invocation outcome summaries) as the higher priority for catching no-op loops early.

4. **cq-4: Scope of Group 4 (post-mortem durability).** Three sub-items. Recommend implementing all three, with 4.2 (session-state TTL expiry event) as the highest priority — it directly addresses the #3509 amnesia incident.

5. **cq-5: Incident #3595 remediation.** Seven proposed work items. Recommend prioritizing: (7) pin/inject/record sampling params first (unblocks measurement for item 1's bisection), then (2) scope peer-liveness and noop-streak to the dependency graph, then (3) detect near-identical loops, then (5) token/call consumption breaker, then (1) expressible compaction threshold, then (6) context surgery recovery, then (4) raise severity/cut latency on protracted-phase-no-progress.

**Coordination notes:**
- Item 3.2's `cost_usd` portion should coordinate with #3508 (token/cost accounting parity).
- Item 1's bisection experiment must use pinned sampling params (item 7) to be credible.
- Item 4.1 (per-arm termination causes) overlaps with existing `_exit_history` infrastructure in `_supervisor.py` — the data is already collected, just not queryable. This is a low-risk addition.

## Artifact

This analysis is written to `.egg-state/drafts/3594-analysis.md`.
