# Analysis: Cleanup: sliced implementation phase — context-PR topology and slice/phase restart

> Issue: #2777 | Phase: refine

## Problem Statement

The sliced implementation phase of the SDLC pipeline
(`_run_implement_phase_slices` / `_run_one_slice_inner` in
`orchestrator/routes/pipelines.py` and the supporting context-PR machinery in
`gateway_client.py` / `gateway.py`) has accreted significant complexity
across #2137, #2548, #2593, and #2744. The current code:

1. Maintains a separate `egg/<id>/context` branch as a parallel stack root
   (introduced by #2548). All downstream complexity — temp-worktree
   materialisation, two-tier idempotency, `ContextBranchDiverged` handling,
   a soft-fail wrapper called from five sites, an observability-dedup set,
   a gateway push-exemption regex, and slice-1 base resolution fallbacks —
   exists only to service that separate branch.
2. Has **no PR-phase backstop** in slice-DAG mode: `_should_skip_pr_phase_auto_pr`
   returns True wholesale on `len(slices) > 1`, so a missed context PR
   leaves the slice stack unmergeable until an operator opens the base PR
   manually (the #2769 incident; previously hit in #2593 and #2744).
3. Routes around the `plan_draft_missing_on_local` reachability bug (#2792)
   with a 3-option HITL even when the orchestrator already knows the draft
   is on origin and the auto-recovery is well-defined.
4. Leaves slice and phase restart with known gaps: bootstrap reconciliation
   only recognises persisted `COMPLETE`; `restart_phase` is slice-unaware;
   `create_slice_pr` cascades the whole slice to FAILED on a transient `gh`
   failure with no idempotent pre-check.
5. Carries unused speculative #2199 hooks
   (`record_cycle`, `teardown_slice`, `respawn_slice`, `cancel_cascade`,
   `hitl_escalator` constructor param, two-tier `max_cycles` cap), a
   deprecated `ConsensusEvaluator` legacy module that is still reset on
   every restart path, 20× `# noqa: BLE001` swallow-all handlers, 9× dual-path
   `except ImportError` shims, and stale archaeology comments narrating
   closed-issue history.

The desired outcome: realign the context-PR topology to the deterministic
`egg/<id>/work → main` model, delete the scaffold (expected net-negative
LOC), harden slice/phase restart, close the `plan_draft_missing_on_local`
reachability loop (#2792) by auto-reconciling instead of HITL-prompting,
and purge accumulated dead code.

## Current Behavior

### Context-PR topology (`#2548` design)

- **`_open_context_pr_for_pipeline`** (`pipelines.py:10002`): the primary
  creator. End-to-end it (a) fast-paths on `contract.pr.context_pr_number`
  being set, (b) calls `_lookup_existing_context_pr` to detect partial
  failures (PR opened but contract update lost), (c) materialises a temp
  git worktree, (d) copies refine/plan artifacts via
  `_gather_context_pr_files` (`pipelines.py:9896`), (e) commits and pushes
  to `egg/<id>/context`, (f) calls `GatewayClient.create_pr`, (g) persists
  the linkage via `_persist_context_pr_linkage_on_contract`. The function
  has **17 silent `return None` paths** (lines 10114, 10121, 10131, 10146,
  10153, 10160, 10189, 10221, 10265, 10279, 10373, 10427, 10453, 10461,
  10472, 10496, 10502, 10539), each logged at warning/info and swallowed.
- **`_lookup_existing_context_pr`** (`pipelines.py:9735`, ~150 lines):
  GitHub-authoritative check via `gh pr list`. Distinguishes full match /
  head-only mismatch / no PR — needed because the multi-step create can
  partially fail (#2582).
- **`create_context_branch`** (`gateway_client.py:2327`, ~90 lines): pushes
  `base_sha:refs/heads/egg/<pipeline_id>/context` via a synthetic session;
  raises `ContextBranchDiverged` (`gateway_client.py:3453`, raised at
  `gateway_client.py:2468`) on divergence.
- **`_maybe_open_base_pr_for_plan_to_implement`** (`pipelines.py:10648`,
  ~230 lines): shared soft-fail wrapper called from five sites —
  `pipelines.py:15120` (slice-loop entry backstop), `pipelines.py:20572`
  (advance_phase REST), `pipelines.py:22051` (auto-advance in
  `_run_pipeline`), `pipelines.py:22994` (IMPLEMENT phase entry backstop),
  and `phases.py:500` (advance_phase MCP). Maintains a
  `_context_pr_events_emitted` dedup set (`pipelines.py:10644–10645`)
  keyed on `(pipeline_id, event_type)`.
- **`_resolve_slice_1_context_branch_from_contract`**
  (`pipelines.py:10883`, ~25 lines): fallback resolver returning
  `contract.pr.context_branch` for slice-1's parent-branch resolution
  (`pipelines.py:15394–15405`).
- **Gateway push-exemption**: `_CONTEXT_BRANCH_RE = r"^egg/[A-Za-z0-9][A-Za-z0-9_-]*/context$"`
  (`gateway.py:1112`), exempting `egg/<id>/context` from the pipeline-session
  push block (#2028), used at `gateway.py:1350` and `gateway.py:1362`.
- **PR-phase skip**: `_should_skip_pr_phase_auto_pr` (`pipelines.py:8222`)
  returns `(True, f"slice_dag_mode_slice_count={slice_count}")` (line 8276)
  whenever `len(contract.slices) > 1`. No backstop runs in slice-DAG mode.
- **Schema** (`shared/egg_contracts/models.py:467`): `PRMetadata` adds
  `context_title: str|None`, `context_description: str|None`,
  `context_branch: str|None`, `context_pr_number: int|None`,
  `deferred_actions: list[DeferredAction]`.
  `contract.pr.context_branch` is written at `pipelines.py:9839`
  (`_persist_context_pr_linkage_on_contract`) and read at
  `pipelines.py:10172`, `10212`, `10910`, `15398`, `15394–15405`, `18719`.

### Plan → implement reconciliation (#2792)

- **`_sync_worktree_with_remote`** (`pipelines.py:6442`, ~200 lines): does
  fetch + reconcile via reset/rebase. The pertinent failure mode is the
  `divergence_rebase_failed` early-return at `pipelines.py:6764–6775`: when
  both local and remote have commits and the rebase fails, the function
  returns without resetting, leaving the worktree HEAD stale. On the next
  `_populate_contract_from_plan_safe` call (`pipelines.py:18408`,
  raises `PlanDraftMissingOnLocalError` at line 18478 when the draft is on
  origin but `local_path.exists()` is False), the populator sees a missing
  draft and triggers the HITL.
- **HITL gate**: `_empty_contract_hitl_question` at `pipelines.py:18247–18259`
  emits the prompt currently used in #2792's reproduction:
  > Pipeline blocked at plan_complete: contract.slices is empty and the plan
  > draft is missing, unparseable, or yielded no tasks
  > (reason=plan_draft_missing_on_local). The populate-from-plan step
  > silently failed earlier (#2337 / #2627), so pipeline state and the
  > contract have diverged. Plain restart_phase implement will respawn into
  > the same broken state. How to proceed?
  >
  > - 'Repopulate contract from plan draft and retry'
  > - 'Restart plan phase'
  > - 'Abort pipeline'

  Decision is raised at `pipelines.py:21508–21545` via `_emit_empty_contract_hitl`
  (line 21537). The "silently failed earlier" phrasing is misleading on a
  clean run because there was no earlier populate event — the divergence is
  reached on the *first* `plan_complete`.
- **Exception classes**: `PlanDraftMissingOnLocalError`
  (`pipelines.py:17987–17998`), `PlanDraftMissingOnLocalAndOriginError`
  (`pipelines.py:18000–18014`), `PopulateProducedEmptyContractError`
  (`pipelines.py:18043–18092`). All inherit from `RuntimeError`, all caught
  at `pipelines.py:21508–21545`.

### Slice scheduler / restart

- **`SliceScheduler` instantiation**: `SliceScheduler(contract)` at
  `pipelines.py:15090` — no `hitl_escalator` passed; the constructor param
  (`slice_scheduler.py:153`) defaults to None.
- **Bootstrap reconciliation** (in `_run_implement_phase_slices`):
  - Layer A (`pipelines.py:15233–15240`): for each slice with
    `contract.slice.status == SliceStatus.COMPLETE`, call
    `scheduler.record_complete(s.id)`.
  - Layer B (`pipelines.py:15242–15295`): for remaining slices, call
    `is_slice_branch_merged_into_parent` (`gateway_client.py:1988`, which
    compares origin SHAs via `merge-base --is-ancestor`); on hit, record
    complete.
  - **Not covered**: slices with status `IN_PROGRESS` / `BLOCKED` that did
    real work (commits pushed, consensus not reached). These are re-yielded
    READY and re-spawned from scratch; the integration-branch push can hit
    a non-fast-forward rejection (mitigated only when the slice's PR was
    merged in the gap — `pipelines.py:15442–15459`).
- **`SliceStatus` enum** (`shared/egg_contracts/models.py:41–55`):
  `PENDING`, `IN_PROGRESS`, `COMPLETE`, `BLOCKED`.
- **`parent_branch_at_creation`** is written under the per-pipeline state
  lock at `pipelines.py:15414–15421`, immediately after resolution and
  BEFORE `create_slice_integration_branch` (line 15492). The reconstruction
  fallback the issue references runs in `_resolve_slice_1_context_branch_from_contract`
  (`pipelines.py:10883`).
- **`create_slice_pr` failure** at `pipelines.py:15775`: catches generic
  Exception, sets `pr_created=False`, calls
  `scheduler.record_failure(slice_id)` (line 15785), returns exit code 1.
  No pre-flight `gh pr list` "PR already open?" check.
- **`restart_phase`** (`pipelines.py:3250–3287`): clears the pipeline-level
  consensus tracker via `get_peer_consensus_tracker(pipeline_id).clear()`
  (lines 3259–3261) and the legacy evaluator via
  `evaluator.clear(pipeline_id)` (line 3279). **Does not iterate per-slice
  consensus trackers** (`{pipeline_id}/{slice_id}` namespace at
  `peer_consensus.py:1865`).
- **`restart_agent`** (`pipelines.py:2255` area): slice-aware, respects
  `slice_id` query param. Asymmetric with `restart_phase`.
- **`startup_reconciliation.py`**: handles containers and pipeline-level
  agents; refrains from marking phase COMPLETE if sibling slices are
  active (lines 358–367) but does NOT reconstruct per-slice consensus
  trackers (#2409 explicitly tracks this gap).

### Dead code

- **`SliceScheduler` methods**: `record_cycle` (`slice_scheduler.py:299`),
  `teardown_slice` (`slice_scheduler.py:417`), `respawn_slice`
  (`slice_scheduler.py:434`), `cancel_cascade` (`slice_scheduler.py:375`)
  are public and unit-tested
  (`orchestrator/tests/test_slice_scheduler.py`) but never called outside
  tests. `poll_cascades` (`slice_scheduler.py:380`) IS live (called at
  `pipelines.py:15322` and `15860`). The documented two-tier `max_cycles`
  cap (3 local / 10 global; `env_config.py:271–272`) is enforced inside
  `record_cycle` (`slice_scheduler.py:323–324`) → the cap is effectively
  inert in production because the method is never called.
- **Legacy `ConsensusEvaluator`** (`orchestrator/consensus.py:38`, singleton
  `get_consensus_evaluator()` at line 153): marked DEPRECATED in its
  module docstring; the BRC `PeerConsensusTracker`
  (`orchestrator/peer_consensus.py:69`) is the only consensus path in
  production. Still reset on every `restart_phase`
  (`pipelines.py:3279`).
- **"Umbrella" terminology** (#2389): `gateway_client.py:1231,1238,1258`
  (`create_slice_pr` docstring + literal banner string),
  `pipelines.py:11270,11292,11301,11308` (`umbrella_has_program_block`
  variable + comments), test fixtures pinning the banner string.
- **`# noqa: BLE001` swallow-all handlers** in slice code: 20 sites at
  `pipelines.py:15131, 15196, 15274, 15336, 15386, 15422, 15451, 15471,
  15501, 15709, 15742, 15775, 15795, 15841, 15901, 15910, 15946, 15964,
  16080, 16105`.
- **`except ImportError` dual-path import shims**: 9 sites at
  `pipelines.py:15045, 15050, 15147, 15154, 15161, 15875, 16026, 16034,
  16209`.
- **Bare `slice_count > 1` recomputed** at three sites (`pipelines.py:8259,
  15060, 15519`), each loading the contract independently. No
  `_is_slice_dag_mode` helper.
- **`_run_implement_phase_slices`** is ~900 lines containing the
  ~430-line `_run_one_slice_inner` closure (`pipelines.py:15364`), with
  ~145 lines of slice-PR payload assembly inline. Sample archaeology
  comments at `pipelines.py:15073–15080`, `15099–15119`, `15204–15228`
  narrate closed-issue history rather than current behavior.

## Constraints

- **Schema versioning**: `PRMetadata` ships in contract schema v1.1. Any
  field removal needs a v1.2 bump and a decision on migration shape
  (see cq-2).
- **In-flight pipelines**: deploying mid-pipeline (slice-DAG, RUNNING)
  must not orphan the in-flight slice trackers or the in-flight
  `egg/<id>/context` branch. Either the deploy is gated on quiescence, or
  the cleanup keeps a back-compat read path for the legacy context-branch
  field until quiescence.
- **Gateway policy**: removing the `_CONTEXT_BRANCH_RE` exemption requires
  the new context PR's branch (`egg/<id>/work`) to already be on the
  pipeline-session push-allow list — verify before deletion.
- **Test surface**: tests assert the literal banner string `"Program-level
  umbrella PR"` (`orchestrator/tests/test_gateway_client.py:1377–1423`)
  and the dedup-set behaviour around `_maybe_open_base_pr_for_plan_to_implement`
  — these need lockstep updates.
- **Backwards compatibility**: `restart_phase` and `restart_agent` are
  MCP-exposed verbs; their externally-observable behaviour must be
  preserved or explicitly versioned.
- **Net-negative LOC goal**: the issue explicitly favours deletion over
  refactor; structural extractions (e.g. decomposing
  `_run_implement_phase_slices`) should be scoped (see cq-10) rather than
  expanded into a full #2261-style overhaul.
- **No code in this phase**: refine produces analysis only; the plan phase
  owns task decomposition and the implement phase owns the change.

## Runtime-Primitive Inventory (per #2594)

The plan phase will need to reason about the following primitives. Each is
named with file:line evidence and execution-context scope.

**Trusted-CI / orchestrator-only (host-side Python in the orchestrator pod):**
- `pipelines._run_implement_phase_slices` — `pipelines.py:15013`
- `pipelines._run_one_slice_inner` — `pipelines.py:15364` (nested closure)
- `pipelines._open_context_pr_for_pipeline` — `pipelines.py:10002`
- `pipelines._lookup_existing_context_pr` — `pipelines.py:9735`
- `pipelines._gather_context_pr_files` — `pipelines.py:9896`
- `pipelines._persist_context_pr_linkage_on_contract` — `pipelines.py:9839`
- `pipelines._maybe_open_base_pr_for_plan_to_implement` — `pipelines.py:10648`
- `pipelines._resolve_slice_1_context_branch_from_contract` — `pipelines.py:10883`
- `pipelines._should_skip_pr_phase_auto_pr` — `pipelines.py:8222`
- `pipelines._sync_worktree_with_remote` — `pipelines.py:6442`
- `pipelines._populate_contract_from_plan` — `pipelines.py:18535`
- `pipelines._populate_contract_from_plan_safe` — `pipelines.py:18408`
- `pipelines._empty_contract_hitl_question` — `pipelines.py:18247`
- `pipelines._empty_contract_hitl_reason` — `pipelines.py:18287`
- `pipelines._emit_empty_contract_hitl` — invoked at `pipelines.py:21537`
- `pipelines.restart_phase` route — `pipelines.py:3250–3287`
- `pipelines.restart_agent` route — `pipelines.py:2255` area
- `PlanDraftMissingOnLocalError` / `…AndOriginError` / `PopulateProducedEmptyContractError` —
  `pipelines.py:17987 / 18000 / 18043`
- `phases.advance_phase` (calls the wrapper at `phases.py:500`)
- `GatewayClient.create_context_branch` — `gateway_client.py:2327`
- `GatewayClient.create_pr` — `gateway_client.py` (existing)
- `GatewayClient.create_slice_pr` — `gateway_client.py:1491` (~400 lines)
- `GatewayClient.create_slice_integration_branch` — `gateway_client.py`
- `ContextBranchDiverged` — `gateway_client.py:3453`
- `is_slice_branch_merged_into_parent` — `gateway_client.py:1988`
- `SliceScheduler` — `orchestrator/slice_scheduler.py:127`
  - `record_complete`, `record_failure`, `iter_ready`, `mark_spawned`,
    `poll_cascades`, `all_done` (live)
  - `record_cycle`, `teardown_slice`, `respawn_slice`, `cancel_cascade`,
    `hitl_escalator` param (dead — see cq-3)
- `SliceStatus` enum — `shared/egg_contracts/models.py:41–55`
- `PRMetadata` — `shared/egg_contracts/models.py:467`
  - Fields: `title`, `description`, `test_plan`, `manual_steps`,
    `context_title`, `context_description`, `context_branch`,
    `context_pr_number`, `deferred_actions`
- `PeerConsensusTracker` — `orchestrator/peer_consensus.py:69`
  - `_consensus_tracker_namespace()` — `peer_consensus.py:1865` (key pattern
    `{pipeline_id}/{slice_id}`)
  - `get_peer_consensus_tracker(...)`, `tracker.clear()`,
    `remove_peer_consensus_tracker(...)`
- `ConsensusEvaluator` (legacy/deprecated) — `orchestrator/consensus.py:38`,
  `get_consensus_evaluator()` at line 153
- `startup_reconciliation` — `orchestrator/startup_reconciliation.py`
  (esp. lines 312–376 for consensus reconstruction)

**Trusted-CI / gateway service (separate pod, fronts git/gh):**
- `_CONTEXT_BRANCH_RE` — `gateway/gateway.py:1112`
- `_SLICE_INTEGRATION_BRANCH_RE` — `gateway/gateway.py:1103`
- The pipeline-session push-block enforcement points at
  `gateway/gateway.py:1350` and `1362`

**Defaults / config (read once at orchestrator startup):**
- `DEFAULT_SLICE_LOCAL_MAX_CYCLES = 3` — `orchestrator/env_config.py:271`
- `DEFAULT_SLICE_GLOBAL_MAX_CYCLES = 10` — `orchestrator/env_config.py:272`

**On-disk state:**
- Contract JSON: `.egg-state/contracts/<pipeline_id>.json`
- Plan draft: `.egg-state/drafts/<prefix>-plan.md` (prefix from issue or
  pipeline id; resolution in `_get_draft_path` at `pipelines.py:4954`)
- BRC history: `.egg-state/brc-history/<id>-<phase>.{json,md}`

**In-sandbox-agent primitives:** none — all changes in scope are
orchestrator-side host code. Agents do not invoke any of the above
directly; they interact only through MCP verbs and contract reads.

**Human-operator surfaces:**
- MCP `restart_phase`, `restart_agent`, `advance_phase`, `cancel_task`,
  `submit_task`
- HITL decision rendered into `.egg-state/contracts/<id>.json` by
  `_emit_empty_contract_hitl`
- PR-body content emitted by `create_slice_pr` (terminal banner string per
  #2389) and the proposed PR-phase backstop

## Options Considered

### Option A: Full collapse + auto-reconcile + restart hardening + dead-code purge (issue's framing)

**Approach**: Implement the issue as written, sliced according to the
operator's decomposition choice in cq-1:

- Collapse `egg/<id>/context` onto `egg/<id>/work`. The PR phase becomes a
  guaranteed terminal backstop that idempotently opens a single
  `head=egg/<id>/work base=main` PR via one `gh pr list` check.
- Delete the entire context-branch scaffold: `_open_context_pr_for_pipeline`'s
  17 silent return paths, `_lookup_existing_context_pr`'s head-only-match
  handling, `create_context_branch` / `ContextBranchDiverged`,
  `_gather_context_pr_files`, the temp-worktree materialisation, the
  `_maybe_open_base_pr_for_plan_to_implement` wrapper + its five call
  sites + the dedup set, `_resolve_slice_1_context_branch_from_contract`,
  the `_CONTEXT_BRANCH_RE` regex exemption. Drop `context_branch`,
  `context_title`, `context_description` from `PRMetadata` (per cq-2).
  Estimated ~600 lines deleted against ~30 added.
- Close the `plan_draft_missing_on_local` reachability loop (#2792): scope
  set by cq-7 — either root-cause-fix `_sync_worktree_with_remote`'s
  `divergence_rebase_failed` early-return, or add an auto-recover wrapper
  at the HITL gate (fetch + hard-reset + re-run populator), or both. The
  HITL prompt is rephrased: down-weight "Restart plan phase" and drop the
  misleading "step silently failed earlier" framing on the first
  plan_complete event.
- Harden slice and phase restart: extend bootstrap reconciliation to
  handle non-COMPLETE slices (per cq-9: eager-persist
  `parent_branch_at_creation` and/or add merge-base fallback);
  `restart_phase` iterates per-slice consensus trackers; `create_slice_pr`
  gets idempotency (per cq-8); per-slice tracker reconstruction wired
  into `startup_reconciliation` (closing #2409).
- Dead-code purge (per cq-3, cq-5, cq-6): remove unused `SliceScheduler`
  methods + `hitl_escalator` param; remove the legacy `ConsensusEvaluator`;
  drop "umbrella" terminology (#2389 overlap); extract `_is_slice_dag_mode`
  helper.
- Structural decomposition is bounded by cq-10 (aggressive vs surgical vs
  defer-to-#2261). BLE001 / ImportError shim cleanup is bounded by Q2 /
  Q3 in feedback-1.

**Pros**:
- Eliminates the recurring "context PR not opened" failure class by making
  the design idempotent-by-construction; the backstop runs in the PR phase
  every pipeline.
- Net-negative LOC (~600 deletions estimated) addresses the primary goal.
- Closes #2792 with a self-diagnosing recovery path, removes a
  3-option HITL for a case the orchestrator can handle.
- Hardens slice/phase restart against the known gaps (#2409 overlap).
- Removes the deprecated `ConsensusEvaluator` and speculative #2199 hooks
  before more code accretes on top of them.
- Operator controls scope and aggressiveness via cq-1, cq-3, cq-10,
  feedback-1.

**Cons**:
- Schema bump in `PRMetadata` (v1.1 → v1.2) is a real coordination cost
  for in-flight pipelines (cq-2 + Q5).
- Deleting #2199 hooks means re-implementing them when #2199 lands —
  small but non-zero re-work cost.
- Aggregate scope is large; even split across slices each slice is
  meaningful surface to review (especially A+D, which can come to several
  hundred LOC of touched files).
- If the operator picks the most-aggressive cq-7 / cq-9 options, blast
  radius on `_sync_worktree_with_remote` (a load-bearing primitive)
  increases.

### Option B: Symptom-fix-only — patch the trigger bug, defer the architecture realignment

**Approach**: Treat #2777 as the bug it was filed for (context PR not
opened for `issue-2769`). Add one more call site or one more idempotency
check to `_maybe_open_base_pr_for_plan_to_implement` to plug the specific
missed transition; leave the context-branch design intact. Defer the
context-PR collapse, the #2792 auto-reconcile, the restart hardening, and
the dead-code purge to separate issues.

**Pros**:
- Minimal blast radius.
- Ships in days, not weeks.
- Leaves the existing test surface untouched.

**Cons**:
- This is exactly what #2593 → #2744 → #2769 did. The pattern has not
  converged in three iterations. Doing it again is a strictly worse bet
  than the issue's framing.
- Leaves the dead code, the deprecated `ConsensusEvaluator`, and the #2792
  HITL in place — all of which continue to accrete maintenance cost.
- Does not address the issue's primary stated goal ("clean up that
  accumulated mess — not merely patch the bug that triggered the issue").

### Option C: Hybrid — collapse context-PR + auto-reconcile only; defer restart hardening and most cleanup

**Approach**: Do the context-PR collapse (Goal 1 from the issue) and the
#2792 auto-reconcile (Goal 4 from the issue) in this cleanup. Spin
restart hardening (Goal 3), dead-code purge (Goal 2's "favour deletion"
clause), and the umbrella-terminology rename into separate follow-up
issues.

**Pros**:
- Tackles the two highest-leverage changes (the recurring context-PR bug
  and the operator-visible HITL).
- Smaller surface than Option A; faster to land.
- Restart hardening can be sequenced after the context-PR design lands,
  which simplifies its design (no more separate context branch to track
  across restarts).

**Cons**:
- The "clean up the accumulated mess" goal is the issue's stated primary
  goal, not a nice-to-have. Deferring it means the cleanup happens in
  another issue or never.
- Dead-code (the speculative #2199 hooks, the legacy `ConsensusEvaluator`)
  and the umbrella rename are textually coupled to the same files the
  context-PR collapse touches — splitting them creates merge conflicts
  with the follow-ups.
- Two separate refactors of `_run_implement_phase_slices` (this cleanup +
  the deferred restart hardening) is more aggregate churn than doing it
  once.

## Recommended Approach

**Option A**, with the decomposition shape and per-area scope chosen by the
operator via the registered decisions:

- The issue explicitly favours deletion and aligns the scope to the full
  cleanup (Goals 1–4 in the issue body).
- Three prior iterations (#2593, #2744, #2769) have shown that
  symptom-fix-only does not converge for the context-PR class of bugs.
- The dead code and the deprecated `ConsensusEvaluator` are textually
  intertwined with the context-PR scaffold — collapsing the scaffold
  without the surrounding cleanup leaves stale call sites and stale
  observability events. Doing both in one cleanup is materially cheaper
  than two refactors.

Scope and shape are bounded by the operator's answers to cq-1 through
cq-10 and feedback-1. The default reading (if the operator picks the
recommended option per question):

- cq-1: Option 3 (3 slices in parallel — A+D combined, B, C). This caps
  the largest slice (A+D) at the size it would have anyway and keeps B
  and C fully parallel.
- cq-2: Option 2 (deprecated/ignored fields with a v1.2 bump). Lowest-risk
  schema migration for any in-flight pipelines.
- cq-3: Option 1 (delete all five #2199 hooks). Net-negative LOC; #2199
  re-adds with real requirements when it lands.
- cq-4: Option 1 (backstop opens only the context PR). Minimal and
  deterministic; per-slice PR idempotency is handled by cq-8.
- cq-5: Option 1 (delete `ConsensusEvaluator`). The deprecation banner
  is years old; nothing reads it.
- cq-6: Option 1 (subsume #2389). The terminology change is structurally
  part of the collapse, not an independent oneshot.
- cq-7: Option 3 (do auto-recover wrapper now + open a follow-up for the
  root-cause fix). Eliminates the operator-visible HITL today;
  diagnostic-then-fix sequence is safer for `_sync_worktree_with_remote`.
- cq-8: Option 1 (idempotent `gh pr list` pre-flight in `create_slice_pr`).
  Same shape as the new context-PR idempotency; structurally consistent.
- cq-9: Option 3 (both eager-persist + merge-base fallback). Correctness
  fix plus defence-in-depth; cheap.
- cq-10: Option 2 (surgical: just `_is_slice_dag_mode` extraction + what
  the context-PR collapse forces). Defers the full `_run_implement_phase_slices`
  decomposition to #2261 where it belongs.

These are defaults only; the operator's answers govern.

## Open Questions

All open questions are registered as contract decisions / feedback. The
operator answers in the contract; plan-phase agents consume the
resolutions.

### Resolved in Pre-Refine

None — the issue had no `## Additional Context` HITL bundle.

### Multiple-choice decisions

- **cq-1 — Decomposition shape (slices)**: 5 options, A+B+C+D bundling vs
  parallel split. Anchors the entire plan-phase decomposition.
- **cq-2 — PRMetadata schema cleanup**: hard-remove vs deprecate vs
  migrate.
- **cq-3 — SliceScheduler speculative #2199 hooks**: delete all five vs
  keep with markers vs split vs move to a `slice_scheduler_future` module.
- **cq-4 — PR-phase backstop scope**: context-PR only vs also re-verify
  slice PRs vs also reposts program-level body.
- **cq-5 — Legacy `ConsensusEvaluator`**: delete now vs keep+remove
  restart-path `clear()` vs audit-first with deprecation warning.
- **cq-6 — Umbrella terminology rename**: subsume #2389 vs leave
  independent vs split (delete + cosmetic rename).
- **cq-7 — `_sync_worktree_with_remote` divergence**: full root-cause
  fix vs symptom + retry vs both.
- **cq-8 — `create_slice_pr` idempotency**: gateway-method pre-flight vs
  caller-side wrapper vs defer.
- **cq-9 — `parent_branch_at_creation` write timing**: tighten window vs
  merge-base fallback vs both.
- **cq-10 — `_run_implement_phase_slices` decomposition**: aggressive vs
  surgical vs defer to #2261.

### Open-ended feedback (feedback-1)

- **Q1**: Bundle in #2570 (work branch rebased onto main breaks isolation)
  / #2627 (draft missing on both local AND origin) / #2409 (per-slice
  consensus tracker reconstruction)?
- **Q2**: Preferred treatment for the 20 BLE001 swallow-all handlers
  (audit individually vs leave alone vs single `SliceLoopRecoverableError`).
- **Q3**: Are the 9 `except ImportError` dual-path shims still needed
  (test harness compatibility?), or safe to collapse?
- **Q4**: Acceptance bar for "the trigger bug is fixed" — integration test
  for the PR-phase backstop path, or structural deletion sufficient?
- **Q5**: Pipelines currently in flight that this cleanup must remain
  compatible with during deployment (constrains cq-2 and cq-3 choices)?

## Complexity Assessment

**high**. Architectural realignment of the context-PR topology + closing
the #2792 reachability loop + restart hardening + dead-code purge across a
load-bearing 23k-line orchestrator module. Touches contract schema,
gateway policy, multiple MCP verbs (`restart_phase`, `restart_agent`,
`advance_phase`), and the slice scheduler's public surface. The work is
naturally parallelisable into 2–4 slices (cq-1).

---

*Authored-by: egg*
