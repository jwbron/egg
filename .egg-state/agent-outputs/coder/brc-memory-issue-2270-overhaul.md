# Coder BRC memory — issue-2270 overhaul

## slice-6 — Authority: bounded CorrectiveExecutor (§4) — PROPOSED (reconciled to tester)

- **branch**: egg/issue-2270-overhaul-slice-6-coder/work (merged origin/.../slice-6 = tester task-6-2)
- **tasks**: task-6-1 (complete)
- **verdict**: shipped; ALL 36 tester rows PASS (25 test_corrective_executor + 11
  test_overseer_authority); 795+ adjacent tests green; ruff clean.

### RECONCILED to tester contract (task-6-2) — first cut diverged, fixed:
The tester pinned a DIFFERENT surface than my v1. Reconciled (tester-leads-coder):
- Executor ctor kwargs are `open_operator_hitl=/nudge_agent=/respawn_cohort=`
  (NOT open_hitl/nudge/respawn), `audit_sink=` (list-append callable, NOT logger),
  `max_actions_per_window=/window_seconds=/clock=` (NOT rate_limit_window/time_fn).
- `execute(action, *, pipeline_id, running_agent_count=1, phase, target_role,
  finding, idempotency_key, question, options)` — KWARGS, no CorrectiveContext.
- Status set: executed|denied|barred|deduplicated|rate_limited (NOTE
  "deduplicated" not "deduped"; NO failed/unknown_action/noop). Out-of-vocab →
  `denied` (the executor does NOT do an identity/RBAC gate — that lives at the
  gateway file patterns). Precedence: vocab→denied, zero-agent→barred,
  dup-key→deduplicated, rate-limit→rate_limited, else→executed.
- Deps invoked with kwargs (pipeline_id +target_role for nudge/respawn, +question
  for hitl); rate-limit is GLOBAL sliding window of executed-action timestamps;
  dedup is a seen-key set (record both only on execute).
- DROPPED shared/egg_restrictions/corrective.py + __init__ wiring (tester's gateway
  contract wants the deny enforced by EXISTING OVERSEER_PATTERNS, not a new shared
  predicate). Gateway guardrail now lives directly in gateway/agent_restrictions.py
  as `check_corrective_action(*, action, identity=None) -> CorrectiveAuthorityResult`
  + `CORRECTIVE_ACTIONS` — name matches the tester's candidate list so its optional
  guardrail row goes STRICT (rejects force_merge/delete_repo/none/"").

### Change model
- `shared/egg_restrictions/corrective.py` (NEW): the RBAC gate, cycle-free (pure
  strings, no patterns/egg_contracts dep). `CORRECTIVE_ACTIONS` = {open_operator_hitl,
  nudge_agent, respawn_cohort}; `ORCHESTRATOR_CONTROL_PLANE_IDENTITY="orchestrator"`;
  `corrective_action_authorized(identity, action)` — deny-by-default, ONLY the
  orchestrator control plane authorized; EVERY agent (incl. overseer) denied;
  unknown action rejected before identity check. Lives in shared so BOTH gateway
  and orchestrator import one source of truth.
- `shared/egg_restrictions/__init__.py`: lazy `__getattr__` re-export of the three
  corrective names (PEP-562, cycle-safe).
- `gateway/agent_restrictions.py` (the NAMED enforcement surface): re-exports
  `corrective_action_authorized` + constants from shared; added to `__all__`.
- `orchestrator/overseer/corrective.py` (NEW): `CorrectiveExecutor` — closed
  3-action vocabulary (`CorrectiveAction` StrEnum; `.actions` == the 3). `execute()`
  ordering: closed-vocab → RBAC (re-checks shared predicate) → zero-agent-park BAR
  (running_agent_count<=0) → idempotency (exact (action,target,dedupe_key) within
  window → DEDUPED no-op returning prior outcome) → rate-limit (per (action,target),
  sliding window, default 1/600s) → dispatch (FAILED on raise, audited). Records
  rate-limit + idempotency ONLY on success so a transient failure doesn't block
  retry. `execute_verdict()` maps AdjudicationVerdict.recommended_action ("none"→NOOP).
  Every path audit-logged (`logger.info` on EXECUTED, `warning` otherwise) with
  structured fields. Handlers injected → unit-testable. `CorrectiveStatus`:
  executed/denied/barred/rate_limited/deduped/failed/unknown_action/noop.
- `orchestrator/overseer/__init__.py`: export CorrectiveExecutor/Context/Outcome/
  Action/Status.
- `orchestrator/routes/pipelines.py`: the production seams (after
  `_send_brc_confirmation_nudge`):
  - `_corrective_open_operator_hitl(ctx)` → loads contract, `next_cq_id`, builds a
    `Decision(type=HITL)` (Intervene / Dismiss-calibration / Other), writes via
    `apply_mutation(role=Role.IMPLEMENTER, actor="orchestrator-overseer-corrective",
    field_path="decisions.N")` + `save_contract`. SAME decisions.* owner as
    register_open_question / impasse router (RBAC-gated); orchestrator-distinct actor.
  - `_corrective_nudge_agent(ctx)` → `_send_brc_confirmation_nudge` (synthesizes the
    brc_confirmation_timeout-shaped escalation; elapsed defaults to 1).
  - `_corrective_respawn_cohort(ctx)` → POST `/agents/<role>/restart` per role
    (comma-split cohort) — the SAME public general-restart endpoint the overseer
    monitor's `_execute_restart_agent` uses (budget/consensus/Job teardown
    server-side, request-context-free, no bespoke respawn plumbing).
  - `_build_overseer_corrective_executor(...)` factory (seams injectable; windows
    from overseer_infra_error_dedup_window_seconds when set).
  - `_execute_overseer_verdicts(results, ...)` runs the authority plane over
    `(finding, verdict)` pairs from `_run_overseer_detection_plane` (target resolved
    from verdict.target or finding.evidence agent_role/agent_id). NON-breaking:
    slice-4's `_run_overseer_detection_plane` untouched.
  - TYPE_CHECKING import of CorrectiveExecutor.

### Verified (manual smoke, no venv)
- RBAC: orchestrator→allow; overseer/coder→deny; unknown action→deny. Gateway
  re-export + lazy __init__ both resolve.
- Executor: `.actions`==3; unknown_action/denied/barred/executed/deduped(idempotent,
  handler called once)/after-window re-fire/rate_limited/failed(executed=False)/noop
  all correct.
- ruff check + format clean on all touched files; py_compile clean.

### For tester (task-6-2)
- Inject the three handler seams; assert: exactly-3 actions; RBAC deny for every
  agent role incl. overseer (and at the gateway re-export); zero-agent bar;
  idempotent no-op (handler fired once); rate-limit per (action,target); FAILED on
  handler raise (not recorded → retry allowed); execute_verdict NOOP on "none".
- For `_corrective_open_operator_hitl`: assert a HITL Decision appended to
  contract.decisions via apply_mutation under Role.IMPLEMENTER (decisions.* RBAC) —
  agents cannot reach it. Mock load/save_contract.
- respawn seam: mock urllib opener; assert POST to the restart endpoint per role.

## slice-4 — Detection plane + escalation→adjudicator (§-core) — PROPOSED

- **commit**: 63fb5d073 (branch egg/issue-2270-overhaul-slice-4-coder/work)
- **tasks**: task-4-1, task-4-2 (both complete)
- **verdict**: shipped; corpus phase_stall rows strict-pass; ruff clean.

### Change model
- `health_checks/types.py`: added production `Finding` (finding_class, severity,
  evidence, recommended_action, requires_adjudication, detector_key, ts) +
  `Severity`/`FindingClass` StrEnums. StrEnum so `Finding` satisfies the slice-1
  corpus `Finding` Protocol structurally (Severity.HIGH == "high").
- `health_checks/detection_plane.py` (NEW): `EventStreamSnapshot`/`RunningAgent`/
  `LifecycleOwner` (production mirror of corpus snapshot — same field names, so
  the slice-1 harness drives production detectors verbatim, no corpus import);
  `Detector` Protocol (callable); `DetectionPlane` (register + exception-isolated
  `evaluate` + `requires_adjudication` filter); `PhaseStallDetector` (the #3230
  fix CORE — silent when lifecycle_owner∈{orchestrator,agent} or awaiting_spawn
  or HITL parked; fires high+requires_adjudication=True only on genuine wedge
  past grace=3600s); `default_detection_plane`; `snapshot_from_health_context`.
- `runner.py`: `HealthCheckRunner.run_detection_plane(snapshot, plane)` emits
  findings on the bus. `context.py`: `lifecycle_owner` property (#3230).
- `decision_maker.py`: `AdjudicationVerdict` + `build_adjudication_prompt` +
  `parse_adjudication_verdict` (closed advisory vocab {none,nudge_agent,
  respawn_cohort,open_operator_hitl}; malformed → conservative defer-to-operator
  open_operator_hitl so a broken adjudicator never drops a deadlock).
- `monitor.py`: on-demand `OverseerMonitor.adjudicate(finding)` (single-shot,
  no poll loop); `start()` loop docstring marks it the RETIRED standing-pod
  shape (respawn machinery removed in slice-5).
- `routes/pipelines.py`: `_escalate_finding_to_adjudicator` STRICTLY gated on
  `requires_adjudication` (routine findings return None, never spawn an agent);
  reuses slice-3 `_spawn_overseer_agent` via a NEW optional `prompt_override`
  kwarg (default monitoring prompt unchanged → slice-3 tester contract intact);
  `_consume_adjudicator_verdict` consumes structured verdict in-process;
  `_run_overseer_detection_plane` integration helper (evaluate → escalate).

### Verified
- phase_stall: false_stall_3230__normal → None; phase_stall__bad → (phase_stall,
  high, requires_adjudication=True). Scoreboard precision=1.0, FP=0, TN=6 (all
  normals incl. #3230 silent), TP=1; remaining known-bad rows are slices 7/8.
- DetectionPlane.evaluate survives a raising detector. Verdict parsing: good
  JSON, malformed→defer, out-of-vocab action→coerced to open_operator_hitl.
- ruff check + format clean on all 8 files. No network → couldn't build venv;
  CI/tester (task-4-3) runs the full suite + flips plane rows to strict.

### For tester (task-4-3)
- Register the production detector into the corpus registry in the test:
  `register_detector("phase_stall", PhaseStallDetector())` at collection time so
  the `_ROW_PARAMS` xfail evaporates and phase_stall rows go strict.
- `test_detection_plane.py`: Finding contract, plane exception-isolation, and
  adjudicator gating — assert `_escalate_finding_to_adjudicator` spawns ONLY when
  requires_adjudication (inject `spawn_overseer`/`consume_verdict` seams; a
  routine Finding must return None without calling spawn).

### IMPORTANT — false confirm-nudge observed this slice
An orchestrator STATUS claimed "your proposal v2 is ready to confirm" while
coder had ZERO CONSENSUS_PROPOSE messages and a clean tree. Verified against
`mcp__brc__get_state` + `read_peer_artifact` (CONSENSUS_PROPOSE, coder → empty)
before acting; did NOT confirm. This is itself an instance of the §2 calibration
pathology. Always verify confirm-readiness against BRC ground truth.


## slice-3 — Spawn normalization (§1.5) — PROPOSED (reconciled to tester contract)

- **proposal_sha**: (set at propose; built on merge of origin slice-3 + reconciliation)
- **tasks**: task-3-1, task-3-2 (both complete)
- **verdict**: shipped; net-negative.

### Final change model (reconciled to tester task-3-4 contract)
The tester's `TestOverseerSpawnNormalization` (test_kubernetes_spawner.py)
demands a STRICTER shape than my first cut (thin wrapper): full removal of the
bespoke spawn method + alias, and zero `overseer_monitor` string in the spawner
source. Reconciled to it (slice-2 precedent: coder reconciles to tester):

- `kubernetes_spawner.py`: **DELETED** `spawn_overseer_job` method (168 LOC) and
  the `spawn_overseer_container` alias. No `overseer_monitor`/`EGG_OVERSEER_*`
  string remains in the spawner. The overseer now goes through the generic
  `spawn_agent_job(agent_role=OVERSEER)` exactly like every other role.
- `routes/pipelines.py`: NEW module helper `_spawn_overseer_agent(...)` builds
  the overseer command at the call site — resolves model via
  `resolve_overseer_model("adversarial") -> Opus` (slice-2), builds the generic
  monitoring prompt (observe→classify→alert via MCP tools / `egg-orch` CLI; NO
  baked script), `build_agent_command`, conditional #2769 upstream routing, then
  `spawner.spawn_agent_job(agent_role=AgentRole.OVERSEER, command=..., extra_env=
  {BASH_COMMAND_TIMEOUT, **model_env})`. Inert `decision_model` deprecation
  warning preserved. Both call sites (respawn + phase-start) use it.
  Added `SpawnedContainer` to the TYPE_CHECKING import block.
- `sandbox/overseer_monitor.py` (802 LOC) + its 2 dedicated tests DELETED
  (first commit a341fd825). No Dockerfile edit — baked only via generic
  `COPY . /opt/egg-runtime/` (Dockerfile:356).
- Fixed a paste-artifact in the tester's `test_overseer_monitor_script_deleted`
  (a stray `assert statistics.median(samples) >= 60_000` orphaned at method end)
  that was blocking the tester's own new test. **Flag to tester to confirm.**

### Verified
- `TestOverseerSpawnNormalization` (5 tests) all PASS.
- ruff clean on both production files.
- No production (non-test) code references the removed symbol.

### Open for tester (flagged in proposal)
Legacy overseer-spawn tests still call the removed `spawn_overseer_container`
and assert removed env/prompt — need tester delete/retarget (slice-2 pattern):
- `test_overseer_spawn.py` (38 tests, 100% obsolete — replaced by
  TestOverseerSpawnNormalization → delete candidate)
- `test_phase_scoped_overseer.py`, `test_overseer_max_turns.py` (retarget to new
  `_spawn_overseer_agent` path)
- `test_pipeline_failure_path.py` (`mock_spawner.spawn_overseer_container.assert_called`)
I left these to the tester to avoid concurrent-edit thrash on their domain.

### Notes
- Config `overseer_poll_interval_seconds` RETAINED (used by
  orchestrator/overseer/monitor.py); only the spawn-path plumbing dropped it.
- `shared/egg_overseer/advisor.py:27` has a stale docstring mention of the
  deleted script — left for slice-9 docs cleanup (not a code dep).
