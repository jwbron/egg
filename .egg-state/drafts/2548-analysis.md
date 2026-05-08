# Analysis: Slice PRs are missing analysis/plan docs and all BRC history; need a 'context' PR for refine+plan phases plus per-slice BRC in each slice PR

> Issue: #2548 | Phase: refine

## Problem Statement

When a multi-slice pipeline lands on GitHub today, reviewers approaching any slice PR see only the slice's code-level diff. They cannot see (a) the refine-phase analysis document, (b) the plan-phase plan document, or (c) the BRC consensus history (proposals, NACKs, ACKs, CONFIRMs) that produced any of it. All of those artifacts are committed to a side branch (`egg/<id>/work`) that is not part of any slice PR's review surface against `main`.

The desired outcome: every PR that reviewers approach carries enough in-tree, in-diff context to understand the strategic decision (refine + plan) and the consensus that approved each artifact (BRC histories) — without external state archaeology.

Concretely the issue calls for two new mechanisms:

1. A "context" / "preface" PR (new branch `egg/<id>/context` based on `main`) that ships the program-level analysis, plan, and refine/plan BRC histories.
2. Per-slice implement-phase BRC histories (`.egg-state/brc-history/<id>-implement-slice-<N>.{json,md}`) committed to each slice's integration branch as a final commit before the slice PR is opened, so the slice PR's diff includes its own BRC consensus record.

## Current Behavior

**Where artifacts live today** (verified against the codebase):

- Refine and plan agents write their outputs to `.egg-state/drafts/<id>-{analysis,plan}.md` on the agent's worktree, and `_commit_statefiles_to_worktree()` (`orchestrator/routes/pipelines.py:7179-7318`) commits them to the umbrella **work branch** `egg/<id>/work` (line 5141-5203 fetch them from per-agent worktrees during phase transitions).
- BRC history is written by `_write_brc_history()` (lines 8110-8228) to `.egg-state/brc-history/<id>-{phase}.{json,md}` and committed at phase boundaries by `_persist_phase_brc_history()` (lines 8355-8392). The implement-phase file is a **single, cross-slice aggregate** keyed by phase name only — there is no per-slice split today.
- Slice integration branches are created by `_run_one_slice_inner()` (lines 12405-12454) via `gateway.create_slice_integration_branch()`. The base branch resolution (lines 12407-12410) is:
  - Slice-1 (root): `parent_branch = pipeline_branch` → `egg/<id>/work`
  - Slice-N (N>1): `parent_branch = f"{issue_branch}/{parent_slice_id}"` → previous slice's integration branch.
- Slice PRs are opened by `create_slice_pr()` (lines 12631-12648) with `base = parent_branch` and `head = integration_branch`. After #2541 the PR author is `orchestrator`; after #2543 every slice PR (terminal and non-terminal) carries the program narrative from `contract.pr.{title,description,test_plan,manual_steps}`.
- The stacked-PR reconciler (`orchestrator/stacked_pr_reconciler.py`) heals orphan child PRs after a parent merges by walking up the slice DAG via `_resolve_extant_new_base()` (lines 87-132). The fallback when the entire ancestor chain is gone is `pipeline_branch` (`egg/<id>/work`) — **not** `main`.

**Concrete reproduction** — slice-1 PR [#2533](https://github.com/jwbron/egg/pull/2533) of pipeline `issue-2474-v2`:

- `baseRefName = "egg/issue-2474-v2/work"`, `headRefName = "egg/issue-2474-v2/slice-1"`. Confirmed merged into `work`, never directly into `main`.
- 21 files in the diff, **zero** of which are under `.egg-state/drafts/` or `.egg-state/brc-history/`.
- PR body has the program description (post-#2543) and a task bullet list — but no link to `2474-analysis.md`, `2474-plan.md`, or any BRC consensus record.
- `2474-analysis.md` (committed at `0c92bab7d` during refine) and `2474-plan.md` (committed at `6d325e9f4` during plan) are present on `egg/issue-2474-v2/work` but invisible to anyone reviewing `#2533` against `main`.

**The deeper issue surfaced by this analysis**: there is no automatic mechanism today to merge `egg/<id>/work` into `main` at all. Slice PRs target the work branch (or each other in the stack), and the orphan reconciler retargets up to the work branch as a fallback. The contract has no `pr.context_branch` / `pr.context_pr_number` field. The work-branch-as-permanent-base appears to be an unintended state — none of the docs / BRC artifacts ever reach `main` under the current setup. Any solution to #2548 has to also implicitly answer "how does this content reach `main`?" (see Open Questions Q1).

## Constraints

**Technical**

- **Gateway-enforced file boundaries**: roles cannot push files outside their allowlist. Coder/tester cannot push under `.egg-state/brc-history/` or `.egg-state/drafts/` — only the orchestrator (or a refiner/planner role) can. The "commit BRC history to slice integration branch" step in the proposal must be orchestrator-authored.
- **BRC history is currently aggregate, not per-slice** (`{id}-implement.{json,md}`). Per-slice splitting requires a code change in `_write_brc_history()` so writes are routed to `{id}-implement-slice-<N>.{json,md}` when a slice context is in scope, plus a corresponding read-side change in `_rewrite_brc_history_for_pr()` (lines 8265-8328).
- **Stacked-PR reconciler invariants**: the reconciler assumes parent → child base relationships drawn from `slice.dependencies`. If we insert a context PR as the new root (slice-1 base = `egg/<id>/context`), the reconciler's `_resolve_extant_new_base()` fallback (`pipeline_branch`, line 132) needs to be reconsidered — the new root of the chain should fall back to the context branch, not the work branch.
- **Contract model**: `PRMetadata` (`shared/egg_contracts/models.py:371-395`) has no `context_pr_number` / `context_branch` / `context_title` / `context_description` fields today. Adding them requires a contract schema bump.
- **HITL gate / merge ordering**: the terminal slice carries `deferred_actions` (`contract.pr.deferred_actions`) that block merge until obligations resolve. If we put the context PR at the bottom of the stack, the merge order becomes context → slice-1 → … → slice-N (terminal). The deferred-action gate stays on slice-N.
- **Orphan reconciler latency**: it runs on a ~30s cadence (`stacked_pr_reconciler.py:1-324`); inserting another PR layer adds one more level the reconciler must walk after parent merges.

**Business / scope**

- The issue is reported against an in-flight pipeline (`issue-2474-v2`). Whether the fix backports retroactively or is forward-only changes scope significantly (see Q4 / decision-4).
- This change is adjacent to #2534 (slice PR titles + attribution), which has been partially fixed in #2541 and #2543. Some of the proposal text (e.g. "drop the duplicated 'slice slice-1' prefix") is already addressed; the remaining surface is the missing-context problem itself.

**Dependencies**

- Depends on `_commit_statefiles_to_worktree()` and the gateway's `create_slice_integration_branch()` flow staying as the canonical commit/branch primitives.
- Adjacent to: #2534 (slice PR titles + attribution; partially fixed), #2538 (program narrative on every slice; merged), #2354 (deferred actions on umbrella PR), #2543 (PR body rendering).

## Options Considered

### Option A: Dedicated context PR, slice-1 stacks on context

**Approach**: After plan_gate approval, the orchestrator creates `egg/<id>/context` from `main`, cherry-picks (or rebases) the analysis/plan/refine-BRC/plan-BRC commits onto it, and opens a PR with base=`main`, head=`egg/<id>/context`. Slice-1's integration branch is then created from `egg/<id>/context` (instead of `egg/<id>/work`). Slice-N>1 stacks on slice-(N-1) as today. Each slice's implement-phase BRC is committed to its integration branch as a final orchestrator-authored commit before the slice PR opens. Merge order: context PR first → slice-1 → … → slice-N (terminal). The orphan reconciler's fallback for the bottom-most slice changes from `pipeline_branch` to the context branch.

**Pros**:

- Reviewers approaching any PR can navigate up the stack to the context PR for strategic background.
- Analysis/plan/BRC docs reach `main` once the context PR merges — closes the discoverability gap for future readers using `git log` / `git blame`.
- Replay-able: a pipeline that restarts can rebuild context by reading in-tree BRC history rather than ephemeral `.egg-state/`.
- Cleanly separates "this is the program-level rationale" from "this is one slice's code change".
- Dovetails with the existing stacked-PR reconciler: it already handles multi-PR stacks where parents merge before children.

**Cons**:

- Adds a new PR type with its own state machine (creation, retry on failure, what to do if a human refuses to merge it before slicing).
- Schema change: contract grows `pr.context_branch` / `pr.context_pr_number` (or similar) so slice provisioning can find the right base.
- Per-slice implement-BRC split is mandatory (otherwise slice PR diffs still wouldn't carry their BRC).
- Modifies the slice-1 base resolution code path that recently stabilized after #2535 (slice-N consensus inheritance) and #2532 (gateway boundary fix).

### Option B: Embed in slice-1's diff (no separate context PR)

**Approach**: Continue creating slice-1 from `egg/<id>/work`, but commit `2548-analysis.md`, `2548-plan.md`, `2548-refine.{json,md}`, `2548-plan.{json,md}` on top of slice-1's integration branch as final orchestrator-authored commits before slice-1's PR is opened. Per-slice implement-BRC files are also committed to each slice's branch (same as Option A). No new branch, no new PR.

**Pros**:

- Smallest delta from current orchestrator code: reuse `_commit_statefiles_to_worktree()` against the slice integration branch.
- No contract schema change.
- No new state machine for "context PR" lifecycle.
- Slice-1 PR's "Files changed" tab now shows analysis + plan + refine/plan BRC + slice-1 code together.

**Cons**:

- Strategic context is bundled with one slice's code change; reviewers approaching slice-2+ first still don't see it in the slice's own diff.
- Mixes concerns in slice-1's PR: docs review and code review become one workflow.
- Does not solve the root problem that `egg/<id>/work` is never merged to `main` (see Q1) — analysis/plan still arrive on main as part of slice-1's eventual cascade-merge, not as a standalone reviewable artifact.
- If slice-1 fails or is dropped, the docs disappear too.

### Option C: Embed in terminal slice's diff (co-locate with program narrative)

**Approach**: Commit analysis + plan + all BRC histories (refine, plan, implement-aggregate) onto the terminal slice's integration branch. Terminal-slice PR already carries the program-level narrative (post-#2543) and the merge-gate banner; co-locating the docs there centralizes "the program ends here" in one PR.

**Pros**:

- Reuses the existing "terminal slice is special" mechanism (#2354 deferred actions, #2543 program narrative).
- One PR carries the full program context at merge time.
- Lowest schema/code surface.

**Cons**:

- Reviewers see strategic context only after they've already reviewed N-1 code slices — the inverse of the issue's desired flow.
- For a 5-slice pipeline, slice-1 reviewer still has zero context until they navigate to PR-N.
- Terminal slice PR becomes large and mixed-purpose.

### Option D: Render context into slice PR bodies (no in-diff files)

**Approach**: Don't commit any new files into slice diffs. Instead, the orchestrator renders a "Strategic context" section into each slice PR body containing inline links / inline excerpts of the analysis, plan, and BRC consensus summary.

**Pros**:

- Zero git diff change.
- No new branches, no new PRs.
- Discoverability via PR body (already where reviewers look first).

**Cons**:

- Not durable in `git log` / `git blame`. Future readers cannot use `git show <commit>` to retrieve the analysis that motivated a change — the PR body is GitHub-only.
- Doesn't satisfy the issue's "auditability via git" requirement.
- BRC history doesn't survive PR body length limits / character escaping.

## Recommended Approach

**Option A (dedicated context PR) + per-slice implement BRC files** is the recommended approach, contingent on operator selection in decision-1.

Rationale:

1. **It is the only option that durably lands the strategic context on `main`.** Once the context PR merges, `git log -- .egg-state/drafts/` and `git log -- .egg-state/brc-history/` produce a real history — Options B/C only land docs as a side-effect of the eventual slice cascade, and Option D never lands them at all.
2. **Separation of concerns** matches how reviewers actually work. A reviewer who is approving "the strategic plan for #N" is doing different work from one approving "the code for slice-3 of #N".
3. **The infrastructure cost is bounded.** The orphan reconciler already handles multi-PR stacks; the contract schema delta is small (`pr.context_branch` / `pr.context_pr_number`); the orchestrator already has the primitives to commit statefiles to a branch.
4. **It is forward-compatible with #2534's vision of contract-driven PR narratives**: context PR uses the program-level `contract.pr.{title,description}`, slice PRs use slice-level metadata + a backlink (already done after #2543).

The recommendation is conditional on the operator's answers in `decision-1`, `decision-3`, and `decision-5`. If the operator prefers a smaller-blast-radius change, Option B is acceptable as a stepping stone — but it leaves the "docs never reach main" gap unfixed.

## Open Questions

The full set of decisions and feedback questions has been registered via `egg-contract` so they appear on the issue for the operator. Snapshot:

<!-- egg-hitl-decision id=decision-1 -->

**Where should refine/plan analysis docs and BRC consensus history live so they are reviewable on PRs targeting main?**

- [ ] Dedicated context PR (new egg/<id>/context branch based on main; slice-1 stacks on top of it)
- [ ] Embed in slice-1's diff (commit analysis.md/plan.md/refine+plan BRC history to slice-1 integration branch on top of egg/<id>/work)
- [ ] Embed in terminal slice's diff (terminal slice already carries the program narrative; co-locate the docs there)
- [ ] All slices carry a snapshot of analysis/plan as part of the slice integration branch
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**How should the implement-phase BRC consensus history be split so each slice PR carries its own slice's history?**

- [ ] Split file at write time: orchestrator writes to .egg-state/brc-history/<id>-implement-slice-<N>.{json,md} (one per slice; no aggregate file)
- [ ] Keep single .egg-state/brc-history/<id>-implement.{json,md} but also write per-slice files for slice PR diffs
- [ ] Keep single file unchanged; rely on a per-slice 'view' rendered into the slice PR body (no per-slice .json/.md committed)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**Should the new context PR go through BRC review (reviewer_refine + reviewer_agent_design + reviewer_plan), or land as a doc-only PR auto-merged after plan_gate approval?**

- [ ] BRC-reviewed (treated like any other producer PR; reviewers ACK the docs PR before slice-1 spawns)
- [ ] Doc-only auto-open (orchestrator opens; humans review on the PR; pipeline does not block on its merge before slicing)
- [ ] Doc-only with merge gate (pipeline blocks slicing until human merges the context PR)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**Rollout scope: which pipelines should the context-PR / per-slice BRC mechanism apply to?**

- [ ] Only new pipelines started after the change lands
- [ ] New pipelines + retroactively backfill in-flight pipelines (e.g. issue-2474-v2) by opening a context PR mid-stream
- [ ] New pipelines + provide a one-shot 'egg-contract emit-context-pr' CLI for operators to backfill on demand
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**Where in the stack should the context PR sit (and what should slice-1's PR base be)?**

- [ ] Context PR base=main, slice-1 base=egg/<id>/context (slice-1 stacks on context; context merges first to main, then slices cascade-merge)
- [ ] Context PR base=main, slice-1 base=egg/<id>/work (context PR is a side-channel docs PR; slice stack is unchanged)
- [ ] No context PR; instead retarget egg/<id>/work itself to be the merge target on main (slice-N terminal merges into work, then a final 'merge work to main' PR is opened automatically)
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

**Open-ended feedback** (registered as `feedback-1`):

- **Q1**: Today, slice PRs target `egg/<id>/work` (not main directly), and there's no automatic 'merge work to main' PR. Is that an existing gap that this issue should also fix, or is the work-branch-as-base intentional and out of scope here?
- **Q2**: The proposal says context PR uses `contract.pr.title` and `pr.description` (per #2534). #2534 has already been partially fixed in #2541 and #2543 (slice attribution + program narrative on every slice). Do you want context-PR title/body to be authored from those same contract fields, or should the planner emit a separate `contract.pr.context_title` / `pr.context_description` so the context PR can have a different framing than the slice PRs (e.g. 'Strategic plan for #N' vs 'Implement #N')?
- **Q3**: Should the context PR include the per-phase agent transcripts (e.g. `.egg-state/agent-outputs/<id>-refine-*.md`)? Or only the final analysis.md, plan.md, and BRC consensus records?
- **Q4**: When a slice's implement-phase BRC concludes, the orchestrator would need to commit `.egg-state/brc-history/<id>-implement-slice-<N>.{json,md}` to the slice's integration branch as a final commit before opening the slice PR. Is that final orchestrator-authored commit acceptable, or should it be authored by the coder/tester role (and would that conflict with role file boundaries — coder cannot push under `.egg-state/brc-history/`)?
- **Q5**: The issue lists 'analysis + plan + refine/plan BRC histories' for the context PR. Should the implement-phase aggregate BRC history (cross-slice) ALSO live on the context PR, or is each slice's BRC history sufficient for audit purposes?

## Complexity Assessment

**Complexity: high**

Rationale:

- Multi-component change touching `orchestrator/routes/pipelines.py` (slice provisioning, base-branch resolution, BRC persistence), `orchestrator/stacked_pr_reconciler.py` (orphan-rebase fallback), `orchestrator/gateway_client.py` (new branch creation primitive), and `shared/egg_contracts/models.py` (contract schema delta).
- Introduces a new PR type with its own lifecycle (creation, retry, gating semantics, deferred-action interaction) that must integrate with the existing stacked-PR reconciler's invariants and the HITL plan_gate.
- Per-slice BRC split is itself a non-trivial refactor of `_write_brc_history()` and `_rewrite_brc_history_for_pr()` — every implement-phase write site has to learn about slice context.
- Naturally decomposable into independent slices (contract schema, BRC split, context-branch creation primitive, slice-1 rebasing, reconciler updates, slice PR body re-render).

---

*Authored-by: egg*
