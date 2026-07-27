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

- **The overseer poll cycle (`_poll_cycle`)** — This is the deprecated
  standing-pod shape. The issue's "health monitor was logging, every 30 seconds"
  refers to the old in-pod overseer. In the current architecture, the
  orchestrator-side detection plane (`health_checks/`) and the event loop's
  convergence-stall check are the active supervision paths. The overseer is now
  on-demand (adjudication only). Changes to the overseer poll cycle are not
  proposed because it is deprecated (#2270 slice-4).
- **The `detect_loop` / `classify_activity_pattern` LLM classifiers** — These are
  Haiku-tier classifiers that run only after an alert is raised. They are not the
  right tool for proactive loop detection. The issue explicitly states the
  empirical finding is about counting unique tool inputs, which is deterministic.
- **The 4-hour K8s `active_deadline_seconds`** — This is a K8s-level safety net,
  not an application-level timeout. It is correctly set higher than the 2-hour
  agent timeout. No change needed.
