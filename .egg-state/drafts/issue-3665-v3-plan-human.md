# Issue #3665 — Implementation Plan (Plain-English Summary)

**Supervision, second pass: wire detection plane + loop detector + timeout classification + alert evidence**

## What this plan does

This plan addresses issue #3665 — the supervision layer that is supposed to distinguish
working agents from stuck ones was silent on seven livelocks and loud on healthy agents.
The root cause is that the detection plane (from issue #2270) is fully built but never
wired into production: it builds data snapshots with only 5 of 13 fields populated, and
the function that would run it has zero call sites.

The plan selects the Tier 1 and Tier 2 items from the 30-item candidate list produced
during refine (Tier 3–5 are input to the gate, not a work queue). It decomposes them
into **five linear slices** because the slices share overlapping files
(`kubernetes_monitor.py`, `detection_plane.py`, `health_monitor.py`) and must be ordered
along a single dependency chain per the #3046 overlap validator.

## The five slices (in order)

### Slice 1 — Populate the detection plane's data snapshot (Medium risk)

**Goal:** Populate the 5 in-scope fields in `EventStreamSnapshot` so the detection
plane's detectors can actually read the data they need. The remaining 4 fields
(`decision_state`, `gateway_error_counters`, `cost_counters`, `git_state`) are
Tier 3/4 candidates explicitly excluded from this plan's scope — they stay empty
by decision and the detectors reading them remain inert. This is the prerequisite
for everything that follows.

**Tasks (6 total, coder + tester):**

- **TASK-1-1** — Populate `midturn_messages` from agent tool-call logs. This is the
  prerequisite for the loop detector in slice 3 (the primary deliverable). Sequenced
  first per the refine gate's directive.
- **TASK-1-2** — Populate the `runtime` section with driver heartbeat ages (how long
  since the last tick, how long since spawn). Activates the run-pipeline-thread-liveness
  detector and DriverLivenessCheck.
- **TASK-1-3** — Populate the `consensus` section from the peer consensus tracker.
  Activates the BRC thrash detector and incomplete-consensus deferral detector.
- **TASK-1-4** — Populate `container_transitions` from the kubernetes_monitor's pod-state
  log. Activates container death, OOM-evicted, restart-loop, and self-injection detectors.
- **TASK-1-5** — Fix the `RunningAgent` role field (currently uses container ID instead
  of the agent role) and populate `last_tool_call_age_s` / `last_heartbeat_age_s`.
  Activates the heartbeat-stall detector.
- **TASK-1-6** — Tests verifying the 5 in-scope fields are populated (`midturn_messages`,
  `runtime`, `consensus`, `container_transitions`, `running_agents` with role + age fields).
  The 4 excluded fields (`decision_state`, `gateway_error_counters`, `cost_counters`,
  `git_state`) are asserted to remain empty by decision.

**Files touched:** `orchestrator/health_checks/detection_plane.py`,
`orchestrator/driver_heartbeat.py`, `orchestrator/peer_consensus/__init__.py`,
`orchestrator/kubernetes_monitor.py`, `orchestrator/health_monitor.py`,
`orchestrator/agent_log_store.py`, `orchestrator/tests/test_detection_plane_wiring.py`

### Slice 2 — Wire the detection plane into production (High risk — hot loop)

**Goal:** Make the detection plane actually run by calling `_run_overseer_detection_plane()`
from the RUNTIME_TICK path. Route findings that need human judgment to the overseer agent
and routine findings to the corrective executor. Also route findings to the operator alert
surface so the operator sees one consistent alert stream.

**Tasks (3 total, coder + tester):**

- **TASK-2-1** — Wire `_run_overseer_detection_plane()` into `_run_runtime_tick_checks()`,
  guarding against double-evaluation (two call sites: `_check_pod` and
  `_reconciliation_sweep`). Also guard against double-firing with the existing
  `ConsensusStallCheck` class in `health_checks/tier1/consensus_stall.py` — that class
  already runs on every runtime tick and contains a dormant `detect_heartbeat_stall`
  function. Once the plane is live, both layers must not report the same consensus stall.
- **TASK-2-2** — Route detection-plane findings to the OVERSEER_ALERT / HITL / Slack
  surfaces.
- **TASK-2-3** — Tests verifying the detection plane is invoked on RUNTIME_TICK, no
  double-evaluation, and findings are routed to the alert surface.

**Files touched:** `orchestrator/kubernetes_monitor.py`,
`orchestrator/routes/pipelines/_overseer.py`, `orchestrator/health_checks/runner.py`,
`orchestrator/tests/test_detection_plane_wiring.py`

### Slice 3 — Deterministic loop detector (Medium risk)

**Goal:** Implement the issue's empirical finding — "counting tool inputs never issued
before in the session over a trailing window separates a loop from work cleanly" — as a
deterministic detector that runs in the detection plane. This catches the seven agents
that got stuck in repetition loops.

**Tasks (3 total, coder + tester):**

- **TASK-3-1** — Implement `detect_tool_input_loop()` in `health_checks/tier1/`. Reads
  `midturn_messages` from the snapshot (populated in slice 1), counts distinct tool-input
  strings over a trailing window. If zero for N consecutive polls, fires an alert. Must
  handle variable cycle shapes (1-, 2-, 3-, 8-cycles — not keyed on a fixed shape).
- **TASK-3-2** — Increase log capture fidelity for one-shot event pods. The k8s log API
  truncates lines at ~100 characters; increase so the counter can distinguish distinct
  tool calls sharing a prefix.
- **TASK-3-3** — Tests covering single-input, 2-cycle, 3-cycle, and 8-cycle loops;
  productive agents do not trigger.

**Files touched:** `orchestrator/health_checks/tier1/loop_detection.py` (new),
`orchestrator/kubernetes_client.py`, `orchestrator/agent_log_store.py`,
`orchestrator/tests/test_loop_detection.py`

### Slice 4 — Timeout visibility and classification (Medium risk)

**Goal:** Make the 2-hour timeout visible to agents and classify timeout-killed pods
distinctly from crashes, so they don't consume the failure streak budget.

**Tasks (5 total, coder + tester):**

- **TASK-4-1** — Add `agent_timeout_seconds` to `PipelineConfig` (default 7200).
- **TASK-4-2** — Pass `EGG_AGENT_TIMEOUT` env through the spawner so the agent can
  self-report its remaining budget.
- **TASK-4-3** — Classify timeout-killed pods distinctly. When exit code is -1 and the
  timeout fired, classify as a clean timeout (not a crash). Add `JOB_OUTCOME_TIMEOUT`
  outcome and route to `record_timeout` (not `record_abort`).
- **TASK-4-4** — Surface the timeout to the agent via heartbeat. Emit a HEARTBEAT at
  90-minute intervals saying "approaching 2h timeout."
- **TASK-4-5** — Tests verifying the streak is untouched by timeout, heartbeat warning
  is emitted, and exit classification distinguishes timeout from crash.

**Files touched:** `orchestrator/models/_config.py`,
`orchestrator/kubernetes_spawner/_spawn.py`, `sandbox/llm/claude/config.py`,
`orchestrator/kubernetes_monitor.py`, `orchestrator/kubernetes_spawner/_models.py`,
`orchestrator/event_loop/__init__.py`, `orchestrator/event_loop/_supervisor.py`,
`sandbox/egg_lib/orch_cli/_message.py`, `orchestrator/tests/test_timeout_classification.py`

### Slice 5 — Alert evidence + false-positive fixes (Medium risk)

**Goal:** Make alerts actionable by enriching them with evidence, and fix the
convergence-stall false positive by unifying timestamp sources.

**Tasks (4 total, coder + tester):**

- **TASK-5-1** — Enrich OVERSEER_ALERT payloads with structured evidence (container logs,
  BRC state, tracker evaluation) so the operator can diagnose without grepping.
- **TASK-5-2** — Fix the convergence-stall false positive. Unify the timestamp source
  between `_check_convergence_stall()` and `_has_recent_peer_progress()` so they use the
  same "bus activity" signal for both alerting and deferral.
- **TASK-5-3** — Name the 2-hour timeout explicitly in exit classification (depends on
  TASK-4-3). When a pod is killed by timeout, the alert should say "killed by 2h agent
  timeout" not "container exited with code -1."
- **TASK-5-4** — Tests verifying evidence in alert payloads and convergence-stall doesn't
  fire when peer heartbeat is recent.

**Files touched:** `orchestrator/overseer/monitor/_alerting.py`,
`orchestrator/event_loop/_loop.py`, `orchestrator/health_monitor.py`,
`orchestrator/kubernetes_monitor.py`, `orchestrator/tests/test_alert_evidence.py`

## Ordering and dependencies

```
slice-1 → slice-2 → slice-3 → slice-4 → slice-5
```

- **slice-1** is the prerequisite for everything — without populated snapshot fields,
  no detector can work.
- **slice-2** depends on slice-1 — the detection plane needs populated snapshots to
  evaluate.
- **slice-3** depends on slice-1 (midturn_messages) and slice-2 (detection plane running)
  — the loop detector reads midturn_messages from the snapshot and runs inside the
  detection plane.
- **slice-4** depends on slice-1 (shares kubernetes_monitor.py) but can be developed
  in parallel with slice-2/slice-3 since it touches different functions in the shared
  files. Must be sequenced after slice-1 for branch integration.
- **slice-5** depends on slice-1 (shared files) and slice-2 (finding routing).
  TASK-5-3 has a soft dependency on TASK-4-3 (timeout classification).

## What to leave out

- Do not rebuild the overseer agent (working; problem is input pipeline).
- Do not remove the HealthMonitor tripwires (wired and working).
- Do not add LLM classification to the hot path (expensive Haiku calls).
- Do not change the 2-hour timeout default (7200s is reasonable).
- Tier 3–5 from the candidate list are input to the gate, not a work queue.

## Scope decisions (registered per refine gate directive)

Two Tier 3 candidates are included in this plan with justification:

- **TASK-3-2 (candidate #20, log capture fidelity):** Included because the ~100-character
  k8s log truncation is a hard dependency — without full-length tool-call logs, the
  `detect_tool_input_loop()` detector in slice 3 cannot distinguish distinct tool calls
  sharing a prefix, making the primary deliverable unimplementable. This is a dependency
  argument, not a scope expansion.
- **TASK-2-2 (candidate #19, route findings to operator alert surface):** Included because
  the detection plane's findings are invisible without it — the plane may detect loops
  but the operator never sees them. This is a necessary companion to the detection plane
  wiring in slice 2, not a standalone Tier 3 enhancement.

The remaining 4 `EventStreamSnapshot` fields (`decision_state`, `gateway_error_counters`,
`cost_counters`, `git_state`) are Tier 3/4 candidates that stay empty by decision. The
detectors reading them remain inert until a future plan picks them up.

## Verification

- `make test` narrows to reachable suites per slice; `make test-all` before phase exit.
- `make lint` green throughout.
- Acceptance criteria cover: the 5 in-scope snapshot fields populated (not all 13 — the
  remaining 4 are excluded by decision), detection plane invoked on RUNTIME_TICK with
  double-evaluation guard and no double-firing with `ConsensusStallCheck`, loop detector
  fires on zero-new-input windows of any cycle shape, timeout-killed pods classified as
  clean timeouts (not crashes), agents receive 90-minute heartbeat warning,
  `PipelineConfig` has `agent_timeout_seconds`, OVERSEER_ALERT payloads carry structured
  evidence, convergence-stall and alive-signal gate use the same timestamp source,
  timeout alerts say "killed by 2h agent timeout", and detection-plane findings are
  routed to the operator alert surface.
