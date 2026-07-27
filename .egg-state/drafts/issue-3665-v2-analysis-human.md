# Issue #3665 — Supervision Layer, Second Pass: Operator Summary

## What's broken

Over the last three days, two long pipeline runs exposed a supervision layer that
fails in both directions:

1. **Silent on real problems.** Seven agents across five different roles and all
   three phases entered repetition loops — the same tool call (or a short cycle of
   them) repeated until something outside the system intervened. None were detected
   by the product. All seven were caught by a hand-rolled monitor running in a
   terminal, and cleared by typing answers into the bus.

2. **Loud on healthy agents.** A convergence-stall alert fired at `[high]` priority
   against a coder whose peer heartbeat was seconds old, while the health monitor
   was concurrently logging that the same agent was alive every 30 seconds. The
   misdiagnosis cost more than the alert would have saved. Separately, two agents
   were killed at exactly two hours by a timeout they couldn't see coming, and both
   kills were counted as crashes against the fail-streak budget.

The result: operators stopped trusting the signal and went back to watching by
hand.

## The five "is this role stuck?" states

While building the hand-rolled monitor, the operator rewrote the "is this role
stuck?" rule five times. Each version looked correct and each produced false
positives against live data. The five states any detector must handle:

1. **Producers are legitimately podless between events** — no live pod, but
   correctly waiting for the next BRC event.
2. **Reviewers legitimately wait on upstream producers** — a reviewer with a
   pending ACK/NACK edge is not stalled.
3. **A declared no-op leaves its review edges pending forever** — a no-op park
   must release on fingerprint change or retry heartbeat.
4. **A NACK is a verdict** — it discharges the obligation just as an ACK does.
5. **Two of those five states are not visible in the status payload at all** —
   the `WAITING_ON_ROLE` self-report and the `awaiting_spawn` flag are not
   surfaced in the BRC status payload.

## Four problem areas

### Area 1: Signals that exist but are not consulted

The information needed to distinguish working from wedged is already computed,
often in the same process, seconds away from the code that decides — but it is not
read. Some detectors are also unreachable because an input they require is never
populated.

**Key finding:** The convergence-stall check in `event_loop/_loop.py`
(`_check_convergence_stall`, line 836) fires `high`-priority alerts without
consulting the health monitor's alive-signal gates. It checks:
- BRC bus activity (`bus_timestamp`) — yes
- In-flight jobs (`_live_keys`) — yes

But it does NOT check:
- Whether the role has an active one-shot Job via the health monitor's
  `_orchestrator_skip_tripwire` (health_monitor.py:507)
- Whether the role is a reviewer waiting on a live upstream producer via
  `_is_brc_idle` (health_monitor.py:524)
- Whether the role self-reported `WAITING_ON_ROLE` via the probe in
  `_supervisor.py` (line 824)

This is the exact false positive described in the issue: a coder with seconds-old
heartbeats gets flagged as stalled.

**What IS working in this area:**
- `PhaseStallDetector` (detection_plane.py:295) — correctly handles the #3230
  false-stall case with lifecycle-owner awareness.
- `DriverLivenessCheck` (driver_liveness.py:93) — three modes (dead/hung/no-progress).
- `detect_brc_thrash` (brc_thrashing.py:57) — detects NACK→propose→NACK cycles.
- `detect_container_restart_loop` (container_k8s.py:220) — detects crash-loops.

### Area 2: Session boundaries read as failures

An agent that simply ran long, exited cleanly without emitting the verdict it owed,
or whose pod vanished is currently indistinguishable from one that crashed.

**Key findings:**
- The 2-hour timeout is hardcoded in `shared/egg_agent/__main__.py:47`
  (default=7200). The agent is killed by `asyncio.timeout(7200)` in
  `client.py:765` with no pre-warning.
- A timeout exit (rc=-1 from `asyncio.TimeoutError`) maps to
  `JOB_OUTCOME_ABNORMAL` in `kubernetes_spawner/_models.py:80` (`outcome_for`),
  which increments the failure streak and consumes retry budget.
- There is no `JOB_OUTCOME_TIMEOUT` constant in `event_loop/__init__.py:172-177`.
- The `JobSupervisor` (`event_loop/_supervisor.py:158-229`) has `record_abort`
  (increments streak) and `record_success` (resets streak), but no
  `record_timeout` method.
- A clean exit (rc=0) that produced no BRC progress is classified as
  `JOB_OUTCOME_SUCCESS` and increments the no-op streak (#3425). But a clean exit
  that DID produce progress is also `JOB_OUTCOME_SUCCESS` — the two are
  indistinguishable at the outcome level.

### Area 3: Loops that nothing detects

The issue's empirical finding: "counting *tool inputs never issued before in the
session* over a trailing window separates a loop from work cleanly."

**Key finding:** No mechanism exists to count unique tool inputs over a trailing
window. The empirical finding has NOT been implemented.

- `overseer/classifier.py:224` (`detect_loop`) is LLM-based (Haiku tier) and only
  runs after an alert is raised — it cannot proactively detect loops.
- `health_checks/tier1/consensus_stall.py:217` (`detect_heartbeat_stall`) is
  deterministic and fires when BOTH `last_tool_call_age_s` AND
  `last_heartbeat_age_s` are stale. BUT it is **unreachable**:
  `snapshot_from_health_context` (detection_plane.py:534-538) creates
  `RunningAgent` entries from `context.live_container_ids` (a set of container ID
  strings), setting only `role`, `state="running"`, and `lifecycle_owner`. The
  `last_tool_call_age_s` and `last_heartbeat_age_s` fields are never populated —
  they default to `None`, so the detector's guard at line 242
  (`if tool_age is None or hb_age is None: continue`) always skips.
- `shared/egg_agent/client.py` truncates tool-call logs to 2000 chars but does
  not track unique inputs.
- `shared/egg_agent/tool_interceptor.py:29` intercepts tool calls for permission
  checking only — not for loop detection.

**Constraints from the incident analysis:**
- The pod log cannot support this — tool inputs are truncated at ~100 characters
  in the log capture, and distinct commands sharing a prefix collapse together.
- The cycle shape varies (single-input, 2-, 3-, and 8-cycles were observed), so
  anything keyed on a fixed shape or dominance misses most of them.

### Area 4: Alerts an operator cannot act on

Some alerts fire without the evidence that would make them readable. Some name a
threshold whose detector does not exist. Some outcomes are distinguishable only by
which log string was emitted.

**Key findings:**
- The escalation payload from `health_monitor.py:730-736` contains only
  `{type, agent_id, reason, timestamp}` — no structured evidence.
- The overseer fetches container logs separately (`_poll.py:78-85`), but the alert
  itself carries no heartbeat age, tool-call age, or consensus state.
- The convergence-stall alert (`_loop.py:942-957`) carries `anomaly`, `priority`,
  `summary`, `detail` but no `latest_heartbeat_age_s`, `latest_tool_call_age_s`,
  or `consensus_state`.

## What has already landed (verified)

All nine items in the issue's "already landed" list are present and verified in
the tree:

1. **Terminating-Job adoption** (#3613) — `kubernetes_spawner/_events.py:110`
   (`_await_terminating_event_jobs`)
2. **Worktree preservation** (#3644, #3647, #3652, #3654, #3656, #3660) —
   `kubernetes_spawner/_worktree.py`
3. **Cancel stops driver** (#3645, #3649, #3655, #3657) — `event_loop/_loop.py:1011`
   (`stop()`)
4. **Phase-gate approvals parse on first line** (#3648) —
   `overseer/monitor/_anomaly_checks.py:126`
5. **Never-heartbeated roles anchor at Job start** (#3612) —
   `health_monitor.py:248-275` (`_job_active_since`)
6. **Simplifier's first propose gated on upstream producer** (#3607) —
   `event_loop/_loop.py:683` (`noop_parked`)
7. **Green gate defaults to on** (#3609) — `models/_config.py:191`
8. **Decoding config recorded** (#3611, #3625) — `consensus_wrapper.py`
9. **Re-reviews blocking-only** (#3661) — `peer_consensus`

## Proposed work (four priorities)

### Priority 1: Tool-input loop detection

Add a deterministic detector that counts unique tool inputs over a trailing window.
Requires:
- Structured tool-call history in the session state store
- A new `detect_tool_input_loop` detector in `health_checks/tier1/`
- Wiring in `snapshot_from_health_context` to populate tool-call data

### Priority 2: Fix convergence-stall false positives

Modify `_check_convergence_stall` to consult alive-signal gates before alerting:
- Check `_live_keys` for in-flight jobs (already done)
- Check `WAITING_ON_ROLE` self-report for reviewer waits (NOT done — gap)
- Check `_orchestrator_skip_tripwire` for podless-between-events (NOT done — gap)
- Downgrade to `low` priority when waiting on a live producer

### Priority 3: Timeout vs. crash distinction

Add a `JOB_OUTCOME_TIMEOUT` category and surface the timeout to the agent:
- Add `JOB_OUTCOME_TIMEOUT` constant to `event_loop/__init__.py`
- Add `record_timeout` method to `JobSupervisor`
- Detect timeout exit code (-1) in `_EventJobStatusView.outcome_for`
- Emit pre-timeout heartbeat with countdown
- Do NOT increment failure streak on timeout

### Priority 4: Alert evidence bundling

Enrich `OVERSEER_ALERT` payloads with structured evidence:
- `latest_heartbeat_age_s`, `latest_tool_call_age_s`
- `last_progress_event`, `blocking_agents`, `consensus_state`
- `container_logs_tail` (already fetched, just include in payload)

## What was left out (and why)

- **The overseer poll cycle (`_poll_cycle`)** — CORRECTION: The overseer is NOT
  deprecated. Verified in the tree:
  - `grep -n deprecated orchestrator/overseer/monitor/__init__.py` returns nothing;
    `start()` carries no deprecation marker.
  - `overseer_poll_interval_seconds` (default 30, `overseer/monitor/__init__.py:80`)
    is live and consumed at `overseer/monitor/_anomaly_checks.py:218` and
    `overseer/monitor/_consensus_stall.py:113` and `:288`.
  - The overseer pod runs in this pipeline — `_run_pipeline.py:386` spawns it
    phase-scoped.
  - The standing-pod *respawn loop* was removed (#2270 slice-5,
    `_run_pipeline_support.py:76-84`), but the overseer pod itself is still spawned
    and runs its poll cycle.
  - The issue's "health monitor was logging, every 30 seconds, that the same agent
    was alive" refers to the orchestrator-side health monitor's alive-signal gate
    (`health_monitor.py:928`, "Heartbeat alert deferred by alive-signal gate"),
    NOT the overseer.
  - The overseer has two issues that belong in the candidate list (below), not in
    the four priorities: #3577 (`detect_phase_long_running` referenced by config
    but absent) and #3212 (no crash-respawn backoff — single spawn with no retry).
- **The `detect_loop` / `classify_activity_pattern` LLM classifiers** — These are
  Haiku-tier classifiers that run only after an alert is raised. They are not the
  right tool for proactive loop detection. The issue explicitly states the
  empirical finding is about counting unique tool inputs, which is deterministic.
- **The 4-hour K8s `active_deadline_seconds`** — This is a K8s-level safety net
  (`kubernetes_client.py:350`), not an application-level timeout. It is correctly
  set higher than the 2-hour agent timeout. No change needed, but the distinction
  between K8s deadline kills (exit 137) and agent timeouts (exit -1) is not
  surfaced — this is candidate #3.

## Candidate list (ranked)

Ranked by (operator pain × cheapness to build). Each entry is an improvement NOT
proposed in priorities 1–4. Every entry carries a file-and-symbol citation and an
explicit PRESENT or ABSENT verdict for the thing in the tree today.

### 1. [ABSENT] Overseer phase-duration affordance (#3577)
**Citation:** `models/_config.py:428` (`overseer_long_running_phase_seconds`,
default 3600, description references `detect_phase_long_running`);
`overseer/monitor/_anomaly_checks.py` and `overseer/monitor/_consensus_stall.py`
(consumers of `overseer_poll_interval_seconds`).
**Verdict:** ABSENT. The config field `overseer_long_running_phase_seconds` exists
and is read at `_routes_status.py:135-136` for status reporting, but no
`detect_phase_long_running` function exists anywhere in the codebase. The overseer
has no phase-duration detector — a phase can run indefinitely without the overseer
noticing. The orchestrator-side `detect_duration_drift` (runtime_liveness.py:138)
fills a similar role but is itself unwired (see candidate #4).
**Ranking rationale:** High operator pain (long-running phases waste resources and
delay feedback) × low build cost (one detector function + registration).

### 2. [ABSENT] Overseer crash-respawn backoff (#3212)
**Citation:** `routes/pipelines/_run_pipeline.py:385-411` (overseer spawn wrapped
in try/except, continues without monitoring on failure);
`_run_pipeline_support.py:76-84` (standing-pod respawn loop removed).
**Verdict:** ABSENT. The overseer is spawned once per phase. If it crashes (e.g.,
due to a route rate limit), there is no backoff or retry — the spawn is wrapped in
a try/except that logs a warning and continues without monitoring. The standing-pod
respawn loop that used to keep it alive was removed (#2270 slice-5). There is no
crash-respawn loop for the overseer — it is a single spawn with no retry.
**Ranking rationale:** Medium operator pain (overseer death = blind spot for the
rest of the phase) × medium build cost (need backoff logic + spawn retry).

### 3. [ABSENT] K8s deadline kill vs. agent timeout distinction
**Citation:** `kubernetes_client.py:350` (`active_deadline_seconds=14400`, 4h K8s
deadline); `shared/egg_agent/__main__.py:47` (2h agent timeout, default=7200);
`kubernetes_spawner/_models.py:80` (`outcome_for` maps all non-zero exits to
`JOB_OUTCOME_ABNORMAL`).
**Verdict:** ABSENT. A K8s deadline kill produces exit code 137 (SIGKILL), while
an agent timeout produces exit code -1 (from `asyncio.timeout`). Both map to
`JOB_OUTCOME_ABNORMAL` and both increment the failure streak. There is no way to
distinguish a K8s-level deadline from an agent-level timeout at the outcome
classification level. The 4h K8s deadline and 2h agent timeout are independent
kill horizons, but no supervision path distinguishes them.
**Ranking rationale:** Medium operator pain (misclassified kills consume retry
budget) × low build cost (add exit-code discrimination in `outcome_for`).

### 4. [PRESENT but UNWIRED] `detect_duration_drift` detector
**Citation:** `health_checks/tier1/runtime_liveness.py:138`
(`detect_duration_drift`); `health_checks/detection_plane.py:525-532`
(`snapshot_from_health_context` does NOT populate `expected_duration_s`).
**Verdict:** PRESENT but UNWIRED. The detector fires when
`started_age_s > expected_duration_s * factor`, but `expected_duration_s` is only
set in `phase_state` if the pipeline config provides it. The
`snapshot_from_health_context` function does NOT populate
`expected_duration_s` from the pipeline config. **Fix:** populate it from
`pipeline.config.consensus_timeout_minutes` or a similar config field.
**Ranking rationale:** Medium operator pain (phase-duration drift goes undetected)
× very low build cost (one-line fix in `snapshot_from_health_context`).

### 5. [PRESENT but UNWIRED] `detect_heartbeat_stall` detector
**Citation:** `health_checks/tier1/consensus_stall.py:217`
(`detect_heartbeat_stall`); `health_checks/detection_plane.py:534-538`
(`snapshot_from_health_context` does NOT populate `last_tool_call_age_s` /
`last_heartbeat_age_s`).
**Verdict:** PRESENT but UNWIRED. The detector fires when BOTH
`last_tool_call_age_s` AND `last_heartbeat_age_s` are stale. But
`snapshot_from_health_context` only creates `RunningAgent` entries from
`context.live_container_ids` (container ID strings), setting only `role`,
`state`, and `lifecycle_owner`. The age fields are never populated — they default
to `None`, so the detector's guard at line 242 always skips. **Fix:** populate
these fields from the health monitor's state or the session state store.
**Ranking rationale:** High operator pain (heartbeat stalls are the most common
wedge) × low build cost (populate two fields in the snapshot builder).

### 6. [ABSENT] Structured tool-call history for loop detection
**Citation:** `shared/egg_agent/client.py:31` (`_MAX_TOOL_CONTENT_LOG_LEN = 2000`);
`shared/egg_agent/tool_interceptor.py:29` (intercepts tool calls for permissions
only).
**Verdict:** ABSENT. Tool calls are logged (truncated to 2000 chars) but not
tracked in a structured history that could be queried for uniqueness. The
`tool_interceptor.py` module intercepts calls but only for permission checking,
not for loop detection. A structured tool-call history (tool name + input hash)
would enable the deterministic loop detector in priority 1.
**Ranking rationale:** High operator pain (seven livelocks went undetected) ×
medium build cost (add structured logging + storage in the SDK client).

### 7. [ABSENT] Alert evidence bundling
**Citation:** `health_monitor.py:730-736` (escalation dict has only
`{type, agent_id, reason, timestamp}`); `overseer/monitor/_poll.py:78-85`
(fetches container logs separately); `event_loop/_loop.py:942-957`
(convergence-stall alert carries no structured evidence).
**Verdict:** ABSENT. The escalation payload from `health_monitor.py` contains only
`{type, agent_id, reason, timestamp}` — no structured evidence. The overseer
fetches container logs separately, but the alert itself carries no heartbeat age,
tool-call age, or consensus state. An operator reading the alert must manually
correlate multiple sources. The convergence-stall alert goes directly to
`OVERSEER_ALERT` via the `convergence_stall_notifier` callback, bypassing the
overseer's own filtering.
**Ranking rationale:** Medium operator pain (misdiagnosis costs more than missed
alerts) × medium build cost (enrich payload structure + include evidence).

### 8. [ABSENT] Timeout outcome category in JobSupervisor
**Citation:** `event_loop/__init__.py:172-177` (`JOB_OUTCOME_*` constants, no
`TIMEOUT`); `event_loop/_supervisor.py:158-229` (`record_abort` increments streak);
`kubernetes_spawner/_models.py:80` (`outcome_for` maps exit -1 to `ABNORMAL`).
**Verdict:** ABSENT. A timeout (exit code -1 from `asyncio.timeout` in
`client.py:765`) is classified as `JOB_OUTCOME_ABNORMAL`, incrementing the failure
streak and consuming retry budget. There is no `JOB_OUTCOME_TIMEOUT` constant. The
2-hour timeout in `__main__.py:47` is invisible to the agent. This is the same
issue as priority 3, but listed here as a candidate because the priority proposes
the full solution (timeout category + agent-visible warning + streak protection)
while this entry is just the outcome-category gap.
**Ranking rationale:** High operator pain (timeouts consume retry budget) × low
build cost (add constant + outcome mapping).

### 9. [ABSENT] Agent-visible timeout warning
**Citation:** `shared/egg_agent/__main__.py:47` (default=7200);
`shared/egg_agent/client.py:765` (`asyncio.timeout(7200)`).
**Verdict:** ABSENT. The agent has no way to know it will be killed at 2 hours.
The timeout is a hard process-level kill with no pre-warning. The agent cannot
self-terminate gracefully or emit a final verdict. (Same as priority 3, but listed
as a separate candidate because it's the agent-visibility half of the solution.)
**Ranking rationale:** Medium operator pain (agents can't clean up before being
killed) × low build cost (emit a heartbeat with countdown before timeout).

### 10. [PRESENT but NOISY] Overseer poll cycle alert filtering
**Citation:** `overseer/monitor/_poll.py:71` (`_filter_current_phase_agents`);
`event_loop/_loop.py:942-957` (convergence-stall alert bypasses overseer).
**Verdict:** PRESENT but NOISY. The overseer filters alerts to current-phase
agents, but the convergence-stall alert from the event loop bypasses the overseer
entirely — it goes directly to `OVERSEER_ALERT` via the
`convergence_stall_notifier` callback. The overseer's filtering does not apply.
**Ranking rationale:** Low operator pain (false positives are annoying but not
critical) × low build cost (route convergence-stall alerts through the overseer
filter).

### 11. [ABSENT] Session-boundary outcome classification
**Citation:** `event_loop/_supervisor.py:37` (`record_success` increments no-op
streak); `event_loop/_loop.py:88-91` (`JOB_OUTCOME_SUCCESS`).
**Verdict:** ABSENT. A clean exit (rc=0) that produced no BRC progress is
classified as `JOB_OUTCOME_SUCCESS` and increments the no-op streak (#3425). But a
clean exit that DID produce progress is also `JOB_OUTCOME_SUCCESS` — the two are
indistinguishable at the outcome level. The no-op streak mechanism (#3425) is the
closest thing, but it only catches repeated no-ops of the SAME dedupe key, not a
one-shot clean exit that forgot to emit a verdict.
**Ranking rationale:** Medium operator pain (clean exits without verdicts are
silent failures) × medium build cost (distinguish progress-producing from
progress-less exits).

### 12. [PRESENT] No-op park mechanism (#3425)
**Citation:** `event_loop/_supervisor.py:37-98` (`record_success` increments no-op
streak); `event_loop/_supervisor.py:665-779` (`noop_parked`).
**Verdict:** PRESENT and FUNCTIONAL. A clean exit with no BRC progress re-derives
the identical dedupe key, climbing the no-op streak. After
`SUPERVISION_NOOP_STREAK_PARK` (3) clean completions, the arm parks.
Self-releases on contract-decision fingerprint change or retry heartbeat. This
handles the "declared no-op" case from the issue.
**Ranking rationale:** Listed for completeness — this is the one session-boundary
mechanism that works correctly. No action needed.

### 13. [PRESENT] WAITING_ON_ROLE self-report probe (#3520)
**Citation:** `event_loop/_supervisor.py:924-988` (`_emit_noop_alert` with
`waiting` probe); `event_loop/_supervisor.py:824` (`_probe_waiting_on`).
**Verdict:** PRESENT but NARROWLY APPLIED. The probe is only consulted in
`_emit_noop_alert` for no-op parks. It is NOT consulted in:
- `_check_convergence_stall` (the false-positive convergence-stall alert)
- `health_monitor.py:check_heartbeats` (the heartbeat timeout alert)
This is the exact gap that produced the false positive described in the issue.
**Ranking rationale:** High operator pain (false positives erode trust) × low
build cost (consult the existing probe in two more call sites).

### 14. [ABSENT] Pod-vanishing detection
**Citation:** `kubernetes_monitor.py:458-463` (reconciliation marks agents
FAILED when container_id not in live_ids);
`health_checks/tier1/container_liveness.py:88-108` (`ContainerLivenessCheck`).
**Verdict:** PARTIALLY PRESENT. `ContainerLivenessCheck` detects missing
containers but marks them as `FAILED` (infrastructure problem), not as a session
boundary. A pod that vanishes (node drain, eviction) is indistinguishable from a
crash. The `agent_log_store` preserves logs, but the outcome classification
doesn't distinguish "pod vanished" from "agent crashed."
**Ranking rationale:** Low operator pain (pod vanishing is rare) × medium build
cost (add outcome classification for vanished pods).

### 15. [ABSENT] Fail-streak budget protection for timeouts
**Citation:** `event_loop/_supervisor.py:158-229` (`record_abort` increments
streak); `event_loop/_supervisor.py:32` (`JOB_OUTCOME_ABNORMAL`).
**Verdict:** ABSENT. A timeout exit (rc=-1) maps to `JOB_OUTCOME_ABNORMAL`, which
increments the failure streak toward `SUPERVISION_FAILURE_STREAK_ALERT` (10) and
engages `AGENT_FAILED`. The issue states "both kills were counted as crashes
against the fail-streak budget." There is no timeout-specific path.
**Ranking rationale:** High operator pain (timeouts consume retry budget) × low
build cost (route timeout exits to a separate streak counter).

### 16. [PRESENT] BRC thrash detector
**Citation:** `health_checks/tier1/brc_thrashing.py:57` (`detect_brc_thrash`).
**Verdict:** PRESENT and FUNCTIONAL. Detects NACK→propose→NACK cycles (threshold 3)
and late CONFIRMED-then-re-NACK. Registered in the detection plane. This is the
one detector that correctly handles a loop pattern.
**Ranking rationale:** Listed for completeness — this is the one loop detector
that works. No action needed.

### 17. [PRESENT] Container restart loop detector
**Citation:** `health_checks/tier1/container_k8s.py:220`
(`detect_container_restart_loop`).
**Verdict:** PRESENT and FUNCTIONAL. Detects crash-loops (restart count ≥ 3).
Registered in the detection plane.
**Ranking rationale:** Listed for completeness. No action needed.

### 18. [PRESENT] Driver liveness check
**Citation:** `health_checks/tier1/driver_liveness.py:93` (`DriverLivenessCheck`).
**Verdict:** PRESENT and FUNCTIONAL. Three modes: `driver_dead`, `driver_hung`,
`driver_no_progress`. Runs on RUNTIME_TICK. This caught the #3540 incident (11+
hours of RUNNING with zero spawns).
**Ranking rationale:** Listed for completeness. No action needed.

### 19. [PRESENT] Phase stall detector (lifecycle-owner-aware)
**Citation:** `health_checks/detection_plane.py:295` (`PhaseStallDetector`).
**Verdict:** PRESENT and FUNCTIONAL. The #3230 fix: fires only when phase is
RUNNING, zero running agents, NO lifecycle owner queued, no HITL pending, and past
grace window. This is the model the other detectors should follow.
**Ranking rationale:** Listed for completeness. No action needed.

### 20. [PRESENT] Post-consensus-timeout absolute cap
**Citation:** `models/_config.py:167-177`
(`post_consensus_max_total_seconds` default 14400 = 4 hours);
`routes/pipelines/_run_concurrent.py:1227-1234`.
**Verdict:** PRESENT. The post-timeout poll loop has an absolute cap of 4 hours
(14400s). This prevents unbounded waiting but does not address the 2-hour agent
timeout being counted as a crash.
**Ranking rationale:** Listed for completeness. No action needed.

### 21. [ABSENT] Tool-input truncation in pod logs
**Citation:** `agent_log_store.py:49-51` (MAX_LOG_BYTES = 1 MiB tail);
`shared/egg_agent/client.py:32` (`_MAX_TOOL_CONTENT_LOG_LEN = 2000`).
**Verdict:** PARTIALLY ADDRESSED. The issue states "the pod log cannot support
this, because tool inputs are truncated at about 100 characters." The
`agent_log_store` captures the tail (1 MiB), and the SDK client truncates
individual log entries to 2000 chars. But neither captures the full structured
tool input — the truncation is at the log level, not the data level. A structured
tool-call history (candidate #6) would bypass this.
**Ranking rationale:** Low operator pain (pod logs are a secondary signal) × low
build cost (already partially addressed by candidate #6).
