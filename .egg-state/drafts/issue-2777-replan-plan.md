# Plan: Cleanup — sliced implementation phase (context-PR topology + slice/phase restart)

> Issue: #2777 | Phase: plan | Pipeline: `issue-2777-replan`

## Approach

The refine phase already produced a comprehensive analysis
(`.egg-state/drafts/issue-2777-replan-analysis.md`) and the operator
answered every HITL decision (cq-1..cq-10, decision-11, feedback-1
Q1..Q5; plan-gate decision-13 approved). This plan executes those
decisions verbatim and translates them into a 2-slice DAG.

**Re-plan note (#2809)**: this pipeline (`issue-2777-replan`) re-runs
the plan phase against the previously approved refine artifacts.
Slice composition is the architect's call per #2809; the architect's
binding scaffold file
(`.egg-state/agent-outputs/issue-2777-replan-architect-slices.yaml`)
landed mid-cycle in commit `0dc42f4b6`. **The yaml-tasks appendix
below copies the architect's slice `id`, `name`, `goal`, and
`parent_slice_id` values verbatim from the scaffold; tasks under
each slice are the task_planner's enumeration.** The architect's
scaffold confirms what the contract's already-populated `slices`
field encoded (the operator's decision-13 approval): slice-1 = A+D
context-PR collapse + cleanup + PR-phase removal (`parent_slice_id:
null`, the root); slice-2 = C slice/phase restart hardening
(`parent_slice_id: 1`); #2792 OUT OF SCOPE per decision-11 (now
independently resolved in merged PR #2797).

### Iteration 1: operator HITL kickback on slice-1 granularity — task_planner surfaces a slice-size concern for reviewers

> **Status**: this is plan-phase iteration 1. Iteration 0 reached
> consensus (reviewer_plan and risk_analyst both ACKed the prior
> architect, task_planner, and risk_analyst proposals), but the
> operator kicked the phase back via HITL with a directive on the
> slice-1 granularity. Per the BRC framing in this role's system
> prompt, "later directives override earlier ones."

**Operator's iteration-1 directive (verbatim, abridged for the
slice-size point — full text in the system prompt directives
section)**:

> Sub-slice slice-1. It currently carries 16 coder tasks — larger
> than the 14-task slice-1 that wedged the prior run. Per #2809 the
> architect owns composition and should subdivide when a slice is too
> large for a coder to land in completable, reviewable passes; this
> one is. […]
>
> Split slice-1 into sequential sub-slices along its natural seams.
> Suggested shape (boundaries and dependency edges are the
> architect's call — this is a sketch, not a mandate):
> - slice-1a — new primitives + wiring, landed first so the primary
>   use case is reachable/reviewable on its own […]
> - slice-1b — the scaffold + PR-phase deletions that depend on 1a's
>   opener being live […]
> - slice-1c — cohesion-independent cleanup […]
>
> Keep slice-2 (restart hardening, closes #2409) unchanged. ALL other
> Wave 2 decisions (cq-1's [A+D]→[C] dependency direction, cq-2..cq-10,
> feedback Q1–Q5) remain binding and unchanged — only the granularity
> WITHIN the A+D work changes, from one slice to sequential
> sub-slices.

**Architect's iteration-1 response (from
`.egg-state/agent-outputs/issue-2777-replan-architect-output.json`,
`replan_change_log` entry 4 and `recommended_approach.rejected_options_summary.further_subdivision_of_slice_1`)**:

The architect preserved the 2-slice DAG without further sub-slicing.
Their stated rationale:

> "(a) operator explicitly chose option 4 in cq-1 ('Two slices with
> dependency: [A+D] → [C]'); (b) most slice-1 components have textual
> conflicts on `pipelines.py` — sub-slicing would create merge pain
> inside the same function (`_run_implement_phase_slices`) and the
> same scaffold (the ~600-line context-PR machinery); (c) the prior
> plan with this shape was approved at the operator gate (decision-13
> on 2026-05-27); (d) the natural seam between A's context-PR
> collapse and D's coupled cleanup is the SAME `pipelines.py` change
> region in nearly every case, so the right unit of review IS the
> bundled slice-1."

And in the rejected-options summary:

> "further_subdivision_of_slice_1: Considered under #2809
> (architect-owned slice composition). Rejected because the natural
> seams within slice-1 (Context-PR-collapse vs PR-phase-deletion vs
> ConsensusEvaluator-deletion vs cleanup tail) all touch the SAME
> files (pipelines.py, gateway_client.py, gateway.py, models.py,
> phases.py) at overlapping line ranges; further sub-slicing converts
> intra-PR review work into cross-PR merge-conflict resolution. The
> 2-slice shape from cq-1 stands."

**Task_planner's position (this iteration, this plan)**:

Per the role-system-prompt directive — *"Slice composition is NOT
your call (#2809). architect owns slice count, slice boundaries,
slice DAG shape, and sub-slicing — and emits the binding scaffold."*
— **this plan enumerates tasks against the architect's binding
2-slice scaffold without re-shaping it.** I am not silently
re-grouping tasks into 1a/1b/1c.

But I am also *required by that same role-system-prompt section* to
surface the slice-size concern when I see it: *"If a slice has too
many tasks for one BRC cycle, or you discover a natural sub-seam
the architect missed, that is a slicing problem the architect must
fix — surface it as NACK pressure (your peer reviewer risk_analyst
and the structural reviewer reviewer_plan will NACK architect on
slice_size when evidence supports it; you can also flag the concern
in your plan prose so the reviewers pick it up)."*

The empirical evidence the operator cited:

- Iteration 0's slice-1 carried 16 coder tasks (TASK-1-1, 1-1a, 1-2,
  1-3, 1-4, 1-5, 1-5b, 1-6, 1-7, 1-8, 1-9, 1-10, 1-11, 1-12, 1-13,
  1-14; plus tester tasks 1-3a, 1-4b, 1-5a, 1-15, 1-15a, 1-16, 1-16a,
  1-17; plus documenter task 1-18 — 25 tasks total in slice-1). This
  iteration's enumeration is unchanged (same 16 coder tasks against
  the same scaffold).
- The operator explicitly noted: "larger than the 14-task slice-1
  that wedged the prior run." That prior wedge predates this
  pipeline, but the operator's read is that the 16-task slice is
  *worse* than a known-wedged 14-task slice.
- The operator's natural-seam sketch (1a = primitives + wiring; 1b =
  scaffold + PR-phase deletions that depend on 1a's opener being
  live; 1c = cohesion-independent cleanup) directly maps to
  topological dependencies in the existing plan's task graph:
  TASK-1-1 / 1-1a / 1-13 produce primitives that TASK-1-2 / 1-3 /
  1-4 / 1-5 / 1-5b / 1-6 / 1-7 / 1-14 consume; TASK-1-8 / 1-9 /
  1-10 / 1-11 / 1-12 / 1-15a are cohesion-independent (they don't
  depend on the scaffold deletions and don't produce the new
  primitives). Roughly: 1a = {1-1a, 1-1, 1-13} (3 coder tasks);
  1b = {1-2, 1-3, 1-4, 1-5, 1-5b, 1-6, 1-7, 1-14} (8 coder tasks);
  1c = {1-8, 1-9, 1-10, 1-11, 1-12} (5 coder tasks); the tester /
  documenter tasks (1-3a, 1-4b, 1-5a, 1-15, 1-15a, 1-16, 1-16a,
  1-17, 1-18) re-anchor under whichever sub-slice their target code
  ships in. If the architect adopts this shape, the BRC review unit
  shrinks from one 16-coder slice to three sequential 3/8/5-coder
  slices.

The architect's counter-argument (textual conflicts inside the same
`pipelines.py` function) is real and is on the table. The
task_planner's role is not to adjudicate it — the **structural
review** belongs to reviewer_plan, and the **risk weighting** belongs
to risk_analyst. **I am flagging this so both reviewers see the
disagreement before they vote on architect's proposal.**

If reviewer_plan or risk_analyst NACKs architect on `slice_size` per
the prompt-template's structural review rubric, the architect's
revised scaffold (1a/1b/1c, presumably) will land, and I will
re-propose this plan against the revised scaffold — at that point
the per-task `id`s will be re-numbered into the sub-slices but the
task content is largely additive (no task is removed; each task moves
into one of {1a, 1b, 1c} based on whether it produces primitives,
deletes scaffold downstream of those primitives, or is
cohesion-independent cleanup).

If reviewer_plan and risk_analyst ACK architect's 2-slice scaffold
on its merits (e.g. the textual-conflict argument outweighs the
slice-size concern), this plan stands as-is and the implement-phase
coder takes on the 16-task slice-1 with the verification artifacts
and re-anchoring protocol described below.

**This section exists to make the disagreement visible. The plan
itself follows architect's binding scaffold.**

### Anchor SHA & re-anchoring (added per reviewer_plan v2 blocker 7)

Every `file:line` citation in this plan is anchored against the
refine-phase commit `1cb235871` (the SHA the analysis was authored
against). HEAD at the time of plan-phase iteration-1 consensus is
`28f7ef9b2` (on branch `egg/issue-2777-replan-task_planner/work`,
with `origin/main` at `3a51f72d9`) — re-verified at iteration-1
plan-time via direct grep against `orchestrator/routes/pipelines.py`:
`_should_skip_pr_phase_auto_pr` at `:8854`,
`_open_context_pr_for_pipeline` at `:10634`,
`_maybe_open_base_pr_for_plan_to_implement` at `:11280` with call
sites at `:16503, :22132, :23671, :24666` (plus
`orchestrator/routes/phases.py:500`),
`_persist_context_pr_linkage_on_contract` at `:10423` (writes
context_branch downstream of `:10547`'s gather call), the SIX
ConsensusEvaluator clusters in `pipelines.py` at
`:1813-1816, :2859-2863, :3289-3293, :3516-3522, :4489-4493,
:4498-4502` PLUS two additional clusters in
`orchestrator/routes/phases.py:119-124` and
`orchestrator/routes/signals.py:847-871` (eight total — see
architect's AC-18); the three PipelinePhase.PR reference sites in
`pipelines.py:4355, :20221, :21354` (plus phases.py:70-71); and
PRMetadata field locations at `shared/egg_contracts/models.py:499,
:507, :514`. Gateway primitives: `_CONTEXT_BRANCH_RE` at
`gateway/gateway.py:1113` with adjacent `is_context_push` lifecycle
at `:1344-1392`. The drift will continue to grow between plan-phase
consensus and implement-phase start as `main` advances.

**Mandatory implementer protocol**: before editing any cited line,
the implement-phase coder MUST run `grep -n` against HEAD to
re-anchor the symbol's actual location. Prefer symbol names +
adjacent-text anchors over absolute line numbers — `git diff`
preserves the symbol; the absolute line is ephemeral. Acceptance
criteria across every task in this plan implicitly include a
"verified via grep at HEAD before editing" preflight; commits
should include the before-grep output in the commit message so
reviewers can rerun the verification. The specific re-anchoring
steps for each multi-site task are encoded explicitly (TASK-1-2,
TASK-1-3, TASK-1-6, TASK-1-9).

### Operator decision summary (drives every task below)

- **cq-1** (slice shape): **Two slices, sequential** — `slice-1` (A+D: context-PR collapse + dead-code purge + PR-phase removal) → `slice-2` (C: slice/phase restart hardening). Goal B (#2792 / `plan_draft_missing_on_local`) is **OUT OF SCOPE** per `decision-11`; no task may touch `_sync_worktree_with_remote`, `_populate_contract_from_plan*`, `_empty_contract_hitl_*`, `_emit_empty_contract_hitl`, `PlanDraftMissingOnLocalError`, `PlanDraftMissingOnLocalAndOriginError`, or `PopulateProducedEmptyContractError`.
- **cq-2** (PRMetadata schema): **Hard-remove** `context_branch`, `context_title`, `context_description`. Keep `context_pr_number`. Bump `schemaVersion` 1.1 → 1.2. Q5 confirmed no in-flight pipelines must remain compatible, so the clean break is safe.
- **cq-3** (SliceScheduler #2199 hooks): **Keep all five** (`record_cycle`, `teardown_slice`, `respawn_slice`, `cancel_cascade`, `hitl_escalator` param) with `# noqa: ARG002` / dead-code markers and a doc-pointer to #2199. No deletion.
- **cq-4** (PR-phase): **Delete the PR phase entirely.** Open the context PR (`egg/<id>/work → main`) up-front at the plan→implement boundary, hard-required and idempotent (one `gh pr list` + maybe one `gh pr create`). Apply uniformly to monolithic and sliced pipelines. Delete `_should_skip_pr_phase_auto_pr` and its caller, delete the PR-phase route/runner registration, shrink `_maybe_open_base_pr_for_plan_to_implement` to a single non-soft-fail call at the implement-start hook.
- **cq-5** (legacy `ConsensusEvaluator`): **Delete `orchestrator/consensus.py` entirely** + remove the `evaluator.clear(pipeline_id)` call at `pipelines.py:3279`.
- **cq-6** (umbrella terminology): **Subsume #2389.** Structurally delete the umbrella treatment in `create_slice_pr` and the `umbrella_has_program_block` branch in the slice-PR builder; the rename becomes a delete-not-rename for those sites.
- **cq-7** (`_sync_worktree_with_remote` divergence): **OUT OF SCOPE.** No work on this primitive.
- **cq-8** (`create_slice_pr` idempotency): Add `gh pr list --head <slice_branch> --base <parent> --state open` pre-flight in `create_slice_pr`; on hit, return the existing PR number without calling `gh pr create`.
- **cq-9** (`parent_branch_at_creation`): **Both** — eager-persist at PENDING→IN_PROGRESS transition AND keep a merge-base fallback in the new slice-base resolver.
- **cq-10** (`_run_implement_phase_slices` decomposition): **Surgical** — extract `_is_slice_dag_mode` helper (dedupes 3 sites) and a new slice-base resolver helper (forced by the context-PR collapse). Leave the rest of the 900-line function to #2261.
- **feedback-1 Q1**: Bundle in **#2570** (silent rebase of work onto main — slice-1) and **#2409** (per-slice consensus tracker reconstruction — slice-2). EXCLUDE #2627 (#2792-coupled).
- **feedback-1 Q2** (BLE001 handlers, 20 sites): **Audit individually and replace with specific exception types where the failure mode is knowable**; leave the rest with deliberate comments explaining what each catches and why. No blanket `SliceLoopRecoverableError`.
- **feedback-1 Q3** (ImportError dual-path shims, 9 sites): **Collapse to canonical `from orchestrator.X import Y` imports.** Verify by running the full suite after collapse.
- **feedback-1 Q4** (acceptance bar for the trigger bug): **Integration test required** — exercise slice-DAG pipeline → context PR opens up-front → hard-required → idempotent path. Plus unit tests for the `gh pr list` pre-flight and the hard-required failure semantics.

### Why two slices, not three (the cq-1 axis — separate from iteration-1's slice-1 sub-slicing question)

This section addresses the original **cq-1 decomposition axis** —
i.e. parallel A/B/C/D vs. sequential — which the operator answered
in iteration 0. **It does NOT speak to iteration 1's distinct
question of whether slice-1 (A+D) should be further sub-sliced
sequentially into 1a/1b/1c.** That distinct question is owned by
the architect per #2809 and surfaced above as task_planner NACK
pressure on slice-size.

The cq-1 HITL chose `[A+D] → [C]` rather than parallelising
A/B/C/D, because:

- A's deletions and D's dead-code purge overlap textually (most D sites live inside or adjacent to the context-PR scaffold A deletes). Parallelising would create textual merge conflicts on `pipelines.py` / `gateway_client.py`.
- C's restart hardening references the new slice-base resolver that A creates (cq-10 surgical extraction). Sequencing A first means C consumes a stable primitive instead of a moving target.
- #2792 is out of scope, so there is no third slice B to parallelise alongside.

The operator's iteration-1 directive is **orthogonal to cq-1**: it
preserves the `[A+D] → [C]` dependency direction and the
out-of-scope status of #2792, and asks only that the A+D work be
sub-sliced sequentially *within* slice-1 (so the dependency edges
inside slice-1 become explicit PR boundaries instead of intra-PR
ordering). The architect's iteration-1 response declined that
sub-slicing for textual-conflict reasons. **The plan below follows
the architect's response.**

## Primitives

Every primitive cited below was grep-verified against `HEAD` (`1cb235871`) before this plan was written. The `(NEW — task TASK-X-Y)` marker denotes primitives that this plan introduces; the named task creates them and downstream tasks order after it.

### Existing primitives (verbatim file:line — orchestrator-only, host-side Python in the orchestrator pod)

| Primitive | file:line | Scope |
|-----------|-----------|-------|
| `_run_implement_phase_slices` | `orchestrator/routes/pipelines.py:15013` | orchestrator-only |
| `_run_one_slice_inner` (nested closure) | `orchestrator/routes/pipelines.py:15364` | orchestrator-only |
| `_open_context_pr_for_pipeline` | `orchestrator/routes/pipelines.py:10002` | orchestrator-only (delete in task-1-2) |
| `_lookup_existing_context_pr` | `orchestrator/routes/pipelines.py:9735` | orchestrator-only (delete in task-1-2) |
| `_gather_context_pr_files` | `orchestrator/routes/pipelines.py:9896` | orchestrator-only (delete in task-1-2) |
| `_persist_context_pr_linkage_on_contract` | `orchestrator/routes/pipelines.py:9791` (writes `contract.pr.context_branch` at `pipelines.py:9839`) | orchestrator-only (delete in task-1-2) |
| `_maybe_open_base_pr_for_plan_to_implement` | `orchestrator/routes/pipelines.py:10648` | orchestrator-only (rewrite/inline in task-1-1) |
| `_maybe_open_base_pr_for_plan_to_implement` call sites | `pipelines.py:15120`, `pipelines.py:20572`, `pipelines.py:22051`, `pipelines.py:22994`, `orchestrator/routes/phases.py:500` | orchestrator-only (collapse to one in task-1-1) |
| `_resolve_slice_1_context_branch_from_contract` | `orchestrator/routes/pipelines.py:10883` | orchestrator-only (delete in task-1-2, replaced by helper from task-1-13) |
| `_should_skip_pr_phase_auto_pr` | `orchestrator/routes/pipelines.py:8222`; sole caller `pipelines.py:20844` | orchestrator-only (delete in task-1-3) |
| `_context_pr_events_emitted` dedup set + lock | `pipelines.py:10644`, `pipelines.py:10645`; touch sites `pipelines.py:1850, 1851, 10801, 10802` | orchestrator-only (delete in task-1-2) |
| `_rebase_pipeline_branch_onto_base` | `orchestrator/routes/pipelines.py:6833`; sole caller `pipelines.py:19873` | orchestrator-only (audit/replace in task-1-9 for #2570) |
| `restart_phase` route fn | `orchestrator/routes/pipelines.py:2968`; consensus-clear block `pipelines.py:3250–3287` (legacy `evaluator.clear()` at `pipelines.py:3279`) | orchestrator-only (extend in task-2-1) |
| `restart_agent` route fn | `orchestrator/routes/pipelines.py:2255` | orchestrator-only (read-only reference for symmetry) |
| `_run_implement_phase_slices` ImportError shims | `pipelines.py:15045, 15050, 15147, 15154, 15161, 15875, 16026, 16034, 16209` (9 sites) | orchestrator-only (collapse in task-1-12) |
| `# noqa: BLE001` handlers under slice loop | `pipelines.py:15131, 15196, 15274, 15336, 15386, 15422, 15451, 15471, 15501, 15709, 15742, 15775, 15795, 15841, 15901, 15910, 15946, 15964, 16080, 16105` (20 sites) | orchestrator-only (audit in task-1-11) |
| Bare `slice_count > 1` recompute sites | `pipelines.py:8259` (under `_should_skip_pr_phase_auto_pr`), `pipelines.py:15060`, `pipelines.py:15519` | orchestrator-only (refactor to helper in task-1-13) |
| `umbrella_has_program_block` assignment & read | assign `pipelines.py:15615`; read `pipelines.py:15620` | orchestrator-only (delete in task-1-7) |

### Existing primitives (gateway service, separate pod)

| Primitive | file:line | Scope |
|-----------|-----------|-------|
| `GatewayClient.create_context_branch` | `orchestrator/gateway_client.py:2327` | gateway-client (delete in task-1-4) |
| `ContextBranchDiverged` | `orchestrator/gateway_client.py:3453` | gateway-client (delete in task-1-4) |
| `GatewayClient.create_pr` | `orchestrator/gateway_client.py` (existing) | gateway-client (re-used by task-1-1) |
| `GatewayClient.create_slice_pr` | `orchestrator/gateway_client.py:1491` (~400 lines) | gateway-client (idempotency pre-flight in task-1-8; umbrella strip in task-1-7) |
| `is_slice_branch_merged_into_parent` | `orchestrator/gateway_client.py:1988` | gateway-client (read-only) |
| Umbrella sites in `create_slice_pr` | `gateway_client.py:299, 1523, 1539, 1542, 1550, 1569, 1600, 1611, 1615, 1624, 1629, 1670, 1692` | gateway-client (delete/rename in task-1-7) |
| `_CONTEXT_BRANCH_RE` | `gateway/gateway.py:1112` | gateway service (delete in task-1-4) |
| Push-block enforcement | `gateway/gateway.py:1350, 1362` | gateway service (verify `egg/<id>/work` already on session push-allow list in task-1-4) |
| `_SLICE_INTEGRATION_BRANCH_RE` | `gateway/gateway.py:1103` | gateway service (read-only, kept) |

### Existing primitives (schema / config)

| Primitive | file:line | Scope |
|-----------|-----------|-------|
| `PRMetadata` | `shared/egg_contracts/models.py:467` (fields `context_branch`, `context_title`, `context_description`, `context_pr_number`, `deferred_actions` at lines 499–531) | shared schema (mutate in task-1-5) |
| Schema version | `shared/egg_contracts/models.py:763` (`default="1.1"`) | shared schema (bump to 1.2 in task-1-5) |
| `SliceStatus` enum | `shared/egg_contracts/models.py:41` (members lines 52–55, alias `PhaseStatus = SliceStatus` at line 59) | shared schema (read-only) |
| `DEFAULT_SLICE_LOCAL_MAX_CYCLES = 3` | `orchestrator/env_config.py:271` | config (kept; markers in task-1-10 reference) |
| `DEFAULT_SLICE_GLOBAL_MAX_CYCLES = 10` | `orchestrator/env_config.py:272` | config (kept) |

### Existing primitives (slice scheduler / consensus / reconciler)

| Primitive | file:line | Scope |
|-----------|-----------|-------|
| `SliceScheduler` class | `orchestrator/slice_scheduler.py:127` | orchestrator-only (instantiated at `pipelines.py:15090`) |
| `SliceScheduler.record_cycle` | `slice_scheduler.py:299` | orchestrator-only (mark dead in task-1-10) |
| `SliceScheduler.teardown_slice` | `slice_scheduler.py:417` | orchestrator-only (mark dead in task-1-10) |
| `SliceScheduler.respawn_slice` | `slice_scheduler.py:434` | orchestrator-only (mark dead in task-1-10) |
| `SliceScheduler.cancel_cascade` | `slice_scheduler.py:375` | orchestrator-only (mark dead in task-1-10) |
| `SliceScheduler.poll_cascades` | `slice_scheduler.py:380` (LIVE; called at `pipelines.py:15322, 15860`) | orchestrator-only (kept) |
| `SliceScheduler.__init__` `hitl_escalator` param | `slice_scheduler.py:153` | orchestrator-only (mark dead in task-1-10) |
| `ConsensusEvaluator` class + singleton | `orchestrator/consensus.py:38`, `get_consensus_evaluator()` `consensus.py:153` | orchestrator-only (DELETE module in task-1-6) |
| `PeerConsensusTracker` class | `orchestrator/peer_consensus.py:69` | orchestrator-only (read-only) |
| `PeerConsensusTracker._tracker_key` | `peer_consensus.py:1844` (returns `f"{pipeline_id}/{slice_id}"`; call sites `1872, 1890, 1899, 2011`) | orchestrator-only (consumed by task-2-1, task-2-5) |
| `startup_reconciliation` consensus block | `orchestrator/startup_reconciliation.py:312–376` (esp. `358–367`) | orchestrator-only (extend in task-2-5) |

### Existing primitives (tests)

| Primitive | file:line | Scope |
|-----------|-----------|-------|
| `test_terminal_slice_keeps_umbrella_rollup_and_uses_merge_gate_marker` | `orchestrator/tests/test_gateway_client.py:1493` (related asserts `1378, 1379, 1421, 1525`) | tester (rewrite in task-1-15) |

### Integration-test trust-boundary scope (#2594 §10)

The integration tests in TASK-1-16 and TASK-2-6 need to spawn a
sliced pipeline against the live local stack and assert the context
PR is opened up-front / per-slice consensus trackers reconstruct
across an orchestrator restart. The relevant pytest fixtures live on
`integration_tests/conftest.py` (kubectl-gated via `_kubectl_available`
at `integration_tests/conftest.py:158`; the `egg_stack` session
fixture skips at line 347 when kubectl is unavailable, see
`docs/guides/testing.md`). The available primitives are:

- `orchestrator_url` — pytest fixture at
  `integration_tests/conftest.py:357` (session-scoped, delegates to
  `egg_stack.orchestrator_url`).
- `egg_stack.gateway_url` — **attribute** on the `EggStack` dataclass
  (`integration_tests/conftest.py:78`); NOT a pytest fixture. Tests
  that need the gateway URL inject the `egg_stack` fixture and read
  `.gateway_url` off of it (see existing examples at
  `integration_tests/regression/conftest.py:429–432` and
  `integration_tests/regression/test_hitl_round_trip.py:120,128`).
- `lifecycle_secret` — pytest fixture at
  `integration_tests/conftest.py:362` (skips when
  `egg_stack.lifecycle_secret` is missing).

**Therefore the new integration tests MUST live under one of the
kubectl-gated integration-tests subdirectories.** The legacy
`integration_tests/local_pipeline/` directory was deleted on
2026-05-11 in commit `f7803637d1` ("test: delete deprecated
local_pipeline + squid tests"); **do not reference it.** The
surviving kubectl-gated tiers are:

- `integration_tests/regression/` — recovery, BRC, slice-restart,
  HITL HTTP round-trip, and message-bus regression tiers. Has its
  own conftest that reuses the parent fixtures (see
  `integration_tests/regression/conftest.py`). The slice-restart
  branch-invariant tests already live here
  (`test_slice_restart_branch_invariants.py`), so the new context-PR
  up-front and restart-hardening tests are natural fits.
- `integration_tests/sdlc/` — end-to-end SDLC pipeline tests
  (`test_happy_path.py`, `test_role_enforcement.py`,
  `test_hitl_flow.py`, etc.). TASK-1-16a rewrites two tests here.
- `integration_tests/epic_pipeline/` — Jira epic SDLC pipeline tests.
  Not in scope for this plan.

TASK-1-16 places its new test under `integration_tests/regression/`
(slice-DAG-context-PR-up-front is a regression tier — preventing
recurrence of #2769 / #2593 / #2744). TASK-2-6 places its new test
under `integration_tests/regression/` for the same reason
(orchestrator-pod recycle is a recovery/regression test).

### NEW primitives (created by this plan)

| Primitive | Creator task | Description |
|-----------|--------------|-------------|
| `_open_context_pr_at_implement_start(pipeline_id)` | `(NEW — task TASK-1-1)` | Hard-required, idempotent up-front context-PR opener. One `gh pr list --head egg/<id>/work --base main --state open`; if hit, returns the PR number; if not, calls `GatewayClient.create_pr` to open `egg/<id>/work → main` and persists `context_pr_number` to `contract.pr`. Raises on failure (no soft-fail return). |
| `_is_slice_dag_mode(contract) -> bool` | `(NEW — task TASK-1-13)` | Dedupes the 3 bare `slice_count > 1` sites (`pipelines.py:8259, 15060, 15519`). |
| `_resolve_slice_base_branch(contract, slice_id)` | `(NEW — task TASK-1-13)` | Replacement for the deleted `_resolve_slice_1_context_branch_from_contract`. Returns the slice's parent branch — `egg/<id>/work` for root slices, the parent slice's integration branch otherwise. Reads `parent_branch_at_creation` from the contract slice record. |
| Merge-base fallback inside `_resolve_slice_base_branch` | `(NEW — task TASK-2-3)` | When `parent_branch_at_creation` is empty (legacy / orphaned slices), derive parent from `git merge-base` against origin. Closes the drift window cq-9 references. |

## Slice DAG

```
slice-1 (root, parent = main)
   │  Context-PR collapse + cleanup + PR-phase removal
   │  Subsumes #2389, #2570
   ▼
slice-2 (parent = slice-1)
   Slice/phase restart hardening
   Closes #2409
```

Forest constraint satisfied: each slice has exactly one parent. No `serialized_chain_order` needed.

## Test strategy

### Automated coverage (per slice)

- **slice-1**:
  - **Unit** (`orchestrator/tests/`): exercise `_open_context_pr_at_implement_start` happy path (no existing PR, opens one + persists `context_pr_number`), idempotent path (PR exists, no `gh pr create` call), and hard-required path (gateway failure raises, no swallowed `return None`). Mock `GatewayClient.create_pr` and `gh pr list`. Update `test_gateway_client.py:1378–1525` to drop the umbrella terminal-banner asserts and instead assert that the slice PR body no longer contains the `"Program-level umbrella PR"` literal. Add unit tests for the `create_slice_pr` idempotency pre-flight. Add unit tests for `_is_slice_dag_mode`. Add a regression test that asserts `_rebase_pipeline_branch_onto_base` no longer silently rebases `egg/<id>/work` onto `main` (covers #2570).
  - **Integration** (`integration_tests/regression/`): end-to-end test that spawns a 2-slice DAG pipeline, advances to the plan→implement boundary, asserts a single PR exists with `head=egg/<id>/work base=main`, then deliberately deletes the contract's `context_pr_number` and re-triggers the implement-start hook, asserting the idempotent path finds the existing PR without opening a duplicate. (The legacy `integration_tests/local_pipeline/` directory was deleted on 2026-05-11 in commit `f7803637d1`; `regression/` is the kubectl-gated recovery/regression tier.)
- **slice-2**:
  - **Unit**: tests for `restart_phase`'s per-slice consensus tracker iteration (asserts `tracker.clear()` is called once per slice key); tests for eager-persist of `parent_branch_at_creation` (asserts the field is written during PENDING→IN_PROGRESS, not after `create_slice_integration_branch`); tests for the merge-base fallback in `_resolve_slice_base_branch` (asserts a slice with empty `parent_branch_at_creation` but pushed commits resolves correctly); tests for the extended bootstrap reconciliation that resumes non-COMPLETE slices without re-spawning.
  - **Integration** (`integration_tests/regression/`): restart a sliced pipeline mid-phase; assert per-slice consensus trackers reconstruct correctly across the orchestrator-pod recycle (closes #2409).

### Manual verification

- After slice-1 lands: run a small sliced pipeline through to the implement phase boundary and confirm the context PR is opened automatically with no operator action. Confirm the PR phase no longer appears in pipeline status (it's been deleted).
- After slice-2 lands: deliberately kill the orchestrator pod mid-implement-phase on a sliced pipeline; restart; confirm slice resumes without re-spawning agents, and per-slice consensus trackers report the prior state.

## Manual steps

**Pre-merge (slice-1)**:
- Confirm there are NO in-flight slice-DAG pipelines in RUNNING state at deploy time. Feedback Q5 explicitly stated none exist; re-confirm at merge time. The schema bump (PRMetadata v1.1 → v1.2) is a clean break — in-flight contracts on disk will fail to load until repaired.
- Verify the gateway's pipeline-session push-allow list already includes `egg/<id>/work` (it does — the work branch is the canonical pipeline tip), so removing `_CONTEXT_BRANCH_RE` does not leave a hole.

**Pre-merge (slice-2)**: None.

**Post-merge (slice-1)**: Close #2389 (umbrella terminology rename) with a reference to slice-1. Close #2570 (work branch rebased onto main) with a reference to slice-1.

**Post-merge (slice-2)**: Close #2409 (per-slice consensus tracker reconstruction) with a reference to slice-2.

---

```yaml
# yaml-tasks
pr:
  title: "Cleanup: collapse context-PR onto egg/<id>/work + harden slice restart"
  description: |
    Issue #2777 — clean up the sliced implementation phase of the SDLC pipeline.

    The sliced implement path (`_run_implement_phase_slices` /
    `_run_one_slice_inner` plus the context-PR machinery in
    `gateway_client.py` and `gateway.py`) has accreted significant
    complexity across #2137, #2548, #2593, and #2744. A separate
    `egg/<id>/context` branch was introduced as a parallel stack root,
    and every downstream piece of complexity exists only to service that
    separate branch: temp-worktree materialisation, two-tier idempotency,
    `ContextBranchDiverged` handling, a soft-fail wrapper called from
    five sites, an observability-dedup set, and a gateway push-exemption
    regex. Each prior recurrence of the "context PR not opened" bug
    (#2593 → #2744 → #2769) added another call site to the scaffold
    instead of removing the fragility. The PR phase is also a no-op in
    slice-DAG mode (`_should_skip_pr_phase_auto_pr` returns `True`
    wholesale), so there is no backstop when the context PR is silently
    missed.

    This stack realigns the topology and trims the accumulated mess in
    two stacked PRs:

    1. **Slice 1 — Context-PR collapse + cleanup + PR-phase removal.**
       Replaces the entire `egg/<id>/context` scaffold with a single
       idempotent up-front opener at the plan→implement boundary that
       opens `egg/<id>/work → main` (one `gh pr list` + maybe one
       `gh pr create`). Deletes `_open_context_pr_for_pipeline` and its
       21 silent `return None` paths, `_lookup_existing_context_pr`,
       `_gather_context_pr_files`, `_persist_context_pr_linkage_on_contract`,
       `_maybe_open_base_pr_for_plan_to_implement` and its five call
       sites, `_resolve_slice_1_context_branch_from_contract`, the
       `_context_pr_events_emitted` dedup set, the
       `create_context_branch` gateway-client method,
       `ContextBranchDiverged`, and the `_CONTEXT_BRANCH_RE` gateway
       push-exemption. Deletes the PR phase entirely
       (`_should_skip_pr_phase_auto_pr` + caller + route registration).
       Removes the `context_branch`, `context_title`, and
       `context_description` fields from `PRMetadata` (schema v1.1 →
       1.2; `context_pr_number` is kept). Deletes the legacy
       `orchestrator/consensus.py` `ConsensusEvaluator` module. Drops
       the "umbrella" terminology in `create_slice_pr` and the
       slice-PR builder (subsumes #2389). Adds a `gh pr list`
       idempotency pre-flight to `create_slice_pr` so a transient `gh`
       failure no longer cascades the slice to FAILED. Diagnoses and
       stops the silent rebase of `egg/<id>/work` onto `main` (#2570).
       Adds `# noqa: ARG002` / dead-code markers to the five
       `SliceScheduler` #2199 hooks (kept for the planned per-slice
       MCP controls). Audits the 20 BLE001 swallow-all handlers and
       replaces each with the specific exception type where the
       failure mode is knowable. Collapses the 9 dual-path
       `except ImportError` shims to canonical
       `from orchestrator.X import Y` imports. Extracts two helpers:
       `_is_slice_dag_mode` (dedupes 3 sites) and
       `_resolve_slice_base_branch` (replaces the deleted slice-1
       context-branch resolver).

    2. **Slice 2 — Slice/phase restart hardening.** Makes
       `restart_phase` slice-aware (iterates per-slice consensus
       trackers when clearing, mirroring `restart_agent`'s
       slice-awareness). Eager-persists `parent_branch_at_creation` at
       the PENDING→IN_PROGRESS transition, before
       `create_slice_integration_branch` runs, so a crash mid-branch-
       creation cannot leave the field empty. Adds a merge-base
       fallback in `_resolve_slice_base_branch` for legacy/orphaned
       slices whose `parent_branch_at_creation` was never written.
       Extends bootstrap reconciliation to handle slices in
       `IN_PROGRESS` / `BLOCKED` status that did real work (commits
       pushed, consensus not reached), so they resume cleanly instead
       of re-spawning agents from scratch. Adds per-slice consensus
       tracker reconstruction to `startup_reconciliation` (closes
       #2409).

    **Impact**: idempotent-by-construction context PR removes the
    recurring "context PR not opened" failure class (#2593, #2744,
    #2769). Pipelines surviving an orchestrator-pod recycle resume
    instead of re-spawning. The schema bump and PR-phase deletion are
    breaking changes for the in-flight pipelines; per feedback Q5 none
    exist, so the clean break is safe. Net deletions estimated at
    ~600 lines against ~200 added (new opener, new helpers,
    BLE001 audit replacements, tests).
  test_plan: |
    - Automated (slice-1):
      - Unit tests under `orchestrator/tests/` for
        `_open_context_pr_at_implement_start` (happy / idempotent /
        hard-required paths), the `create_slice_pr` idempotency
        pre-flight, `_is_slice_dag_mode`, the `_rebase_pipeline_branch_onto_base`
        behaviour change (#2570 regression), the PRMetadata schema
        cleanup, the `consensus.py` deletion (no dangling imports),
        the umbrella-deletion path in `create_slice_pr`, and the
        per-site BLE001 replacements.
      - Integration test under `integration_tests/regression/`
        (the kubectl-gated recovery/regression tier; the legacy
        `integration_tests/local_pipeline/` directory was deleted
        on 2026-05-11 in commit `f7803637d1`) that spawns a 2-slice
        DAG pipeline, asserts the context PR opens automatically at
        the plan→implement boundary with `head=egg/<id>/work
        base=main`, then re-triggers the implement-start hook and
        asserts no duplicate PR is opened.
    - Automated (slice-2):
      - Unit tests for slice-aware `restart_phase` (per-slice tracker
        clear), eager-persist of `parent_branch_at_creation` (field
        present at PENDING→IN_PROGRESS), the merge-base fallback in
        `_resolve_slice_base_branch`, and the extended bootstrap
        reconciliation that resumes non-COMPLETE slices without
        re-spawning.
      - Integration test under `integration_tests/regression/`
        that kills the orchestrator pod mid-implement on a sliced
        pipeline, restarts, and asserts per-slice consensus trackers
        reconstruct (#2409 closure proof).
    - Manual:
      - After slice-1 merges: run a small sliced pipeline through to
        the implement phase boundary and confirm the context PR is
        opened automatically with no operator action; confirm the PR
        phase no longer appears in pipeline status.
      - After slice-2 merges: deliberately kill the orchestrator pod
        mid-implement-phase on a sliced pipeline; restart; confirm
        slice resumes without re-spawning agents, and per-slice
        consensus trackers report the prior state.
  manual_steps: |
    Pre-merge (slice-1):
      - Confirm there are NO in-flight slice-DAG pipelines in RUNNING
        state at deploy time (feedback Q5 confirmed none; re-confirm
        at merge). The PRMetadata schema bump (v1.1 → v1.2) is a
        clean break — in-flight contracts on disk will fail to load
        until repaired.
      - Verify the gateway's pipeline-session push-allow list already
        accepts pushes to `egg/<id>/work`; removing `_CONTEXT_BRANCH_RE`
        must not leave a hole.

    Pre-merge (slice-2): None.

    Post-merge (slice-1): Close #2389 with a reference to slice-1
    (subsumed). Close #2570 with a reference to slice-1 (subsumed).

    Post-merge (slice-2): Close #2409 with a reference to slice-2
    (subsumed).
slices:
  - id: 1
    name: |-
      Context-PR collapse + cleanup + PR-phase removal
    goal: |-
      Realign the slice-DAG PR topology (Goal 1) and execute the coupled
      cleanup pass (Goal 2). Specifically: collapse egg/<id>/context onto
      egg/<id>/work, delete the PR phase entirely (cq-4) and replace it with
      a single hard-required idempotent up-front opener at the
      plan->implement boundary, hard-remove the redundant PRMetadata fields
      with a v1.1->v1.2 schema bump (cq-2), delete orchestrator/consensus.py
      (cq-5), subsume #2389 umbrella terminology as a structural deletion
      (cq-6), add gh-pr-list idempotency to create_slice_pr (cq-8), keep the
      speculative #2199 SliceScheduler hooks with noqa+docs (cq-3), bundle
      #2570 (silent work->main rebase) by diagnosing and stopping the
      rebase, audit the BLE001 swallow-all handlers in the slice loop
      individually (feedback Q2), collapse the slice-loop except-ImportError
      shims to canonical orchestrator.X imports (feedback Q3), surgical
      _is_slice_dag_mode + slice-1-base-resolver extractions (cq-10), and
      ship the integration test for the trigger bug (feedback Q4). #2792
      remains OUT OF SCOPE per decision-11 (now independently resolved in
      merged PR #2797 -- still no work on the listed primitives here).
    parent_slice_id: null
    tasks:
      - id: TASK-1-1a
        role: coder
        description: |-
          Implement the AC-1a plan-phase pre-flight validator. The
          validator runs at plan-phase completion (before the
          implement-phase entry hook from TASK-1-1 fires) and
          rejects the plan with a plan-phase NACK if the planner
          output is missing the structural inputs the new
          idempotent context-PR opener depends on. Required
          rejections: (a) `yaml-tasks` block missing or unparseable;
          (b) `pr.title` missing or empty; (c) `pr.description`
          missing or empty; (d) `pr.test_plan` missing or empty;
          (e) `pr.manual_steps` missing (empty string is allowed).
          The validator lives in `shared/egg_contracts/plan_parser.py`
          (or `orchestrator/routes/phases.py` if the planner parser
          is invoked through the phase router; locate by searching
          for `# yaml-tasks` parsing). Raise a typed
          `PlanPreflightError(BaseException)` with a structured
          payload naming the missing field(s) so the BRC NACK
          surface emits a clear actionable message. Unit test in
          TASK-1-15: feed three malformed plan drafts (missing
          yaml-tasks; missing `pr:`; missing `pr.test_plan`) and
          assert the validator raises with the expected field name.
          Ordering: this validator MUST be in place BEFORE TASK-1-1's
          runtime opener — the opener depends on a well-formed
          contract — so prefer to land this first within the slice.
        acceptance: |-
          - A pre-flight validator exists at plan-phase completion
            and rejects malformed planner output with a typed
            `PlanPreflightError`.
          - The five rejection cases (a)–(e) are each exercised
            by a unit test in TASK-1-15.
          - The NACK message names the missing field by name (not
            a generic "plan invalid").
        files:
          - shared/egg_contracts/plan_parser.py
          - orchestrator/routes/phases.py
      - id: TASK-1-1
        role: coder
        description: |-
          Add a new module-level helper
          `_open_context_pr_at_implement_start(pipeline_id: str) -> int`
          in `orchestrator/routes/pipelines.py`. The helper is the
          single up-front context-PR opener for the plan→implement
          boundary. Behaviour: (1) call
          `gh pr list --head egg/<pipeline_id>/work --base main --state open --json number`
          via `GatewayClient.create_pr`'s existing `gh` plumbing
          (extract a `_gh_pr_list_for_head_base` helper if needed);
          (2) on hit, persist `pr_number` to `contract.pr.context_pr_number`
          and return it; (3) on miss, call `GatewayClient.create_pr`
          with title/description from `contract.pr.title` and
          `contract.pr.description` (existing fields), persist
          `context_pr_number`, return; (4) on gateway failure, raise
          `ContextPrCreationError` (new typed exception, top-level in
          `pipelines.py`) — NO soft-fail `return None`.

          **Persistence call site (added per reviewer_plan v2
          blocker 5)**. After TASK-1-2 deletes
          `_persist_context_pr_linkage_on_contract` (currently at
          `pipelines.py:9791` plan-anchor / `:10423` HEAD), the new
          opener becomes the SOLE writer of `context_pr_number`. To
          avoid making the opener a non-transactional state mutator,
          extract a private helper
          `_persist_context_pr_number(pipeline_id: str, pr_number: int) -> None`
          that wraps the contract write through the existing
          per-pipeline state-lock + `update_contract` machinery in
          `pipelines.py` (locate the existing pattern via
          `grep -n "update_contract\|_update_contract\|with _pipeline_state_lock" orchestrator/routes/pipelines.py | head -20`).
          The opener calls `_persist_context_pr_number(...)` once,
          immediately after either the `gh pr list` hit or the
          successful `gh pr create`. The helper is single-purpose
          (no other consumers); ordering with TASK-1-2 deletion is
          critical — TASK-1-2 depends on TASK-1-1 having extracted
          the helper before tearing down the old persistence path.

          Wire the opener into the single plan→implement transition
          site: replace the existing
          `_maybe_open_base_pr_for_plan_to_implement` call at
          `phases.py:500` (the only call site that survives) and
          delete the other four call sites (`pipelines.py:15120,
          20572, 22051, 22994`) — those existed only because the
          soft-fail wrapper needed multiple retry points. Document
          the new helper with a docstring stating "hard-required;
          raises on failure; idempotent via gh pr list pre-flight".
        acceptance: |-
          - `_open_context_pr_at_implement_start` exists in
            `pipelines.py`, raises `ContextPrCreationError` on
            gateway failure, no `return None` swallow path.
          - `_persist_context_pr_number` exists as a private helper
            in `pipelines.py`, wraps `update_contract` (or the
            equivalent under the per-pipeline state-lock pattern
            named by `grep -n "update_contract" pipelines.py`), and
            is called exactly once by `_open_context_pr_at_implement_start`
            (after either the `gh pr list` hit or the successful
            `gh pr create`).
          - The function is called exactly once per plan→implement
            transition (via `phases.py:500` advance_phase).
          - The four soft-fail call sites at `pipelines.py:15120,
            20572, 22051, 22994` are removed.
          - The function uses `contract.pr.title` and
            `contract.pr.description` (NOT `context_title` /
            `context_description`, which are removed in TASK-1-5).
          - Idempotency verified by unit test in TASK-1-15: when
            `gh pr list` returns an existing PR, no `gh pr create`
            is invoked AND `_persist_context_pr_number` IS still
            called with the existing PR number (the persistence
            write must be observed even on the idempotent path —
            covers the resume-from-orphaned-pipeline case where the
            contract on disk lost `context_pr_number` mid-run).
          - A unit test (in TASK-1-15) verifies that a gateway
            failure surfaces as `ContextPrCreationError` and is NOT
            silently swallowed by the implement-phase entry handler
            in `phases.py` (i.e. the error propagates to the BRC
            surface, not into a `return None` path).
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/routes/phases.py
      - id: TASK-1-2
        role: coder
        description: |-
          Delete the entire `egg/<id>/context` parallel-stack-root
          scaffold in `orchestrator/routes/pipelines.py`. Specifically
          delete: `_open_context_pr_for_pipeline` (line 10002, ~640
          lines including the 21 silent `return None` paths);
          `_lookup_existing_context_pr` (line 9735, ~150 lines);
          `_gather_context_pr_files` (line 9896);
          `_persist_context_pr_linkage_on_contract` (line 9791);
          `_maybe_open_base_pr_for_plan_to_implement` (line 10648,
          ~230 lines — note TASK-1-1 has replaced its single
          surviving call site already, so this is a pure deletion);
          `_resolve_slice_1_context_branch_from_contract` (line
          10883); the `_context_pr_events_emitted` dict and lock at
          lines 10644–10645 and their touch sites at 1850, 1851,
          10801, 10802; the `context_pr.skipped` and
          `context_pr.failed` event-bus entries at lines 291–292 and
          1036–1037 (and `EventType.CONTEXT_PR_SKIPPED` /
          `CONTEXT_PR_FAILED` if they exist in
          `orchestrator/events.py`).

          **Surviving `context_branch` / `context_title` /
          `context_description` read sites in `pipelines.py` outside
          the deleted function bodies (added per reviewer_plan v2
          blocker 2)** — these are NOT inside the function deletions
          above and MUST be removed in this task or routed through
          the new helpers from TASK-1-13:

          - `pipelines.py:10801, 10804, 10844` — `context_branch`
            reads (plan-anchor lines). Re-anchor against HEAD; if
            inside a now-deleted function, drop with the function;
            if standalone, replace with the resolved parent branch
            via `_resolve_slice_base_branch` from TASK-1-13.
          - `pipelines.py:11096-11097` — `context_title` /
            `context_description` reads in slice-PR builder. After
            cq-2 these no longer exist; replace with reads of
            `contract.pr.title` and `contract.pr.description`
            (the canonical fields used by TASK-1-1).
          - `pipelines.py:11519-11542` — `context_branch` read in
            cascade-base sub-block. Reroute through
            `_resolve_slice_base_branch`.
          - `pipelines.py:16755, 16781` — `context_branch` reads
            outside any deleted function. Reroute through
            `_resolve_slice_base_branch` or drop if dead.
          - `pipelines.py:20193` — `context_branch` read at
            advance-phase boundary. Drop if covered by TASK-1-1's
            new opener path; otherwise reroute.

          The implement-phase coder MUST run the verification grep
          `rg 'context_branch\|context_title\|context_description'
          orchestrator/routes/pipelines.py` BEFORE editing to
          re-anchor each of the named lines against HEAD (per the
          global re-anchoring note in §Approach) and AGAIN after
          editing to confirm zero hits outside test scaffolding.

          Update slice-1 base resolution in
          `pipelines.py:15394–15405` to call the new
          `_resolve_slice_base_branch` from TASK-1-13 instead of
          `_resolve_slice_1_context_branch_from_contract`.

          Verify no other references to deleted symbols remain via
          `grep -rn '<symbol>' orchestrator/ shared/ gateway/ tests/ integration_tests/`
          — widened scope catches leaks into gateway code, gateway
          tests, and integration tests.

          Ordering note: this task `depends_on: [TASK-1-1,
          TASK-1-1a, TASK-1-5, TASK-1-5b, TASK-1-13]` (new opener,
          plan validator, schema cleanup, cascade rewire, and new
          slice-base resolver must all exist first; TASK-1-5 +
          TASK-1-5b clear the structural consumer ahead of this
          deletion).
        acceptance: |-
          - The seven functions listed above are removed from
            `pipelines.py`.
          - The `_context_pr_events_emitted` dict, lock, and all four
            touch sites are removed.
          - The `context_pr.skipped` / `context_pr.failed` event-bus
            entries are removed (along with their `EventType` members
            if present).
          - Each of the seven enumerated surviving read sites
            (10801, 10804, 10844, 11096-11097, 11519-11542, 16755,
            16781, 20193) is either removed or rerouted through
            `_resolve_slice_base_branch` / `contract.pr.title /
            description`.
          - `grep -rn` for each deleted symbol across
            `orchestrator/ shared/ gateway/ tests/ integration_tests/`
            returns zero hits outside test files actively being
            rewritten by TASK-1-15 / TASK-1-15a / TASK-1-17.
          - The post-edit verification grep
            `rg 'context_branch|context_title|context_description'
            orchestrator/routes/pipelines.py` returns zero hits.
          - Slice-1 base resolution at `pipelines.py:15394–15405`
            now calls `_resolve_slice_base_branch` (from TASK-1-13).
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-3
        role: coder
        description: |-
          Delete the PR phase entirely (cq-4). Per the risk-analyst's
          R1 audit, the actual surface is ~9 sites — not just the
          four named by cq-4. Touch ALL of these in one task so the
          delete is atomic:

          (1) `_should_skip_pr_phase_auto_pr` def `pipelines.py:8222`
              and its sole caller `pipelines.py:20844`.
          (2) `_finalize_pr_phase_failed` at `pipelines.py:8280, 21024`
              (owns HEAD-recovery semantics post-PR-phase-failure;
              becomes obsolete).
          (3) Two further `PipelinePhase.PR` references in pipelines:
              `pipelines.py:18747` and `pipelines.py:19843`.
          (4) `_get_pr_url_from_pipeline` at `pipelines.py:4067-4075`
              reads from `phases['pr'].artifacts['pr_url']`. After
              deletion, the PR URL is read directly from
              `contract.pr.context_pr_number` (set by TASK-1-1) —
              update this helper or remove and inline.
          (5) `PipelinePhase.PR = 'pr'` enum value at
              `shared/egg_contracts/models.py:78` (StrEnum at
              line 62). Hard-remove per Q5 (no in-flight pipelines).
          (6) Phase-graph constants at
              `orchestrator/routes/phases.py:70-71`:
              `PipelinePhase.IMPLEMENT: [PipelinePhase.PR]` and
              `PipelinePhase.PR: []`. Change IMPLEMENT's downstream
              list to `[]` (terminal), drop the PR row entirely.
              Also fix the `'next_phase': 'pr'` response payload at
              `orchestrator/routes/phases.py:849`.
          (7) `mcp_tools.py:728` advance_phase MCP tool definition
              advertises 'pr' as a valid `target` value — remove.
              `mcp_tools.py:1409` reads `phases['pr'].artifacts` for
              the PR URL on completion — re-point at
              `contract.pr.context_pr_number`.
          (8) Overseer monitor: `_check_pr_phase_outcome` and the
              `pr_phase_no_pr` alert at
              `orchestrator/overseer/monitor.py:481, 1168-1180,
              1707-1741`. Delete the probe, the alert wiring, and
              the alert-type registration. After deletion there is
              no `pr` phase to check; an alert that would now never
              fire is dead code.
              **Plus `_check_post_consensus_stall` semantic rewire
              (added per reviewer_plan v2 blocker 6)**: at
              `orchestrator/overseer/monitor.py:1122-1160` the
              post-consensus-stall predicate short-circuits on the
              old "PR phase has run and recorded artifacts" signal
              via a `getattr` chain through `phases['pr'].artifacts`.
              That signal is a STRICT SUPERSET of the new
              "context PR exists" signal (`context_pr_number is not
              None`), because cq-4 moves the context PR to the
              plan→implement boundary — so `context_pr_number` is
              set throughout implement, not after a PR-phase
              boundary. A blanket find/replace would silently weaken
              the #1911 stall signal the predicate was wired to
              produce. **Required choice**: either (a) DELETE the
              short-circuit entirely if the underlying stall signal
              becomes unreachable post-PR-phase-deletion (verify via
              code-walk; document the proof in the commit message),
              OR (b) re-derive the equivalent predicate — e.g.
              "all slices closed AND context_pr_number is set AND no
              consensus events in last N seconds" — preserving the
              superset semantics. **Pick one explicitly in the
              commit message and state why**; silent acceptance of
              the weaker predicate is a regression on #1911.
          (9) `dag_visualizer.py:53, 61` — `PipelinePhase.PR` as a
              node in the visualizer's graph. Remove the node and
              the edge from IMPLEMENT to PR.
          (10) **Gateway-side PR-phase entries (lock-step with
               orchestrator)**: `gateway/phase_filter.py:526`
               (`PipelinePhase.PR: PhasePermissions(...)`) and
               `gateway/phase_filter.py:642`
               (`PR: PhaseFileRestriction(...)`); the PR-phase row
               in the state-machine transition table in
               `gateway/phase_transition.py`. Removing PipelinePhase.PR
               from orchestrator without lock-step gateway removal
               leaves the gateway state machine inconsistent — a
               v1.1 contract load post-deploy, or any test that
               invokes `advance_phase target='pr'`, surfaces the
               mismatch. Delete both sites in the same task so
               the deploy is atomic.
          (11) **`shared/egg_contracts/phase_defaults.py:105` row
               removal (added per reviewer_plan v2 blocker 3)**:
               the `PipelinePhase.PR: PhaseConfig(...)` row in the
               phase-defaults table. Removing `PipelinePhase.PR`
               from the StrEnum without removing this row produces
               a `KeyError` at startup when downstream consumers
               iterate the defaults dict. Verified at HEAD via
               `grep -n "PipelinePhase.PR\|'pr'" shared/egg_contracts/phase_defaults.py`.

          DO NOT touch `gateway_client.py:1441` where `create_pr`
          registers a temp gateway session with `phase='pr'`. That
          is the **gateway session-namespace** phase string used so
          the gateway accepts the `gh pr create` op; it is NOT the
          same as `PipelinePhase.PR`. **Note (added per
          reviewer_plan v2 blocker 3)**: the prior plan also listed
          `gateway_client.py:1409` and `:2567` in the carve-out.
          Verified at HEAD: `:1409` is in the same namesake region
          and remains preserved; `:2567` is unrelated — it's a
          `gh pr list` CLI args list entry (the literal `'pr',`
          argument to `gh`) and **MUST NOT be in the carve-out
          enumeration**. Drop `:2567` from the preserve list and
          re-anchor `:1409` against HEAD before editing (per the
          global re-anchoring note in §Approach).

          **Additional preserve targets** (added per reviewer_plan
          v2 non-blocking note): `gateway/tests/test_session_manager.py:1127, 1170`
          and `gateway/tests/test_gateway.py:4371` are namesake hits
          that assert the gateway-session namespace `phase='pr'`
          survives `PipelinePhase.PR` removal. They MUST NOT be
          deleted by this task's grep sweep.

          Verification artifact: run
          `rg 'PipelinePhase\\.PR|phases\\["pr"\\]|phase=.pr.|phase == .pr.' orchestrator/ shared/ gateway/`
          BEFORE the task starts AND AFTER all 11 site-categories
          are addressed; the after-grep must show only the
          gateway-session `phase='pr'` hits in `gateway_client.py`
          (`:1409`, `:1441`) and the namesake test hits in
          `gateway/tests/test_session_manager.py` and
          `gateway/tests/test_gateway.py` (the documented carve-out).
          **Commit BOTH the before-grep and the after-grep output
          verbatim in the commit message** so reviewer_plan can
          spot-check the delta without rerunning the audit.

          Tests are owned by TASK-1-3a (schema/phase_defaults
          tests), TASK-1-15a (gateway PR-phase tests),
          TASK-1-17 (orchestrator PR-phase tests), and TASK-1-18
          (docs).
        acceptance: |-
          - All 11 site-categories above are addressed (#10 covers
            the gateway-side phase_filter + phase_transition
            entries; #11 covers `shared/egg_contracts/phase_defaults.py:105`).
          - Verification grep
            `rg 'PipelinePhase\\.PR|phases\\["pr"\\]|phase=.pr.|phase == .pr.' orchestrator/ shared/ gateway/`
            returns ONLY the gateway-session `phase='pr'` hits in
            `gateway_client.py` (`:1409`, `:1441`) and the namesake
            test hits in `gateway/tests/test_session_manager.py:1127, 1170`
            and `gateway/tests/test_gateway.py:4371`. `:2567` is NOT
            in the carve-out (it's a `gh pr list` CLI args list,
            not the session namespace).
          - `PipelinePhase.PR` enum member removed from BOTH
            `shared/egg_contracts/models.py` AND
            `shared/egg_contracts/phase_defaults.py` AND
            `gateway/phase_filter.py` AND
            `gateway/phase_transition.py`.
          - Phase graph `IMPLEMENT: []` is terminal.
          - Overseer `_check_pr_phase_outcome` and its alert wiring
            are removed.
          - `_check_post_consensus_stall` semantic rewire is
            addressed per #8: either the short-circuit is deleted
            (with proof of unreachability in commit message) or the
            equivalent stall predicate is re-derived from
            `contract.pr.context_pr_number` + slice-closure +
            consensus-quiescence. The choice and rationale are
            stated in the commit message.
          - `dag_visualizer` no longer renders a PR node.
          - The advance_phase MCP definition no longer accepts
            `target='pr'`.
          - **Commit message contains BOTH the BEFORE and AFTER
            output of the verification grep**, verbatim.
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/routes/phases.py
          - orchestrator/mcp_tools.py
          - orchestrator/overseer/monitor.py
          - orchestrator/dag_visualizer.py
          - shared/egg_contracts/models.py
          - shared/egg_contracts/phase_defaults.py
          - gateway/phase_filter.py
          - gateway/phase_transition.py
      - id: TASK-1-3a
        role: tester
        description: |-
          Rewrite `tests/shared/egg_contracts/test_phase_defaults.py`
          to reflect the removal of `PipelinePhase.PR` from
          `shared/egg_contracts/phase_defaults.py:105` (and the
          `PipelinePhase` StrEnum at
          `shared/egg_contracts/models.py:62-78` per TASK-1-3 (5)).
          Specifically: drop any test that asserts PR is in the
          phase-defaults table; assert IMPLEMENT is the terminal
          phase with no downstream; add a default-deny coverage
          test that asserts a planner trying to default to phase
          'pr' is rejected. The test runs under `make test`.
        acceptance: |-
          - `tests/shared/egg_contracts/test_phase_defaults.py`
            passes with the PR-phase removed.
          - The test file no longer references `PipelinePhase.PR`.
          - A new default-deny test asserts that 'pr' is not an
            accepted phase string.
        files:
          - tests/shared/egg_contracts/test_phase_defaults.py
      - id: TASK-1-4
        role: coder
        description: |-
          Delete `GatewayClient.create_context_branch`
          (`orchestrator/gateway_client.py:2327`, ~90 lines) and
          `ContextBranchDiverged` (`gateway_client.py:3453`) — both
          are dead once TASK-1-2 removes the only callers. Delete
          `_CONTEXT_BRANCH_RE` from `gateway/gateway.py:1112` and
          remove the regex from the push-block enforcement at
          `gateway/gateway.py:1350` and `1362`. Before deletion,
          confirm via grep that the gateway's pipeline-session
          push-allow list already accepts `egg/<id>/work` pushes (it
          does — the work branch is the canonical pipeline tip
          tracked by the session). If a pipeline session does NOT
          already cover `egg/<id>/work` on slice-loop entry, surface
          an impasse instead of silently leaving a hole.

          **`is_context_push` cleanup (added per reviewer_plan v2
          non-blocking R7)**: `gateway/gateway.py:1344-1392`
          carries a dangling `is_context_push` variable that becomes
          unreachable once `_CONTEXT_BRANCH_RE` is removed (the
          regex was the only thing that ever flipped it to True).
          Locate the variable's full lifecycle via
          `grep -n "is_context_push" gateway/gateway.py` (currently
          ~5 hits: line 1344 narrative comment, line 1349
          `is_context_push = False` initializer, line 1363
          `is_context_push = bool(_CONTEXT_BRANCH_RE.match(branch))`
          assignment, line 1376 read inside conditional, line 1392
          `elif is_context_push:` branch). Two acceptable
          treatments: (a) hard-replace with `is_context_push = False`
          everywhere (preserves the narrative comment and the
          conditional structure, makes dead-codepath status
          obvious), OR (b) remove the variable entirely along with
          its narrative comment and downstream conditional branches
          (collapses dead branches and net-negative LOC). Pick (b)
          unless an audit reveals the variable is referenced by
          callers/audit-log emitters outside this file.
        acceptance: |-
          - `create_context_branch` and `ContextBranchDiverged`
            removed from `gateway_client.py`.
          - `_CONTEXT_BRANCH_RE` removed from `gateway/gateway.py`;
            both push-block call sites updated to no longer reference
            it.
          - `is_context_push` variable + narrative comment + the
            four downstream conditional references at
            `gateway/gateway.py:1344-1392` are either removed
            entirely (preferred) or hard-pinned to `False` with a
            comment explaining the residual is intentional
            scaffolding.
          - `grep -rn 'ContextBranchDiverged\|create_context_branch\|is_context_push'`
            returns zero hits outside test files (or only the
            hard-pinned-False if option (a) is chosen).
          - Gateway pipeline-session push-allow logic still permits
            `egg/<id>/work` pushes (manual verification step in
            commit message).
        files:
          - orchestrator/gateway_client.py
          - gateway/gateway.py
      - id: TASK-1-4b
        role: tester
        description: |-
          Rewrite `gateway/tests/test_pipeline_push_block.py` to
          reflect `_CONTEXT_BRANCH_RE` deletion (TASK-1-4). The
          existing context-branch allow-test class at lines
          994-1052 becomes obsolete because the exemption regex no
          longer exists. Delete the class. Add a replacement
          regression test that verifies `egg/<id>/context` pushes
          are now BLOCKED (the exemption is gone — the branch
          itself is gone — but a misbehaving caller might still
          try to push to it; assert the gateway rejects the push
          with a clear policy-violation error). Run under
          `make test` to confirm.
        acceptance: |-
          - Lines 994-1052 (the context-branch allow-test class)
            are deleted from
            `gateway/tests/test_pipeline_push_block.py`.
          - A replacement regression test asserts that a push to
            `egg/<id>/context` is rejected by the gateway with a
            policy-violation error message.
          - `make test` passes.
        files:
          - gateway/tests/test_pipeline_push_block.py
      - id: TASK-1-5
        role: coder
        description: |-
          PRMetadata schema cleanup (cq-2 — hard-remove). In
          `shared/egg_contracts/models.py`: delete the `context_branch`,
          `context_title`, and `context_description` fields from the
          `PRMetadata` class (currently at lines 499–531). KEEP
          `context_pr_number` (still used as the PR number of the
          `egg/<id>/work → main` PR). KEEP `deferred_actions`. Bump
          the schema version constant at line 763 from `"1.1"` to
          `"1.2"`.

          **Add a `_migrate_schema_version_to_1_2` migration entry**
          (per reviewer_plan v2 blocker 2): the operator's Q5
          confirms no in-flight pipelines, but on-disk fixtures
          (`.egg-state/contracts/issue-2777-replan.json`, `issue-2769.json`,
          `issue-2548.json`, `issue-2474.json`, `issue-1557-v2.json`)
          carry the three removed fields. The contract loader for
          THIS very pipeline will refuse the v1.2 load without
          migration. The migration entry must (a) drop the three
          fields when present on load, (b) preserve `context_pr_number`
          and `deferred_actions`, (c) leave fresh-v1.2 contracts
          untouched (no-op). The existing migration registry pattern
          is the precedent — locate it via `grep -rn
          "schemaVersion\|_migrate" shared/egg_contracts/`.

          Search for all read sites of the three deleted fields
          across the codebase
          (`grep -rn 'context_branch\|context_title\|context_description'`)
          and either delete them (if covered by TASK-1-2 or
          TASK-1-7) or note them for the new TASK-1-5b structural
          rewire (`stacked_pr_reconciler.py` cascade-base and the
          seven `pipelines.py` read sites enumerated in TASK-1-2's
          extended scope). Any read site that survives outside the
          deletion-task scope is a bug.
        acceptance: |-
          - The three fields are removed from `PRMetadata`.
          - `schemaVersion` default is `"1.2"`.
          - `_migrate_schema_version_to_1_2` exists and drops the
            three removed fields from on-disk v1.1 contracts on
            load (no-op for v1.2).
          - No surviving read site of any deleted field outside
            test files AND outside the new TASK-1-5b structural
            rewire scope (`stacked_pr_reconciler.py`).
          - The pipeline's own contract on disk (`.egg-state/contracts/issue-2777-replan.json`)
            loads successfully under the v1.2 schema via the
            migration entry.
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-5b
        role: coder
        description: |-
          **NEW — added per reviewer_plan v2 blocker 2** (cascade-base
          rewire from `context_branch` to `context_pr_number`).
          `orchestrator/stacked_pr_reconciler.py` is a STRUCTURAL
          consumer of the deleted `contract.pr.context_branch` field
          — at HEAD the references are at lines 94, 112, 120, 129,
          150, 157-158, 247, 275, 283 (verified via
          `grep -n "context_branch\|context_title\|context_description" orchestrator/stacked_pr_reconciler.py`).
          These are NOT covered by TASK-1-2's pipelines.py deletion
          scope nor TASK-1-7's umbrella deletion. The reconciler
          threads `context_branch` through the cascade-base fallback
          for orphaned slices — exactly the safety net cq-9 tries to
          preserve. After TASK-1-5 deletes the field, every read
          site here raises `AttributeError` at runtime.

          Rewire the cascade-base resolution onto the new
          `_resolve_slice_base_branch` helper from TASK-1-13 (which
          gains a merge-base fallback in TASK-2-3 for orphaned
          slices). For the specific case where the reconciler today
          falls back to `context_branch` for "PR shouldn't get here"
          paths (line 150 comment), the new path resolves through
          `_resolve_slice_base_branch` instead. Replace each read
          site with the appropriate helper call:

          - Default-argument `context_branch: str | None = None` →
            replace with `parent_branch: str | None = None`.
          - Docstring + comment references → update to describe the
            new cascade-base contract.
          - Argument-passing sites → switch to passing the resolved
            parent branch via `_resolve_slice_base_branch`.

          Add a unit test in TASK-1-15 that exercises the
          `stacked_pr_reconciler.py` cascade-base fallback with the
          new helper.

          Ordering: `depends_on: [TASK-1-13, TASK-1-5]` —
          `_resolve_slice_base_branch` from TASK-1-13 must exist,
          and TASK-1-5 must have removed the schema field so the
          rewire isn't redundant.
        acceptance: |-
          - `orchestrator/stacked_pr_reconciler.py` no longer reads
            `contract.pr.context_branch`.
          - The cascade-base resolution goes through
            `_resolve_slice_base_branch` (from TASK-1-13 / TASK-2-3).
          - `grep -n "context_branch" orchestrator/stacked_pr_reconciler.py`
            returns zero hits.
          - The orphaned-slice safety net (cq-9 intent) is preserved
            by routing through the merge-base fallback (TASK-2-3).
          - Unit test in TASK-1-15 covers the new cascade-base
            fallback path.
        files:
          - orchestrator/stacked_pr_reconciler.py
      - id: TASK-1-5a
        role: tester
        description: |-
          Update schema and doc-terminology tests for the
          PRMetadata field removal in TASK-1-5. Three changes:

          (1) `tests/shared/egg_contracts/test_pr_metadata.py:91-142`
              currently has ~18 asserts on the three deleted
              fields (`context_branch`, `context_title`,
              `context_description`). Delete those asserts; add
              one positive test that asserts `PRMetadata` no longer
              accepts those field names (Pydantic rejects with
              `extra='forbid'` validation error); add one positive
              test that asserts `context_pr_number` and
              `deferred_actions` still work as before.
          (2) `tests/docs/test_context_pr_doc_terminology.py:70-243`
              has doc-terminology asserts on the deleted field
              names (the test asserts docs mention the fields).
              Delete those asserts; add a replacement test that
              asserts docs DO mention `context_pr_number` (still
              live) but do NOT mention the three removed fields
              (regression test that docs were updated).
          (3) Any test in `tests/` or `orchestrator/tests/` that
              imports `context_branch` / `context_title` /
              `context_description` from `PRMetadata` — grep
              `tests/ orchestrator/tests/ integration_tests/`
              before completing to catch stragglers.

          Run `make test-all` to confirm a green suite.
        acceptance: |-
          - `test_pr_metadata.py:91-142` asserts on deleted
            fields are removed; positive tests for the field
            removal exist.
          - `test_context_pr_doc_terminology.py:70-243` doc
            asserts on deleted fields are removed; replacement
            doc-update regression test exists.
          - No surviving test imports the three deleted fields
            (verified by `grep -rn 'context_branch\|context_title\|context_description' tests/ orchestrator/tests/ integration_tests/`).
        files:
          - tests/shared/egg_contracts/test_pr_metadata.py
          - tests/docs/test_context_pr_doc_terminology.py
      - id: TASK-1-6
        role: coder
        description: |-
          Delete the legacy `ConsensusEvaluator` module (cq-5).
          reviewer_plan v1→v2 + risk_analyst v2→v3 verified the
          production call surface. There are EIGHT reference
          clusters total — all must be removed in this task.
          Citations below give BOTH the refine-anchor `1cb235871`
          line range AND the HEAD line range; per the global
          re-anchoring note in §Approach, the coder MUST re-anchor
          again at implement-time HEAD before editing.

          **In `orchestrator/routes/pipelines.py` (6 clusters —
          per architect AC-18 + risk_analyst v2 blocker 1, verified
          at HEAD via
          `grep -n "get_consensus_evaluator|from consensus import|from ..consensus" orchestrator/routes/pipelines.py`)**:
          (1) refine-anchor `1805-1808` / HEAD `1813-1816` — import
              + `.clear()` call (the early-cancel path).
          (2) refine-anchor `2844-2848` / HEAD `2859-2863` — import
              + `evaluator = get_consensus_evaluator()` handle.
          (3) refine-anchor `3274-3279` / HEAD `3289-3293` — import
              + handle + `.clear()` call (the `restart_phase`
              consensus-clear block named by the analysis).
          (4) **refine-anchor `~3486-3496` / HEAD `3516-3526`** —
              import + handle + `.clear()` call inside the
              "Failed to clear legacy consensus after hard-reset
              ack" block. **NEW — added per risk_analyst v2
              blocker 1**; this cluster was missed in v2 and is a
              distinct call site from (3) (the hard-reset-ack
              path vs the restart_phase consensus-clear path).
              Deletion of `consensus.py` without removing this
              cluster guarantees an `ImportError` post-restart on
              the hard-reset ack path. Verified at HEAD via
              `sed -n '3510,3530p' orchestrator/routes/pipelines.py`.
          (5) refine-anchor `4206-4210` / HEAD `4489-4493` —
              import + handle (nested path).
          (6) refine-anchor `4215-4219` / HEAD `4498-4502` —
              second import + handle in the same neighbourhood.

          **In `orchestrator/routes/phases.py` (1 cluster — added
          per reviewer_plan v2 blocker 1, verified at HEAD via
          `grep -n "consensus" orchestrator/routes/phases.py`)**:
          (7) `phases.py:119-124` — `try: from consensus import
              get_consensus_evaluator; except ImportError: from
              ..consensus import get_consensus_evaluator` +
              `get_consensus_evaluator().clear(pipeline_id)` call
              inside the `complete_phase` route's
              "Clear ephemeral message store and consensus state on
              phase transition" block. Deletion of `consensus.py`
              without removing this cluster guarantees an
              `ImportError` at startup the first time
              `complete_phase` is invoked.

          **In `orchestrator/routes/signals.py` (1 cluster — added
          per reviewer_plan v2 blocker 1, verified at HEAD via
          `grep -n "get_consensus_evaluator|ConsensusEvaluator|from consensus|from .consensus" orchestrator/routes/signals.py`)**:
          (8) `signals.py:847-871` — `try: from consensus import
              ReadinessState, get_consensus_evaluator; except
              ImportError: from ..consensus import ReadinessState,
              get_consensus_evaluator` + `evaluator =
              get_consensus_evaluator()` + `evaluator.evaluate(...)`
              call in the READY heartbeat handler. Deletion of
              `consensus.py` without removing this cluster
              guarantees an `ImportError` the first time a READY
              signal fires.

          Each cluster has a 3-line `try: from consensus import
          get_consensus_evaluator; except ImportError: from
          ..consensus import get_consensus_evaluator` shim plus
          the actual usage. Delete all eight clusters AND the
          `orchestrator/consensus.py` module
          (`ConsensusEvaluator` class at line 38,
          `get_consensus_evaluator()` singleton at line 153,
          `ReadinessState` enum). Verify via
          `grep -rn 'ConsensusEvaluator|get_consensus_evaluator|ReadinessState|from consensus|from .consensus|from orchestrator.consensus' orchestrator/ shared/ gateway/ tests/ integration_tests/`
          that no other module imports from it. The BRC
          `PeerConsensusTracker` (`orchestrator/peer_consensus.py:69`)
          is the only consensus path; nothing else needs to change.

          **Notes / follow-on cleanup (added per risk_analyst v2
          non-blocking)**: the `peer_consensus.py:1604` alias method
          is a compatibility shim on the surviving tracker (not the
          deleted module). It is NOT a runtime-breaker for this
          task's deletion, but verify whether it remains dead
          post-deletion via `grep -rn '<alias-method-name>'`. If
          dead, remove it lockstep with this task; if reachable
          (test scaffolding or future-#2199 hook), leave it and
          file a follow-up issue noting the residual coupling.

          Commit the after-grep output in the commit message for
          reviewer_plan to spot-check.
        acceptance: |-
          - `orchestrator/consensus.py` is deleted.
          - All EIGHT reference clusters are removed: 6 in
            `pipelines.py` (refine-anchored lines 1805-1808,
            2844-2848, 3274-3279, ~3486-3496, 4206-4210, 4215-4219;
            HEAD-anchored 1813-1816, 2859-2863, 3289-3293,
            3516-3526, 4489-4493, 4498-4502), 1 in `phases.py`
            (119-124), 1 in `signals.py` (847-871). Re-anchored
            against HEAD before editing.
          - The hard-reset-ack consensus-clear block at HEAD
            `3516-3526` (cluster 4) is explicitly removed —
            verified by the post-edit grep returning zero hits in
            that line range.
          - No surviving import of the deleted module (or its
            `ReadinessState` enum) across the repo (verified by the
            widened grep).
          - `peer_consensus.py:1604` alias method's
            reachability is verified post-deletion; if dead,
            removed lockstep; if reachable, noted in commit message
            with follow-up issue link.
          - Commit message contains the after-grep output.
        files:
          - orchestrator/consensus.py
          - orchestrator/routes/pipelines.py
          - orchestrator/routes/phases.py
          - orchestrator/routes/signals.py
          - orchestrator/peer_consensus.py
      - id: TASK-1-7
        role: coder
        description: |-
          Drop "umbrella" terminology (cq-6 subsumes #2389). In
          `orchestrator/gateway_client.py`, restructure
          `create_slice_pr` (starts at line 1491) to remove the
          umbrella treatment entirely: program-level content (test
          plan, manual steps, pre-merge obligations) is no longer
          inserted into terminal slices because it now lives on the
          `egg/<id>/work → main` context PR opened by TASK-1-1.
          Delete the umbrella sites at `gateway_client.py:299` (lazy-
          import comment), `1523, 1539, 1542, 1550, 1569, 1600, 1611,
          1615, 1624` (docstring + body comments), `1629` (the
          literal banner string `"> **Program-level umbrella PR —
          terminal slice of pipeline `{pipeline_id}`.**"`), and
          `1670, 1692` (obligation-on-umbrella error messages — the
          obligation now goes on the context PR; emit a normal error
          instead). In `orchestrator/routes/pipelines.py`, delete
          `umbrella_has_program_block` (assigned line 15615, read
          line 15620) and collapse the
          `is_terminal or not umbrella_has_program_block` condition
          to whatever remains. Delete narrative comments at
          `pipelines.py:9010, 9038, 9047, 15608, 15610, 15686, 15691`
          that reference "umbrella". Search-and-fix any remaining
          "umbrella" string in non-test code via
          `grep -rn 'umbrella' orchestrator/ gateway/ shared/`. Test
          updates are owned by TASK-1-15.
        acceptance: |-
          - `create_slice_pr` no longer emits the terminal-banner
            string.
          - `umbrella_has_program_block` and its condition removed.
          - `grep -rn 'umbrella' orchestrator/ gateway/ shared/`
            returns zero hits outside test files.
        files:
          - orchestrator/gateway_client.py
          - orchestrator/routes/pipelines.py
      - id: TASK-1-8
        role: coder
        description: |-
          Add idempotent `gh pr list` pre-flight to
          `GatewayClient.create_slice_pr`
          (`orchestrator/gateway_client.py:1491`, cq-8). Before the
          existing `gh pr create` call, run
          `gh pr list --head <slice_branch> --base <parent> --state open --json number`.
          On hit, return the existing PR number without invoking
          `gh pr create`. On miss, fall through to the existing
          create path. Extract a private
          `_lookup_open_pr(self, head: str, base: str) -> int | None`
          helper so the same idempotency primitive can also serve
          TASK-1-1 (the context-PR opener). Tests are owned by
          TASK-1-15.
        acceptance: |-
          - `_lookup_open_pr` exists as a private helper on
            `GatewayClient`.
          - `create_slice_pr` calls `_lookup_open_pr` before
            `gh pr create` and returns the existing PR number on
            hit.
          - A transient `gh pr create` failure that is retried after
            a partial success no longer cascades the slice to FAILED
            — verified by unit test in TASK-1-15.
        files:
          - orchestrator/gateway_client.py
      - id: TASK-1-9
        role: coder
        description: |-
          Diagnose and stop the silent rebase of `egg/<id>/work` onto
          `main` (#2570).

          **EXPECTATION (added per reviewer_plan v2 blocker 4 +
          risk_analyst R1)**: BOTH reviewers independently verified
          that the diagnosed root cause lies inside an OOS primitive
          — specifically the bare-rebase fallback inside
          `_sync_worktree_with_remote` at `pipelines.py:7219-7232`,
          which the code documents as the "#2222 contamination"
          vector. `_sync_worktree_with_remote` is OUT OF SCOPE per
          decision-11. **The AC-9a gate below WILL fire by
          construction on the first audit pass.** Do not treat this
          as a surprise discovery — the expected resolution path is
          AC-9a option 3 ("Mark #2570 as xfail in slice-1 and open
          a follow-up issue co-scheduled with the #2792 work"). The
          implement-phase coder should plan for this from the start.

          Audit procedure: read
          `_sync_worktree_with_remote` (`pipelines.py:6442`
          plan-anchor / re-anchor at HEAD via `grep -n "def _sync_worktree_with_remote" orchestrator/routes/pipelines.py`)
          AND `_rebase_pipeline_branch_onto_base` (`pipelines.py:6833`
          plan-anchor / `:7465` HEAD) AND its sole caller
          (`pipelines.py:19873` plan-anchor / `:21446` HEAD) AND any
          `egg-exec-…/work` worktree merge sites surfaced by the
          #2570 evidence section. Document the diagnosis in a written
          audit note (commit as a checkpoint artifact under
          `.egg-state/agent-outputs/issue-2777-replan-task-1-9-audit.md`),
          THEN trigger AC-9a.

          If — counterfactually — the diagnosis surfaces an in-scope
          root cause (not the `_sync_worktree_with_remote` vector),
          choose ONE of two fixes: (a) replace the force-push rebase
          with `git merge --ff-only origin/main` and abort if it
          can't fast-forward (then surface as HITL), preserving
          incoming SHAs and making the merge visible; OR (b) delete
          the auto-rebase call entirely if the audit shows it's no
          longer needed (the original #2098 scenario may no longer
          be reachable now that pipeline branches are short-lived).
          Document the chosen fix in the commit message with a
          paragraph explaining why the alternative was rejected. In
          this counterfactual path, AC-9a does not fire and the task
          ships a real code change. Tests are owned by TASK-1-17.

          **AC-9a — OOS-scope-escalation gate (HARD REQUIREMENT)**:
          Before modifying any site discovered by the diagnosis,
          check the function name against the
          `explicitly_out_of_scope.files_or_symbols` list in this
          plan's Primitives section (which mirrors the architect's
          OOS list). The OOS symbols are: `_sync_worktree_with_remote`
          (pipelines.py:6442), `_populate_contract_from_plan*`
          (pipelines.py:18408, 18535),
          `_empty_contract_hitl_*` (pipelines.py:18202, 18287),
          `_emit_empty_contract_hitl` (pipelines.py:14176),
          `PlanDraftMissingOnLocalError` (pipelines.py:17987),
          `PlanDraftMissingOnLocalAndOriginError` (pipelines.py:18000),
          `PopulateProducedEmptyContractError` (pipelines.py:18043).
          If the diagnosed root cause lies inside any OOS primitive
          (per the EXPECTATION above, this is the expected outcome),
          you MUST register an HITL via
          `mcp__sdlc__register_open_question` with three options
          before any code change: (1) "Extend scope to include the
          OOS primitive in slice-1" — operator overrides decision-11;
          (2) "Defer slice-1 until the #2792 work lands" — wait for
          the OOS-coupled work; (3) "Mark #2570 as xfail in
          slice-1 and open a follow-up issue for the OOS-coupled
          fix" — ship slice-1 without the #2570 fix. Silent
          modification of an OOS primitive is a NACK-blocking
          violation per the operator's decision-11 / cq-7 directive.
          The default-recommended HITL option (per the reviewers'
          R1) is option 3 — surface this in the registered HITL.
        acceptance: |-
          - An audit note exists at
            `.egg-state/agent-outputs/issue-2777-replan-task-1-9-audit.md`
            documenting which silent-rebase vectors were verified at
            HEAD and which root-cause hypothesis the audit reached.
          - EITHER (a) AC-9a fires with an HITL registered as
            expected, the HITL resolves to one of the three options,
            and the task is marked complete per the resolution
            (option 3 → xfail + follow-up issue link is the
            default expectation), OR (b) a code change ships against
            an in-scope root cause and the regression test in
            TASK-1-17 passes — including `git merge-base origin/main
            origin/egg/<id>/work` equalling the pipeline-creation
            SHA after **N≥3 phase transitions** with **M≥2 main PRs
            merged in parallel** (architect AC-9 NB#1 pinning).
          - Commit message names the audit outcome and the chosen
            resolution path with rationale.
          - **If diagnosis hits an OOS primitive (the expected
            outcome), an HITL decision is registered via
            `mcp__sdlc__register_open_question` BEFORE any code
            change** (AC-9a hard requirement).
          - If the HITL resolves to xfail / defer, the task is
            marked complete with a follow-up issue link instead of
            a code change.
        files:
          - orchestrator/routes/pipelines.py
          - gateway/gateway.py
          - .egg-state/agent-outputs/issue-2777-replan-task-1-9-audit.md
      - id: TASK-1-10
        role: coder
        description: |-
          Keep the five `SliceScheduler` #2199 hooks with dead-code
          markers (cq-3). Per the architect's AC-12 NB#2 noqa
          precision: parameters are USED internally by the four
          methods (so they do not warrant `# noqa: ARG002`); only
          the `hitl_escalator` constructor param at line 153
          carries an unused-arg lint. Specifically:

          (a) `record_cycle` at `slice_scheduler.py:299` — add a
              docstring banner pointing at #2199: "Reserved for
              per-slice MCP controls landing in #2199; not wired in
              the production run loop." No `# noqa: ARG002` (all
              params used internally).
          (b) `teardown_slice` at `slice_scheduler.py:417` — same
              docstring banner. No `# noqa: ARG002`.
          (c) `respawn_slice` at `slice_scheduler.py:434` — same
              docstring banner. No `# noqa: ARG002`.
          (d) `cancel_cascade` at `slice_scheduler.py:375` — same
              docstring banner. No `# noqa: ARG002`.
          (e) `hitl_escalator` param in `SliceScheduler.__init__`
              at `slice_scheduler.py:153` — add `# noqa: ARG002` +
              inline comment `# TODO(#2199): wired-but-not-called`.
              This is the only unused-arg lint in the file.

          Verify the existing unit tests in
          `orchestrator/tests/test_slice_scheduler.py` still pass
          unchanged. Do NOT delete; do NOT add `# noqa` suppression
          to the four methods (they are tested and the tests
          exercise them). Note: `poll_cascades` (line 380) is
          LIVE — do not touch its docstring.
        acceptance: |-
          - All five hooks retain their bodies (no deletion).
          - The `hitl_escalator` param has `# noqa: ARG002` +
            TODO(#2199) comment.
          - The four methods have docstring banners naming #2199.
          - No `# noqa: ARG002` on the four methods (their params
            are used internally).
          - Existing `test_slice_scheduler.py` tests pass unchanged.
        files:
          - orchestrator/slice_scheduler.py
      - id: TASK-1-11
        role: coder
        description: |-
          BLE001 audit (feedback Q2). Each of the 20
          `# noqa: BLE001` swallow-all handlers at
          `pipelines.py:15131, 15196, 15274, 15336, 15386, 15422,
          15451, 15471, 15501, 15709, 15742, 15775, 15795, 15841,
          15901, 15910, 15946, 15964, 16080, 16105` is reviewed
          individually. For each site: read the protected call,
          identify the concrete exception types the call can raise
          (consulting the called function's signature/docstring),
          and replace `except Exception` with a tuple of those
          types. Where the failure mode is genuinely unknowable
          (e.g. unbounded third-party callbacks), leave the bare
          handler with an explanatory comment naming what it
          catches and why (one sentence minimum, not "swallow
          all"). The acceptance bar is per-site clarity, not blanket
          replacement. Commit message should list the per-site
          decisions inline.
        acceptance: |-
          - Each of the 20 sites is either narrowed to a specific
            exception tuple or carries an explanatory comment
            naming the catch rationale.
          - No site retains a bare `# noqa: BLE001` without either
            (a) narrowing or (b) a comment.
          - Commit message lists the per-site decisions.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-12
        role: coder
        description: |-
          Collapse the 9 dual-path `except ImportError` shims at
          `pipelines.py:15045, 15050, 15147, 15154, 15161, 15875,
          16026, 16034, 16209` (feedback Q3). Each currently has
          the shape `try: from orchestrator.X import Y; except
          ImportError: from X import Y`. Collapse to the canonical
          `from orchestrator.X import Y` form (the in-package
          import). After collapse, run `make test-all` and confirm
          all tests pass; if any test relies on the flat-layout
          fallback (unlikely but worth checking), revisit. Do NOT
          touch the existing import structure beyond these 9 sites.
        acceptance: |-
          - All 9 shim sites are collapsed to single canonical
            imports.
          - `make test-all` passes after the collapse.
          - No new test failures attributable to the import
            collapse.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-13
        role: coder
        description: |-
          Surgical decomposition (cq-10). Two extractions only:
          (1) Add `_is_slice_dag_mode(contract) -> bool` as a
          module-level helper in `orchestrator/routes/pipelines.py`
          that returns `len(contract.slices) > 1`. Replace the 3
          bare recompute sites at `pipelines.py:8259` (inside
          `_should_skip_pr_phase_auto_pr` — verify the site survives
          TASK-1-3's deletion; if not, drop this replacement),
          `pipelines.py:15060`, and `pipelines.py:15519` with calls
          to the helper. (2) Add
          `_resolve_slice_base_branch(contract, slice_id) -> str`
          as a module-level helper. Reads
          `contract.slices[<slice_id>].parent_branch_at_creation`
          and returns it; for root slices (no upstream slice
          dependencies), returns `f"egg/{pipeline_id}/work"`. This
          replaces the deleted
          `_resolve_slice_1_context_branch_from_contract` and is
          extended by TASK-2-3 to include a merge-base fallback.
          Wire the new helper into the slice-1 base resolution at
          `pipelines.py:15394–15405` (note: TASK-1-2 already did
          this wiring — this task supplies the helper that TASK-1-2
          consumes). Ordering: TASK-1-13 must complete BEFORE
          TASK-1-2 so that TASK-1-2 has a non-empty helper to call.
        acceptance: |-
          - `_is_slice_dag_mode` exists and is called at the 2 or
            3 surviving sites (depending on TASK-1-3's outcome).
          - `_resolve_slice_base_branch` exists, returns
            `egg/<id>/work` for root slices and
            `parent_branch_at_creation` otherwise.
          - The new helpers have docstrings.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-14
        role: coder
        description: |-
          Remove the stale archaeology comments at
          `pipelines.py:15073–15080, 15099–15119, 15204–15228` that
          narrate closed-issue history rather than current
          behaviour. Replace with brief comments that describe what
          the surrounding code does NOW; if a comment block has no
          surviving descriptive value, delete it. Do NOT touch
          comments that document current behaviour (only the stale
          historical narratives are targets). One-line summary in
          the commit message of what was removed.
        acceptance: |-
          - The named line ranges no longer contain multi-paragraph
            archaeology narrating closed issues (#2137, #2548,
            #2593, #2744 closures specifically).
          - Surviving comments describe current behaviour only.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-15
        role: tester
        description: |-
          Update orchestrator unit tests for the context-PR
          scaffold deletion, umbrella deletion, and create_slice_pr
          idempotency. Specifically: in
          `orchestrator/tests/test_gateway_client.py`, delete
          `test_terminal_slice_keeps_umbrella_rollup_and_uses_merge_gate_marker`
          (line 1493) and the related umbrella asserts at lines
          1378, 1379, 1421, 1525; replace with positive tests that
          assert (a) `create_slice_pr` does NOT emit the
          terminal-banner string and (b) the `_lookup_open_pr`
          pre-flight returns the existing PR number on hit and the
          fall-through `gh pr create` is NOT invoked. Add unit
          tests for `_open_context_pr_at_implement_start`
          (happy / idempotent / hard-required-raises paths) in
          a new file
          `orchestrator/tests/test_context_pr_opener.py` (the
          orchestrator-tests dir uses feature-split filenames —
          `test_pipeline_*.py` / `test_pipelines_*.py`; there is
          no monolithic `test_pipelines.py`, so the new file
          matches the established pattern). Mock
          `GatewayClient.create_pr` and `_lookup_open_pr`. All
          tests run under `make test` (changeset-aware).
        acceptance: |-
          - The named umbrella-asserting tests are removed.
          - New tests cover the three
            `_open_context_pr_at_implement_start` paths.
          - New tests cover the `create_slice_pr` idempotency
            pre-flight.
          - `make test` passes; the umbrella-banner literal does
            not appear in `gateway_client.py` (verified by a
            negative-assert test).
        files:
          - orchestrator/tests/test_gateway_client.py
          - orchestrator/tests/test_context_pr_opener.py
      - id: TASK-1-15a
        role: tester
        description: |-
          Rewrite the four gateway PR-phase test files to drop
          PR-phase assertions and add default-deny coverage for
          `target='pr'` (architect v2 AC-4c, lock-step with
          TASK-1-3's `PipelinePhase.PR` removal):

          (1) `gateway/tests/test_phase_api.py` — drop any test
              that asserts PR-phase advancement succeeds; add a
              test that asserts `advance_phase target='pr'` is
              rejected (default-deny).
          (2) `gateway/tests/test_phase_filter.py` — drop any
              assertion that PipelinePhase.PR exists in the
              phase-permissions table; add a test that asserts
              PR is not a valid permission key.
          (3) `gateway/tests/test_phase_filter_restrictions.py` —
              drop any assertion that PR-phase has a
              PhaseFileRestriction entry; add a test that asserts
              looking up restrictions for 'pr' returns the
              default-deny.
          (4) `gateway/tests/test_phase_transition.py` — drop any
              assertion that IMPLEMENT → PR or PR → COMPLETE is
              an accepted transition; add a test that asserts
              IMPLEMENT is the terminal phase.

          Run `make test` to confirm green.
        acceptance: |-
          - All four files updated per the above.
          - Each file has at least one new default-deny test
            verifying 'pr' is no longer accepted.
          - `make test` passes.
        files:
          - gateway/tests/test_phase_api.py
          - gateway/tests/test_phase_filter.py
          - gateway/tests/test_phase_filter_restrictions.py
          - gateway/tests/test_phase_transition.py
      - id: TASK-1-16
        role: tester
        description: |-
          Add an integration test under
          `integration_tests/regression/` (the kubectl-gated
          recovery/regression tier where the parent conftest's
          `orchestrator_url` pytest fixture and `egg_stack`
          dataclass — with `gateway_url` attribute — are available;
          see Primitives §"trust-boundary scope" in the plan prose;
          the legacy `integration_tests/local_pipeline/` directory was
          deleted on 2026-05-11 in commit `f7803637d1` and MUST NOT be
          referenced). The new test exercises the slice-DAG →
          context-PR-opens-up-front → idempotent path (feedback Q4).
          The test (a) spawns a 2-slice DAG pipeline against the local
          stack, (b) advances to the plan→implement boundary, (c)
          asserts a single PR exists with `head=egg/<id>/work
          base=main`, (d) extracts the PR number, (e) deliberately
          clears `contract.pr.context_pr_number` on disk, (f)
          re-triggers the implement-start hook via `advance_phase`,
          (g) asserts no duplicate PR is opened and the same PR number
          is re-persisted. This is the regression test for #2769 /
          #2593 / #2744. Inject `orchestrator_url` and `egg_stack`
          fixtures (kubectl-skip is automatic). Document in a
          docstring that this test MUST live under
          `integration_tests/regression/` because that's where the
          recovery/regression tier lives and the parent kubectl-gated
          fixtures are exposed.
        acceptance: |-
          - File `integration_tests/regression/test_context_pr_up_front.py`
            (or similar) exists.
          - Test runs under `make test-all` and passes against the
            local stack.
          - Test exercises the idempotency path (steps e-g above).
          - Test skips cleanly when `kubectl` is unavailable (via the
            inherited `egg_stack` fixture's skip).
        files:
          - integration_tests/regression/test_context_pr_up_front.py
      - id: TASK-1-16a
        role: tester
        description: |-
          Rewrite the two SDLC integration tests that assert the
          old `implement → pr → complete` transition (architect v2
          AC-4, delete_integration_tests bucket):

          (1) `integration_tests/sdlc/test_happy_path.py` — change
              every phase-progression assertion that expects
              `pr` after `implement` to expect `complete`. Drop
              any assertion that the PR-phase agent ran.
          (2) `integration_tests/sdlc/test_role_enforcement.py` —
              drop the PR-phase role-enforcement assertions; the
              PR-phase no longer exists so there is no PR-phase
              role surface to enforce. The new context-PR opener
              in TASK-1-1 is invoked from the orchestrator-side
              (no agent), so no role-enforcement check applies.

          Run under `make test-all` against the local stack.
        acceptance: |-
          - `test_happy_path.py` asserts implement → complete (no
            PR phase).
          - `test_role_enforcement.py` no longer asserts on the
            PR-phase role surface.
          - Both tests pass under `make test-all`.
        files:
          - integration_tests/sdlc/test_happy_path.py
          - integration_tests/sdlc/test_role_enforcement.py
      - id: TASK-1-17
        role: tester
        description: |-
          Update the remaining orchestrator unit tests affected by
          slice-1's code changes. The named files are explicit
          (per architect v2 AC-4b):

          (1) **DELETE** `orchestrator/tests/test_finalize_pr_phase.py`
              entirely — the `_finalize_pr_phase_failed` function
              is removed in TASK-1-3 (2).
          (2) **DELETE** `orchestrator/tests/test_auto_pr.py`
              entirely — the auto-PR backstop path is removed
              with `_should_skip_pr_phase_auto_pr` in TASK-1-3 (1).
          (3) **REWRITE** `orchestrator/tests/test_dag_visualizer.py`
              to assert the new DAG terminates at IMPLEMENT
              (no PR node, no IMPLEMENT→PR edge) per TASK-1-3 (9).
          (4) `orchestrator/tests/test_consensus.py` — delete any
              test importing `ConsensusEvaluator` (the module is
              removed in TASK-1-6).
          (5) `orchestrator/tests/test_restart_phase.py` — drop
              any test asserting `evaluator.clear()` is called;
              update to mirror TASK-2-1's new slice-aware
              semantics (which will land in slice-2; mark the
              affected tests `xfail` if they need slice-2 behaviour
              that hasn't landed yet, OR leave them passing under
              slice-1's pipeline-level-only semantics).
          (6) `orchestrator/tests/test_rebase_pipeline_branch.py`
              (existing file dedicated to `_rebase_pipeline_branch_onto_base`
              regression tests) — extend with the #2570 regression
              test that calls `_rebase_pipeline_branch_onto_base`
              (or its replacement from TASK-1-9) on a fixture
              pipeline branch and asserts the merge-base against
              `main` does NOT change after a simulated main advance.
              **Pin N and M per AC-9 NB#1**: the test must exercise
              **N≥3 phase transitions** with **M≥2 main PRs merged
              in parallel**, then assert the merge-base is still
              the pipeline-creation SHA. Use the surviving
              `orchestrator/tests/test_pipeline_failure_path.py` /
              `test_pipelines_api.py` files only to delete or update
              tests that imported the now-removed functions
              (`_open_context_pr_for_pipeline`, etc.) — grep for
              those names before completing. Note: the legacy
              monolithic `orchestrator/tests/test_pipelines.py` does
              NOT exist in the current tree; pipeline tests live
              under `test_pipeline_*.py` and `test_pipelines_*.py`
              files (split by feature).
          (7) BLE001 audit (TASK-1-11) — where TASK-1-11 narrowed
              a swallow-all handler to a specific exception, add
              a unit test that asserts the new specific exception
              triggers the documented recovery path. Sample 3-5
              sites; full coverage is not required (BLE001 audit
              is per-site judgement, not per-site test).

          Run `make test-all` and confirm a green suite before
          marking the task complete.
        acceptance: |-
          - `test_finalize_pr_phase.py` is deleted.
          - `test_auto_pr.py` is deleted.
          - `test_dag_visualizer.py` is rewritten to assert
            IMPLEMENT-terminal.
          - All tests referring to `ConsensusEvaluator` are
            removed.
          - #2570 regression test exists in
            `orchestrator/tests/test_rebase_pipeline_branch.py`
            and passes.
          - 3-5 BLE001-narrowing unit tests added in TASK-1-11
            sample sites.
          - `make test-all` passes.
        files:
          - orchestrator/tests/test_rebase_pipeline_branch.py
          - orchestrator/tests/test_pipeline_failure_path.py
          - orchestrator/tests/test_pipelines_api.py
          - orchestrator/tests/test_consensus.py
          - orchestrator/tests/test_restart_phase.py
          - orchestrator/tests/test_dag_visualizer.py
          - orchestrator/tests/test_finalize_pr_phase.py
          - orchestrator/tests/test_auto_pr.py
      - id: TASK-1-18
        role: documenter
        description: |-
          Update docs to reflect the new context-PR topology and
          deleted PR phase. Specifically: (a) update
          `docs/architecture/orchestrator.md` (and any
          phase-narrative docs under `docs/guides/`) to remove
          references to the `egg/<id>/context` branch and the PR
          phase; describe the new model — context PR is
          `egg/<id>/work → main`, opened up-front at the
          plan→implement boundary, hard-required and idempotent.
          (b) Update any reference docs that mention the deleted
          PRMetadata fields (`context_branch`, `context_title`,
          `context_description`). (c) Update the slice-PR docs to
          drop the "umbrella" terminology (subsumes #2389). (d)
          Update `docs/guides/concurrent-execution.md` or similar
          if it lists the legacy `ConsensusEvaluator`. (e) Add a
          CHANGELOG / migration-note doc summarising the schema
          bump 1.1 → 1.2 and the PR-phase deletion. No code
          changes (documenter role is doc-only). Run `make lint`
          to catch Markdown lint issues.
        acceptance: |-
          - References to `egg/<id>/context` branch removed from
            docs.
          - References to the PR phase removed from docs.
          - "Umbrella" terminology removed from docs.
          - Migration note for v1.1 → v1.2 schema bump exists.
          - `docs/guides/pipeline-health-monitoring.md` no longer
            references the deleted `pr_phase_no_pr` alert.
        files:
          - docs/architecture/orchestrator.md
          - docs/guides/sdlc-pipeline.md
          - docs/guides/pipeline-health-monitoring.md
  - id: 2
    name: |-
      Slice/phase restart hardening (bundles #2409)
    goal: |-
      Harden slice and phase restart (Goal 3) so an interrupted sliced
      implement phase resumes correctly. Eager-persist
      parent_branch_at_creation under the contract lock at PENDING->
      IN_PROGRESS plus a merge-base fallback (cq-9), make restart_phase
      iterate per-slice consensus trackers (today's pipeline-only clear is
      asymmetric vs slice-aware restart_agent), reconstruct per-slice
      trackers in startup_reconciliation using slice_id-tagged
      reconstruct_tracker_from_messages calls (bundles #2409), and extend
      bootstrap reconciliation to recognise IN_PROGRESS / BLOCKED slices
      that did real work so they aren't silently re-yielded READY and
      respawned. Depends on slice-1 so the restart logic reasons about the
      post-collapse topology (no egg/<id>/context branch in scope, no
      PR-phase route to consider).
    parent_slice_id: 1
    tasks:
      - id: TASK-2-1
        role: coder
        description: |-
          Make `restart_phase` slice-aware. In
          `orchestrator/routes/pipelines.py`, the consensus-clear
          block at lines 3250–3287 currently calls
          `get_peer_consensus_tracker(pipeline_id).clear()` only
          for the pipeline-level key. Extend it to iterate
          `contract.slices` and call
          `get_peer_consensus_tracker(pipeline_id, slice_id=s.id).clear()`
          for each slice (the slice-aware key is
          `f"{pipeline_id}/{slice_id}"` per `peer_consensus.py:1844`).
          Use the `_tracker_key` helper if it's accessible, else
          inline the format string with a comment naming the
          source of truth. Mirror the pattern from `restart_agent`
          (`pipelines.py:2255`) which is already slice-aware. Note:
          slice-1's TASK-1-6 already removed
          `evaluator.clear(pipeline_id)` from this block, so the
          slice-aware iteration is the only consensus clear left.
          **Sanity check before changes**: verify
          `pipelines.py:3279` no longer contains
          `evaluator.clear(pipeline_id)`. If the line is still
          present, slice-1's TASK-1-6 has not landed yet — escalate
          via `mcp__sdlc__report_impasse` (category=plan_bug) and
          wait for the slice-1 rebase before proceeding.
        acceptance: |-
          - `restart_phase` clears both the pipeline-level
            consensus tracker AND iterates per-slice trackers.
          - The pattern mirrors `restart_agent`'s slice-aware path.
          - Verified by unit test in TASK-2-6.
          - Pre-flight sanity check: `pipelines.py:3279` does NOT
            contain `evaluator.clear(pipeline_id)` at task start.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        role: coder
        description: |-
          Eager-persist `parent_branch_at_creation` (cq-9 part 1).
          Today the field is written at `pipelines.py:15414–15421`,
          immediately after resolution but BEFORE
          `create_slice_integration_branch` (line 15492). Move the
          persist call to the moment the slice transitions
          PENDING → IN_PROGRESS in the contract (find the status-
          transition site in the slice loop — look for
          `SliceStatus.IN_PROGRESS` assignment). Persist
          `parent_branch_at_creation` in the SAME contract write
          that flips the status, under the per-pipeline state
          lock, so a crash between the status flip and the branch
          creation cannot leave the field empty. Read the existing
          comment block at the current write site to preserve the
          rationale; add a new comment at the new site explaining
          why the eager persist matters (cq-9 / crash recovery).
        acceptance: |-
          - `parent_branch_at_creation` is persisted in the same
            contract write that flips a slice to IN_PROGRESS.
          - The old persist site at lines 15414–15421 is removed.
          - Crash-recovery test in TASK-2-6 confirms the field is
            present on an artificially interrupted slice.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        role: coder
        description: |-
          Add a merge-base fallback to `_resolve_slice_base_branch`
          (cq-9 part 2 — depends on TASK-1-13 having created the
          helper). When `parent_branch_at_creation` is empty
          (legacy / orphaned slices that pre-date TASK-2-2's eager
          persist), call
          `GatewayClient.merge_base(slice_branch, origin/main)` (or
          the equivalent gateway shell command — find the existing
          merge-base wrapper in `gateway_client.py`) and use the
          merge-base SHA as the implicit parent. If the slice
          branch has no commits on origin yet, fall back to
          `egg/<id>/work`. The fallback is defence-in-depth; the
          eager persist from TASK-2-2 is the correctness fix.
          Document the fallback in the helper's docstring.
        acceptance: |-
          - `_resolve_slice_base_branch` falls back to merge-base
            when `parent_branch_at_creation` is empty.
          - Final fallback to `egg/<id>/work` for slices with no
            origin commits.
          - Docstring documents the three-tier resolution.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-4
        role: coder
        description: |-
          Extend bootstrap reconciliation for non-COMPLETE slices.
          Per the risk-analyst's R5 mis-classification matrix, the
          third layer must implement the following 5-way decision
          based on `SliceStatus` and observed origin state:

          1. **IN_PROGRESS + no commits pushed to integration
             branch** → re-yield as READY (existing path; correct).
          2. **IN_PROGRESS + commits pushed + consensus NOT
             reached** → reconstruct the per-slice
             `PeerConsensusTracker` (via TASK-2-5's
             reconstruction primitive), call
             `scheduler.mark_spawned(slice_id)` so the run loop
             does NOT respawn agents, and resume the BRC wait.
             Producer agents may still be alive in their pods; if
             gone, the orchestrator's normal spawn-on-need path
             handles it.
          3. **IN_PROGRESS + commits pushed + consensus REACHED +
             PR NOT opened** → complete the slice and call the
             slice-PR opener (subject to TASK-1-8's idempotency
             pre-flight). Do not respawn agents.
          4. **BLOCKED (HITL pending)** → do NOT respawn; preserve
             the BLOCKED status until the operator resolves the
             HITL. Verify the HITL decision is still on the
             contract; if not, escalate to a new HITL ("BLOCKED
             slice with no pending decision — manual investigation
             required").
          5. **Unknown / corrupt state** (e.g. SliceStatus.COMPLETE
             but no commits, or impossible status combinations) →
             escalate via `mcp__sdlc__register_open_question` (do
             NOT silently re-yield as READY — silent classification
             error is worse than an operator pause).

          The third layer is additive: existing Layer A
          (`pipelines.py:15233-15240`, marks COMPLETE) and Layer B
          (`pipelines.py:15242-15295`, marks merged-on-origin) are
          unchanged. Add the third layer immediately after Layer B
          with a comment block explaining the 5-way decision. Tests
          in TASK-2-6 (must cover each of the 5 classifications).

          Race-condition note from R5: an orchestrator-pod recycle
          can leave the slice's agent containers dead while the
          contract still shows IN_PROGRESS. Case 2 explicitly
          tolerates this — the reconciliation marks the slice
          "spawned" and the next BRC tick will re-detect missing
          agents via the standard spawn-on-need path.
        acceptance: |-
          - Bootstrap reconciliation has a third layer.
          - The third layer implements the 5-way classification
            above (verified by 5 unit tests in TASK-2-6).
          - Case 5 (unknown / corrupt state) escalates to HITL
            instead of silent re-yield.
          - Existing Layer A and Layer B are unchanged.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-5
        role: coder
        description: |-
          Per-slice consensus tracker reconstruction in
          `startup_reconciliation.py` (closes #2409). Per the
          architect's AC-16, the closure requires three coupled
          changes:

          (1) **Add an optional `slice_id` field to
              `message_store.Message`** in
              `orchestrator/message_store.py` (architect v2 located
              the dataclass there). Default None for back-compat;
              persist when set so on-disk message history carries
              the slice scope.
          (2) **Extend `reconstruct_tracker_from_messages`** (find
              the function in `peer_consensus.py` or the
              reconstruction module) to accept an optional
              `slice_id` parameter. When set, the replay window
              filters to messages whose `slice_id` matches; when
              unset, replay all messages (pipeline-level only,
              back-compat).
          (3) **Extend the `startup_reconciliation.py` loop** at
              lines 312-376 (especially 358-367): for each
              pipeline that has slices, iterate
              `contract.slices`, and for each slice call the
              reconstruction primitive with the matching
              `slice_id`. The reconstruction populates the
              `f"{pipeline_id}/{slice_id}"` tracker key per
              `peer_consensus.py:1844`. If on-disk message
              history has no entries scoped to a given slice
              (e.g. the slice never started), skip silently —
              TASK-2-4's bootstrap reconciliation handles the
              slice's runtime resumption from scratch.
          (4) **Fix `handle_consensus_confirmed_signal`** in
              `orchestrator/routes/signals.py` (architect v2
              AC-16): today the handler skips reconstruction when
              `slice_id` is supplied; remove the skip so
              slice-scoped confirms also reconstruct via the
              new slice-id-filtered path.

          AC-16 explicitly names the test bar: two concurrent
          slices, orchestrator restart between slice-1 confirming
          and slice-2 starting, asserts the reconstructed slice-2
          tracker does NOT contain slice-1's messages (i.e. the
          slice_id filter works). TASK-2-6 owns that test.
        acceptance: |-
          - `message_store.Message` carries an optional `slice_id`
            field, persisted to disk when set.
          - `reconstruct_tracker_from_messages` accepts and
            filters by `slice_id`.
          - `startup_reconciliation.py` reconstructs per-slice
            trackers for every pipeline with slices, keyed
            `{pipeline_id}/{slice_id}`.
          - AC-16 cross-slice isolation test in TASK-2-6 passes.
          - `handle_consensus_confirmed_signal` in
            `orchestrator/routes/signals.py` no longer skips
            reconstruction when `slice_id` is supplied.
          - #2409 is closed by this task.
        files:
          - orchestrator/startup_reconciliation.py
          - orchestrator/peer_consensus.py
          - orchestrator/message_store.py
          - orchestrator/routes/signals.py
      - id: TASK-2-6
        role: tester
        description: |-
          Tests for restart hardening. Unit tests under
          `orchestrator/tests/`:

          (a) Slice-aware `restart_phase` — assert `tracker.clear()`
              is called for the pipeline-level key AND for each
              per-slice key (TASK-2-1).
          (b) Eager-persist of `parent_branch_at_creation` —
              assert the field is written in the same contract
              mutation as the PENDING→IN_PROGRESS status flip,
              NOT after `create_slice_integration_branch`
              (TASK-2-2).
          (c) Merge-base fallback in `_resolve_slice_base_branch`
              — assert a slice with empty
              `parent_branch_at_creation` but pushed commits
              resolves correctly via merge-base; and a slice with
              no origin commits falls back to `egg/<id>/work`
              (TASK-2-3).
          (d) Extended bootstrap reconciliation 5-way
              classification — FIVE separate tests, one per case
              (TASK-2-4's matrix): (d1) IN_PROGRESS + no commits
              → re-yield READY; (d2) IN_PROGRESS + commits +
              no consensus → reconstruct + mark_spawned, no
              respawn; (d3) IN_PROGRESS + commits + consensus +
              no PR → complete + open PR via idempotent pre-flight;
              (d4) BLOCKED + pending HITL → preserve status;
              (d5) corrupt state → escalate HITL.
          (e) AC-16 cross-slice isolation — two concurrent slices,
              reconstruct slice-2's tracker after orchestrator
              restart between slice-1 confirming and slice-2
              starting, assert slice-2's tracker has NO slice-1
              messages (TASK-2-5).

          Integration test under
          `integration_tests/regression/` (the kubectl-gated
          recovery/regression tier where `orchestrator_url` is
          available via the parent conftest fixture and `egg_stack`
          carries `gateway_url` as an attribute; this directory is
          REQUIRED — see Primitives §"trust-boundary scope" in the
          plan prose; the legacy `integration_tests/local_pipeline/`
          directory was deleted on 2026-05-11 in commit `f7803637d1`
          and MUST NOT be referenced): kill the orchestrator pod
          mid-implement-phase on a sliced pipeline, restart, assert
          per-slice consensus trackers reconstruct from on-disk
          message history (the AC-16 closure proof for #2409). Inject
          `orchestrator_url` and `egg_stack`. Document in a docstring
          that this test MUST live under
          `integration_tests/regression/`.
        acceptance: |-
          - Tests (a)–(e) all pass, with case-d split into 5
            separate tests for the classification matrix.
          - Integration test for orchestrator-pod recycle passes
            and exercises cross-slice tracker isolation.
          - Integration test skips cleanly when `kubectl` is
            unavailable.
          - `make test-all` is green.
        files:
          - orchestrator/tests/test_restart_phase.py
          - orchestrator/tests/test_startup_reconciliation.py
          - integration_tests/regression/test_restart_hardening.py
      - id: TASK-2-7
        role: documenter
        description: |-
          Update docs for the restart-hardening changes. (a) Update
          `docs/architecture/orchestrator.md` (and any restart-
          related docs under `docs/guides/`) to describe the
          slice-aware `restart_phase` semantics and the new
          bootstrap-reconciliation layer that handles non-COMPLETE
          slices. (b) Update the `restart_phase` reference in
          `docs/reference/orchestrator-cli.md` (or wherever the
          MCP-verb reference lives) to note that it now clears
          per-slice consensus trackers in addition to the pipeline-
          level tracker. (c) Add a brief note on the per-slice
          consensus tracker reconstruction (#2409 closure) so
          operators know that an orchestrator-pod recycle no
          longer loses in-flight slice consensus.
        acceptance: |-
          - Restart docs describe slice-aware semantics.
          - Reference docs updated.
          - #2409 closure note exists.
        files:
          - docs/architecture/orchestrator.md
          - docs/reference/orchestrator-cli.md
```


## HITL Resolution

The following was approved by a human reviewer at the plan phase gate:

Plan approved. Begin implement: slice-1 (context-PR collapse + cleanup + PR-phase removal, subsuming #2389 and #2570) starts at the plan->implement boundary with the new up-front context PR opener; slice-2 (restart hardening, closes #2409) follows after slice-1 lands. All Wave 2 decisions honored. #2792 OUT OF SCOPE — no work on _sync_worktree_with_remote, _populate_contract_from_plan*, _empty_contract_hitl_*, PlanDraftMissing* exceptions.
