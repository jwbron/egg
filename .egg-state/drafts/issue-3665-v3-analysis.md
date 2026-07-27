# Refine Analysis: Supervision, Second Pass (Issue #3665)

## Executive summary

The supervision layer has three parallel mechanisms, but the newest and most promising one —
the #2270 detection plane — is **completely unwired** in production. `snapshot_from_health_context()`
populates only 5 of 13 snapshot fields, and `_run_overseer_detection_plane()` has zero call sites.
The HealthMonitor tripwires ARE wired (via kubernetes_monitor RUNTIME_TICK) but operate on a
different data path. The overseer agent IS spawned per-phase but its LLM-based classification
requires a poll cycle that has no production construction site.

The root cause of "silent on seven livelocks, loud at healthy agents" is: the deterministic
detectors that could catch loops never run (the detection plane is unwired), and the LLM-based
overseer that does run has no reliable input pipeline (its poll cycle has no production
construction site).

## What has already landed (verified)

All 9 items from the issue are confirmed in the tree (verified via per-item file-and-symbol
citations — the git log confirms a commit message exists, but the file-and-symbol anchors are
the real evidence that the code is present):

| Item | PR | File(s) | Status |
|------|-----|---------|--------|
| Terminating-Job adoption | #3613 | `kubernetes_spawner/_events.py` `_job_is_terminating()`, `_await_terminating_event_jobs()` | Present |
| Worktree preservation | #3644+#3647+#3652+#3654+#3656+#3660 | `kubernetes_spawner/_worktree.py`, `_spawn.py` `reuse_worktree_id` | Present |
| Cancel stops driver | #3645+#3649+#3655+#3657 | `routes/pipelines/_run_concurrent.py` `_phase_bail_reason_impl()` | Present |
| Phase-gate approvals parse first line | #3648 | `routes/pipelines/_hitl_rerun.py` | Present |
| Never-heartbeated roles anchor at Job start | #3612 | `health_monitor.py` `_job_active_since`, `_never_seen_escalated` | Present |
| Simplifier's first propose gated on upstream | #3607 | `event_loop/_loop.py` `_check_convergence_stall` | Present |
| Green gate defaults on, red escalates to HITL | #3609+#3628 | `slice_green_gate.py`, `_run_concurrent.py` | Present |
| Decoding config recorded | #3611+#3625 | `agent_model_resolution.py`, `kubernetes_spawner/_spawn.py` | Present |
| Re-reviews blocking-only | #3661 | `peer_consensus/_confirm.py` `handle_re_propose` | Present |

## Proposed work across the four areas

### Area 1: Signals that exist and are not consulted

**Problem:** The detection plane's `snapshot_from_health_context()` populates only 5 of 13
`EventStreamSnapshot` fields (`snapshot_id`, `pipeline_id`, `phase`, `running_agents`,
`phase_state`). The `consensus`, `container_transitions`, `runtime`, `cost_counters`,
`gateway_error_counters`, `midturn_messages`, `git_state`, and `decision_state` fields are all
left empty. This means every detector that reads those fields is silently inert in production.

**Proposed work (ordered):**

1. **Populate `runtime` section** — Wire `driver_heartbeat.tick_age_seconds()` and
   `driver_heartbeat.spawn_age_seconds()` into `snapshot_from_health_context()` under
   `raw["runtime"]`. This activates `detect_run_pipeline_thread_liveness()` (slice-8)
   and `DriverLivenessCheck` (which already reads these via `driver_heartbeat` directly).
   *Effort: small. Risk: low. File: `health_checks/detection_plane.py` `snapshot_from_health_context()`.*

2. **Populate `consensus` section** — Wire `peer_consensus.get_peer_consensus_tracker().evaluate()`
   into the snapshot builder. This activates `detect_brc_thrash()`,
   `detect_incomplete_consensus_deferral()`, and the `consensus` field readers in
   `PhaseStallDetector`. *Effort: medium. Risk: medium (tracker availability varies).*

3. **Populate `container_transitions`** — Wire the kubernetes_monitor's pod-state transition
   log into the snapshot. This activates `detect_container_death()`, `detect_container_oom_evicted()`,
   `detect_container_restart_loop()`, and `detect_overseer_self_injection()`.
   *Effort: medium. Risk: medium.*

4. **Populate `running_agents` with role + age data** — Currently `snapshot_from_health_context()`
   creates `RunningAgent(role=str(cid), ...)` where `role` is the container ID, not the agent role.
   Fix to use the agent role from pipeline state and populate `last_tool_call_age_s` /
   `last_heartbeat_age_s` from the health monitor's anchors. This activates
   `detect_heartbeat_stall()` (slice-8). *Effort: medium. Risk: medium.*

5. **Wire `_run_overseer_detection_plane()` into the RUNTIME_TICK path** — Call it from
   `kubernetes_monitor._run_runtime_tick_checks()` after building the snapshot, routing
   `requires_adjudication` findings to the overseer agent and routine findings to the
   corrective executor. *Effort: large. Risk: high (new code path on the hot loop).*

### Area 2: Session boundaries read as failures

**Problem:** An agent killed by the 2-hour `ClaudeConfig.timeout` (7200s) exits with code -1,
which `_classify_exit()` in `kubernetes_monitor.py:1148` classifies as FAILED (not 0/143),
incrementing the failure streak. The agent never sees the timeout coming.

**Proposed work (ordered):**

1. **Surface the 2-hour timeout to the agent** — Emit a `HEARTBEAT` with state `WAITING_FOR_EVENT`
   and a body like "approaching 2h timeout" at 90-minute intervals, so the agent knows it's
   time-bounded. *Effort: small. Risk: low.*

2. **Classify timeout-killed pods distinctly** — When `exit_code == -1` and the pod's
   `active_deadline_seconds` or the agent's own timeout fired, classify as a clean
   timeout (not a crash). The `agent_log_store` captures the exit code; the supervisor
   should distinguish "timeout" from "crash" in `record_abort` vs `record_success`.
   *Effort: medium. Risk: medium.*

3. **Make the timeout configurable per-pipeline** — Add `agent_timeout_seconds` to
   `PipelineConfig` (default 7200) and pass it through the spawner env as `EGG_AGENT_TIMEOUT`
   so the agent can self-report its remaining budget. *Effort: small. Risk: low.*

### Area 3: Loops that nothing detects

**Problem:** The issue's empirical finding — "counting tool inputs never issued before in the
session over a trailing window separates a loop from work cleanly" — is not computed anywhere.
`detect_loop()` and `classify_activity_pattern()` in `overseer/classifier.py` use an LLM but
require the overseer agent to be running and classifying, and the overseer's poll cycle has no
production construction site.

**Proposed work (ordered):**

1. **Implement a deterministic unique-tool-input counter** — A new detector
   `detect_tool_input_loop()` in `health_checks/tier1/` that reads `midturn_messages` from
   the snapshot (which must be populated — see Area 1) and counts distinct tool-input strings
   over a trailing window. If the count is zero for N consecutive polls, fire
   `tool_input_loop` / `high` with `requires_adjudication=False`. *Effort: medium.
   Risk: medium (depends on midturn_messages being populated).*

2. **Wire the existing `classify_activity_pattern()` into the overseer agent's prompt** —
   The overseer already has this classifier; the problem is the overseer isn't running
   its poll cycle. Fix the poll cycle wiring (Area 1, step 5) and the classifier becomes
   available. *Effort: large (depends on step 5). Risk: high.*

3. **Surface agent tool-call logs in the container log capture** — The `agent_log_store`
   captures pod logs at removal, but `read_job_log_snapshot()` in `kubernetes_client.py:455`
   uses `tail_lines=2000` and the k8s log API truncates at ~100 chars per line. Increase the
   log capture fidelity for one-shot event pods so the unique-tool-input counter has enough
   data. *Effort: small. Risk: low.*

### Area 4: Alerts an operator cannot act on

**Problem:** Alerts fire without evidence, name thresholds whose detectors don't exist, or
outcomes distinguishable only by log strings.

**Proposed work (ordered):**

1. **Enrich all OVERSEER_ALERT payloads with evidence** — The `_broadcast_alert()` in
   `overseer/monitor/_alerting.py` sends a message to the bus. Add structured evidence
   (container logs, BRC state, tracker evaluation) to the alert body so the operator can
   diagnose without grepping. *Effort: small. Risk: low.*

2. **Fix the convergence-stall alert false positive** — The issue describes a
   convergence-stall alert firing at `[high]` priority against a coder whose peer heartbeat
   was seconds old, while the health monitor was concurrently logging the agent as alive.
   The `_check_convergence_stall()` in `event_loop/_loop.py:859` uses
   `tracker.get_latest_progress_timestamp()` as the bus-timestamp anchor, but the
   `HealthMonitor._has_recent_peer_progress()` gate uses a different timestamp source.
   Unify these so the same "bus activity" signal is used for both alerting and deferral.
   *Effort: medium. Risk: medium.*

3. **Name the 2-hour timeout explicitly in alerts** — When a pod is killed by timeout,
   the alert should say "killed by 2h agent timeout" not "container exited with code -1."
   Wire the `ClaudeConfig.timeout` value into the exit classification in
   `kubernetes_monitor.py:_classify_exit()`. *Effort: small. Risk: low.*

4. **Wire the detection plane into the operator alert surface** — Once Area 1 step 5 is
   done, route detection-plane findings to the same `OVERSEER_ALERT` / HITL / Slack surfaces
   the overseer uses, so the operator sees one consistent alert stream. *Effort: medium.
   Risk: medium.*

## Ordering and dependencies

1. Area 1 steps 1-4 (populate snapshot fields) — **prerequisite for everything else**
2. Area 1 step 5 (wire detection plane into RUNTIME_TICK) — **enables Areas 3 and 4**
3. Area 3 step 1 (deterministic loop detector) — depends on snapshot populated
4. Area 2 steps 1-3 (timeout visibility and classification) — independent, low risk
5. Area 4 steps 1-4 (alert evidence and false-positive fixes) — partially depends on step 2

## What to leave out

- **Do not rebuild the overseer agent.** The per-phase overseer spawn in `_run_pipeline.py:386`
  is working. The problem is its input pipeline (the poll cycle), not the agent itself.
- **Do not remove the HealthMonitor tripwires.** They ARE wired and working for heartbeat/
  progress/container-exit detection. The detection plane should complement, not replace them.
- **Do not add LLM classification to the hot path.** The `classify_stall()` /
  `classify_activity_pattern()` functions in `overseer/classifier.py` are expensive (Haiku
  calls). Keep them in the overseer agent, not in the deterministic detection plane.
- **Do not change the 2-hour timeout default.** It's a reasonable budget. The fix is to make
  it visible and to classify timeout-kills distinctly, not to shorten it.

---

# Ranked candidate list

Every entry below is verified present or absent in the tree. "Present" means the code exists;
"absent" means it does not exist at all. "Wired" means it is actually invoked in a production
code path; "unwired" means it exists but is never called.

## Tier 1 — Must do (directly addresses the seven silent livelocks)

| # | Improvement | File-and-symbol citation | Present? | Wired? |
|---|------------|--------------------------|----------|--------|
| 1 | Populate `runtime` section of `EventStreamSnapshot` with driver heartbeat ages | `health_checks/detection_plane.py:snapshot_from_health_context()` + `driver_heartbeat.tick_age_seconds()` / `spawn_age_seconds()` | Absent | N/A |
| 2 | Populate `consensus` section of snapshot from peer_consensus tracker | `health_checks/detection_plane.py:snapshot_from_health_context()` + `peer_consensus.get_peer_consensus_tracker().evaluate()` | Absent | N/A |
| 3 | Populate `container_transitions` from kubernetes_monitor pod-state log | `health_checks/detection_plane.py:snapshot_from_health_context()` + `kubernetes_monitor.KubernetesMonitor._pod_states` | Absent | N/A |
| 4 | Fix `RunningAgent` role field — use agent role, not container ID | `health_checks/detection_plane.py:snapshot_from_health_context()` line 536: `RunningAgent(role=str(cid), ...)` | Present (bug) | N/A |
| 5 | Populate `last_tool_call_age_s` / `last_heartbeat_age_s` on RunningAgent | `health_checks/detection_plane.py:RunningAgent` fields (lines 89-90) | Present (unpopulated) | N/A |
| 6 | Wire `_run_overseer_detection_plane()` into RUNTIME_TICK path | `routes/pipelines/_overseer.py:309` (def) + `kubernetes_monitor.py:221` (`_run_runtime_tick_checks`) | Present (unwired) | No |
| 7 | Implement deterministic unique-tool-input loop detector | `health_checks/tier1/` (new `detect_tool_input_loop`) | Absent | N/A |
| 8 | Populate `midturn_messages` in snapshot for loop detection | `health_checks/detection_plane.py:snapshot_from_health_context()` | Absent | N/A |

## Tier 2 — Must do (directly addresses the false positives and timeout kills)

| # | Improvement | File-and-symbol citation | Present? | Wired? |
|---|------------|--------------------------|----------|--------|
| 9 | Classify timeout-killed pods (exit -1 from `asyncio.timeout`) as clean timeout, not crash | `kubernetes_monitor.py:_classify_exit()` (line 1148) | Present (bug) | Yes |
| 10 | Surface the 2-hour `ClaudeConfig.timeout` to the agent via heartbeat before expiry | `sandbox/egg_lib/orch_cli/_message.py:cmd_message_heartbeat` + `sandbox/llm/claude/config.py:23` | Present (not surfaced) | No |
| 11 | Make agent timeout configurable per-pipeline | `models/_config.py:PipelineConfig` + `sandbox/llm/claude/config.py:ClaudeConfig.timeout` | Present (hardcoded 7200) | No |
| 12 | Unify convergence-stall alert and alive-signal gate timestamp sources | `event_loop/_loop.py:_check_convergence_stall()` (line 859) vs `health_monitor.py:_has_recent_peer_progress()` (line 388) | Present (divergent) | Yes (both) |
| 13 | Enrich OVERSEER_ALERT payloads with evidence (logs, BRC state) | `overseer/monitor/_alerting.py:_broadcast_alert()` (line 56) | Present (minimal) | Yes |
| 14 | Name the 2-hour timeout explicitly in exit classification | `kubernetes_monitor.py:_classify_exit()` (line 1148) | Present (bug) | Yes |

## Tier 3 — Should do (improves signal quality)

| # | Improvement | File-and-symbol citation | Present? | Wired? |
|---|------------|--------------------------|----------|--------|
| 15 | Wire `classify_activity_pattern()` into the overseer agent's routine classification | `overseer/classifier.py:298` | Present (unwired) | No |
| 16 | Populate `cost_counters` in snapshot for cost anomaly detection | `health_checks/detection_plane.py:snapshot_from_health_context()` + `overseer/self_monitor.py:OverseerSelfMonitor._lifetime_cost` | Absent | N/A |
| 17 | Populate `gateway_error_counters` in snapshot | `health_checks/detection_plane.py:snapshot_from_health_context()` | Absent | N/A |
| 18 | Populate `decision_state` in snapshot for HITL queue backlog detection | `health_checks/detection_plane.py:snapshot_from_health_context()` + `decision_queue.py` | Absent | N/A |
| 19 | Route detection-plane findings to the operator alert surface (OVERSEER_ALERT / HITL / Slack) | `overseer/monitor/_alerting.py` + `kubernetes_monitor.py:_handle_driver_liveness_results()` (line 923) | Present (partial) | Yes (overseer only) |
| 20 | Increase log capture fidelity for one-shot event pods (beyond 100-char truncation) | `kubernetes_client.py:read_job_log_snapshot()` (line 455) + `agent_log_store.py:MAX_LOG_BYTES` (line 51) | Present (truncated) | Yes |

## Tier 4 — Could do (nice to have, lower impact)

| # | Improvement | File-and-symbol citation | Present? | Wired? |
|---|------------|--------------------------|----------|--------|
| 21 | Populate `git_state` in snapshot for worktree corruption detection | `health_checks/detection_plane.py:snapshot_from_health_context()` + `health_checks/context.py:git_log` | Present (unused) | No |
| 22 | Wire `detect_duration_drift()` with `expected_duration_s` from pipeline config | `health_checks/tier1/runtime_liveness.py:138` + `models/_config.py` | Present (field absent) | No |
| 23 | Implement `detect_auto_advance_wedge()` detector | `health_checks/tier1/decision_queue.py:detect_auto_advance_wedge` | Present (unpopulated) | No |
| 24 | Add `EGG_HEARTBEAT_RATE_LIMIT` config to PipelineConfig | `sandbox/egg_lib/orch_cli/_message.py:588` (`cmd_message_heartbeat`, no rate-limit config) | Present (not configurable) | No |
| 25 | Surface `noop_park_report()` and `exhausted_report()` in get_status | `event_loop/_supervisor.py:558` / `610` | Present (not surfaced) | No |

## Tier 5 — Won't do (out of scope or already handled)

| # | Improvement | File-and-symbol citation | Present? | Why not |
|---|------------|--------------------------|----------|---------|
| 26 | Rebuild the overseer agent | `overseer/` | Present | Working; problem is input pipeline, not the agent |
| 27 | Remove HealthMonitor tripwires | `health_monitor.py` | Present | They ARE wired and working; complement, don't replace |
| 28 | Add LLM classification to the hot path | `overseer/classifier.py` | Present | Expensive (Haiku calls); keep in overseer agent only |
| 29 | Change the 2-hour timeout default | `sandbox/llm/claude/config.py:23` | Present | 7200s is a reasonable budget; fix visibility, not the value |
| 30 | Rebuild the post-consensus stall detector | `overseer/monitor/_consensus_stall.py` | Present | Already handles post-consensus and incomplete-consensus stalls |
