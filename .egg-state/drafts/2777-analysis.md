# Analysis: Cleanup: sliced implementation phase — context-PR topology and slice/phase restart

> Issue: #2777 | Phase: refine

## Problem Statement

The **sliced implementation phase** of the SDLC pipeline
(`_run_implement_phase_slices` / `_run_one_slice_inner` and surrounding
machinery in `orchestrator/routes/pipelines.py`) has accreted significant
complexity across #2137, #2548, #2593, #2744 and adjacent fixes. The
recurring "context PR not opened" bug (most recently on `issue-2769`,
#2775) is the visible symptom; investigation surfaces four root causes:

1. **Context-PR topology is over-engineered.** A separate
   `egg/<id>/context` branch was introduced in #2548 as a parallel stack
   root. Every downstream piece of complexity exists only to service that
   separate branch — multi-step branch materialisation, two-tier
   idempotency, divergence handling, soft-fail observability, slice-1
   fallback discovery. Each regression (#2593 → #2744 → this issue) added
   *another call site* of the soft-fail wrapper instead of removing the
   fragility, and it has not converged.

2. **The sliced implement phase has accumulated dead and duplicated code.**
   Two functions are over-sized (`_run_implement_phase_slices` 904 lines,
   `_run_one_slice_inner` 434 lines as a nested closure); the
   slice-PR payload assembly is ~145 inline lines; monolithic vs sliced
   branching is duplicated; several public scheduler methods
   (`record_cycle`, `teardown_slice`, `respawn_slice`, `cancel_cascade`,
   `hitl_escalator`) are never called by the run loop; the documented
   two-tier `max_cycles` cap is inert; `# noqa: BLE001` swallow-all
   handlers and dual-path `except ImportError` shims are dense.

3. **Slice and phase restart have known gaps.** The slice scheduler is
   fully in-memory and only recovers `COMPLETE` from contract on pod
   recycle; "agents ran, commits pushed, consensus not reached" is
   silently re-yielded `READY` and re-spawned, hitting a
   non-fast-forward push rejection on the integration branch that
   cascade-fails the subtree. `restart_phase` clears the pipeline-level
   consensus tracker but leaks per-slice trackers
   (`{pipeline_id}/{slice_id}` keys), while `restart_agent` *is*
   slice-aware — an asymmetry. `startup_reconciliation` does not
   reconstruct per-slice trackers (#2409). `parent_branch_at_creation`
   is persisted late; the fallback reconstruction path admits it can
   "silently drift".

4. **The plan→implement reconciliation loop is open** (#2792). When
   `plan_draft_missing_on_local` fires on a clean run and the draft is
   verifiably on origin, the orchestrator forces a 3-option HITL even
   though it already knows enough to auto-reconcile (fetch + reset +
   re-run populator). The HITL prompt also reads "populate-from-plan
   step silently failed earlier" on the very first `plan_complete` event
   — implying #2337's `_sync_worktree_with_remote` divergence is still
   reachable on a clean run; the recurrence loop is open, not closed.

The desired outcome is a net-negative cleanup of the sliced implement
phase: a deterministic context-PR topology that does not need a soft-fail
observability scaffold, a unified slice run loop with smaller functions,
hardened restart so an interrupted sliced implement phase resumes
correctly, and an auto-reconciled plan→implement boundary that only
escalates to HITL when the orchestrator genuinely cannot recover.

## Current Behavior

### Context-PR topology (current)

The orchestrator maintains a *separate* `egg/<pipeline_id>/context`
branch as the parallel stack root for slice PRs. Building it is
multi-step and non-atomic:

- `gateway_client.create_context_branch`
  (`orchestrator/gateway_client.py:2327-2521`) pushes
  `<base_sha>:refs/heads/egg/<id>/context`, raises
  `ContextBranchDiverged` (`gateway_client.py:3453-3480`) when the
  ref exists at a different SHA.
- `_open_context_pr_for_pipeline`
  (`orchestrator/routes/pipelines.py:10002-10553`) is the main hook:
  load contract → fast-path idempotency on
  `contract.pr.context_pr_number` → authoritative `gh pr list` via
  `_lookup_existing_context_pr` (`pipelines.py:9735-9788`) → call
  `create_context_branch` → materialise a temp `git worktree add` →
  copy curated refine/plan artifacts via `_gather_context_pr_files`
  (`pipelines.py:9896-9999`) → commit → push → `gh pr create` →
  persist linkage. **18 distinct `return None` paths** swallow every
  failure mode (lines 10114, 10121, 10131, 10146, 10153, 10160, 10189,
  10221, 10265, 10279, 10373, 10427, 10453, 10461, 10472, 10496, 10502,
  10539).
- Because the create is multi-step and silently fail-able, it is
  wrapped by `_maybe_open_base_pr_for_plan_to_implement`
  (`pipelines.py:10648-10800+`) — a soft-fail wrapper that emits
  `context_pr.skipped` / `context_pr.failed` events to three
  observability sinks (`message_store.add_message`,
  `_emit_pipeline_event`, `report_pipeline_status`) with dedup
  (#2611).
- The wrapper is invoked from **4 distinct call sites** (the issue
  estimated 5): `pipelines.py:15120` (`slice_loop_entry`),
  `pipelines.py:20572` (`implement_entry_backstop`),
  `pipelines.py:22051` (`run_pipeline_autoadvance`), and
  `pipelines.py:22994` (`hitl_resume`). The duplication exists because
  the create is fragile enough that every plan→implement entry path
  has to re-attempt it.
- Slice-1 has its own discovery shim
  `_resolve_slice_1_context_branch_from_contract`
  (`pipelines.py:10883-10910`) that reads `contract.pr.context_branch`
  and falls back to `pipeline_branch` (`egg/<id>/work`) when missing
  — the caller catches exceptions and silently degrades.
- The PR phase **creates zero PRs in slice-DAG mode** —
  `_should_skip_pr_phase_auto_pr` (`pipelines.py:8222-8277`) returns
  `(True, "slice_dag_mode_slice_count=...")` whenever `len(slices) >
  1`. No terminal backstop opens the context PR if every plan→implement
  hook silently failed.
- `PRMetadata` (`shared/egg_contracts/models.py:467-542`) carries four
  context-PR fields: `context_branch`, `context_title`,
  `context_description`, `context_pr_number`. The first three become
  redundant once context PR = `work → main`; `context_pr_number`
  remains the linkage.
- The gateway has a push exemption regex
  `_CONTEXT_BRANCH_RE = re.compile(r"^egg/[A-Za-z0-9][A-Za-z0-9_-]*/context$")`
  (`gateway/gateway.py:1112`, used at lines 1350 and 1362) so the
  synthetic launcher session can push the context branch. It becomes
  dead code once the branch is gone.
- The stacked-PR reconciler (`orchestrator/stacked_pr_reconciler.py`)
  already treats `egg/<id>/work` as the canonical pipeline tip and
  `context_branch` as an *optional preference* in
  `_resolve_extant_new_base` (lines 87-175). The codebase is already
  half on the simpler model.
- Ten "umbrella" references remain in `pipelines.py` (lines 9010,
  9038, 9047, 15608, 15615, 15620, 15686, 15691) and
  `stacked_pr_reconciler.py` (lines 125, 235); the term overlaps with
  #2389.

### Sliced implement phase (current)

- `_run_implement_phase_slices` (`pipelines.py:15013-15916`, **904
  lines**) holds the slice run loop. Its structure:
  - 6 dual-path `except ImportError` shims at lines 15043-15166
    (`SliceScheduler`, `egg_contracts.loader`, `global_slice_admit`,
    `peer_consensus`, `state_store`, plus `message_store` at
    15873-15879). Some try orchestrator-prefixed first, some try
    non-prefixed first.
  - Inner helper `_persist_slice_status_complete` (lines 15168-15202).
  - Bootstrap reconciliation pass (lines 15204-15353, ~150 lines)
    handling COMPLETE-from-contract (Layer A) and
    merged-on-origin-but-not-COMPLETE (Layer B).
  - Nested closure `_run_one_slice` (lines 15354-15362) wrapping
    `_run_one_slice_inner`.
  - Nested closure `_run_one_slice_inner` (lines 15364-15797, **434
    lines**) which:
    - Resolves parent branch (slice-1 via
      `_resolve_slice_1_context_branch_from_contract`; children via
      `f"{issue_branch}/{parent_slice_id}"`).
    - **Persists `parent_branch_at_creation` late** (lines
      15414-15428, under state lock, swallow-all on save error). The
      reconstruction-on-restart path at lines 2634-2652 admits a
      "silently drift" failure mode in a multi-paragraph comment when
      the namespace shape changes without parallel updates.
    - Detects already-merged slices (lines 15430-15476).
    - Creates the integration branch (lines 15478-15523).
    - Runs concurrent phase with impasse retry (lines 15525-15546).
    - Assembles **~145 inline lines of slice-PR payload** (lines
      15564-15708): terminal-slice detection, position marker
      derivation, `files_affected` union, dict literal with
      `context_pr_number` fallback (covers #2744 regression) and
      `program_deferred_actions` collection.
    - Calls `gateway.create_slice_pr` with **no "PR already open"
      idempotent check** (`gateway_client.py:1491-1742`); on any
      exception the slice is marked FAILED at line 15784-15789, even
      when consensus succeeded and commits are pushed.
  - Main slice-loop polling (lines 15799-15914) with
    `ThreadPoolExecutor`, cascade drainage, `OVERSEER_ALERT` emission.
- `slice_count > 1` is recomputed by re-loading the contract at **3
  distinct sites** with no shared helper:
  - `pipelines.py:21134` (`_use_slice_loop` gate in `_run_pipeline`)
  - `pipelines.py:8275` (`_should_skip_pr_phase_auto_pr`)
  - `pipelines.py:15627` (slice-PR position marker)
- 15+ `# noqa: BLE001` swallow-all handlers in the slice code (lines
  15131, 15196, 15274, 15336, 15386, 15422, 15451, 15501, 15709,
  15742, 15775, 15795, 15841, 15901, 15910). Many would benefit from
  granular `except (SpecificError, AnotherError)` rewrites.
- Multi-paragraph archaeology comments narrating closed-issue history
  (e.g. #2549 race-protection note at 15430-15440, #2538 narrative
  fallback at 15599-15604, #2744 context-PR fallback at 15661-15669,
  #2758/#2755 BRC history fallback at 15717-15731).
- The monolithic implement path is gated at line 21192 by
  `if _use_slice_loop` and runs through `_run_concurrent_phase`
  directly; the sliced path runs through
  `_run_implement_phase_slices`. Common pre/post-phase logic is
  duplicated.

### Slice scheduler and restart (current)

- `SliceScheduler` (`orchestrator/slice_scheduler.py:127-591`) is
  fully in-memory: a `dict[str, SliceRuntime]` plus
  `_global_cycles`, `_pending_cascades`, and a `_hitl_escalator`
  callback slot.
- `record_cycle` (lines 299-344) computes "cap tripped" and *would*
  call `hitl_escalator(slice_id, reason)` — but the run loop never
  passes the callback (constructor at `pipelines.py` ~15145 does
  `SliceScheduler(contract)`, so `hitl_escalator` defaults to
  `None`) and **the run loop never calls `record_cycle` at all**.
- `teardown_slice` (417-432), `respawn_slice` (434-459), and
  `cancel_cascade` (375-378) are public, unit-tested, and unused by
  the run loop. They are speculative #2199 hooks.
- The two-tier `max_cycles` cap (`DEFAULT_SLICE_LOCAL_MAX_CYCLES=3`,
  `DEFAULT_SLICE_GLOBAL_MAX_CYCLES=10` in
  `orchestrator/env_config.py:269-272`) is detected by
  `record_cycle` but has no run-loop caller to escalate — effectively
  inert.
- Bootstrap reconciliation (`pipelines.py:15204-15353`) covers:
  - Layer A: `SliceStatus.COMPLETE` on contract → mark complete
    in-memory.
  - Layer B: merged-on-origin → mark complete on contract.
  - **Gap:** "agents ran, commits pushed, consensus not reached" is
    neither A nor B. Scheduler re-yields READY → re-spawn → integration
    branch push hits non-fast-forward → slice marked FAILED → cascade
    timer fires → subtree cascade-failed.
- `create_slice_pr` has no "PR already open" check; a transient `gh`
  failure marks the slice FAILED post-consensus even with commits
  pushed.
- `restart_phase` (`pipelines.py:2966-3338`) clears the pipeline-level
  tracker (`get_peer_consensus_tracker(pipeline_id)` at lines
  3259-3261) but does not iterate per-slice tracker keys
  (`{pipeline_id}/{slice_id}`) — those survive in CONFIRMED state and
  wedge re-spawned agents.
- `restart_agent` (`pipelines.py:2253-2963`) **is** slice-aware:
  takes `slice_id` from body/query, auto-derives if omitted,
  validates against contract, and calls
  `get_peer_consensus_tracker(pipeline_id, slice_id).remove_agent(role)`
  at lines 2822-2824. The asymmetry with `restart_phase` is the bug.
- `startup_reconciliation`
  (`orchestrator/startup_reconciliation.py:291-383`) reconstructs only
  the pipeline-level tracker
  (`reconstruct_tracker_from_messages(pipeline_id, graph)` at line
  312, with no `slice_id` arg). Per-slice trackers are never
  reconstructed on orchestrator restart (#2409 territory; comment at
  lines 333-341 acknowledges the limitation).
- Legacy `ConsensusEvaluator` (`orchestrator/consensus.py:1-161`) is
  module-headered as `DEPRECATED ... superseded by peer_consensus.py
  (BRC protocol)` but is still reset on every restart path
  (`restart_phase` line 3279, `restart_agent` line 2849, `_clear_consensus`
  line 1817).

### Plan→implement transition (current)

- `_sync_worktree_with_remote` (`pipelines.py:6442-6820`, introduced
  in #2337) runs fetch → divergence-check → push/rebase/reset to
  bring the worktree in sync. Two callers: line 19855 (phase startup)
  and line 21416 (post-PLAN, before `_populate_contract_from_plan_safe`).
- `_populate_contract_from_plan` (`pipelines.py:18535-18790+`) reads
  plan draft markdown, parses tasks, writes to contract. Called via
  `_populate_contract_from_plan_safe` (line 18408) from line 21457
  (plan-complete) and 20271 (start-phase safety net).
- `PlanDraftMissingOnLocalError` (line 17991) and
  `PlanDraftMissingOnLocalAndOriginError` (line 18006, the #2627
  follow-up) are caught at lines 21507-21587. The handler:
  - Marks pipeline/phase FAILED.
  - Emits the dedicated empty-contract HITL via
    `_emit_empty_contract_hitl` with `gate="plan_complete"`,
    `reason="plan_draft_missing_on_local"` (or `..._and_origin`).
- The HITL question (built at `pipelines.py:18202-18259`) reads:
  > "...The populate-from-plan step silently failed earlier (#2337 /
  > #2627), so pipeline state and the contract have diverged."
- Options (`_EMPTY_CONTRACT_HITL_OPTIONS`, lines 18158-18162):
  `["Repopulate contract from plan draft and retry", "Restart plan
  phase", "Abort pipeline"]`.
- On a clean run where the draft is intact on origin, the orchestrator
  *already* knows enough to run `git fetch` + `git reset --hard
  origin/<branch>` + re-invoke `_populate_contract_from_plan_safe`,
  but does not — it surfaces all three options for operator triage.
  This is the gap #2792 names.

## Constraints

- **Single repository, single file is dense.** Most of the cleanup
  touches `orchestrator/routes/pipelines.py` (23,318 lines). Extraction
  needs to land alongside the changes that motivate it, or it risks
  drifting from #2261.
- **State persistence shape is fixed by contract schema.** Changes to
  `PRMetadata` (`shared/egg_contracts/models.py`) need migration
  handling because in-flight pipelines have already-persisted contracts
  with `context_branch` / `context_title` / `context_description`
  populated. Field removal must keep the loader tolerant of legacy
  values for one release cycle, or eagerly drop them on contract load.
- **Gateway push policy is a security surface.** Removing
  `_CONTEXT_BRANCH_RE` (`gateway/gateway.py:1112`) must come AFTER
  every caller stops pushing to `egg/<id>/context`. The exemption is
  also referenced at lines 1350 and 1362; all three sites need to
  disappear together with the corresponding `create_context_branch`
  call.
- **Slice integration branches collide with #2570.** The pipeline
  `egg/<id>/work` branch is being silently rebased onto main as fresh
  commits with new SHAs, which breaks the model where context PR =
  `work → main`. The rebase-source needs to be diagnosed and stopped
  before the topology realignment is merged, or the new context PR
  will lose its parent reference and the rebase-driven slice failures
  documented in #2570 (`issue-2548`'s slice-1 incident) will recur.
- **The legacy monolithic implement path is still alive** behind the
  `_use_slice_loop` gate. Unifying it with the sliced path is in
  scope, but must keep the single-slice (≤1) case working — single
  PRs from monolithic implement should still merge cleanly without a
  context PR.
- **`#2199` (per-slice MCP controls) is independent work.** This
  refine surfaces dead-code candidates that were originally added in
  anticipation of #2199. Deleting them now and re-adding them when
  #2199 lands is one option; leaving them is another. Either choice
  needs a reviewer sign-off so #2199 is not blocked.
- **Existing in-flight pipelines (`issue-2769`, `pipeline-f82240dc`)
  must not be retroactively broken.** Both ran with the current model
  and have artifacts on disk; the bootstrap reconciliation pass must
  remain backwards-compatible with their persisted shapes.
- **The work is net-negative in lines.** The issue body estimates ~600
  lines deleted vs ~30 added. The plan phase will need to keep the
  decomposition consistent with that estimate to avoid scope creep
  (e.g. extracting into a new module without deleting the original
  inline code).

## Options Considered

The four goals are coherent and mutually reinforcing — context-PR
realignment removes the soft-fail scaffold; the resulting smaller
surface makes the slice run loop tractable; the unified loop makes
restart hardening cheaper; the auto-reconciled plan→implement boundary
sheds the remaining HITL gates that exist because populate could
silently fail. The real question is **how to decompose the work into
slices** (the plan phase's job) and **what to fold in vs leave for
follow-ups**.

The slice scheduler runs sibling slices in the same wave in parallel,
so slice count = PR count by construction. The DAG shape matters more
than the absolute count.

### Option A: Single slice — ship topology + cleanup + restart + #2792 together (1 PR)

**Approach**: One slice covering all four goals. Context PR
realignment, dead-code deletion, slice-PR payload extraction,
`_is_slice_dag_mode` helper, restart hardening (per-slice tracker
reconstruction, `restart_phase` slice-awareness, "agents-pushed-but-
no-consensus" recovery), plan→implement auto-reconciliation, HITL
re-phrasing, terminology cleanup, and #2792 closure all in one diff.

**Pros**:
- Maximises deletion: removing the context-PR scaffold without
  simultaneously removing the call sites and PRMetadata fields would
  leave the codebase in an inconsistent state for a release cycle.
- Eliminates ordering risk between goals — e.g. the slice-PR payload
  extraction (#2261 candidate) depends on context-PR realignment to
  know whether `context_pr_number` is still a meaningful fallback.
- The PR body becomes the canonical incident retrospective for
  #2548 → #2593 → #2744 → this issue.

**Cons**:
- One large diff is hard to review and revert.
- Cascading failure if any one goal hits an unforeseen blocker.
- Long lived branch increases risk of #2570-style rebase contamination.

### Option B: Two slices in parallel — [topology realignment + cleanup] || [restart + plan-implement reconciliation] (2 PRs)

**Approach**:
- **Slice A**: Goals 1 + 2. Realign context PR to `work → main`,
  delete the scaffold (`_open_context_pr_for_pipeline` collapse,
  `_maybe_open_base_pr_for_plan_to_implement` removal,
  `_gather_context_pr_files` removal, gateway
  `_CONTEXT_BRANCH_RE` removal, `create_context_branch` removal,
  `ContextBranchDiverged` removal, `PRMetadata` field deprecation,
  "umbrella" terminology drop, PR-phase terminal backstop, three-site
  `_is_slice_dag_mode` consolidation, slice-PR payload assembly
  extraction).
- **Slice B**: Goals 3 + 4. Slice/phase restart hardening
  (`restart_phase` per-slice awareness, per-slice tracker
  reconstruction in `startup_reconciliation`, "agents-pushed-but-no-
  consensus" bootstrap-layer C, `create_slice_pr` idempotent "PR
  already open" check, late-persistence fix for
  `parent_branch_at_creation`, dead-code deletion for vestigial
  scheduler methods, `ConsensusEvaluator` removal) plus
  plan→implement auto-reconciliation (HITL re-phrasing,
  `_sync_worktree_with_remote` divergence diagnosis, auto-recovery
  path).

**Pros**:
- Both slices touch `pipelines.py` but in non-overlapping function
  ranges (A ≈ context-PR + payload assembly; B ≈ scheduler + restart
  + plan-complete handler), so parallel work is plausible.
- Reviewer load halved; each slice tells one coherent story.
- Slice B does not need the realigned topology to land first — the
  per-slice tracker leak and bootstrap gap are independent of the
  context-PR model.

**Cons**:
- Coordination cost: both slices touch the slice-PR payload
  assembly (Slice A extracts; Slice B may modify it for restart-time
  PR linkage). Plan phase must scope the extraction so both can
  proceed without merge conflicts.
- "Net-negative lines" is harder to attribute across two PRs.
- The `_is_slice_dag_mode` helper sits at the boundary of both
  slices; whichever lands first defines its signature.

### Option C: Three slices fully parallel — [topology] || [implement-phase cleanup] || [restart + #2792] (3 PRs)

**Approach**:
- **Slice A**: Context-PR realignment only. Realign topology, delete
  scaffold, terminology cleanup, schema cleanup, gateway exemption
  removal, PR-phase backstop.
- **Slice B**: Implement-phase cleanup only. Slice-PR payload
  assembly extraction, `_is_slice_dag_mode` helper, dead-code
  deletion (`record_cycle`, `teardown_slice`, etc.), archaeology
  comment audit, `# noqa: BLE001` audit, monolithic↔sliced unification.
- **Slice C**: Restart hardening + plan→implement reconciliation.
  `restart_phase` per-slice awareness, per-slice tracker
  reconstruction, "agents-pushed-but-no-consensus" recovery,
  `create_slice_pr` idempotency, `parent_branch_at_creation` early
  persistence, plan→implement auto-recovery, HITL re-phrasing.

**Pros**:
- Three coherent stories, three reviewable diffs.
- Maximises parallelism — three independent slices in one wave.

**Cons**:
- Slice A and Slice B both delete code from the same regions of
  `pipelines.py` (context-PR scaffold lives in the same file as the
  implement-phase cleanup targets). Three-way merge conflict risk is
  high.
- Slice B's "dead code deletion" includes the dual-path import shims;
  those depend on whether Slice C has landed (the shims wrap
  consensus/state-store imports).
- The slice-PR payload assembly extraction crosses Slice A and
  Slice B boundaries; whoever extracts first owns the API.

### Option D: Two slices with dependency — [topology realignment] → [cleanup + restart + #2792] (2 PRs)

**Approach**:
- **Slice A** (root): Goal 1 only — context-PR realignment, scaffold
  deletion, schema field deprecation/removal, terminology cleanup,
  gateway exemption removal, PR-phase backstop.
- **Slice B** (depends on A): Goals 2 + 3 + 4. Cleanup, restart
  hardening, plan→implement reconciliation, dead-code deletion,
  `_is_slice_dag_mode` consolidation, slice-PR payload assembly
  extraction.

**Pros**:
- Slice A is a clean topology change that can land first and stabilise
  the bug surface.
- Slice B inherits the simplified slice-PR payload (no
  `context_pr_number` fallback, no `context_title`/`description`
  branching) and can extract the smaller-surface assembly.
- Sequential dependency removes most merge-conflict risk between
  slices.

**Cons**:
- Sequential means longer wall-clock time.
- Slice A is still a large diff (deletion-heavy, but spans
  `pipelines.py`, `gateway/gateway.py`, `shared/egg_contracts/models.py`,
  `stacked_pr_reconciler.py`).
- The plan phase has to define a stable "topology landed" point for
  Slice B to start from; in-flight migration of `PRMetadata` legacy
  fields complicates that boundary.

## Recommended Approach

**Option D (two slices with dependency: topology → cleanup+restart+#2792)** for
these reasons:

1. The context-PR realignment is the **prerequisite** for honest
   cleanup. Until `egg/<id>/context` is gone, the slice-PR payload
   still needs the `context_pr_number` fallback (line 15670-15671)
   and the slice-1 resolver still needs the
   `_resolve_slice_1_context_branch_from_contract` shim. Extracting
   the payload assembly before topology realignment locks the
   extraction's API to a soon-dead shape.
2. The cleanup, restart hardening, and #2792 work are deeply
   interconnected — they all touch `_run_implement_phase_slices`,
   `_run_one_slice_inner`, the bootstrap reconciliation pass, and
   the post-plan-complete handler. Splitting them into separate
   slices (Option C) maximises three-way merge risk in the same file
   regions.
3. The slice-DAG `slice_count > 1` gate (the
   `_is_slice_dag_mode` candidate) and the per-slice tracker leak
   in `restart_phase` are slice-mechanics bugs that don't depend on
   the context-PR shape. Bundling them with the topology change
   (Option A) makes the diff too coarse to review.
4. Option D's PR-1 (topology) is mostly **deletions** and is the
   right shape for a focused, fast-to-review PR; PR-2 (cleanup +
   restart + #2792) is a larger but still coherent body of work whose
   reviewers understand the topology is already simplified.

Option D is sequential, which costs wall-clock time, but the
sequential dependency is genuine — PR-2's extraction targets shrink
materially once PR-1 lands. Forcing parallelism (Option B or C)
re-introduces the cross-slice ordering problems the new topology was
designed to eliminate.

The plan phase will need to decide how `PRMetadata` field deprecation
is staged across the two slices: drop in PR-1 vs deprecate-in-PR-1-then-
remove-in-PR-2 has different in-flight pipeline compatibility
implications.

## Open Questions

<!-- egg-hitl-decision id=cq-1 -->

**How should this work be decomposed into slices?**

- [ ] Single slice: topology + cleanup + restart + #2792 ship together (1 PR)
- [ ] Two slices in parallel: [topology + cleanup] || [restart + #2792] (2 PRs)
- [ ] Two slices with dependency (recommended): [topology realignment] -> [cleanup + restart + #2792] (2 PRs)
- [ ] Three slices fully parallel: [topology] || [implement-phase cleanup] || [restart + #2792] (3 PRs)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-2 -->

**Should vestigial scheduler hooks (record_cycle, teardown_slice, respawn_slice, cancel_cascade, hitl_escalator) be deleted now or kept for #2199?**

- [ ] Delete now (favours net-negative cleanup goal in this issue)
- [ ] Keep — #2199 (per-slice MCP controls) will re-add them; deletion + re-add churns the public API
- [ ] Delete the unused ones (record_cycle / teardown_slice / respawn_slice / cancel_cascade / hitl_escalator param) but keep max_cycles plumbing for cheap re-wiring
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-3 -->

**How should the legacy ConsensusEvaluator (orchestrator/consensus.py, marked DEPRECATED in module header) be handled?**

- [ ] Delete now — superseded by peer_consensus.PeerConsensusTracker; reset sites in restart_phase / restart_agent / _clear_consensus can drop the evaluator clear
- [ ] Keep — leave the module and its three reset sites untouched for this issue
- [ ] Delete only the resets in restart paths; leave the module for a follow-up cleanup
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-4 -->

**Should per-slice consensus tracker reconstruction in startup_reconciliation (#2409) be closed by #2777?**

- [ ] Yes — implement per-slice tracker reconstruction here; closes #2409 alongside #2777
- [ ] No — leave #2409 as a separate work item; #2777's restart hardening covers restart_phase per-slice awareness only
- [ ] Implement the reconstruction loop but leave #2409 open for the deeper per-slice replay edge cases (#2422 caveat)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-5 -->

**How should PRMetadata schema fields (context_branch, context_title, context_description) be retired? (context_pr_number stays as the linkage.)**

- [ ] Remove fields outright in the topology PR; loader tolerates unknown keys via pydantic extras
- [ ] Mark fields as deprecated in the topology PR; remove in a follow-up after one release cycle
- [ ] Migrate-and-remove: topology PR adds a contract-load migration that drops legacy values, then removes the fields
- [ ] Keep context_title / context_description (planners optionally use them for context-PR framing); remove only context_branch
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-6 -->

**When auto-recovery for plan_draft_missing_on_local fails (fetch+reset+repopulate cannot bring the contract back), what is the fallback?**

- [ ] Surface the existing 3-option HITL (repopulate / restart-plan / abort), but down-weight 'Restart plan phase' relative to 'Repopulate'
- [ ] Surface a 2-option HITL (repopulate / abort); remove 'Restart plan phase' since auto-recovery exhausted the repopulate path
- [ ] Fail-loud with OVERSEER_ALERT and no HITL — pipeline goes FAILED; operator restarts via existing endpoints
- [ ] Retry auto-recovery once with exponential backoff before any HITL fallback
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-7 -->

**What is the strategy for the 'agents ran, commits pushed, consensus not reached' bootstrap-gap (non-fast-forward push rejection on slice integration branch after restart)?**

- [ ] Add a Layer-C bootstrap detector: if the integration branch tip is ahead of the parent on origin AND no slice PR is open, attempt to resume consensus from the pushed state (rebuild peer_consensus tracker, re-spawn agents pointed at the existing branch)
- [ ] Make create_slice_integration_branch idempotent: if the branch exists with commits, no-op the parent-sha push and let the run loop proceed (lets the run loop discover state via the existing peer_consensus reconstruction path)
- [ ] Mark the slice FAILED on detect; require operator restart_agent to recover (preserves existing semantics but the operator gets a clearer cascade signal)
- [ ] Combination: Layer-C detector + idempotent integration-branch push
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-8 -->

**Does #2777 need to resolve the #2570 interaction (work branch silently rebased onto main breaks the new context-PR model)?**

- [ ] Yes — diagnose and stop the silent rebase as part of the topology realignment; the new model is unsafe without it
- [ ] Partially — add a guard that detects work-branch rebase and blocks the slice, escalating to #2570 for the actual fix
- [ ] No — #2570 is a separate root-cause investigation; #2777 ships assuming work-branch isolation is intact
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-9 -->

**Should the slice-PR payload assembly extraction (the ~145-line dict literal in _run_one_slice_inner) be part of #2777, or deferred to #2261 (pipelines.py decomposition)?**

- [ ] Extract here — the simpler-surface form (no context_pr_number fallback, no terminal-narrative differences) is the right API to extract
- [ ] Defer to #2261 — keep the inline assembly in #2777 to minimise churn; let #2261 do the systematic file decomposition
- [ ] Extract a thin helper here (assemble_slice_pr_data) but leave deeper module-level reorg to #2261
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-10 -->

**Should the PR-phase terminal backstop (open egg/<id>/work -> main PR if still missing) be added unconditionally, including for single-slice / monolithic pipelines?**

- [ ] Yes — always check / open at PR-phase entry; removes the slice-DAG vs non-slice-DAG asymmetry in _should_skip_pr_phase_auto_pr
- [ ] Slice-DAG only — preserve the existing monolithic auto-PR path; the backstop only fires when slice_count > 1
- [ ] Single-slice pipelines should also use the new model — collapse monolithic auto-PR into the same idempotent op
- [ ] Other (explain in reply)

<!-- egg-hitl-feedback id=feedback-1 -->

**Open-ended feedback:**

- **Q1:** Are there in-flight pipelines on jwbron/egg (or other instances) whose persisted contracts have context_branch / context_pr_number set and that must keep working through this migration? If so, list their pipeline IDs.
- **Q2:** Are there constraints on touching gateway/gateway.py (e.g. policy-review gate, separate deploy cadence) that affect whether the _CONTEXT_BRANCH_RE exemption can be removed in the same PR that stops calling create_context_branch?
- **Q3:** Is 'context PR' or 'base PR' the canonical name to land on after the umbrella drop? The issue body uses both; the codebase should pick one.
- **Q4:** Is the wall-clock cost of sequential slices (Option D) acceptable, or is parallel work (Option B/C) preferred despite the merge-conflict risk surfaced in the analysis?

## Complexity Assessment

**high** — this work spans architectural change (context-PR topology
realignment), cross-cutting cleanup (multi-hundred-line surface in
`pipelines.py`, plus `gateway/gateway.py`, `stacked_pr_reconciler.py`,
`shared/egg_contracts/models.py`, `slice_scheduler.py`,
`peer_consensus.py`, `startup_reconciliation.py`), restart hardening
that touches consensus state, and the plan→implement reconciliation
loop. Net-negative in lines (~600 deletions vs ~30 additions) does not
reduce the cognitive load: the diff crosses multiple subsystems whose
interactions are subtle.

---

*Authored-by: egg*
