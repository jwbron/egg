# Issue #2270 — Overseer Overhaul: Implementation Plan

**Pipeline:** `issue-2270-overhaul` · **Phase:** plan · **Author:** task_planner
**Grounded against the tree:** 2026-06-26 · **Builds on:** refine analysis (`issue-2270-overhaul-analysis.md`)

## Resolved direction (refine HITL — binding)

- **cq-1 = Option C (hybrid).** Deterministic detection + a bounded corrective vocabulary run
  **orchestrator-side**; spawn a **normal, on-demand** agent (`spawn_agent_job`, Opus-tier, no
  special plumbing) **only** to adjudicate adversarial/high-stakes escalations.
- **cq-2 = All-in-one.** Deliver the full §1–§6 **including** the entire §5 coverage-gap survey
  in this pipeline.

The §1–§6 findings are **commitments**; Option C reshapes *how* §1/§1.5/§3/§5 land, not *whether*.
Net-negative-in-lines is a goal — a good chunk of the win is deletion (the baked-in monitor
script, the bespoke spawn/respawn plumbing, the fail-soft scaffolding).

## Strategy & slice topology

The overseer subsystem is tightly coupled: nearly every change touches some subset of
`orchestrator/overseer/monitor.py`, `orchestrator/kubernetes_spawner.py`,
`orchestrator/routes/pipelines.py`, and `orchestrator/health_checks/`. To keep the implement
phase legal under the **#3046 file-overlap validator** (overlapping slices must be ordered along
the DAG) and the **#2137 forest validator** (≤1 parent/slice), the plan is a **single serialized
chain** `slice-1 → … → slice-9`. Each slice is one reviewable stacked PR; later slices fork from
the prior slice's tip, so shared-file edits never collide.

**Calibration is deliverable #1** (slice-1): the tested known-normal/known-bad corpus lands first
so every subsequent detector change is measured against it (red→green per slice), not tuned by
eyeball.

### Grounded anchors honored (defend on NACK unless shown wrong)

- §1 model: `orchestrator/models.py:726-728` (`overseer_decision_maker_model` default `"sonnet"`);
  bypasses the resolver via `classify_model(decision_model)` at `kubernetes_spawner.py:2919`.
- §1.5: `spawn_overseer_job` `kubernetes_spawner.py:2883-2960`; `EGG_OVERSEER_*` env at `2922-2926`;
  generic `spawn_agent_job` at `:1228`; `AgentRole.OVERSEER` already recognized. Baked-in
  `sandbox/overseer_monitor.py` invoked via prompt at `:2931`.
- §2 alert-reflection: `shared/egg_agent/midturn_messages.py:63-75` `_INJECT_FROM_ROLES` includes
  `overseer` — no distinction between an overseer agent-bus broadcast and an operator HITL directive.
  (Observed live again this phase: an `[info]` `overseer_restart` alert reflected into context as an
  "operator directive".)
- §2 branch-divergence: subject regex `\(#\d+\)` at `routes/pipelines.py:15819`; detector `15822-15907`.
- §3 lifecycle: `_check_and_respawn_overseer` at `routes/pipelines.py:685-848`, called at `:23318`.
- §4 authority: the **real** enforcement point is the gateway — `gateway/phase_filter.py` `add-decision`
  command allowlist + `gateway/agent_restrictions.py` — **not** `roles.py:can_modify` (the issue's
  claim is STALE; `overseer` isn't even a `Role` enum value there).
- §5 Tier-1: `orchestrator/health_checks/tier1/` (6 classes); `OverseerSelfMonitor`
  (`overseer/self_monitor.py`) is already instantiated/health-checked — the open nuance is whether
  `check_health()` *emits alerts* vs only logs.
- §6: `overseer/issue_filer.py` **IS used** (`__init__.py:27`, `monitor.py:36/675`) — do NOT delete on
  the #1962 "unused" premise. `monitor.py` decomposition rides **#2817** — out of scope here.

### Role ↔ file ownership (verified via check_file_restriction, phase=implement)

- **coder** — all `.py`, `Dockerfile`, `.yml`/`.json` under orchestrator/shared/gateway/sandbox.
- **tester** — `orchestrator/tests/`, `gateway/tests/`, `**/test_*.py`, corpus `.py`/`.json` fixtures.
- **documenter** — `.md` only: `docs/`, READMEs, `sandbox/agent-config/rules/overseer.md`
  (`coder` is blocked from `**/*.md` / prompt rules).

---

## Slice summaries

1. **Calibration corpus & detection harness** (deliverable #1, §2) — root.
2. **Trustworthy signals** (§2): kill the false-positive vector; calibrate vs the corpus.
3. **Model tiering** (§1, folds #2813): resolve overseer model through `resolve_agent_model`.
4. **Run it like every other agent** (§1.5): fold `spawn_overseer_job`; delete bespoke flags + baked-in monitor.
5. **Lifecycle** (§3): kill respawn churn; fold `_check_and_respawn_overseer`; restart/generation hygiene.
6. **Hybrid overseership** (Option C core): orchestrator-side evaluation + bounded corrective vocabulary + on-demand adjudicator.
7. **Structural authority** (§4): authorized control-plane path to open operator HITLs.
8. **Coverage-gap survey** (§5): verified/extended detectors across every layer, corpus-tested.
9. **Cleanup + docs** (§6): net-negative deletion; dedup; enforce-gate; refresh architecture docs.

---

## Test strategy

- **Corpus-driven calibration (slice-1 →):** every detector change asserts expected verdicts against
  the labeled corpus. Slice-1 lands the harness with `xfail` markers for not-yet-fixed defects; each
  later slice flips its cases to strict (red→green) as the fix lands.
- **Per-slice unit tests** for each behavioral change (lifecycle-aware stall, ancestor/patch-id
  divergence, model resolution, spawn-via-`spawn_agent_job`, no-respawn-during-HITL, authority path,
  each new detector class).
- `make test` (changeset-aware) per slice; the corpus harness is the cross-cutting regression gate.

## Out of scope / deferred

- `monitor.py` (~2,030-line) structural decomposition → rides **#2817**.
- Flipping `overseer_auto_file_issues_mode` shadow→enforce in production → guarded flag only;
  flip after telemetry validates the gate (§6).
- Deleting `issue_filer.py` on the #1962 "unused" premise → it is used; re-confirm before any removal.

```yaml
# yaml-tasks
pr:
  title: |-
    Overseer overhaul: hybrid orchestrator-side detection (#2270)
  description: |-
    Overhaul of the overseer subsystem (#2270, all-in-one scope, refine HITL Option C).

    Replaces the respawning watcher pod + baked-in `overseer_monitor.py` bootstrap with
    deterministic **orchestrator-side** detection over the event stream plus a bounded
    corrective vocabulary (nudge, cohort respawn); a **normal, on-demand** agent
    (`spawn_agent_job`, Opus-tier) is spawned only to adjudicate adversarial escalations.

    - §1  Model: overseer decision/classification tiers resolved through `resolve_agent_model`
          (Haiku classify / Sonnet routine / Opus adversarial); `overseer_decision_maker_model`
          deprecated; `classify_model(decision_model)` bypass removed (folds #2813).
    - §1.5 No special case: `spawn_overseer_job` folded into `spawn_agent_job(OVERSEER, …)`;
          `EGG_OVERSEER_*` flags and the baked-in `sandbox/overseer_monitor.py` deleted.
    - §2  Calibration (deliverable #1): tested known-normal/known-bad corpus; lifecycle-owner-aware
          stall detection (#3230/#2242); ancestor/patch-id branch-divergence (#2222/#2224);
          alert-reflection vector closed (overseer broadcasts no longer surfaced as operator
          directives); thrashing/spinning defs (#2059/#2132).
    - §3  Lifecycle: respawn churn eliminated; `_check_and_respawn_overseer` folded into
          agent-restart machinery; escalation-history / generation-token reset hygiene.
    - §4  Authority: structural authorized path to open operator HITLs + bounded corrective
          vocabulary (real enforcement point = gateway `phase_filter`/`agent_restrictions`).
    - §5  Coverage-gap survey: extended detectors across orchestrator runtime, worktree/branch,
          container/k8s, gateway, BRC/thrashing, HITL queue, cost/budget, self-health, external
          state, and LLM substrate — each corpus-tested.
    - §6  Cleanup (net-negative): fail-soft scaffolding collapsed; advisor-escalation de-duped;
          two-tier `file_issue` dedup hardened; per-alert issue templates; shadow→enforce gate.

    `monitor.py` decomposition (#2817) is out of scope.
  test_plan: |-
    - Automated: corpus-driven calibration harness (`test_overseer_calibration`) asserts each
      Tier-1 / orchestrator-side detector's verdict against the labeled known-normal/known-bad set;
      per-slice unit tests for model resolution, spawn-via-`spawn_agent_job`, lifecycle-aware stall,
      ancestor/patch-id divergence, no-respawn-during-HITL, the authority path, and every new
      detector class. `make test` per slice; corpus harness is the cross-cutting regression gate.
    - Manual: confirm a synthetic deadlock opens an operator HITL via the authorized path; confirm
      no overseer pod is spawned during a multi-hour HITL park with zero agents running.
  manual_steps: |-
    Pre-merge: stack the slice PRs in order (slice-1 → slice-9); review each independently.
    Post-merge: monitor an `EGG_EVENT_LOOP_OWNER=orchestrator` run for a clean overseer alert
    stream (no self-injection / false-stall / reflected-directive alarms) before flipping
    `overseer_auto_file_issues_mode` shadow→enforce.
slices:
  - id: 1
    name: |-
      Calibration corpus & detection harness
    goal: |-
      Deliverable #1 (§2): build the tested known-normal/known-bad corpus that all detector
      calibration is measured against, plus a scoreboard harness. Lands with xfail markers for
      not-yet-fixed defects so the slice is green; later slices flip cases to strict.
    dependencies: []
    exit_criteria: |-
      Corpus fixtures + loader committed; harness runs every existing Tier-1 detector against the
      corpus and reports a precision/recall scoreboard; xfail markers tag the known defects fixed
      downstream. `make test` green.
    tasks:
      - id: TASK-1-1
        role: tester
        description: |-
          Create the calibration corpus: a labeled fixture set covering the known incidents —
          self-injection loop, alert-reflection, #3230 false stall (event-loop pod not in running
          set), #2242 heartbeat-stall mid-draft, #2222/#2224 branch-divergence, #2948 transient
          kubelet eviction misread — each as a structured (input event stream, lifecycle-owner,
          expected verdict) record, plus a loader.
        acceptance: |-
          `corpus.py` exposes the labeled cases and a loader; each case names its expected verdict
          (alert / no-alert) and the defect/issue it pins. Fixtures parse and load in a test.
        files:
          - orchestrator/tests/overseer_calibration/__init__.py
          - orchestrator/tests/overseer_calibration/corpus.py
          - orchestrator/tests/overseer_calibration/fixtures.json
      - id: TASK-1-2
        role: tester
        description: |-
          Build the calibration harness: run each existing Tier-1 detector
          (`health_checks/tier1/`) against the corpus and assert expected verdicts, producing a
          precision/recall scoreboard. Tag currently-failing known-bad/known-normal cases with
          `xfail(reason=...)` referencing the slice that fixes them so this slice stays green.
        acceptance: |-
          `test_overseer_calibration.py` runs the corpus through the detectors and asserts the
          scoreboard; xfail markers cover exactly the defects fixed in slices 2/6/8. Test passes.
        files:
          - orchestrator/tests/test_overseer_calibration.py
      - id: TASK-1-3
        role: documenter
        description: |-
          Document the corpus contract: case schema, how to add a case, and the red→green
          flip-to-strict convention used by downstream slices.
        acceptance: |-
          Doc explains the corpus record shape, the scoreboard, and the xfail→strict workflow.
        files:
          - docs/architecture/overseer-calibration-corpus.md
  - id: 2
    name: |-
      Trustworthy signals — close the false-positive vector
    goal: |-
      §2 (calibration is deliverable #1): eliminate the cry-wolf defects and calibrate detection
      against the slice-1 corpus. Make detection lifecycle-owner-aware; stop reflecting overseer
      agent-bus broadcasts as operator directives; fix branch-divergence; add thrashing defs.
    dependencies: [1]
    exit_criteria: |-
      Alert-reflection, false-stall (#3230/#2242), and branch-divergence (#2222/#2224) corpus cases
      flip from xfail to strict-pass; thrashing/spinning defs (#2059/#2132) added. `make test` green.
    tasks:
      - id: TASK-2-1
        role: coder
        description: |-
          Stop surfacing overseer agent-bus broadcasts as operator HITL directives: in
          `midturn_messages.py`, distinguish operator-authored HITL from `overseer`/`orchestrator`
          agent-bus broadcasts (remove `overseer` from the operator-injection set or tag broadcasts
          so the injection path does not render them as operator messages). Update the client call
          site as needed.
        acceptance: |-
          An overseer `OVERSEER_ALERT` / `overseer_restart` broadcast is no longer injected into a
          working agent's context as an operator directive; genuine operator HITL still is.
        files:
          - shared/egg_agent/midturn_messages.py
          - shared/egg_agent/client.py
      - id: TASK-2-2
        role: coder
        description: |-
          Make the stall/heartbeat detectors lifecycle-owner-aware (#3230, #2242): count
          event-loop-spawned pods in the running-agent set so "0 running agents" cannot fire
          `phase stalled` while a producer is actively drafting; suppress heartbeat-stall false
          positives for plan producers mid-drafting.
        acceptance: |-
          The #3230 and #2242 corpus cases return no-alert; a genuinely stalled phase still alerts.
        files:
          - orchestrator/health_checks/tier1/phase_output.py
          - orchestrator/health_checks/tier1/consensus_stall.py
          - orchestrator/health_checks/context.py
          - orchestrator/overseer/monitor.py
      - id: TASK-2-3
        role: coder
        description: |-
          Replace the branch-divergence subject regex (`\(#\d+\)`, `routes/pipelines.py:15819`)
          with the correct test: ancestor-of-origin/main OR patch-id match (#2222/#2224).
        acceptance: |-
          A `(#NNNN)`-subject commit that IS an ancestor / patch-id match no longer flags as
          merged-main contamination; genuine divergence still flags. Corpus case strict-passes.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-4
        role: coder
        description: |-
          Add #2059/#2132 thrashing / spinning / improper-tool-use classification definitions to the
          overseer classifier so these become first-class, testable verdicts.
        acceptance: |-
          Classifier recognizes thrashing/spinning/improper-tool-use with documented thresholds;
          corpus cases assert the verdicts.
        files:
          - orchestrator/overseer/classifier.py
      - id: TASK-2-5
        role: tester
        description: |-
          Flip the slice-1 corpus cases for the now-fixed defects from xfail to strict; add unit
          tests for midturn injection filtering, lifecycle-aware stall, ancestor/patch-id divergence,
          and the new thrashing defs.
        acceptance: |-
          Corpus scoreboard strict-passes the §2 cases; new unit tests cover each fix. `make test` green.
        files:
          - orchestrator/tests/test_overseer_calibration.py
          - orchestrator/tests/test_branch_divergence_alert.py
          - shared/egg_agent/tests/test_midturn_messages.py
  - id: 3
    name: |-
      Model tiering via the standard resolver
    goal: |-
      §1 (folds #2813): resolve the overseer's decision/classification tiers through
      `resolve_agent_model` (Haiku classify / Sonnet routine / Opus adversarial); deprecate the
      bespoke `overseer_decision_maker_model`; remove the `classify_model(decision_model)` bypass.
    dependencies: [2]
    exit_criteria: |-
      Overseer model resolved via `resolve_agent_model`; deprecated field warns and is no longer
      consulted at spawn; tiering verified by tests. `make test` green.
    tasks:
      - id: TASK-3-1
        role: coder
        description: |-
          Route the overseer model through `resolve_agent_model` with explicit tiering; deprecate
          `overseer_decision_maker_model` (`models.py:726`); remove the
          `classify_model(decision_model)` bypass at `kubernetes_spawner.py:2919`; wire the
          decision_maker/classifier to resolved per-role models.
        acceptance: |-
          No spawn path consults `overseer_decision_maker_model`; the resolver returns Opus for the
          adversarial tier and the cheaper tiers for classify/routine. Deprecation is logged.
        files:
          - orchestrator/models.py
          - orchestrator/agent_model_resolution.py
          - orchestrator/kubernetes_spawner.py
          - orchestrator/overseer/decision_maker.py
          - orchestrator/overseer/classifier.py
      - id: TASK-3-2
        role: tester
        description: |-
          Tests: overseer model resolves through `resolve_agent_model`; tiering correct; the
          deprecated field is ignored/warns; #2813 regression (resolver consulted, not the builder
          shortcut).
        acceptance: |-
          Tests assert resolved models per tier and that the deprecated field no longer drives spawn.
        files:
          - orchestrator/tests/test_agent_model_resolution.py
          - orchestrator/tests/test_overseer_model.py
  - id: 4
    name: |-
      Run the overseer like every other agent
    goal: |-
      §1.5: fold `spawn_overseer_job` into `spawn_agent_job(agent_role=OVERSEER, …)`; delete the
      `EGG_OVERSEER_*` bespoke flags and the baked-in `overseer_monitor.py` trust-and-run bootstrap
      (the root cause of the §1 self-injection loop). Any surviving agent runs the standard
      image/entrypoint; monitoring arrives via tools/MCP/prompt.
    dependencies: [3]
    exit_criteria: |-
      No `spawn_overseer_job`, no `EGG_OVERSEER_*` env, no baked-in monitor script; the overseer (when
      spawned) goes through `spawn_agent_job`. `make test` green.
    tasks:
      - id: TASK-4-1
        role: coder
        description: |-
          Fold `spawn_overseer_job` (`kubernetes_spawner.py:2883-2960`) into
          `spawn_agent_job(agent_role=OVERSEER, …)`; remove `EGG_OVERSEER_MODE/_POLL_INTERVAL/
          _DECISION_MODEL`; remove the `python3 …/overseer_monitor.py --once` prompt bootstrap; update
          the call site(s) in `routes/pipelines.py`.
        acceptance: |-
          The overseer spawns via the generic agent path with no bespoke env or baked-script
          invocation; permissions/prompt differ but plumbing does not.
        files:
          - orchestrator/kubernetes_spawner.py
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        role: coder
        description: |-
          Delete `sandbox/overseer_monitor.py` and remove its bake/copy from `sandbox/Dockerfile`.
        acceptance: |-
          `overseer_monitor.py` is gone; no Dockerfile layer references it; image builds.
        files:
          - sandbox/overseer_monitor.py
          - sandbox/Dockerfile
      - id: TASK-4-3
        role: documenter
        description: |-
          Move whatever monitoring guidance the overseer genuinely needs from the deleted baked
          script into the overseer prompt/rule, framed as tool/MCP usage — not a trust-and-run script.
        acceptance: |-
          The overseer rule describes its monitoring duties via available tools/MCP; no reference to a
          baked-in script remains.
        files:
          - sandbox/agent-config/rules/overseer.md
      - id: TASK-4-4
        role: tester
        description: |-
          Tests: overseer spawn goes through `spawn_agent_job`; no `EGG_OVERSEER_*` env set; no
          monitor-script reference; deletion regression.
        acceptance: |-
          Spawn-path tests assert the generic path and absence of bespoke flags/script.
        files:
          - orchestrator/tests/test_kubernetes_spawner.py
  - id: 5
    name: |-
      Lifecycle — kill respawn churn
    goal: |-
      §3: eliminate respawn churn (no overseer during HITL parks with zero agents running); fold
      `_check_and_respawn_overseer` (`routes/pipelines.py:685-848`) into general agent-restart
      machinery; add restart/generation hygiene (clear per-agent escalation history on restart;
      generation-token reset on orchestrator pod recycle).
    dependencies: [4]
    exit_criteria: |-
      No overseer-specific respawn loop; no spawn during agent-less HITL parks; escalation history and
      generation token reset on restart/recycle. `make test` green.
    tasks:
      - id: TASK-5-1
        role: coder
        description: |-
          Remove/fold `_check_and_respawn_overseer` into the general agent-restart machinery; gate any
          overseer presence on "agents actually running" so multi-hour HITL parks spawn nothing.
        acceptance: |-
          No overseer respawn fires during a zero-agent HITL park; restart logic shares the agent path.
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/models.py
      - id: TASK-5-2
        role: coder
        description: |-
          Restart/generation hygiene: clear per-agent escalation history on restart; add a generation
          token reset on orchestrator pod recycle so stale escalation state can't cascade.
        acceptance: |-
          On restart, escalation history is cleared; on recycle, the generation token resets; stale
          state does not leak across generations.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/routes/pipelines.py
      - id: TASK-5-3
        role: tester
        description: |-
          Tests for no-respawn-during-HITL, escalation-history reset on restart, and generation-token
          reset on recycle.
        acceptance: |-
          Tests assert the churn is gone and the reset hygiene holds. `make test` green.
        files:
          - orchestrator/tests/test_overseer_lifecycle.py
  - id: 6
    name: |-
      Hybrid overseership — orchestrator-side evaluation + on-demand adjudicator
    goal: |-
      Option C core: promote detection to deterministic **orchestrator-side** evaluation over the
      event stream (extend the `health_checks` runner) with a bounded corrective vocabulary (nudge,
      cohort respawn); spawn a normal on-demand agent (`spawn_agent_job`, Opus-tier) ONLY to
      adjudicate adversarial/high-stakes escalations.
    dependencies: [5]
    exit_criteria: |-
      Deterministic detection + corrective vocabulary run orchestrator-side with no standing pod; the
      on-demand adjudicator is spawned only on adversarial escalation. `make test` green.
    tasks:
      - id: TASK-6-1
        role: coder
        description: |-
          Orchestrator-side evaluator over the event stream: extend the `health_checks` runner to
          deterministically detect and apply a bounded corrective vocabulary (nudge a stuck agent,
          cohort respawn) without an LLM in the loop for routine cases.
        acceptance: |-
          Routine detections + corrective actions execute orchestrator-side deterministically; the
          corrective vocabulary is explicitly bounded (enumerated actions only).
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/health_checks/context.py
          - orchestrator/health_checks/corrective.py
          - orchestrator/routes/pipelines.py
      - id: TASK-6-2
        role: coder
        description: |-
          Escalation path: only adversarial/high-stakes verdicts spawn a normal on-demand overseer
          agent (`spawn_agent_job`, Opus-tier) to adjudicate; route the decision_maker through that
          path and retire the standing-pod assumptions in `monitor.py`.
        acceptance: |-
          The agent is spawned only on adversarial escalation; routine cases never spawn it; no
          long-lived pod remains.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/overseer/decision_maker.py
          - orchestrator/routes/pipelines.py
      - id: TASK-6-3
        role: tester
        description: |-
          Tests: deterministic detection + corrective vocabulary fire orchestrator-side; the on-demand
          adjudicator spawns only on adversarial escalation; corpus regressions hold.
        acceptance: |-
          Tests assert the hybrid split and the bounded vocabulary. `make test` green.
        files:
          - orchestrator/tests/test_overseer_hybrid.py
      - id: TASK-6-4
        role: documenter
        description: |-
          Architecture doc for the hybrid shape: deterministic orchestrator-side detection, bounded
          corrective vocabulary, on-demand adjudicator, and what was retired (standing pod, baked
          monitor).
        acceptance: |-
          `docs/architecture/overseer.md` describes the Option C topology and the retired surfaces.
        files:
          - docs/architecture/overseer.md
  - id: 7
    name: |-
      Structural authority to act
    goal: |-
      §4: give the overseer/control-plane a structural, authorized path to open operator HITLs plus
      the bounded corrective vocabulary. The real enforcement point is the gateway
      (`phase_filter.py` add-decision allowlist + `agent_restrictions.py`), NOT `roles.py:can_modify`.
    dependencies: [6]
    exit_criteria: |-
      A confirmed deadlock can open an operator HITL through the authorized path; corrective actions
      are bounded and authorized; the gateway permits exactly those. `make test` green.
    tasks:
      - id: TASK-7-1
        role: coder
        description: |-
          Open the authority path at the real enforcement point: authorize the control plane to open
          operator HITLs (`register_open_question` / `add-decision`) and issue the bounded corrective
          vocabulary. Adjust the gateway add-decision allowlist / agent_restrictions and the contract
          role mapping as needed (locate-then-fix; do not chase the stale `roles.py:can_modify`).
        acceptance: |-
          The authorized control-plane path opens an operator HITL and issues bounded corrective
          actions; unauthorized callers remain 403. Real enforcement point is documented in the diff.
        files:
          - gateway/phase_filter.py
          - gateway/agent_restrictions.py
          - shared/egg_contracts/roles.py
          - orchestrator/routes/pipelines.py
      - id: TASK-7-2
        role: tester
        description: |-
          Tests: a confirmed deadlock opens an operator HITL via the authorized path; the corrective
          vocabulary is bounded; unauthorized paths stay denied.
        acceptance: |-
          Authority tests pass for allow + deny cases. `make test` green.
        files:
          - gateway/tests/test_overseer_authority.py
          - orchestrator/tests/test_overseer_authority.py
  - id: 8
    name: |-
      Coverage-gap survey — verify & extend detectors
    goal: |-
      §5 (all-in-one): verify and extend detector coverage across every layer, each detector
      lifecycle-owner-aware and corpus-tested. Resolve the `OverseerSelfMonitor` alert-emission nuance
      and fix incomplete cost tracking.
    dependencies: [7]
    exit_criteria: |-
      New detector classes for each surveyed layer land with corpus cases; `OverseerSelfMonitor`
      emits alerts (not just logs); cost tracking fixed. `make test` green.
    tasks:
      - id: TASK-8-1
        role: coder
        description: |-
          Orchestrator-runtime detectors: `_run_pipeline` thread liveness (#2234/#3233), auto-advance
          wedge → Tier-1 (#2219), approved-decision-orphaned, duration drift, agent-restart
          propagation deadline, restarted-decision replay.
        acceptance: |-
          Each detector is a registered Tier-1 class with a corpus case; barrel `__init__` exports them.
        files:
          - orchestrator/health_checks/tier1/runtime_liveness.py
          - orchestrator/health_checks/tier1/decision_queue.py
          - orchestrator/health_checks/tier1/__init__.py
          - orchestrator/routes/pipelines.py
      - id: TASK-8-2
        role: coder
        description: |-
          Worktree/branch + container/k8s + gateway detectors: git-history sanity (ancestor+patch-id),
          corruption/lock + disk/inode pressure; pod-transition funnel through the overseer (#2210),
          repeated same-role restarts, OOMKilled/evicted (#2948); gateway error-rate spikes, repeated
          identical denials, token-expiry.
        acceptance: |-
          Each detector is a registered Tier-1 class with a corpus case (including the #2948
          eviction-vs-permanent-death disambiguation).
        files:
          - orchestrator/health_checks/tier1/worktree_branch.py
          - orchestrator/health_checks/tier1/container_k8s.py
          - orchestrator/health_checks/tier1/gateway_health.py
          - orchestrator/health_checks/tier1/__init__.py
      - id: TASK-8-3
        role: coder
        description: |-
          BRC/thrashing + HITL queue + cost/budget + self-health + external-state + LLM-substrate
          detectors: reviewer/producer thrash, late-CONFIRMED-then-re-NACK, incomplete-consensus cap;
          HITL/decision-queue; token-cost anomaly + `max_llm_cost_per_hour` + fix `self_monitor.py`
          cost tracking + wire `OverseerSelfMonitor` into the alert path; `pr_external_mutation` /
          pushed-but-PR-not-updated; LiteLLM-proxy reachability (#2769), effective-model drift,
          sustained Anthropic 5xx.
        acceptance: |-
          Each detector registered + corpus-tested; `OverseerSelfMonitor.check_health()` emits alerts;
          cost tracking is complete.
        files:
          - orchestrator/health_checks/tier1/brc_thrashing.py
          - orchestrator/health_checks/tier1/cost_budget.py
          - orchestrator/health_checks/tier1/llm_substrate.py
          - orchestrator/overseer/self_monitor.py
          - orchestrator/overseer/monitor.py
      - id: TASK-8-4
        role: tester
        description: |-
          Extend the corpus with a labeled case per new detector class and assert verdicts through the
          calibration harness; unit-test the self-monitor alert emission and cost tracking.
        acceptance: |-
          Every new detector has a strict corpus case; self-health + cost tests pass. `make test` green.
        files:
          - orchestrator/tests/test_overseer_calibration.py
          - orchestrator/tests/overseer_calibration/corpus.py
          - orchestrator/tests/test_overseer_self_monitor.py
  - id: 9
    name: |-
      Cleanup (net-negative) + docs
    goal: |-
      §6: net-negative line count. Collapse per-class fail-soft scaffolding; de-dup advisor-escalation
      plumbing; harden two-tier `file_issue` dedup; add a paste-ready issue template entry per new
      alert class; guarded shadow→enforce gate for `overseer_auto_file_issues_mode`. Refresh docs.
      (`monitor.py` decomposition rides #2817 — NOT here; `issue_filer.py` IS used — do NOT delete.)
    dependencies: [8]
    exit_criteria: |-
      Subsystem is net-negative in lines; dedup hardened; enforce-gate present (still shadow by
      default); docs refreshed. `make test` green.
    tasks:
      - id: TASK-9-1
        role: coder
        description: |-
          Collapse per-class fail-soft scaffolding; de-duplicate advisor-escalation plumbing; harden
          the two-tier `file_issue` dedup. Re-confirm `issue_filer.py` is still imported/called before
          touching it (it is — `__init__.py:27`, `monitor.py:36/675`); add a paste-ready issue-template
          entry for each new alert class.
        acceptance: |-
          Net deletion across `monitor.py`/`decision_maker.py`; dedup deterministic under repeats;
          `issue_filer.py` retained and exercised; each new alert class has a template entry.
        files:
          - orchestrator/overseer/monitor.py
          - orchestrator/overseer/issue_filer.py
          - orchestrator/overseer/decision_maker.py
      - id: TASK-9-2
        role: coder
        description: |-
          Add the guarded shadow→enforce gate for `overseer_auto_file_issues_mode` (default stays
          shadow; flip only after telemetry validates) so the enforce path exists but is off by default.
        acceptance: |-
          The enforce mode is selectable via config and defaulted to shadow; the gate is covered by a test.
        files:
          - orchestrator/models.py
          - orchestrator/overseer/monitor.py
      - id: TASK-9-3
        role: tester
        description: |-
          Tests for dedup hardening, fail-soft collapse behavior, and the shadow→enforce gate default.
        acceptance: |-
          Cleanup behaviors are covered; net-negative does not regress detection. `make test` green.
        files:
          - orchestrator/tests/test_overseer_cleanup.py
      - id: TASK-9-4
        role: documenter
        description: |-
          Refresh the architecture/reference docs for the final overseer shape: the hybrid topology,
          the detector catalogue, the authority path, and the net-negative summary.
        acceptance: |-
          `docs/architecture/overseer.md`, `health_checks/README.md`, and `overseer/README.md` reflect
          the delivered subsystem; #2817 decomposition noted as out of scope.
        files:
          - docs/architecture/overseer.md
          - orchestrator/health_checks/README.md
          - orchestrator/overseer/README.md
```
