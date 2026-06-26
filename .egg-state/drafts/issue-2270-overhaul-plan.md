# Issue #2270 — Overseer Overhaul: Implementation Plan

**Pipeline:** `issue-2270-overhaul` · **Phase:** plan · **Author:** task_planner
**Adopts:** architect 9-slice DAG (`issue-2270-overhaul-architect-slices.yaml`) · **Grounds:** refine analysis

## Resolved direction (refine HITL — binding)

- **cq-1 = Option C (hybrid).** Deterministic detection + a bounded corrective vocabulary run
  **orchestrator-side**; spawn a **normal, on-demand** agent (`spawn_agent_job`, Opus-tier, no
  special plumbing) **only** to adjudicate adversarial/high-stakes escalations.
- **cq-2 = All-in-one.** Deliver the full §1–§6 **including** the entire §5 coverage-gap survey.

## Topology — adopting the architect's DAG as a forest-legal linear chain

This task breakdown adopts the architect's 9-slice DAG **numbering, names, and goals verbatim** and
fills in the discrete tasks (role, files, acceptance) for each slice.

The architect's DAG is multi-parent (`s4←[1,3]`, `s7←[1,4]`, `s8←[4,7]`, `s9←[3,5,6,8]`), which the
**#2137 forest validator** (≤1 DAG parent/slice) forbids in the contract. The linear chain
`slice-1 → … → slice-9` is a **verified topological sort** of the architect's DAG, so encoding the
contract `dependencies` linearly preserves *every* ordering constraint the architect specified while
staying a forest and making all **#3046 file overlaps** transitively ordered. The architect's full
parent edges are recorded below for fidelity:

| slice | architect parents | linear dep | why the extra edges are still honored |
|-------|-------------------|------------|----------------------------------------|
| 1 | — | — | head / corpus bedrock |
| 2 | — | slice-1 | added ordering edge only (harmless); 2 has no file overlap with 1 |
| 3 | 2 | slice-2 | exact |
| 4 | 1, 3 | slice-3 | 1<4 holds (1 is position-1) |
| 5 | 4 | slice-4 | exact — **replacement (4) before deletion (5)** |
| 6 | 4 | slice-5 | 4<6 holds; 6 overlaps `routes/pipelines.py` with 5 → after-5 is required anyway |
| 7 | 1, 4 | slice-6 | 1<7 and 4<7 hold |
| 8 | 4, 7 | slice-7 | 4<8 and 7<8 hold |
| 9 | 3, 5, 6, 8 | slice-8 | all four < 9 hold |

**Hard invariant preserved:** the orchestrator-side detection plane (slice-4) and its corpus
(slice-1) are live and corpus-validated **before** slice-5 removes the respawn machinery — never
delete the watcher before its proven replacement.

### Grounded anchors (architect-confirmed; defend on NACK)

- §1 model: `models.py:726-728` default `"sonnet"`; bypass `classify_model(decision_model)` at
  `kubernetes_spawner.py:2919`; resolver `agent_model_resolution.py:497`.
- §1.5: `spawn_overseer_job` `kubernetes_spawner.py:2883-2960`; `EGG_OVERSEER_*` `2922-2926`; baked
  `overseer_monitor.py --once` prompt `2931`; `spawn_agent_job` `:1228`; `AgentRole.OVERSEER` `:672`.
- §2 reflection: `shared/egg_agent/midturn_messages.py:63-75` `_INJECT_FROM_ROLES` includes
  `overseer`; **retain the #3123 brc-confirmation-timeout nudge** (golden-file regression). Observed
  live this phase: an `[info]` `overseer_restart` alert reflected as an "operator directive".
- §2 divergence: `_BRANCH_DIVERGENCE_PR_RE` subject regex `routes/pipelines.py:15819`; detector `15822-15907`.
- §3: `_check_and_respawn_overseer` `routes/pipelines.py:685-848`, called `:23318`.
- §4 authority: real enforcement = **gateway `agent_restrictions.py` + contract RBAC**, NOT the stale
  `roles.py:can_modify` (`overseer` isn't a `Role` enum value there).
- §5: Tier-1 `health_checks/tier1/` (6 classes); `OverseerSelfMonitor` already instantiated/
  health-checked — open nuance: `check_health()` emit-vs-log.
- §6: `issue_filer.py` **IS used** (`__init__.py:27`, `monitor.py:36/675`) — do NOT delete on #1962.
  `monitor.py` decomposition rides **#2817** — out of scope.

### Role ↔ file ownership (verified via check_file_restriction, phase=implement)

- **coder** — `.py`, `Dockerfile`, `.yml`/`.json` under orchestrator/shared/gateway/sandbox.
- **tester** — `orchestrator/tests/`, `gateway/tests/`, `**/test_*.py`, corpus `.py`/`.json` fixtures.
- **documenter** — `.md` only (`docs/`, READMEs, `sandbox/agent-config/rules/overseer.md`). A `.md`
  under `tests/` is unwritable by every role — corpus docs go under `docs/`.

## Acceptance-criteria mapping (architect ac-1..ac-7 → slices)

ac-1 model → s2,s4 · ac-2 no-special-case → s3,s4 · ac-3 calibration corpus → s1,s7 ·
ac-4 lifecycle → s5 · ac-5 authority → s6 · ac-6 coverage → s4,s8 · ac-7 cleanup → s9.

## Test strategy

- **Corpus-driven calibration (slice-1 contract):** a detector under test yields `None` on
  known-normal rows and the expected `Finding` on known-bad rows. Slice-1 lands the harness +
  `xfail` markers for not-yet-built/-fixed detectors; slices 4/7/8 flip their rows to strict as the
  detection plane and fixes land (red→green). Corpus-tested == shippable — the gate that prevents a
  new false-positive flood.
- **Per-slice unit tests** for every behavioral change; **golden-file regression** that the #3123
  brc-confirmation-timeout nudge survives the slice-7 intent-discriminator.
- `make test` (changeset-aware) per slice.

## Out of scope / deferred

- `monitor.py` structural decomposition → **#2817**.
- `overseer_auto_file_issues_mode` shadow→enforce flip in production → guarded; after telemetry.
- Deleting `issue_filer.py` on the #1962 premise → it is used.

```yaml
# yaml-tasks
pr:
  title: |-
    Overseer overhaul: hybrid orchestrator-side detection (#2270)
  description: |-
    Overhaul of the overseer subsystem (#2270, refine HITL Option C, all-in-one §1–§6).
    Adopts the architect's 9-slice DAG, encoded as a forest-legal linear chain (a verified
    topological sort that preserves every ordering edge, incl. the hard "detection plane +
    corpus live before respawn machinery is deleted" invariant).

    Replaces the respawning watcher pod + baked-in `overseer_monitor.py` bootstrap with an
    in-process deterministic detection plane over an EventStreamSnapshot plus a CLOSED corrective
    vocabulary (open_operator_hitl, nudge_agent, respawn_cohort); a NORMAL on-demand OVERSEER agent
    (`spawn_agent_job`, Opus via the resolver) is spawned only to ADVISE on adversarial escalations.

    - §1  (s2) overseer model via `resolve_agent_model` tiering; `overseer_decision_maker_model`
          deprecated; `classify_model` bypass removed (folds #2813).
    - §1.5(s3) `spawn_overseer_job` folded into `spawn_agent_job(OVERSEER)`; `EGG_OVERSEER_*` + baked
          `overseer_monitor.py` deleted.
    - §2  (s1) tested known-normal/known-bad corpus + harness (deliverable #1); (s7) lifecycle-aware
          stall #3230/#2242, alert-reflection intent-discriminator (retain #3123 nudge),
          ancestor/patch-id divergence #2222/#2224, thrashing defs #2059/#2132.
    - core(s4) orchestrator-side detection plane + escalation→adjudicator.
    - §3  (s5) respawn churn retired; `_check_and_respawn_overseer` folded; restart/generation hygiene.
    - §4  (s6) bounded corrective-vocabulary executor; real enforcement = gateway RBAC, not
          roles.py:can_modify.
    - §5  (s8) full coverage-gap detector survey, each corpus-tested + lifecycle-owner-aware.
    - §6  (s9) net-negative cleanup + docs; issue_filer.py retained; monitor.py decomposition (#2817) out.
  test_plan: |-
    - Automated: slice-1 calibration harness asserts each detector's verdict against the labeled
      corpus (None on known-normal, expected Finding on known-bad); per-slice unit tests for model
      resolution, spawn-via-`spawn_agent_job`, the detection plane + Finding contract, no-respawn-
      during-HITL, the corrective-vocabulary executor, each signal fix, and every new detector class;
      golden-file regression that the #3123 nudge survives the intent-discriminator. `make test`/slice.
    - Manual: confirm a synthetic deadlock opens an operator HITL via the authorized control-plane
      path; confirm zero overseer activity during a multi-hour HITL park with no agents running.
  manual_steps: |-
    Pre-merge: stack the slice PRs in DAG order (slice-1 → slice-9); review each independently;
    never merge slice-5 (pod/respawn deletion) before slice-4 (detection plane) is green on the corpus.
    Post-merge: run an `EGG_EVENT_LOOP_OWNER=orchestrator` pipeline and confirm a clean overseer alert
    stream (no self-injection / false-stall / reflected-directive alarms) before flipping
    `overseer_auto_file_issues_mode` shadow→enforce.
slices:
  - id: 1
    name: |-
      Calibration corpus + detector test harness (§2, deliverable #1)
    goal: |-
      Build the known-normal/known-bad EventStreamSnapshot fixtures + calibration harness (AC-3
      contract: a detector under test yields None on known-normal rows and the expected Finding on
      known-bad rows). Head of the chain; no production-code overlap. No production detector changes.
    dependencies: []
    exit_criteria: |-
      Corpus rows (self-injection loop, alert-reflection, #3230 false stall, #2242 heartbeat-stall,
      #2222/#2224 divergence, #2948 eviction) each labelled known-normal|known-bad{class} with the
      expected Finding (or None); harness contract + scoreboard run; xfail markers tag detectors built
      downstream. `make test` green.
    tasks:
      - id: TASK-1-1
        role: tester
        description: |-
          Create the EventStreamSnapshot fixtures package: each corpus row is a labelled
          (snapshot, lifecycle-owner, expected Finding|None) record covering the known incidents —
          self-injection loop, alert-reflection, #3230 false stall (producer drafting under
          orchestrator-owned spawn), #2242 heartbeat-stall (tool calls every 2-3s), #2222/#2224
          branch-divergence (ancestor/patch-id vs subject regex), #2948 transient kubelet eviction.
        acceptance: |-
          `corpus.py` exposes the labelled rows + a loader; each row names its expected verdict and
          the issue/defect it pins; fixtures parse and load under test.
        files:
          - orchestrator/tests/overseer_calibration/__init__.py
          - orchestrator/tests/overseer_calibration/corpus.py
          - orchestrator/tests/overseer_calibration/fixtures.json
      - id: TASK-1-2
        role: tester
        description: |-
          Build the calibration harness encoding the AC-3 contract: run a detector-under-test over the
          corpus and assert it yields None on every known-normal row and the expected Finding on every
          known-bad row; emit a precision/recall scoreboard. Tag detectors that don't exist yet
          (slice-4 plane) or aren't fixed yet (slice-7) with `xfail(reason=...)` so this slice is green.
        acceptance: |-
          `test_overseer_calibration.py` runs the corpus through the harness and asserts the contract;
          xfail markers cover exactly the rows delivered in slices 4/7/8. Test passes.
        files:
          - orchestrator/tests/test_overseer_calibration.py
      - id: TASK-1-3
        role: documenter
        description: |-
          Document the corpus/harness contract: row schema, the None-on-normal / Finding-on-bad rule,
          the scoreboard, and the xfail→strict flip convention downstream slices follow.
        acceptance: |-
          Doc explains the record shape, the AC-3 contract, and the red→green workflow.
        files:
          - docs/architecture/overseer-calibration-corpus.md
  - id: 2
    name: |-
      Model tiering via resolve_agent_model (§1, folds #2813)
    goal: |-
      Route the overseer decision/adjudication model through `resolve_agent_model` with explicit
      tiering (Haiku classify / Sonnet routine / Opus adversarial); deprecate
      `overseer_decision_maker_model` behind a shim; stop `kubernetes_spawner.py:2919` routing the
      overseer model through `classify_model(decision_model)`.
    dependencies: [1]
    exit_criteria: |-
      Resolver returns the tiered model per role/tier; the deprecation shim warns + maps; no spawn
      path consults the deprecated field. `make test` green.
    tasks:
      - id: TASK-2-1
        role: coder
        description: |-
          Route the overseer model through `resolve_agent_model` (`agent_model_resolution.py:497`) with
          tiering; add a deprecation shim for `overseer_decision_maker_model` (`models.py:726-728`);
          remove the `classify_model(decision_model)` bypass at `kubernetes_spawner.py:2919`; wire the
          decision_maker/classifier to the resolved per-tier models.
        acceptance: |-
          Resolver returns Opus for adversarial and cheaper tiers for classify/routine; the deprecated
          field warns and no longer drives spawn (#2813 regression covered).
        files:
          - orchestrator/models.py
          - orchestrator/agent_model_resolution.py
          - orchestrator/kubernetes_spawner.py
          - orchestrator/overseer/decision_maker.py
          - orchestrator/overseer/classifier.py
      - id: TASK-2-2
        role: tester
        description: |-
          Tests: resolver returns the tiered model per role/tier; the deprecation shim warns + maps;
          the spawn path no longer reads `overseer_decision_maker_model`.
        acceptance: |-
          Tiering + deprecation-shim + #2813-bypass-removal assertions pass.
        files:
          - orchestrator/tests/test_agent_model_resolution.py
          - orchestrator/tests/test_overseer_model.py
  - id: 3
    name: |-
      Spawn normalization — fold spawn_overseer_job, drop bespoke flags + baked-in bootstrap (§1.5)
    goal: |-
      Fold `spawn_overseer_job` (`kubernetes_spawner.py:2883-2960`) into
      `spawn_agent_job(agent_role=OVERSEER)`; remove `EGG_OVERSEER_MODE/POLL_INTERVAL/DECISION_MODEL`
      (2922-2926) and the prompt invoking `overseer_monitor.py --once` (2931); delete the baked-in
      `sandbox/overseer_monitor.py` + its Dockerfile bake. Net-negative.
    dependencies: [2]
    exit_criteria: |-
      No `spawn_overseer_job`, no `EGG_OVERSEER_*`, no baked monitor script; the overseer (when
      spawned) goes through `spawn_agent_job`; image builds. `make test` green.
    tasks:
      - id: TASK-3-1
        role: coder
        description: |-
          Fold `spawn_overseer_job` into `spawn_agent_job(agent_role=OVERSEER, …)`; remove the bespoke
          `EGG_OVERSEER_*` env and the `overseer_monitor.py --once` prompt bootstrap; update the call
          site(s) in `routes/pipelines.py`.
        acceptance: |-
          Overseer spawns via the generic agent path with no bespoke env / baked-script invocation.
        files:
          - orchestrator/kubernetes_spawner.py
          - orchestrator/routes/pipelines.py
      - id: TASK-3-2
        role: coder
        description: |-
          Delete `sandbox/overseer_monitor.py` (802 lines) and remove its bake/copy from
          `sandbox/Dockerfile`.
        acceptance: |-
          The script is gone; no Dockerfile layer references it; the image builds. Net-negative lines.
        files:
          - sandbox/overseer_monitor.py
          - sandbox/Dockerfile
      - id: TASK-3-3
        role: documenter
        description: |-
          Move whatever monitoring the on-demand overseer genuinely needs into the prompt/rule, framed
          as MCP/tool usage — not a trust-and-run baked script.
        acceptance: |-
          The overseer rule describes monitoring via available tools/MCP; no baked-script reference.
        files:
          - sandbox/agent-config/rules/overseer.md
      - id: TASK-3-4
        role: tester
        description: |-
          Tests: overseer spawn goes through `spawn_agent_job`; no `EGG_OVERSEER_*` env; no
          monitor-script reference; deletion regression.
        acceptance: |-
          Spawn-path tests assert the generic path and absence of bespoke flags/script.
        files:
          - orchestrator/tests/test_kubernetes_spawner.py
  - id: 4
    name: |-
      Orchestrator-side detection plane + escalation→adjudicator path (Option C core spine)
    goal: |-
      Build the in-process deterministic evaluator that runs detectors over an EventStreamSnapshot on
      the orchestrator event loop (extending `health_checks/tier1/`); each detector returns
      Optional[Finding{class, severity, evidence, recommended_action, requires_adjudication}]. Wire the
      escalation path: when `requires_adjudication` is set, spawn a NORMAL on-demand OVERSEER agent
      (slice-3 path, Opus via slice-2 resolver) returning a structured verdict the orchestrator
      consumes. Corpus-validated against slice-1. The replacement that MUST exist before slice-5.
    dependencies: [3]
    exit_criteria: |-
      The detection plane runs detectors over snapshots in-process; Finding contract honored; the
      on-demand adjudicator is spawned ONLY when `requires_adjudication`; slice-1 corpus rows for the
      plane flip to strict-pass. `make test` green.
    tasks:
      - id: TASK-4-1
        role: coder
        description: |-
          Implement the deterministic detection plane: an in-process evaluator that runs detectors over
          an EventStreamSnapshot on the orchestrator event loop, extending the `health_checks` runner;
          define the `Finding` type (class, severity, evidence, recommended_action,
          requires_adjudication).
        acceptance: |-
          The evaluator yields `Optional[Finding]` per detector over a snapshot; routine cases need no
          LLM; the slice-1 harness can drive it.
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/health_checks/context.py
          - orchestrator/health_checks/types.py
          - orchestrator/health_checks/detection_plane.py
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        role: coder
        description: |-
          Wire the escalation→adjudicator path: a `Finding` with `requires_adjudication` spawns a NORMAL
          on-demand OVERSEER agent (slice-3 normalized spawn, Opus via the slice-2 resolver) that
          returns a structured verdict; the orchestrator consumes the verdict (the authority plane in
          slice-6 executes on it). Retire standing-pod assumptions from `monitor.py`.
        acceptance: |-
          The adjudicator spawns only on `requires_adjudication`; routine findings never spawn it; the
          verdict is structured and consumed in-process.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/overseer/decision_maker.py
          - orchestrator/routes/pipelines.py
      - id: TASK-4-3
        role: tester
        description: |-
          Corpus-validate the detection plane against slice-1 (flip the plane's rows to strict);
          test the Finding contract and that the adjudicator spawns only when `requires_adjudication`.
        acceptance: |-
          Plane corpus rows strict-pass; adjudicator-spawn gating covered. `make test` green.
        files:
          - orchestrator/tests/test_detection_plane.py
          - orchestrator/tests/test_overseer_calibration.py
  - id: 5
    name: |-
      Lifecycle — retire respawn churn + restart/generation hygiene (§3)
    goal: |-
      Now that slice-4 is the replacement: remove/fold `_check_and_respawn_overseer`
      (`routes/pipelines.py:685-848`) and the standing-pod respawn loop; guarantee no overseer activity
      during HITL parks with zero agents running; fold any surviving restart need into the GENERAL
      agent-restart machinery; add escalation-history clear + generation-token reset on
      restart/orchestrator recycle. Net-negative. Strictly AFTER slice-4.
    dependencies: [4]
    exit_criteria: |-
      No overseer-specific respawn loop; zero overseer activity during agent-less HITL parks;
      escalation history + generation token reset on restart/recycle. `make test` green.
    tasks:
      - id: TASK-5-1
        role: coder
        description: |-
          Remove/fold `_check_and_respawn_overseer` and the standing-pod respawn loop into the general
          agent-restart machinery; gate any overseer presence on "agents actually running" so multi-hour
          zero-agent HITL parks spawn nothing.
        acceptance: |-
          No overseer respawn fires during a zero-agent HITL park; restart logic shares the agent path;
          net-negative lines.
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/models.py
      - id: TASK-5-2
        role: coder
        description: |-
          Restart/generation hygiene: clear per-agent escalation history on restart; reset the
          generation token on orchestrator pod recycle so stale escalation state can't cascade.
        acceptance: |-
          Escalation history clears on restart; generation token resets on recycle; no cross-generation
          leakage.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/routes/pipelines.py
      - id: TASK-5-3
        role: tester
        description: |-
          Tests for no-respawn-during-HITL, escalation-history reset on restart, generation-token reset
          on recycle.
        acceptance: |-
          Churn-gone + reset-hygiene assertions pass. `make test` green.
        files:
          - orchestrator/tests/test_overseer_lifecycle.py
  - id: 6
    name: |-
      Authority — bounded corrective vocabulary executor, control-plane-side (§4)
    goal: |-
      Add the orchestrator-side CorrectiveExecutor with a CLOSED vocabulary
      {open_operator_hitl, nudge_agent, respawn_cohort}. `open_operator_hitl` writes a contract
      decision via the orchestrator identity (dissolves the gateway 403 — real enforcement is
      `gateway/agent_restrictions.py` + contract RBAC, NOT `roles.py:can_modify`); `nudge_agent` reuses
      `_send_brc_confirmation_nudge`; `respawn_cohort` uses the general restart machinery. Every action
      rate-limited, audit-logged, idempotent, barred during zero-agent HITL parks. The adjudicator only
      ADVISES; this plane executes.
    dependencies: [5]
    exit_criteria: |-
      A confirmed adversarial finding can open an operator HITL via the authorized path; the
      vocabulary is closed, rate-limited, audit-logged, idempotent, and barred during zero-agent parks.
      `make test` green.
    tasks:
      - id: TASK-6-1
        role: coder
        description: |-
          Implement the orchestrator-side CorrectiveExecutor with the CLOSED vocabulary. Wire
          `open_operator_hitl` to write a contract decision via the orchestrator identity at the REAL
          enforcement point (`gateway/agent_restrictions.py` + contract RBAC); `nudge_agent` →
          `_send_brc_confirmation_nudge`; `respawn_cohort` → general restart. Make every action
          rate-limited, audit-logged, idempotent, and barred during zero-agent HITL parks.
        acceptance: |-
          The executor exposes exactly the three actions; `open_operator_hitl` opens a contract
          decision; unauthorized callers remain denied; actions are bounded/audited/idempotent.
        files:
          - orchestrator/overseer/corrective.py
          - orchestrator/routes/pipelines.py
          - gateway/agent_restrictions.py
      - id: TASK-6-2
        role: tester
        description: |-
          Tests: each action is rate-limited / audit-logged / idempotent; `open_operator_hitl` creates
          an operator HITL via the authorized path; actions are barred during zero-agent parks;
          unauthorized paths stay denied; the adjudicator only advises.
        acceptance: |-
          Allow + deny + idempotency + park-bar assertions pass. `make test` green.
        files:
          - orchestrator/tests/test_corrective_executor.py
          - gateway/tests/test_overseer_authority.py
  - id: 7
    name: |-
      Signal calibration fixes (§2)
    goal: |-
      Apply the calibrated fixes, each asserted against the slice-1 corpus: (a) lifecycle-owner-aware
      stall detector (#3230); (b) alert-reflection intent-discriminator in
      `shared/egg_agent/midturn_messages.py` — gate injection on INTENT (operator-directive vs
      informational/alert), NOT solely `from_role`; overseer/orchestrator informational alerts (e.g.
      `overseer_restart [info]`) MUST NOT render as binding course corrections; RETAIN the #3123
      brc-confirmation-timeout nudge (golden-file regression); (c) branch-divergence ancestor/patch-id
      replacing `_BRANCH_DIVERGENCE_PR_RE` (#2222/#2224), scan window capped; (d) heartbeat-stall fix
      (#2242); (e) #2059/#2132 thrashing/spinning/improper-tool-use definitions.
    dependencies: [6]
    exit_criteria: |-
      Each §2 corpus row flips from xfail to strict-pass; the #3123 nudge golden-file test passes;
      thrashing/spinning defs added. `make test` green.
    tasks:
      - id: TASK-7-1
        role: coder
        description: |-
          Lifecycle-owner-aware stall detector (#3230): a producer drafting under orchestrator-owned
          spawn is not "a phase with 0 running agents"; plug it into the slice-4 plane.
        acceptance: |-
          The #3230 corpus row returns no-alert; a genuinely stalled phase still alerts.
        files:
          - orchestrator/health_checks/tier1/phase_output.py
          - orchestrator/health_checks/context.py
      - id: TASK-7-2
        role: coder
        description: |-
          Alert-reflection intent-discriminator in `midturn_messages.py`: gate injection on intent
          (operator-directive vs informational/alert), not solely `from_role`; overseer/orchestrator
          informational alerts MUST NOT render as binding operator directives. RETAIN the #3123
          brc-confirmation-timeout nudge.
        acceptance: |-
          An `overseer_restart [info]` / `OVERSEER_ALERT` broadcast is no longer injected as a binding
          operator directive; the #3123 nudge still is (golden-file).
        files:
          - shared/egg_agent/midturn_messages.py
          - shared/egg_agent/client.py
      - id: TASK-7-3
        role: coder
        description: |-
          Branch-divergence: replace `_BRANCH_DIVERGENCE_PR_RE` subject matching
          (`routes/pipelines.py:15819`) with ancestor-of-origin/main OR patch-id match (#2222/#2224);
          cap the scan window.
        acceptance: |-
          A `(#NNNN)`-subject commit that is an ancestor / patch-id match no longer flags; genuine
          divergence still flags; corpus row strict-passes.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-7-4
        role: coder
        description: |-
          Heartbeat-stall fix (#2242 — tool calls every 2-3s is not a stall) and #2059/#2132
          thrashing/spinning/improper-tool-use classification definitions.
        acceptance: |-
          #2242 corpus row returns no-alert; thrashing/spinning/improper-tool-use are first-class,
          testable verdicts.
        files:
          - orchestrator/health_checks/tier1/consensus_stall.py
          - orchestrator/overseer/classifier.py
      - id: TASK-7-5
        role: tester
        description: |-
          Flip the §2 corpus rows to strict; add the #3123-nudge-retention golden-file regression and
          unit tests for the intent-discriminator, lifecycle-aware stall, ancestor/patch-id divergence,
          heartbeat-stall, and thrashing defs.
        acceptance: |-
          §2 corpus rows strict-pass; #3123 golden-file passes; each fix unit-tested. `make test` green.
        files:
          - orchestrator/tests/test_overseer_calibration.py
          - orchestrator/tests/test_branch_divergence_alert.py
          - shared/egg_agent/tests/test_midturn_messages.py
  - id: 8
    name: |-
      Coverage-gap detector survey — full §5 (cq-2 all-in-one)
    goal: |-
      Deliver the full §5 survey as corpus-tested, lifecycle-owner-aware deterministic detectors
      plugged into the slice-4 plane, escalating to adjudication only when ambiguous. Each new class
      gets a paste-ready issue-template entry. Gated entirely behind the slice-1 corpus contract.
    dependencies: [7]
    exit_criteria: |-
      Detectors for every surveyed layer land with strict corpus rows; `OverseerSelfMonitor` emits
      alerts (emit-vs-log nuance resolved); `self_monitor.py` cost tracking fixed. `make test` green.
    tasks:
      - id: TASK-8-1
        role: coder
        description: |-
          Orchestrator-runtime detectors: `_run_pipeline` thread liveness (#2234/#3233), auto-advance
          wedge → Tier-1 (#2219), approved-decision-orphaned, duration drift, agent-restart propagation
          deadline, restarted-decision replay. Plugged into the slice-4 plane.
        acceptance: |-
          Each is a registered detector returning `Optional[Finding]` with a strict corpus row.
        files:
          - orchestrator/health_checks/tier1/runtime_liveness.py
          - orchestrator/health_checks/tier1/decision_queue.py
          - orchestrator/health_checks/tier1/__init__.py
          - orchestrator/routes/pipelines.py
      - id: TASK-8-2
        role: coder
        description: |-
          Worktree/branch (ancestor/patch-id git-history sanity, corruption/lock anomalies, disk/inode
          pressure), container/k8s (funnel pod transitions through overseer #2210, repeated same-role
          restarts, OOMKilled/evicted #2948), and gateway (error-rate spikes, repeated identical
          denials, token-expiry) detectors.
        acceptance: |-
          Each is a registered detector with a strict corpus row (incl. the #2948 eviction-vs-
          permanent-death disambiguation).
        files:
          - orchestrator/health_checks/tier1/worktree_branch.py
          - orchestrator/health_checks/tier1/container_k8s.py
          - orchestrator/health_checks/tier1/gateway_health.py
          - orchestrator/health_checks/tier1/__init__.py
      - id: TASK-8-3
        role: coder
        description: |-
          BRC/thrashing (reviewer/producer thrash, late CONFIRMED then re-NACK, cap unbounded
          incomplete-consensus deferral), HITL/decision-queue, cost/budget (token-cost anomaly,
          `max_llm_cost_per_hour`, fix incomplete cost tracking in `self_monitor.py`), overseer
          self-health (classifier/advisor failure-rate wired to the alert path — resolve the
          `check_health` emit-vs-log nuance), external-state (`pr_external_mutation` drift,
          pushed-but-PR-not-updated), and LLM-substrate (LiteLLM reachability #2769, effective-model
          drift, sustained Anthropic 5xx) detectors. Each new class gets a paste-ready issue-template
          entry.
        acceptance: |-
          Each is registered + strict corpus-tested; `OverseerSelfMonitor.check_health()` emits alerts;
          cost tracking complete; each class has a template entry.
        files:
          - orchestrator/health_checks/tier1/brc_thrashing.py
          - orchestrator/health_checks/tier1/cost_budget.py
          - orchestrator/health_checks/tier1/llm_substrate.py
          - orchestrator/overseer/self_monitor.py
          - orchestrator/overseer/issue_filer.py
      - id: TASK-8-4
        role: tester
        description: |-
          Extend the corpus with a labelled row per new detector class and assert verdicts via the
          harness (strict); unit-test the self-monitor alert emission and the cost-tracking fix.
        acceptance: |-
          Every new detector has a strict corpus row; self-health + cost tests pass. `make test` green.
        files:
          - orchestrator/tests/test_overseer_calibration.py
          - orchestrator/tests/overseer_calibration/corpus.py
          - orchestrator/tests/test_overseer_self_monitor.py
  - id: 9
    name: |-
      Cleanup (§6, net-negative) + docs
    goal: |-
      Collapse per-class fail-soft scaffolding; de-duplicate advisor-escalation plumbing; harden the
      two-tier `file_issue` dedup. Confirm-then-remove genuinely-dead code ONLY — do NOT delete
      `issue_filer.py` on the stale #1962 premise (imported `__init__.py:27` + `monitor.py:36`, called
      `monitor.py:675`); do NOT re-decompose `monitor.py` (rides #2817). Update docs (overseer
      architecture, the orchestrator-side overseership model, deprecation notes for
      `overseer_decision_maker_model` + `EGG_OVERSEER_*`). Verify overall net-negative line count.
    dependencies: [8]
    exit_criteria: |-
      Subsystem net-negative in lines; dedup hardened; fail-soft collapsed; docs refreshed;
      `issue_filer.py` retained + exercised. `make test` green.
    tasks:
      - id: TASK-9-1
        role: coder
        description: |-
          Collapse per-class fail-soft scaffolding; de-duplicate advisor-escalation plumbing; harden the
          two-tier `file_issue` dedup. Re-confirm `issue_filer.py` is still imported/called before
          touching it (it is); remove only genuinely-dead code; do NOT re-decompose `monitor.py`.
        acceptance: |-
          Net deletion across `monitor.py`/`decision_maker.py`; dedup deterministic under repeats;
          `issue_filer.py` retained and exercised.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/overseer/issue_filer.py
          - orchestrator/overseer/decision_maker.py
      - id: TASK-9-2
        role: coder
        description: |-
          Finalize deprecation: ensure `overseer_decision_maker_model` + `EGG_OVERSEER_*` shims are
          inert and clearly marked; add the guarded shadow→enforce gate for
          `overseer_auto_file_issues_mode` (default stays shadow; flip only after telemetry validates).
        acceptance: |-
          Deprecated surfaces are inert/marked; the enforce mode is config-selectable and defaulted to
          shadow with test coverage.
        files:
          - orchestrator/models.py
          - orchestrator/overseer/monitor.py
      - id: TASK-9-3
        role: tester
        description: |-
          Tests for dedup hardening, fail-soft collapse, the shadow→enforce gate default, and a
          net-negative sanity check that deletion did not regress detection (corpus still green).
        acceptance: |-
          Cleanup behaviors covered; corpus stays green. `make test` green.
        files:
          - orchestrator/tests/test_overseer_cleanup.py
      - id: TASK-9-4
        role: documenter
        description: |-
          Refresh the docs for the delivered shape: overseer architecture, the orchestrator-side
          overseership model (detection plane + bounded corrective vocabulary + on-demand adjudicator),
          the detector catalogue, the authority path, and the deprecation notes.
        acceptance: |-
          `docs/architecture/overseer.md`, `health_checks/README.md`, and `overseer/README.md` reflect
          the delivered subsystem; #2817 decomposition noted out of scope.
        files:
          - docs/architecture/overseer.md
          - orchestrator/health_checks/README.md
          - orchestrator/overseer/README.md
```
