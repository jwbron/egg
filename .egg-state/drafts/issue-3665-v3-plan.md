# Issue #3665 — Implementation Plan

**Supervision, second pass: wire detection plane + loop detector + timeout classification + alert evidence**

Plan artifact (task_planner). Grounded live against `main @ 1cd0c8ad7` on 2026-07-27;
every refiner anchor re-verified this session. The refine phase produced a 30-item
ranked candidate list with file-and-symbol citations; this plan selects the Tier 1
and Tier 2 items (per the gate's scope discipline) and decomposes them into a single
linear slice chain. Tier 3–5 are input to the gate, not a work queue.

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

## Scope decisions (registered per refine gate)

The refine gate instructed: "Do not expand beyond the Tier 1 and Tier 2 items without
registering a decision. Tier 3 through Tier 5 are input to the gate, not a work queue."

Two Tier 3 items are included in this plan with explicit justification:

- **TASK-3-2 (candidate #20, Tier 3: log capture fidelity)** — Included as a hard
  dependency. The k8s log API truncates tool-call log lines at ~100 characters
  (`kubernetes_client.py:455`), which is exactly why the loop detector cannot be
  built on the pod log alone. Without full-length tool inputs, the unique-tool-input
  counter cannot distinguish distinct tool calls sharing a prefix. This is not scope
  creep — it is a prerequisite for the Tier 1 loop detector (TASK-3-1) to function.

- **TASK-2-2 (candidate #19, Tier 3: route findings to alert surface)** — Included
  because the detection plane's findings are useless if they don't reach the operator.
  The issue explicitly states "alerts an operator cannot act on" as a core problem.
  Routing findings to the OVERSEER_ALERT surface is the minimal wiring needed to make
  the Tier 1 detection plane observable. This is the same class of concern as the
  issue's "signals that exist and are not consulted."

The remaining four `EventStreamSnapshot` fields (`decision_state`, `gateway_error_counters`,
`cost_counters`, `git_state`) are Tier 3-4 candidates (#16, #17, #18, #21) and are
**explicitly excluded** from this plan. They remain empty by decision; the detectors
reading them stay inert.

---

## Slice structure

All areas share overlapping files (notably `kubernetes_monitor.py` and
`detection_plane.py`), so they cannot be independent parallel slices. Instead, they
are organized as a **single linear chain** — slice-1 populates the snapshot fields
(the prerequisite for everything), slice-2 wires the detection plane into RUNTIME_TICK,
slice-3 adds the loop detector (which reads the now-populated `midturn_messages`),
slice-4 adds timeout classification, and slice-5 adds alert evidence + false-positive
fixes. Each slice builds on the previous one's branch.

| Slice | Name | Surface | Risk |
|-------|------|---------|------|
| slice-1 | Populate EventStreamSnapshot fields | `health_checks/detection_plane.py`, `driver_heartbeat.py`, `peer_consensus/`, `kubernetes_monitor.py`, `health_monitor.py`, `agent_log_store.py` | Medium |
| slice-2 | Wire detection plane into RUNTIME_TICK | `kubernetes_monitor.py`, `routes/pipelines/_overseer.py`, `health_checks/runner.py`, `health_checks/tier1/consensus_stall.py` | High (hot-loop) |
| slice-3 | Deterministic loop detector | `health_checks/detection_plane.py`, `health_checks/tier1/loop_detection.py`, `kubernetes_client.py`, `agent_log_store.py` | Medium |
| slice-4 | Timeout visibility and classification | `kubernetes_monitor.py`, `kubernetes_spawner/_models.py`, `event_loop/__init__.py`, `event_loop/_supervisor.py`, `models/_config.py`, `_spawn.py`, `sandbox/llm/claude/config.py`, `sandbox/egg_lib/orch_cli/_message.py` | Medium |
| slice-5 | Alert evidence + false-positive fixes | `overseer/monitor/_alerting.py`, `event_loop/_loop.py`, `health_monitor.py`, `health_checks/runner.py`, `routes/pipelines/_overseer.py` | Medium |

---

## Grounded anchors (verified live @ 1cd0c8ad7, 2026-07-27)

**slice-1 — Snapshot population**

- `health_checks/detection_plane.py:511` — `snapshot_from_health_context()` currently
  populates only 5 of 13 `EventStreamSnapshot` fields (`snapshot_id`, `pipeline_id`,
  `phase`, `running_agents`, `phase_state`). The `EventStreamSnapshot` dataclass
  (lines 106-127) has 8 additional fields left empty: `consensus`,
  `container_transitions`, `runtime`, `cost_counters`, `gateway_error_counters`,
  `midturn_messages`, `git_state`, `decision_state`.
- `health_checks/detection_plane.py:536` — `RunningAgent(role=str(cid), ...)` uses
  container ID as role (bug). Fields `last_tool_call_age_s` / `last_heartbeat_age_s`
  (lines 89-90) are never populated.
- `driver_heartbeat.py:41/54/67` — `record_tick()`, `record_spawn()`, `tick_age_seconds()`,
  `spawn_age_seconds()`.
- `peer_consensus/__init__.py:250` — `get_peer_consensus_tracker()`.
  `_queries.py:110` — `get_latest_progress_timestamp()`. `_queries.py:68` —
  `consensus_state_fingerprint()`. `_queries.py:262` — `get_fully_acked_producers()`.
- `kubernetes_monitor.py` — `_pod_states` dict tracks pod state transitions.

**slice-2 — Detection plane wiring**

- `routes/pipelines/_overseer.py:309` — `_run_overseer_detection_plane()` is defined but
  has zero call sites. Imported in `__init__.py:1277` but never called.
- `kubernetes_monitor.py:221` — `_run_runtime_tick_checks()` is the RUNTIME_TICK sweep,
  called from `_check_pod` (line 218) and `_reconciliation_sweep` (line 616). Currently
  calls `runner.run(ctx, HealthTrigger.RUNTIME_TICK)` but never
  `runner.run_detection_plane()`. Must guard against double-evaluation.
- `health_checks/runner.py:159` — `run_detection_plane()` method exists but is never
  invoked.
- `health_checks/tier1/consensus_stall.py:51` — `ConsensusStallCheck` class already
  runs every tick via HealthCheckRunner. `detect_heartbeat_stall()` at line 217 is a
  dormant function in the same file. Once the detection plane is live, the plane's
  consensus-stall detector and this registered check must not double-fire.

**slice-3 — Loop detector**

- `health_checks/detection_plane.py:126` — `midturn_messages` field on
  `EventStreamSnapshot` (currently never populated).
- `shared/egg_agent/client.py:765` — `asyncio.timeout(timeout)` wraps agent execution.
  Tool calls are logged at lines 819-830 via `logger.info("Tool call", ...)`.
- `orchestrator/kubernetes_client.py:455` — `read_job_log_snapshot()` with
  `tail_lines=2000`; k8s log API truncates at ~100 chars per line.
- `orchestrator/agent_log_store.py:51` — `MAX_LOG_BYTES = 1 * 1024 * 1024` (1 MiB).

**slice-4 — Timeout visibility and classification**

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

**slice-5 — Alert evidence + false-positive fixes**

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

## Slice 1 — Populate EventStreamSnapshot fields

**Goal:** Populate the 5 in-scope `EventStreamSnapshot` fields so that the detection
plane's detectors can actually read the data they need. The remaining 4 fields
(`decision_state`, `gateway_error_counters`, `cost_counters`, `git_state`) are Tier 3-4
candidates and remain empty by decision (see "Scope decisions" above). This is the
prerequisite for slices 2–5.

**Tasks:**

- **TASK-1-1** — Populate `midturn_messages` section. Wire agent tool-call logs into
  `snapshot_from_health_context()` as `midturn_messages` tuples. This is the prerequisite
  for the loop detector in slice-3 and is sequenced first per the refine gate's carry-into-plan
  note ("Area 3 step 1 is the highest-value item... sequence that field first among Area 1's
  steps rather than last").
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/agent_log_store.py`

- **TASK-1-2** — Populate `runtime` section. Wire `driver_heartbeat.tick_age_seconds()`
  and `spawn_age_seconds()` into `snapshot_from_health_context()` under `raw["runtime"]`.
  This activates `detect_run_pipeline_thread_liveness()` and `DriverLivenessCheck`.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/driver_heartbeat.py`

- **TASK-1-3** — Populate `consensus` section. Wire
  `peer_consensus.get_peer_consensus_tracker().evaluate()` into the snapshot builder.
  This activates `detect_brc_thrash()`, `detect_incomplete_consensus_deferral()`,
  and consensus field readers in `PhaseStallDetector`.
  *Effort: medium. Risk: medium (tracker may be None).*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/peer_consensus/__init__.py`

- **TASK-1-4** — Populate `container_transitions` from kubernetes_monitor pod-state log.
  Wire `KubernetesMonitor._pod_states` into the snapshot. This activates
  `detect_container_death()`, `detect_container_oom_evicted()`,
  `detect_container_restart_loop()`, `detect_overseer_self_injection()`.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/kubernetes_monitor.py`

- **TASK-1-5** — Fix `RunningAgent` role field and populate age fields. Use agent role
  from pipeline state instead of container ID; populate `last_tool_call_age_s` and
  `last_heartbeat_age_s` from health monitor anchors. This activates
  `detect_heartbeat_stall()`.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/detection_plane.py`, `orchestrator/health_monitor.py`

- **TASK-1-6** — Tests for snapshot population. Verify the 5 in-scope fields are populated
  (`runtime`, `consensus`, `container_transitions`, `midturn_messages`, `RunningAgent` role+age).
  Verify the 4 excluded fields (`decision_state`, `gateway_error_counters`, `cost_counters`,
  `git_state`) remain empty by decision.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_detection_plane_wiring.py`

---

## Slice 2 — Wire the detection plane into RUNTIME_TICK

**Goal:** Make the detection plane actually run in production by calling
`_run_overseer_detection_plane()` from the RUNTIME_TICK path. Also prevent double-firing
of consensus-stall detectors (the `ConsensusStallCheck` class already runs every tick via
HealthCheckRunner, and the detection plane's consensus-stall detector would fire once
the plane is live).

**Tasks:**

- **TASK-2-1** — Wire `_run_overseer_detection_plane()` into RUNTIME_TICK. Call it from
  `_run_runtime_tick_checks()` after building the snapshot, routing
  `requires_adjudication` findings to the overseer agent and routine findings to the
  corrective executor. Guard against double-evaluation (two call sites: `_check_pod`
  and `_reconciliation_sweep`).
  *Effort: large. Risk: high (new code path on the hot loop).*
  - Files: `orchestrator/kubernetes_monitor.py`, `orchestrator/routes/pipelines/_overseer.py`,
    `orchestrator/health_checks/runner.py`

- **TASK-2-2** — Prevent consensus-stall double-firing. `health_checks/tier1/consensus_stall.py`
  contains both the dormant `detect_heartbeat_stall()` function and a registered
  `ConsensusStallCheck` class that already runs every tick. Once the detection plane is
  live, the plane's consensus-stall detector and this registered check must not double-fire.
  Add a guard in the detection plane's consensus-stall detector to check whether
  `ConsensusStallCheck` has already fired for this snapshot, or vice-versa.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/health_checks/tier1/consensus_stall.py`, `orchestrator/health_checks/detection_plane.py`

- **TASK-2-3** — Route detection-plane findings to the operator alert surface. Route
  findings to the same OVERSEER_ALERT / HITL / Slack surfaces the overseer uses, so
  the operator sees one consistent alert stream. *(Scope decision: this is candidate #19,
  Tier 3, included because the issue explicitly identifies "alerts an operator cannot
  act on" as a core problem — findings without routing are useless.)*
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/runner.py`, `orchestrator/routes/pipelines/_overseer.py`

- **TASK-2-4** — Tests for detection plane wiring. Verify the detection plane is called
  from RUNTIME_TICK; verify no double-evaluation; verify findings are routed to the
  alert surface; verify no double-firing of consensus-stall detectors.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_detection_plane_wiring.py`

---

## Slice 3 — Deterministic loop detector

**Goal:** Implement the empirical finding from the issue — "counting tool inputs never
issued before in the session over a trailing window separates a loop from work cleanly" —
as a deterministic detector that runs in the detection plane.

**Tasks:**

- **TASK-3-1** — Implement `detect_tool_input_loop()` in `health_checks/tier1/`. Read
  `midturn_messages` from the snapshot (populated in slice-1 TASK-1-1), count distinct
  tool-input strings over a trailing window (e.g. 5 polls). If the count is zero for N
  consecutive polls, fire `tool_input_loop` / `high` with `requires_adjudication=False`.
  Must handle variable cycle shapes (1-, 2-, 3-, 8-cycles) — not keyed on a fixed shape.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/health_checks/tier1/loop_detection.py` (new)

- **TASK-3-2** — Increase log capture fidelity for one-shot event pods. The k8s log API
  truncates at ~100 chars per line (`kubernetes_client.py:455`). Increase fidelity so
  the unique-tool-input counter has enough data to distinguish distinct tool calls
  sharing a prefix. *(Scope decision: this is candidate #20, Tier 3, included as a hard
  dependency — without full-length tool inputs, the loop detector cannot distinguish
  distinct tool calls sharing a prefix.)*
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_client.py`, `orchestrator/agent_log_store.py`

- **TASK-3-3** — Tests for the loop detector. Verify it fires on zero-new-input windows,
  does not fire on productive agents, handles variable cycle shapes (1-, 2-, 3-, 8-cycles).
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_loop_detection.py`

---

## Slice 4 — Timeout visibility and classification

**Goal:** Make the 2-hour timeout visible to agents and classify timeout-killed pods
distinctly from crashes, so they don't consume the failure streak budget.

**Tasks:**

- **TASK-4-1** — Add `agent_timeout_seconds` to `PipelineConfig` (default 7200).
  *Effort: small. Risk: low.*
  - Files: `orchestrator/models/_config.py`

- **TASK-4-2** — Pass `EGG_AGENT_TIMEOUT` env through the spawner so the agent can
  self-report its remaining budget.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_spawner/_spawn.py`, `sandbox/llm/claude/config.py`

- **TASK-4-3** — Classify timeout-killed pods distinctly. When `exit_code == -1` and the
  timeout fired (detectable via agent result error message "Timed out after {timeout}
  seconds"), classify as a clean timeout, not a crash. Add `JOB_OUTCOME_TIMEOUT` outcome
  and route to `record_timeout` (not `record_abort`).
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/kubernetes_monitor.py`, `orchestrator/kubernetes_spawner/_models.py`,
    `orchestrator/event_loop/__init__.py`, `orchestrator/event_loop/_supervisor.py`

- **TASK-4-4** — Surface the timeout to the agent via heartbeat. Emit a HEARTBEAT with
  state `WAITING_FOR_EVENT` and body "approaching 2h timeout" at 90-minute intervals.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_monitor.py`, `sandbox/egg_lib/orch_cli/_message.py`

- **TASK-4-5** — Tests for timeout classification. Verify timeout-killed pods don't
  increment the failure streak; verify the agent receives the warning heartbeat.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_timeout_classification.py`

---

## Slice 5 — Alert evidence + false-positive fixes

**Goal:** Make alerts actionable by enriching them with evidence, and fix the
convergence-stall false positive by unifying timestamp sources.

**Tasks:**

- **TASK-5-1** — Enrich OVERSEER_ALERT payloads with evidence. Add structured evidence
  (container logs, BRC state, tracker evaluation) to `_broadcast_alert()`.
  *Effort: small. Risk: low.*
  - Files: `orchestrator/overseer/monitor/_alerting.py`

- **TASK-5-2** — Fix the convergence-stall false positive. Unify the timestamp source
  between `_check_convergence_stall()` (event_loop/_loop.py:859) and
  `_has_recent_peer_progress()` (health_monitor.py:388). Both should use the same
  "bus activity" signal.
  *Effort: medium. Risk: medium.*
  - Files: `orchestrator/event_loop/_loop.py`, `orchestrator/health_monitor.py`

- **TASK-5-3** — Name the 2-hour timeout explicitly in exit classification. When a pod
  is killed by timeout, the alert should say "killed by 2h agent timeout" not
  "container exited with code -1." (Depends on TASK-4-3.)
  *Effort: small. Risk: low.*
  - Files: `orchestrator/kubernetes_monitor.py`

- **TASK-5-4** — Tests for alert evidence and false-positive fixes. Verify alerts carry
  evidence; verify convergence-stall doesn't fire when peer heartbeat is recent.
  *Effort: medium. Risk: low.*
  - Files: `orchestrator/tests/test_alert_evidence.py`

---

## Ordering and dependencies

1. **slice-1** (populate snapshot fields) — prerequisite for everything. Without the data
   in the snapshot, no detector can work. `midturn_messages` (TASK-1-1) is sequenced first
   per the refine gate's instruction.
2. **slice-2** (wire detection plane into RUNTIME_TICK) — depends on slice-1. The
   detection plane needs populated snapshots to evaluate. Also includes the consensus-stall
   double-fire guard (TASK-2-2) per the refine gate's instruction.
3. **slice-3** (loop detector) — depends on slice-1 (midturn_messages) and slice-2
   (detection plane running). The loop detector reads `midturn_messages` from the snapshot
   and runs inside the detection plane.
4. **slice-4** (timeout) — depends on slice-1 (kubernetes_monitor.py is shared).
5. **slice-5** (alerts) — depends on slice-1 (shared files) and slice-2 (finding routing).
   TASK-5-3 depends on TASK-4-3 (timeout classification).

**Linear chain:** slice-1 → slice-2 → slice-3 → slice-4 → slice-5

---

## Acceptance criteria

1. The detection plane's `snapshot_from_health_context()` populates the 5 in-scope
   `EventStreamSnapshot` fields (`runtime`, `consensus`, `container_transitions`,
   `midturn_messages`, `RunningAgent` role+age). The 4 excluded fields
   (`decision_state`, `gateway_error_counters`, `cost_counters`, `git_state`) remain
   empty by decision.
2. `_run_overseer_detection_plane()` is called from the RUNTIME_TICK path with
   double-evaluation guarded.
3. Consensus-stall detectors do not double-fire: the `ConsensusStallCheck` class
   (runs every tick via HealthCheckRunner) and the detection plane's consensus-stall
   detector are guarded against duplicate reporting.
4. A deterministic unique-tool-input counter exists and fires on zero-new-input
   windows of variable cycle shape (1-, 2-, 3-, 8-cycles).
5. Timeout-killed pods (exit code -1 from `asyncio.timeout`) are classified as clean
   timeouts, not crashes, and do not increment the failure streak.
6. Agents receive a heartbeat warning before the 2-hour timeout.
7. `PipelineConfig` has an `agent_timeout_seconds` field (default 7200).
8. OVERSEER_ALERT payloads carry structured evidence (logs, BRC state, tracker
   evaluation).
9. The convergence-stall alert and the alive-signal gate use the same timestamp
   source.
10. Timeout-killed pods produce an alert that says "killed by 2h agent timeout."
11. Detection-plane findings are routed to the operator alert surface.

---

## What to leave out

- Do not rebuild the overseer agent (working; problem is input pipeline).
- Do not remove the HealthMonitor tripwires (wired and working).
- Do not add LLM classification to the hot path.
- Do not change the 2-hour timeout default.
- Tier 3–5 from the candidate list (except the two registered scope decisions above)
  are input to the gate, not a work queue.

---

```yaml
# yaml-tasks
pr:
  title: |-
    Supervision second pass: wire detection plane + loop detector + timeout classification + alert evidence
  description: |-
    Five linear slices across the four analysis areas from issue #3665:
    (1) populate 5 in-scope EventStreamSnapshot fields in snapshot_from_health_context()
    (midturn_messages first per refine gate); (2) wire _run_overseer_detection_plane()
    into RUNTIME_TICK with double-evaluation guard + consensus-stall double-fire guard
    + route findings to alert surface; (3) implement a deterministic unique-tool-input
    loop detector reading midturn_messages from the snapshot + increase log capture fidelity;
    (4) classify timeout-killed pods (exit -1 from asyncio.timeout) as clean timeouts not
    crashes, surface the 2h timeout to agents via heartbeat, and make the timeout
    configurable per-pipeline; (5) enrich OVERSEER_ALERT payloads with evidence and fix
    the convergence-stall false positive by unifying timestamp sources. Slices are linear
    because they share overlapping files (kubernetes_monitor.py, detection_plane.py,
    health_monitor.py). Two Tier 3 items included with registered scope decisions:
    TASK-3-2 (log fidelity, hard dependency for loop detector) and TASK-2-3 (alert routing,
    core to the issue's "alerts an operator cannot act on" problem).
  test_plan: |-
    Automated (per slice; `make test` narrows to reachable suites, then
    `make test-all` before phase exit; `make lint` green throughout):
    - Slice 1: test_detection_plane_wiring.py — 5 in-scope snapshot fields populated;
      4 excluded fields remain empty by decision.
    - Slice 2: test_detection_plane_wiring.py — detection plane invoked on RUNTIME_TICK;
      double-evaluation guard prevents duplicate findings; consensus-stall double-fire
      guard prevents duplicate reporting; findings routed to alert surface.
    - Slice 3: test_loop_detection.py — fires on zero-new-input windows of any
      cycle shape (1-, 2-, 3-, 8-cycles); does not fire on productive agents.
    - Slice 4: test_timeout_classification.py — streak untouched by timeout;
      heartbeat warning emitted at 90 minutes; exit classification distinguishes
      timeout from crash.
    - Slice 5: test_alert_evidence.py — evidence in alert payloads; convergence-stall
      does not fire when peer heartbeat is recent.
  test_command: |-
    make test-all
  lint_command: |-
    make lint
slices:
  - id: 1
    name: |-
      Populate in-scope EventStreamSnapshot fields in snapshot_from_health_context()
    goal: |-
      Populate the 5 in-scope EventStreamSnapshot fields (midturn_messages, runtime,
      consensus, container_transitions, RunningAgent role+age) so that the detection
      plane's detectors can actually read the data they need. The remaining 4 fields
      (decision_state, gateway_error_counters, cost_counters, git_state) are Tier 3-4
      candidates and remain empty by decision. midturn_messages is TASK-1-1 (first)
      per the refine gate's carry-into-plan note.
    repo: jwbron/egg
    dependencies: []
    tasks:
      - id: TASK-1-1
        description: |-
          Populate midturn_messages in snapshot. Wire agent tool-call logs into
          snapshot_from_health_context() as midturn_messages tuples. Sequenced first
          per the refine gate's instruction ("Area 3 step 1 is the highest-value item...
          sequence that field first among Area 1's steps rather than last"). This is
          the prerequisite for the loop detector in slice-3.
        acceptance: |-
          - snapshot.midturn_messages contains tool-call records with tool_name and input_hash
          - populated from agent_log_store or live pod logs
        role: coder
        files:
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/agent_log_store.py
      - id: TASK-1-2
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
      - id: TASK-1-3
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
      - id: TASK-1-4
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
      - id: TASK-1-5
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
      - id: TASK-1-6
        description: |-
          Tests for snapshot population. Verify the 5 in-scope fields are populated
          (runtime, consensus, container_transitions, midturn_messages, RunningAgent
          role+age). Verify the 4 excluded fields (decision_state, gateway_error_counters,
          cost_counters, git_state) remain empty by decision.
        acceptance: |-
          - Tests assert 5 in-scope fields populated
          - Tests assert 4 excluded fields remain empty by decision
          - runtime, consensus, container_transitions, midturn_messages, RunningAgent all non-empty
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_wiring.py
  - id: 2
    name: |-
      Wire the detection plane into RUNTIME_TICK + consensus-stall double-fire guard
    goal: |-
      Make the detection plane actually run in production by calling
      _run_overseer_detection_plane() from _run_runtime_tick_checks(), routing
      requires_adjudication findings to the overseer agent and routine findings
      to the corrective executor. Also prevent consensus-stall double-firing
      (ConsensusStallCheck class runs every tick via HealthCheckRunner; the
      detection plane's consensus-stall detector must not fire on the same snapshot).
    repo: jwbron/egg
    dependencies: [1]
    tasks:
      - id: TASK-2-1
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
      - id: TASK-2-2
        description: |-
          Prevent consensus-stall double-firing. health_checks/tier1/consensus_stall.py
          contains both the dormant detect_heartbeat_stall() function and a registered
          ConsensusStallCheck class that already runs every tick. Once the detection
          plane is live, the plane's consensus-stall detector and this registered check
          must not double-fire. Add a guard in the detection plane's consensus-stall
          detector to check whether ConsensusStallCheck has already fired for this
          snapshot, or vice-versa.
        acceptance: |-
          - ConsensusStallCheck and the detection plane's consensus-stall detector do not both fire on the same snapshot
          - No duplicate consensus-stall alerts or findings
        role: coder
        files:
          - orchestrator/health_checks/tier1/consensus_stall.py
          - orchestrator/health_checks/detection_plane.py
      - id: TASK-2-3
        description: |-
          Route detection-plane findings to the operator alert surface. Route
          findings to the same OVERSEER_ALERT / HITL / Slack surfaces the
          overseer uses, so the operator sees one consistent alert stream.
          (Scope decision: candidate #19, Tier 3 — included because the issue
          explicitly identifies "alerts an operator cannot act on" as a core
          problem; findings without routing are useless.)
        acceptance: |-
          - Detection-plane findings appear on the OVERSEER_ALERT surface
          - requires_adjudication findings escalate to overseer
          - routine findings emit to event bus
        role: coder
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/routes/pipelines/_overseer.py
      - id: TASK-2-4
        description: |-
          Tests for detection plane wiring. Verify the detection plane is called
          from RUNTIME_TICK; verify no double-evaluation; verify findings are routed
          to the alert surface; verify no double-firing of consensus-stall detectors.
        acceptance: |-
          - Tests assert detection plane invoked on RUNTIME_TICK
          - double-evaluation guard prevents duplicate findings
          - consensus-stall double-fire guard prevents duplicate reporting
          - findings routed to alert surface
        role: tester
        files:
          - orchestrator/tests/test_detection_plane_wiring.py
  - id: 3
    name: |-
      Deterministic loop detector
    goal: |-
      Implement the empirical finding from the issue — "counting tool inputs never
      issued before in the session over a trailing window separates a loop from work
      cleanly" — as a deterministic detector that runs in the detection plane.
    repo: jwbron/egg
    dependencies: [2]
    tasks:
      - id: TASK-3-1
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
      - id: TASK-3-2
        description: |-
          Increase log capture fidelity for one-shot event pods. The k8s log API
          truncates at ~100 chars per line (kubernetes_client.py:455 read_job_log_snapshot
          with tail_lines=2000). Increase fidelity so the unique-tool-input counter
          has enough data to distinguish distinct tool calls sharing a prefix.
          (Scope decision: candidate #20, Tier 3 — included as a hard dependency;
          without full-length tool inputs, the loop detector cannot distinguish
          distinct tool calls sharing a prefix.)
        acceptance: |-
          - Tool call log lines are captured at full length (not truncated to 100 chars)
          - the loop detector can distinguish distinct tool calls sharing a prefix
        role: coder
        files:
          - orchestrator/kubernetes_client.py
          - orchestrator/agent_log_store.py
      - id: TASK-3-3
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
  - id: 4
    name: |-
      Timeout visibility and classification
    goal: |-
      Make the 2-hour timeout visible to agents and classify timeout-killed pods
      distinctly from crashes, so they don't consume the failure streak budget.
    repo: jwbron/egg
    dependencies: [3]
    tasks:
      - id: TASK-4-1
        description: |-
          Add agent_timeout_seconds to PipelineConfig (default 7200).
        acceptance: |-
          - PipelineConfig.agent_timeout_seconds field exists with default 7200
          - validated >= 60
        role: coder
        files:
          - orchestrator/models/_config.py
      - id: TASK-4-2
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
      - id: TASK-4-3
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
      - id: TASK-4-4
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
      - id: TASK-4-5
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
  - id: 5
    name: |-
      Alert evidence + false-positive fixes
    goal: |-
      Make alerts actionable by enriching them with evidence, and fix the
      convergence-stall false positive by unifying timestamp sources.
    repo: jwbron/egg
    dependencies: [4]
    tasks:
      - id: TASK-5-1
        description: |-
          Enrich OVERSEER_ALERT payloads with evidence. Add structured evidence
          (container logs, BRC state, tracker evaluation) to _broadcast_alert().
        acceptance: |-
          - Alert payloads include evidence dict with container logs, BRC state, and tracker evaluation
          - operator can diagnose without grepping
        role: coder
        files:
          - orchestrator/overseer/monitor/_alerting.py
      - id: TASK-5-2
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
      - id: TASK-5-3
        description: |-
          Name the 2-hour timeout explicitly in exit classification. When a pod
          is killed by timeout, the alert should say 'killed by 2h agent timeout'
          not 'container exited with code -1.' (Depends on TASK-4-3.)
        acceptance: |-
          - Exit classification message for timeout-killed pods says 'killed by 2h agent timeout'
          - distinct from crash classification
        role: coder
        files:
          - orchestrator/kubernetes_monitor.py
      - id: TASK-5-4
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


## HITL Resolution

The following was approved by a human reviewer at the plan phase gate:

All four items verified fixed against `cf0e5a6fa`, and the revision was correctly scoped: 21 tasks to 22 — exactly the one task I asked you to add — with no other structural change.

Confirmed:
1. **AC-1 is now satisfiable.** It names the five in-scope fields and states explicitly that `decision_state`, `gateway_error_counters`, `cost_counters` and `git_state` "remain empty by decision". The contradiction with the exclusion list is gone, and TASK-1-6 now tests something the tasks actually deliver.
2. **TASK-2-2 added for consensus-stall double-firing**, with `ConsensusStallCheck` correctly cited at `health_checks/tier1/consensus_stall.py:51` — I verified that line. New AC-3 covers it.
3. **Scope decisions registered as `cq-1` and `cq-2`** rather than assumed. Both resolved; the dependency argument for the log-fidelity task was the right one to make.
4. **`midturn_messages` moved to TASK-1-1**, with slice 3's reference updated to match and the ordering rationale stated.

Two constraints for implement, both carried from the `cq` resolutions and both load-bearing for the one task that matters most:

- **The loop detector must not threshold on cycle shape.** Observed in this series: single-input, 2-, 3- and 8-cycles, plus a near-identical variant with 64 distinct inputs at 80% dominance and a 3-cycle at 22% dominance. Any rule keyed on shape, dominance, or distinct-count misses most of them. Count inputs never issued before in the session over a trailing window — every observed instance scored exactly zero, and healthy agents scored well above it. Test all four shapes plus a productive-agent negative case, as TASK-3-3 already specifies.
- **Hash the full `(tool_name, input)` pair.** Truncating the input at any length reintroduces the prefix-collapse that TASK-3-2 exists to remove.

Implement-phase expectations:

- **The propose-time check gate (#3669) now runs.** Configured checks execute against your proposed tree before a proposal becomes reviewable. Be aware of a known limitation, filed as #3681: the `test` check resolves to the changeset-narrowed `make test`, not `make test-all`, because no `full_command` is declared. A green propose-time result is therefore **not** evidence of a green full suite. Do not report "tests pass" on the strength of it.
- **The baseline is genuinely green.** `pytest orchestrator/tests` is 8924 passed / 0 failed on `main` at `cf0e5a6fa`. Any failure you see is yours, not pre-existing. If you believe a failure is pre-existing, verify it against `main` before saying so.
- **Respect the file-size caps.** `scripts/check-file-sizes.py` now counts code lines, not prose, so deleting documentation will not bring a file under the cap. `kubernetes_monitor.py`, `health_monitor.py` and `detection_plane.py` are all touched by multiple slices; watch their size as you go rather than at the end.
- **Slice ordering is linear and the dependencies are real.** Do not parallelise slice 2 ahead of slice 1.
