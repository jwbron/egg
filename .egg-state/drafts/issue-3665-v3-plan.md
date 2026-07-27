# Issue #3665 — Implementation Plan

**Supervision, second pass: wire the detection plane + fix false positives + classify timeout kills**

Plan artifact (task_planner). Grounded live against `main @ 1cd0c8ad7` on 2026-07-27;
every refiner anchor re-verified this session. The refine phase produced a 30-item
ranked candidate list with file-and-symbol citations; this plan selects the Tier 1
and Tier 2 items (per the gate's scope discipline) and decomposes them into slices
and tasks. Tier 3–5 are input to the gate, not a work queue.

## The four areas (from the analysis)

1. **Signals that exist and are not consulted** — The #2270 detection plane is fully
   implemented but completely unwired: `snapshot_from_health_context()` populates only
   5 of 13 `EventStreamSnapshot` fields, and `_run_overseer_detection_plane()` has zero
   call sites.
2. **Session boundaries read as failures** — The 2-hour `ClaudeConfig.timeout` kills
   agents with exit code -1, classified as a crash by `_classify_exit()`, incrementing
   the failure streak. The agent never sees the timeout coming.
3. **Loops that nothing detects** — No deterministic unique-tool-input counter exists.
   The LLM-based `detect_loop()` / `classify_activity_pattern()` require the overseer's
   poll cycle, which has no production construction site.
4. **Alerts an operator cannot act on** — Alerts fire without evidence; the
   convergence-stall alert uses a divergent timestamp source from the alive-signal gate.

## What to leave out (confirmed from the analysis)

- Do not rebuild the overseer agent (working; problem is input pipeline).
- Do not remove the HealthMonitor tripwires (wired and working).
- Do not add LLM classification to the hot path.
- Do not change the 2-hour timeout default (7200s is reasonable).

---

## Slices (four independent PRs, one per area)

| Slice | PR | Theme | Surface | Risk |
|-------|----|-------|---------|------|
| slice-1 | PR 1 | Wire the detection plane into RUNTIME_TICK | `health_checks/detection_plane.py`, `kubernetes_monitor.py`, `routes/pipelines/_overseer.py` | High (hot-loop path) |
| slice-2 | PR 2 | Deterministic loop detector + midturn_messages population | `health_checks/tier1/`, `health_checks/detection_plane.py`, `agent_log_store.py`, `kubernetes_client.py` | Medium |
| slice-3 | PR 3 | Timeout visibility and classification | `kubernetes_monitor.py`, `event_loop/_supervisor.py`, `models/_config.py`, `sandbox/llm/claude/config.py`, `sandbox/egg_lib/orch_cli/_message.py` | Medium |
| slice-4 | PR 4 | Alert evidence + false-positive fixes | `overseer/monitor/_alerting.py`, `event_loop/_loop.py`, `health_monitor.py` | Medium |

**Independence:** All four slices touch disjoint file sets (verified via grep sweep).
slice-1 populates snapshot fields that slice-2's loop detector depends on, but the
dependency is on the *data* (midturn_messages field), not on the wiring code path —
slice-2 can be written and tested against a manually-populated snapshot, and the
detection plane wiring in slice-1 simply makes it live in production. slice-3 and
slice-4 are fully independent.

---

## Grounded anchors (verified live @ 1cd0c8ad7, 2026-07-27)

**slice-1 — Detection plane wiring**

- `health_checks/detection_plane.py:511` — `snapshot_from_health_context()` currently
  populates only `snapshot_id`, `pipeline_id`, `phase`, `running_agents`, `phase_state`.
  The `EventStreamSnapshot` dataclass (lines 106-127) has 8 additional fields left empty:
  `consensus`, `container_transitions`, `runtime`, `cost_counters`,
  `gateway_error_counters`, `midturn_messages`, `git_state`, `decision_state`.
- `health_checks/detection_plane.py:536` — `RunningAgent(role=str(cid), ...)` uses
  container ID as role (bug). Fields `last_tool_call_age_s` / `last_heartbeat_age_s`
  (lines 89-90) are never populated.
- `routes/pipelines/_overseer.py:309` — `_run_overseer_detection_plane()` is defined but
  has zero call sites. Imported in `__init__.py:1277` but never called.
- `kubernetes_monitor.py:221` — `_run_runtime_tick_checks()` is the RUNTIME_TICK sweep,
  called from `_check_pod` (line 218) and `_reconciliation_sweep` (line 616). Currently
  calls `runner.run(ctx, HealthTrigger.RUNTIME_TICK)` but never calls
  `runner.run_detection_plane()`.
- `health_checks/runner.py:159` — `run_detection_plane()` method exists but is never
  invoked.
- `driver_heartbeat.py:41/54/67` — `record_tick()`, `record_spawn()`, `tick_age_seconds()`,
  `spawn_age_seconds()` are the runtime signal sources.
- `peer_consensus/__init__.py:250` — `get_peer_consensus_tracker()` returns the tracker.
  `_queries.py:110` — `get_latest_progress_timestamp()`, `_queries.py:68` —
  `consensus_state_fingerprint()`, `_queries.py:262` — `get_fully_acked_producers()`.
- `kubernetes_monitor.py:1148` — `_classify_exit()` classifies exit codes.

**slice-2 — Loop detector**

- `health_checks/detection_plane.py:126` — `midturn_messages` field on
  `EventStreamSnapshot` (currently never populated).
- `health_checks/tier1/` — existing tier-1 detector directory; new
  `detect_tool_input_loop()` goes here.
- `shared/egg_agent/client.py:765` — `asyncio.timeout(timeout)` wraps agent execution.
  Tool calls are logged at lines 819-830 via `logger.info("Tool call", ...)`.
- `orchestrator/kubernetes_client.py:455` — `read_job_log_snapshot()` with
  `tail_lines=2000`; k8s log API truncates at ~100 chars per line.
- `orchestrator/agent_log_store.py:51` — `MAX_LOG_BYTES = 1 * 1024 * 1024` (1 MiB).

**slice-3 — Timeout visibility and classification**

- `sandbox/llm/claude/config.py:23` — `ClaudeConfig.timeout = 7200` (2 hours).
- `shared/egg_agent/client.py:223` — `timeout: int = 7200` in `run_agent_async`.
- `shared/egg_agent/client.py:765` — `async with asyncio.timeout(timeout)` — server-side
  wrapper, invisible to the agent.
- `shared/egg_agent/client.py:903-921` — `TimeoutError` handler returns
  `AgentResult(returncode=-1, error="Timed out after {timeout} seconds")`.
- `kubernetes_monitor.py:1148` — `_classify_exit()` treats -1 as FAILED (not 0/143).
- `orchestrator/event_loop/_supervisor.py:145` — `record_abort()` increments
  `_streaks[dedupe_key]`; `SUPERVISION_FAILURE_STREAK_ALERT` at 10.
- `orchestrator/models/_config.py:52` — `PipelineConfig` class; no `agent_timeout_seconds`
  field exists.
- `sandbox/egg_lib/orch_cli/_message.py:588` — `cmd_message_heartbeat()`; no
  timeout-warning logic.

**slice-4 — Alert evidence + false-positive fixes**

- `overseer/monitor/_alerting.py:56` — `_broadcast_alert()` sends OVERSEER_ALERT with
  minimal payload (anomaly_type, agent_role, message, priority). No structured evidence.
- `event_loop/_loop.py:859` — `_check_convergence_stall()` uses
  `tracker.get_latest_progress_timestamp()` as the bus-timestamp anchor.
- `health_monitor.py:388` — `_has_recent_peer_progress()` uses the same tracker method
  but with a different gate window (`orchestrator_alert_progress_gate_seconds`).
- `health_checks/detection_plane.py:540` — snapshot builder returns
  `EventStreamSnapshot(...)` with only 5 fields; `Finding.to_dict()` (types.py:119)
  already carries `evidence` but `_emit_finding` (runner.py:184) only emits on the
  event bus — no operator alert surface.

---

## Slice 1 — Wire the detection plane into RUNTIME_TICK (PR 1)

**Goal:** Make the #2270 detection plane actually run in production by (a) populating
the snapshot fields it needs, and (b) calling `_run_overseer_detection_plane()` from
the RUNTIME_TICK path.

**Tasks:**

- **TASK-1-1** — Populate `runtime` section of snapshot. Wire `driver_heartbeat.tick_age_seconds()`
  and `spawn_age_seconds()` into `snapshot_from_health_context()` under `raw["runtime"]`.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/health_checks/detection_plane.py`

- **TASK-1-2** — Populate `consensus` section of snapshot. Wire
  `peer_consensus.get_peer_consensus_tracker().evaluate()` into the snapshot builder.
  *Effort: medium. Risk: medium (tracker may be None).*
  - Files: `orchestrator/health_checks/detection_plane.py`

- **TASK-1-3** — Populate `container_transitions` from kubernetes_monitor pod-state log.
  Wire `KubernetesMonitor._pod_states` into the snapshot.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/kubernetes_monitor.py`

- **TASK-1-4** — Fix `RunningAgent` role field and populate age fields. Use agent role
  from pipeline state instead of container ID; populate `last_tool_call_age_s` and
  `last_heartbeat_age_s` from health monitor anchors.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/health_monitor.py`

- **TASK-1-5** — Wire `_run_overseer_detection_plane()` into RUNTIME_TICK. Call it from
  `_run_runtime_tick_checks()` after building the snapshot, routing `requires_adjudication`
  findings to the overseer agent and routine findings to the corrective executor. Guard
  against double-evaluation (two call sites: `_check_pod` and `_reconciliation_sweep`).
  *Effort: large. Risk: high (new code path on the hot loop).*
  - Files: `orchestrator/kubernetes_monitor.py`, `orchestrator/routes/pipelines/_overseer.py`

- **TASK-1-6** — Tests for snapshot population + detection plane wiring. Verify all 8
  previously-empty fields are populated; verify `_run_overseer_detection_plane` is called
  from the RUNTIME_TICK path; verify no double-evaluation.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_detection_plane_wiring.py`

---

## Slice 2 — Deterministic loop detector (PR 2)

**Goal:** Implement the empirical finding from the issue — "counting tool inputs never
issued before in the session over a trailing window separates a loop from work cleanly" —
as a deterministic detector that runs in the detection plane.

**Tasks:**

- **TASK-2-1** — Populate `midturn_messages` in snapshot. The agent client logs tool
  calls via `logger.info("Tool call", ...)` at `client.py:819-830`. Wire these into
  `snapshot_from_health_context()` as `midturn_messages` tuples. This is the prerequisite
  for the loop detector.
  *Effort: medium. Risk: medium (depends on log parsing).*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/agent_log_store.py`

- **TASK-2-2** — Implement `detect_tool_input_loop()` in `health_checks/tier1/`. Read
  `midturn_messages` from the snapshot, count distinct tool-input strings over a trailing
  window (e.g. 5 polls). If the count is zero for N consecutive polls, fire
  `tool_input_loop` / `high` with `requires_adjudication=False`. Must handle variable
  cycle shapes (1-, 2-, 3-, 8-cycles) — not keyed on a fixed shape.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/tier1/loop_detection.py` (new)

- **TASK-2-3** — Increase log capture fidelity for one-shot event pods. The k8s log API
  truncates at ~100 chars per line (`kubernetes_client.py:455` `read_job_log_snapshot`
  with `tail_lines=2000`). Increase fidelity so the unique-tool-input counter has enough
  data to distinguish distinct tool calls sharing a prefix.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_client.py`, `orchestrator/agent_log_store.py`

- **TASK-2-4** — Tests for the loop detector. Verify it fires on zero-new-input windows,
  does not fire on productive agents, handles variable cycle shapes.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_loop_detection.py`

---

## Slice 3 — Timeout visibility and classification (PR 3)

**Goal:** Make the 2-hour timeout visible to agents and classify timeout-killed pods
distinctly from crashes, so they don't consume the failure streak budget.

**Tasks:**

- **TASK-3-1** — Add `agent_timeout_seconds` to `PipelineConfig` (default 7200).
  *Effort: small. Risk: low.*
  - Files: `orchestrator/models/_config.py`

- **TASK-3-2** — Pass `EGG_AGENT_TIMEOUT` env through the spawner so the agent can
  self-report its remaining budget.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_spawner/_spawn.py`

- **TASK-3-3** — Classify timeout-killed pods distinctly. When `exit_code == -1` and
  the timeout fired (detectable via the agent result's error message "Timed out after
  {timeout} seconds"), classify as a clean timeout, not a crash. Add a
  `JOB_OUTCOME_TIMEOUT` outcome and route to `record_timeout` (not `record_abort`).
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/kubernetes_monitor.py`, `orchestrator/kubernetes_spawner/_models.py`,
    `orchestrator/event_loop/__init__.py`, `orchestrator/event_loop/_supervisor.py`

- **TASK-3-4** — Surface the timeout to the agent via heartbeat. Emit a HEARTBEAT with
  state `WAITING_FOR_EVENT` and body "approaching 2h timeout" at 90-minute intervals.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_monitor.py`, `sandbox/egg_lib/orch_cli/_message.py`

- **TASK-3-5** — Tests for timeout classification. Verify timeout-killed pods don't
  increment the failure streak; verify the agent receives the warning heartbeat.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_timeout_classification.py`

---

## Slice 4 — Alert evidence + false-positive fixes (PR 4)

**Goal:** Make alerts actionable by enriching them with evidence, and fix the
convergence-stall false positive by unifying timestamp sources.

**Tasks:**

- **TASK-4-1** — Enrich OVERSEER_ALERT payloads with evidence. Add structured evidence
  (container logs, BRC state, tracker evaluation) to `_broadcast_alert()`.
  *Effort: small. Risk: low.*
  - Files: `overseer/monitor/_alerting.py`

- **TASK-4-2** — Fix the convergence-stall false positive. Unify the timestamp source
  between `_check_convergence_stall()` (event_loop/_loop.py:859) and
  `_has_recent_peer_progress()` (health_monitor.py:388). Both should use the same
  "bus activity" signal.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/event_loop/_loop.py`, `orchestrator/health_monitor.py`

- **TASK-4-3** — Name the 2-hour timeout explicitly in exit classification. When a pod
  is killed by timeout, the alert should say "killed by 2h agent timeout" not
  "container exited with code -1."
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_monitor.py` (depends on TASK-3-3)

- **TASK-4-4** — Route detection-plane findings to the operator alert surface. Once
  slice-1 is wired, route findings to the same OVERSEER_ALERT / HITL / Slack surfaces
  the overseer uses.
  *Effort: medium. Risk: medium (depends on slice-1).*
  - Files: `orchestrator/health_checks/runner.py`, `orchestrator/routes/pipelines/_overseer.py`

- **TASK-4-5** — Tests for alert evidence and false-positive fixes. Verify alerts carry
  evidence; verify convergence-stall doesn't fire when peer heartbeat is recent.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_alert_evidence.py`

---

## Ordering and dependencies

1. **slice-1** (detection plane wiring) — prerequisite for slice-2 and slice-4's
   detection-plane routing. Must be done first so the snapshot fields exist.
2. **slice-2** (loop detector) — depends on `midturn_messages` being populated (slice-1
   TASK-1-1 populates `runtime`, but `midturn_messages` is TASK-2-1 in this slice).
   Can be developed in parallel with slice-1 since the detector can be tested against
   a manually-populated snapshot.
3. **slice-3** (timeout) — fully independent. Can be done in parallel.
4. **slice-4** (alerts) — TASK-4-2 is independent; TASK-4-4 depends on slice-1 being
   wired. Can be partially parallelized.

**Cross-slice dependency:** slice-4 TASK-4-4 (route findings to alert surface) depends
on slice-1 TASK-1-5 (wire detection plane into RUNTIME_TICK). This is a soft dependency —
the alerting code can be written first and the wiring makes it live.

---

## Acceptance criteria

1. The detection plane's `snapshot_from_health_context()` populates all 13
   `EventStreamSnapshot` fields (currently only 5).
2. `_run_overseer_detection_plane()` is called from the RUNTIME_TICK path with
   double-evaluation guarded.
3. A deterministic unique-tool-input counter exists and fires on zero-new-input
   windows of variable cycle shape (1-, 2-, 3-, 8-cycles).
4. Timeout-killed pods (exit code -1 from `asyncio.timeout`) are classified as clean
   timeouts, not crashes, and do not increment the failure streak.
5. Agents receive a heartbeat warning before the 2-hour timeout.
6. `PipelineConfig` has an `agent_timeout_seconds` field (default 7200).
7. OVERSEER_ALERT payloads carry structured evidence (logs, BRC state, tracker
   evaluation).
8. The convergence-stall alert and the alive-signal gate use the same timestamp
   source.
9. Timeout-killed pods produce an alert that says "killed by 2h agent timeout."
10. Detection-plane findings are routed to the operator alert surface.

---

## What to leave out

- Do not rebuild the overseer agent (working; problem is input pipeline).
- Do not remove the HealthMonitor tripwires (wired and working).
- Do not add LLM classification to the hot path.
- Do not change the 2-hour timeout default.
- Tier 3–5 from the candidate list are input to the gate, not a work queue.

---

```yaml
# yaml-tasks
pr:
  title: |-
    Supervision second pass: wire detection plane + loop detector + timeout classification + alert evidence
  description: |-
    Four independent slices across the four analysis areas from issue #3665:
    (1) wire the #2270 detection plane into RUNTIME_TICK by populating all 13
    EventStreamSnapshot fields and calling _run_overseer_detection_plane() from
    _run_runtime_tick_checks(); (2) implement a deterministic unique-tool-input
    loop detector reading midturn_messages from the snapshot; (3) classify
    timeout-killed pods (exit -1 from asyncio.timeout) as clean timeouts not
    crashes, surface the 2h timeout to agents via heartbeat, and make the
    timeout configurable per-pipeline; (4) enrich OVERSEER_ALERT payloads with
    evidence and fix the convergence-stall false positive by unifying timestamp
    sources. All four slices touch disjoint file sets.
  test_plan: |-
    Automated (per slice; `make test` narrows to reachable suites, then
    `make test-all` before phase exit; `make lint` green throughout):
    - Slice 1: test_detection_plane_wiring.py — all 13 snapshot fields populated;
      detection plane invoked on RUNTIME_TICK; double-evaluation guard prevents
      duplicate findings.
    - Slice 2: test_loop_detection.py — fires on zero-new-input windows of any
      cycle shape (1-, 2-, 3-, 8-cycles); does not fire on productive agents.
    - Slice 3: test_timeout_classification.py — streak untouched by timeout;
      heartbeat warning emitted at 90 minutes; exit classification distinguishes
      timeout from crash.
    - Slice 4: test_alert_evidence.py — evidence in alert payloads; convergence-stall
      does not fire when peer heartbeat is recent.
  test_command: |-
    make test-all
  lint_command: |-
    make lint
slices:
  - id: 1
    name: |-
      Wire the #2270 detection plane into RUNTIME_TICK
    goal: |-
      Make the detection plane actually run in production by (a) populating all
      13 EventStreamSnapshot fields in snapshot_from_health_context(), and (b)
      calling _run_overseer_detection_plane() from _run_runtime_tick_checks().
      This is the prerequisite for the loop detector (slice-2) and alert routing
      (slice-4 TASK-4-4).
    repo: jwbron/egg
    dependencies: []
    tasks:
      - id: TASK-1-1
        description: |-
          Populate runtime section of EventStreamSnapshot. Wire
          driver_heartbeat.tick_age_seconds() and spawn_age_seconds() into
          snapshot_from_health_context() under raw["runtime"]. This activates
          detect_run_pipeline_thread_liveness() and DriverLivenessCheck.
        acceptance: |-
          - snapshot.raw.runtime contains tick_age_s and spawn_age_s
          - detect_run_pipeline_thread_liveness fires when the driver thread is stale
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/driver_heartbeat.py
      - id: TASK-1-2
        description: |-
          Populate consensus section of snapshot. Wire
          peer_consensus.get_peer_consensus_tracker().evaluate() into the snapshot
          builder. This activates detect_brc_thrash(), detect_incomplete_consensus_deferral(),
          and consensus field readers in PhaseStallDetector.
        acceptance: |-
          - snapshot.consensus contains tracker.evaluate() output
          - detectors reading consensus field fire correctly
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/peer_consensus/__init__.py
      - id: TASK-1-3
        description: |-
          Populate container_transitions from kubernetes_monitor pod-state log.
          Wire KubernetesMonitor._pod_states into the snapshot. This activates
          detect_container_death(), detect_container_oom_evicted(),
          detect_container_restart_loop(), detect_overseer_self_injection().
        acceptance: |-
          - snapshot.container_transitions contains pod state transitions
          - container death/oom/restart detectors fire on real transitions
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/kubernetes_monitor.py
      - id: TASK-1-4
        description: |-
          Fix RunningAgent role field and populate age fields. Use agent role
          from pipeline state instead of container ID; populate last_tool_call_age_s
          and last_heartbeat_age_s from health monitor anchors. This activates
          detect_heartbeat_stall().
        acceptance: |-
          - RunningAgent.role is the agent role (not container ID)
          - last_tool_call_age_s and last_heartbeat_age_s are populated
          - detect_heartbeat_stall fires on stale heartbeats
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/health_monitor.py
      - id: TASK-1-5
        description: |-
          Wire _run_overseer_detection_plane() into RUNTIME_TICK path. Call it
          from _run_runtime_tick_checks() after building the snapshot, routing
          requires_adjudication findings to the overseer agent and routine
          findings to the corrective executor. Guard against double-evaluation
          (two call sites: _check_pod and _reconciliation_sweep).
        acceptance: |-
          - Detection plane runs on every RUNTIME_TICK sweep
          - findings with requires_adjudication escalate to overseer
          - routine findings emit to event bus
          - no double-evaluation on dual call sites
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
          - orchestrator/routes/pipelines/_overseer.py
          - orchestrator/health_checks/runner.py
      - id: TASK-1-6
        description: |-
          Tests for snapshot population + detection plane wiring. Verify all 13
          EventStreamSnapshot fields are populated; verify _run_overseer_detection_plane
          is called from RUNTIME_TICK; verify no double-evaluation.
        acceptance: |-
          - Tests assert all 13 fields populated
          - detection plane invoked on RUNTIME_TICK
          - double-evaluation guard prevents duplicate findings
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_wiring.py
  - id: 2
    name: |-
      Deterministic loop detector + midturn_messages population
    goal: |-
      Implement the empirical finding from the issue — "counting tool inputs never
      issued before in the session over a trailing window separates a loop from work
      cleanly" — as a deterministic detector that runs in the detection plane.
    repo: jwbron/egg
    dependencies: []
    tasks:
      - id: TASK-2-1
        description: |-
          Populate midturn_messages in snapshot. Wire agent tool-call logs (from
          client.py logger.info at lines 819-830) into snapshot_from_health_context()
          as midturn_messages tuples. This is the prerequisite for the loop detector.
        acceptance: |-
          - snapshot.midturn_messages contains tool-call records with tool_name and input_hash
          - populated from agent_log_store or live pod logs
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/agent_log_store.py
      - id: TASK-2-2
        description: |-
          Implement detect_tool_input_loop() in health_checks/tier1/. Read
          midturn_messages from the snapshot, count distinct tool-input strings over
          a trailing window (e.g. 5 polls). If the count is zero for N consecutive
          polls, fire tool_input_loop / high with requires_adjudication=False. Must
          handle variable cycle shapes (1-, 2-, 3-, 8-cycles) — not keyed on a fixed shape.
        acceptance: |-
          - Detector fires on zero-new-input windows of any cycle shape
          - does not fire on productive agents
          - evidence includes the tool input hash and window size
        role: coder
        files:
          - orchestrator/health_checks/tier1/loop_detection.py
      - id: TASK-2-3
        description: |-
          Increase log capture fidelity for one-shot event pods. The k8s log API
          truncates at ~100 chars per line (kubernetes_client.py:455 read_job_log_snapshot
          with tail_lines=2000). Increase fidelity so the unique-tool-input counter
          has enough data to distinguish distinct tool calls sharing a prefix.
        acceptance: |-
          - Tool call log lines are captured at full length (not truncated to 100 chars)
          - the loop detector can distinguish distinct tool calls sharing a prefix
        role: coder
        files:
          - orchestrator/kubernetes_client.py
          - orchestrator/agent_log_store.py
      - id: TASK-2-4
        description: |-
          Tests for the loop detector. Verify it fires on zero-new-input windows,
          does not fire on productive agents, handles variable cycle shapes (1-, 2-, 3-, 8-cycles).
        acceptance: |-
          - Tests cover single-input, 2-cycle, 3-cycle, and 8-cycle loops
          - productive agent with new tool inputs does not trigger
          - evidence payload is correct
        role: tester
        files:
          - orchestrator/tests/test_loop_detection.py
  - id: 3
    name: |-
      Timeout visibility and classification
    goal: |-
      Make the 2-hour timeout visible to agents and classify timeout-killed pods
      distinctly from crashes, so they don't consume the failure streak budget.
    repo: jwbron/egg
    dependencies: []
    tasks:
      - id: TASK-3-1
        description: |-
          Add agent_timeout_seconds to PipelineConfig (default 7200).
        acceptance: |-
          - PipelineConfig.agent_timeout_seconds field exists with default 7200
          - validated >= 60
        role: coder
        files:
          - orchestrator/models/_config.py
      - id: TASK-3-2
        description: |-
          Pass EGG_AGENT_TIMEOUT env through the spawner so the agent can
          self-report its remaining budget.
        acceptance: |-
          - Spawner sets EGG_AGENT_TIMEOUT env var from PipelineConfig.agent_timeout_seconds
          - agent reads it and can self-report remaining budget
        role: coder
        files:
          - orchestrator/kubernetes_spawner/_spawn.py
          - sandbox/llm/claude/config.py
      - id: TASK-3-3
        description: |-
          Classify timeout-killed pods distinctly. When exit_code == -1 and the
          timeout fired (detectable via agent result error message 'Timed out after
          {timeout} seconds'), classify as a clean timeout, not a crash. Add
          JOB_OUTCOME_TIMEOUT outcome and route to record_timeout (not record_abort).
        acceptance: |-
          - Timeout-killed pods classified as clean timeout
          - failure streak NOT incremented
          - alert says 'killed by 2h agent timeout'
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
          - orchestrator/kubernetes_spawner/_models.py
          - orchestrator/event_loop/__init__.py
          - orchestrator/event_loop/_supervisor.py
      - id: TASK-3-4
        description: |-
          Surface the timeout to the agent via heartbeat. Emit a HEARTBEAT with
          state WAITING_FOR_EVENT and body 'approaching 2h timeout' at 90-minute
          intervals.
        acceptance: |-
          - Agent receives a heartbeat warning at 90 minutes
          - message is readable and actionable
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
          - sandbox/egg_lib/orch_cli/_message.py
      - id: TASK-3-5
        description: |-
          Tests for timeout classification. Verify timeout-killed pods don't
          increment the failure streak; verify the agent receives the warning heartbeat.
        acceptance: |-
          - Tests assert streak untouched by timeout
          - heartbeat warning emitted at 90 minutes
          - exit classification distinguishes timeout from crash
        role: tester
        files:
          - orchestrator/tests/test_timeout_classification.py
  - id: 4
    name: |-
      Alert evidence + false-positive fixes
    goal: |-
      Make alerts actionable by enriching them with evidence, and fix the
      convergence-stall false positive by unifying timestamp sources.
    repo: jwbron/egg
    dependencies: []
    tasks:
      - id: TASK-4-1
        description: |-
          Enrich OVERSEER_ALERT payloads with evidence. Add structured evidence
          (container logs, BRC state, tracker evaluation) to _broadcast_alert().
        acceptance: |-
          - Alert payloads include evidence dict with container logs, BRC state, and tracker evaluation
          - operator can diagnose without grepping
        role: coder
        files:
          - orchestrator/overseer/monitor/_alerting.py
      - id: TASK-4-2
        description: |-
          Fix the convergence-stall false positive. Unify the timestamp source
          between _check_convergence_stall() (event_loop/_loop.py:859) and
          _has_recent_peer_progress() (health_monitor.py:388). Both should use
          the same 'bus activity' signal.
        acceptance: |-
          - Convergence-stall alert does not fire when peer heartbeat is seconds old and health monitor logs the agent as alive
          - same timestamp source used for both alerting and deferral
        role: coder
        files:
          - orchestrator/event_loop/_loop.py
          - orchestrator/health_monitor.py
      - id: TASK-4-3
        description: |-
          Name the 2-hour timeout explicitly in exit classification. When a pod
          is killed by timeout, the alert should say 'killed by 2h agent timeout'
          not 'container exited with code -1.' (Depends on TASK-3-3.)
        acceptance: |-
          - Exit classification message for timeout-killed pods says 'killed by 2h agent timeout'
          - distinct from crash classification
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
      - id: TASK-4-4
        description: |-
          Route detection-plane findings to the operator alert surface. Once
          slice-1 is wired, route findings to the same OVERSEER_ALERT / HITL / Slack
          surfaces the overseer uses, so the operator sees one consistent alert stream.
        acceptance: |-
          - Detection-plane findings appear on the OVERSEER_ALERT surface
          - requires_adjudication findings escalate to overseer
          - routine findings emit to event bus
        role: coder
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/routes/pipelines/_overseer.py
      - id: TASK-4-5
        description: |-
          Tests for alert evidence and false-positive fixes. Verify alerts carry
          evidence; verify convergence-stall doesn't fire when peer heartbeat is recent.
        acceptance: |-
          - Tests assert evidence in alert payloads
          - convergence-stall does not fire when peer heartbeat is recent
          - timeout classification message is correct
        role: tester
        files:
          - orchestrator/tests/test_alert_evidence.py
```
