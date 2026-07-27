# Supervision, Second Pass — Plain-English Summary (Issue #3665)

## The core problem

The system that is supposed to tell the difference between a working agent and a stuck one
failed in both directions:

- **Silent on real problems:** Seven agents across five roles and three phases got stuck in
  repetition loops. None were detected by the product. All were caught by a hand-rolled
  monitor the operator was running in a terminal.
- **Loud on healthy agents:** A convergence-stall alert fired at high priority against a
  coder whose heartbeat was seconds old, while the health monitor was concurrently logging
  that same agent as alive. The operator misdiagnosed this as the cause of a 110-minute
  work loss it had nothing to do with.
- **Unpredictable timeouts:** Two agents were killed at exactly two hours by a timeout they
  could not see coming, and both kills were counted as crashes against the fail-streak budget.

## Why this happened

There are three parallel supervision mechanisms, but the newest and most promising one —
the detection plane from issue #2270 — is **completely unwired** in production:

1. **The detection plane exists but never runs.** The function
   `snapshot_from_health_context()` builds a data snapshot for the detectors, but it
   only fills in 5 of 13 fields. The other 8 fields — including `consensus`,
   `container_transitions`, `runtime`, `midturn_messages`, and `decision_state` — are
   all left empty. Every detector that reads those fields is silently inert.
   Additionally, the function that would actually invoke the detection plane
   (`_run_overseer_detection_plane()`) has zero call sites in production code.

2. **The HealthMonitor tripwires are wired but on a different data path.** They run via
   the kubernetes_monitor RUNTIME_TICK sweep, but they don't share data with the
   detection plane. So the detection plane can't see what the HealthMonitor already knows.

3. **The overseer agent is spawned per-phase but can't get input.** The overseer agent
   (which does LLM-based classification) IS spawned, but its poll cycle — the code that
   gathers data and feeds it to the classifier — has no production construction site.
   The classifier functions exist (`detect_loop()`, `classify_activity_pattern()`) but
   are never called because the poll cycle that should call them is dead code.

The root cause: the deterministic detectors that could catch loops never run, and the
LLM-based overseer that does run has no reliable input pipeline.

## What has already been fixed (do not rebuild)

All 9 items the issue says have already landed are confirmed present in the tree, verified via
file-and-symbol citations (the per-item citations are the real evidence, not commit messages):

| Item | What it does |
|------|-------------|
| #3613 | Terminating-Job adoption on the event-loop respawn path |
| #3644+#3647+#3652+#3654+#3656+#3660 | Worktree uncommitted work preserved before reset |
| #3645+#3649+#3655+#3657 | Cancel stops the driver |
| #3648 | Phase-gate approvals parse on their first line |
| #3612 | Never-heartbeated roles anchor at Job start |
| #3607 | Simplifier's first propose is gated on its upstream producer |
| #3609+#3628 | Green gate defaults to on; a red escalates to HITL |
| #3611+#3625 | Every routed call records its decoding config |
| #3661 | Re-reviews are blocking-only |

## Proposed work — four areas

### Area 1: Signals that exist but are not consulted

**The problem:** The detection plane's snapshot builder fills in only 5 of 13 fields.
The other 8 are empty, making every detector that reads them silently inert.

**Proposed work (in order):**

1. **Populate the `runtime` section** — Feed driver heartbeat ages (how long since the
   last tick, how long since spawn) into the snapshot. This activates the
   run-pipeline-thread-liveness detector and the DriverLivenessCheck. Small effort, low risk.

2. **Populate the `consensus` section** — Feed the peer consensus tracker's evaluation
   into the snapshot. This activates the BRC thrash detector, the incomplete-consensus
   deferral detector, and consensus field readers in the PhaseStallDetector. Medium effort,
   medium risk.

3. **Populate `container_transitions`** — Feed the kubernetes_monitor's pod-state
   transition log into the snapshot. This activates container death, OOM-evicted,
   restart-loop, and self-injection detectors. Medium effort, medium risk.

4. **Fix the `running_agents` data** — Currently the role field is set to the container ID
   instead of the agent's actual role, and `last_tool_call_age_s` / `last_heartbeat_age_s`
   are never populated. Fix both so the heartbeat-stall detector can work. Medium effort,
   medium risk.

5. **Wire the detection plane into the runtime tick path** — Call
   `_run_overseer_detection_plane()` from the kubernetes_monitor's runtime tick checks,
   routing findings that need human judgment to the overseer agent and routine findings
   to the corrective executor. Large effort, high risk (new code path on the hot loop).

### Area 2: Session boundaries read as failures

**The problem:** An agent killed by the 2-hour timeout (7200 seconds) exits with code -1,
which the supervisor classifies as a crash (not a clean exit), incrementing the failure
streak. The agent never sees the timeout coming because it's a server-side `asyncio.timeout`
wrapper, not a signal sent to the agent.

**Proposed work (in order):**

1. **Warn the agent before the timeout** — Emit a heartbeat at 90-minute intervals saying
   "approaching 2h timeout" so the agent knows it's time-bounded. Small effort, low risk.

2. **Classify timeout-killed pods distinctly** — When exit code is -1 and the timeout
   fired, classify it as a clean timeout, not a crash. Medium effort, medium risk.

3. **Make the timeout configurable per-pipeline** — Add `agent_timeout_seconds` to
   PipelineConfig (default 7200) and pass it as `EGG_AGENT_TIMEOUT` so the agent can
   self-report its remaining budget. Small effort, low risk.

### Area 3: Loops that nothing detects

**The problem:** The issue's key empirical finding is that counting *tool inputs never
issued before in the session* over a trailing window cleanly separates a loop from real
work — a working agent produces new tool inputs, a loop produces none. This signal is
not computed anywhere. The existing `detect_loop()` and `classify_activity_pattern()`
in `overseer/classifier.py` use an LLM but require the overseer agent to be running,
and its poll cycle has no production construction site.

**Proposed work (in order):**

1. **Implement a deterministic unique-tool-input counter** — A new detector
   `detect_tool_input_loop()` that reads `midturn_messages` from the snapshot (which
   must be populated first — see Area 1) and counts distinct tool-input strings over
   a trailing window. If the count is zero for N consecutive polls, fire an alert.
   Medium effort, medium risk (depends on midturn_messages being populated).

2. **Wire the existing classifier into the overseer agent's prompt** — Once the poll
   cycle is fixed (Area 1, step 5), the `classify_activity_pattern()` function becomes
   available. Large effort, high risk (depends on step 5).

3. **Improve log capture fidelity** — The agent log store captures pod logs at removal,
   but the kubernetes log API truncates lines at ~100 characters. Increase fidelity so
   the unique-tool-input counter has enough data. Small effort, low risk.

### Area 4: Alerts an operator cannot act on

**The problem:** Alerts fire without evidence, name thresholds whose detectors don't
exist, or describe outcomes distinguishable only by log strings.

**Proposed work (in order):**

1. **Enrich alert payloads with evidence** — Add structured evidence (container logs,
   BRC state, tracker evaluation) to OVERSEER_ALERT messages so the operator can
   diagnose without grepping. Small effort, low risk.

2. **Fix the convergence-stall false positive** — The convergence-stall alert and the
   alive-signal gate use different timestamp sources. Unify them so the same "bus
   activity" signal is used for both alerting and deferral. Medium effort, medium risk.

3. **Name the 2-hour timeout explicitly in alerts** — When a pod is killed by timeout,
   the alert should say "killed by 2h agent timeout" not "container exited with code -1."
   Small effort, low risk.

4. **Route detection-plane findings to the operator alert surface** — Once Area 1 step 5
   is done, route findings to the same OVERSEER_ALERT / HITL / Slack surfaces the
   overseer uses, so the operator sees one consistent alert stream. Medium effort,
   medium risk.

## Ordering and dependencies

1. **Area 1 steps 1-4** (populate snapshot fields) — prerequisite for everything else.
   Without the data in the snapshot, no detector can work.
2. **Area 1 step 5** (wire detection plane into runtime tick) — enables Areas 3 and 4.
   The loop detector and alert routing both need the detection plane to actually run.
3. **Area 3 step 1** (deterministic loop detector) — depends on the snapshot being
   populated (Area 1 steps 1-4).
4. **Area 2 steps 1-3** (timeout visibility and classification) — independent, low risk.
   Can be done in parallel with Area 1.
5. **Area 4 steps 1-4** (alert evidence and false-positive fixes) — step 2 is independent;
   steps 3-4 partially depend on Area 1 step 5.

## What to leave out

- **Do not rebuild the overseer agent.** The per-phase overseer spawn is working. The
  problem is its input pipeline (the poll cycle), not the agent itself.
- **Do not remove the HealthMonitor tripwires.** They are wired and working for
  heartbeat/progress/container-exit detection. The detection plane should complement,
  not replace them.
- **Do not add LLM classification to the hot path.** The classifier functions in
  `overseer/classifier.py` are expensive (Haiku calls). Keep them in the overseer agent,
  not in the deterministic detection plane.
- **Do not change the 2-hour timeout default.** 7200 seconds is a reasonable budget.
  The fix is to make it visible and to classify timeout-kills distinctly, not to
  shorten it.

## Key technical detail: the five states that cause false positives

The operator noted that five states cause false positives in "is this role stuck?" rules:

1. **Producers legitimately podless between events** — handled by `_orchestrator_skip_tripwire()`
   which only checks roles with active one-shot Jobs.
2. **Reviewers waiting on upstream producers** — handled by `_is_brc_idle()` in health_monitor.py.
3. **Declared no-op leaves review edges pending forever** — handled by `noop_parked()` in
   `event_loop/_supervisor.py`.
4. **NACK is a verdict that discharges the obligation** — handled in `peer_consensus/_state.py`.
5. **Two states not visible in the status payload at all** — the `consensus` field and
   `container_transitions` field in the detection plane snapshot are never populated.
   This is exactly what Area 1 fixes.

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
| 24 | Add `EGG_HEARTBEAT_RATE_LIMIT` config to PipelineConfig | `sandbox/egg_lib/orch_cli/_message.py:cmd_message_heartbeat` (line 588) + `EGG_HEARTBEAT_RATE_LIMIT` env (line 633 is 429 retry-after backoff, not heartbeat cadence) | Present (hardcoded) | No |
| 25 | Surface `noop_park_report()` and `exhausted_report()` in get_status | `event_loop/_supervisor.py:558` / `610` | Present (not surfaced) | No |

## Tier 5 — Won't do (out of scope or already handled)

| # | Improvement | File-and-symbol citation | Present? | Why not |
|---|------------|--------------------------|----------|---------|
| 26 | Rebuild the overseer agent | `overseer/` | Present | Working; problem is input pipeline, not the agent |
| 27 | Remove HealthMonitor tripwires | `health_monitor.py` | Present | They ARE wired and working; complement, don't replace |
| 28 | Add LLM classification to the hot path | `overseer/classifier.py` | Present | Expensive (Haiku calls); keep in overseer agent only |
| 29 | Change the 2-hour timeout default | `sandbox/llm/claude/config.py:23` | Present | 7200s is a reasonable budget; fix visibility, not the value |
| 30 | Rebuild the post-consensus stall detector | `overseer/monitor/_consensus_stall.py` | Present | Already handles post-consensus and incomplete-consensus stalls |
