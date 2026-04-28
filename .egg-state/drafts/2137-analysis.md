# Analysis: Independent implement phases

> Issue: #2137 | Phase: refine

## Problem Statement

The `implement` phase today runs as a **monolithic** unit:

- One pipeline branch (`egg/issue-N`).
- One agent team (coder + tester + documenter + 5 CRITICAL reviewers + dual-role tester) is spawned in a single concurrent wave.
- One BRC consensus round (PROPOSE → ACK/NACK → CONFIRMED) covers the entire feature diff.
- One PR per ticket (the pipeline-branch PR).

Tickets large enough to fill the context window cause **compaction**, which produces a measurable drop in output quality. The producer-side response (#2029) and reviewer-side responses (#2127 fan-out, #1965 subagent delegation, #2067 cleanup) chased the symptom by **fanning out** — partitioning the diff and running multiple subagents — rather than removing the underlying cause: a single oversized unit of work. Closed PR #2152 (issue #2139) already tore out the `reviewer_code` subagent fan-out paths and promoted `reviewer_security` / `reviewer_concurrency` from ADVISORY to CRITICAL, deliberately leaving #2137 as the producer-side fix.

The desired outcome is to **slice** the implement phase into a DAG of independent units. Each slice is small enough that a single agent can comfortably implement it (rough guidance ≲ 1,000 LOC; not enforced). The orchestrator schedules slices into parallel waves, runs a fresh BRC consensus per slice, and **each slice opens its own PR stacked along the DAG**: root slices target the pipeline branch; single-parent slices target their parent slice's branch. **There is no orchestrator-side merge step and no new gateway merge endpoint** — PRs go through the normal review/merge flow on GitHub.

The MVP enforces a **forest constraint** (each slice has ≤ 1 DAG parent). Diamond DAGs (multi-parent slices) are deferred to a follow-up; the planner auto-serializes would-be multi-parent slices into chains.

This issue **lifts the one-PR-per-pipeline constraint** for the first time in the platform's history, which is also a prerequisite for #1557's epic pipeline producing per-child-ticket PRs cleanly.

The acceptance criteria from the issue:

1. Plan phase emits `contract.slices[]` with `dependencies: list[str]` per slice; the resulting DAG is validated to be a forest at plan ingestion.
2. Orchestrator schedules slices via `ExecutionWave`; independent slices spawn in parallel (no concurrency cap — DAG width drives parallelism).
3. Each slice runs on its own branch with a fresh agent team and its own BRC consensus round.
4. Each slice's BRC uses the **full implement-phase reviewer roster** (no per-slice customization; lenses are CRITICAL per #2139).
5. Each slice opens its own PR. Root slices target the pipeline branch; single-parent slices target their parent slice's branch (stacked).
6. A failed slice does not cancel siblings; the resulting DAG deadlock triggers HITL escalation.
7. End-to-end: a previously-oversized ticket completes without compaction and is delivered as a stack of PRs.

## What's changed since the prior refine cycle

This is the second refine pass on #2137. The issue text was substantially revised between cycles. The prior draft is preserved at commit `f9de3ad56e` for reference. Material changes:

- **Stacked PRs replaced orchestrator-driven merges.** The prior issue text said slice branches were "topologically merged into the pipeline branch via a new gateway merge endpoint." That model is gone. Each slice now opens its own PR; merging happens through the normal GitHub flow. Consequence: **decision-1** (slice merge strategy) and **decision-15** (orchestrator merge-endpoint authorization) are obsolete. They remain on the contract but the human can pick "Other (explain in reply)" with a short note such as "moot — superseded by stacked PRs in revised issue text" or simply leave them unresolved if the operator prefers; the plan phase will treat them as no-ops.
- **Forest constraint introduced.** Multi-parent slices (diamond DAGs) are deferred to a follow-up. The planner auto-serializes upstream chains. Two new decisions registered: **decision-17** (auto-serialization heuristic) and **decision-18** (where the forest constraint is enforced).
- **Stacked-PR rebase mechanics is a new concern.** When a parent slice's PR merges, child PRs need to retarget. New decision: **decision-16** (GitHub auto-retarget vs orchestrator explicit rebase vs hybrid vs manual).
- **#2139 (PR #2152) merged.** The reviewer roster cleanup (subagent fan-out tear-out + lens promotion to CRITICAL) is done. Consequence: **decision-4** (reviewer roster removal) is resolved by #2152 — the human can mark it accordingly. **feedback-1 Q5** ("salvage from `ReviewerCodeConfig.parallel`?") is also resolved: per the merged PR, it was a clean tear-out with no salvage.
- **Several decisions are partially answered by the issue's own text.** Issue commits to "no concurrency cap" (partial answer to **decision-5**, but the operational cap question via feedback-1 Q4 remains valid). Issue commits to "no per-slice roster customization" (answers **decision-12** → option A, identical roster). Issue commits to "siblings keep running" (answers **decision-2** → option A, literal text). Plan phase should treat these as defaulted-to-the-issue's-text and only flip them if the human resolves them differently.
- **Lens reviewers are CRITICAL today.** This makes the original **decision-3** framing correct again and the **decision-13** ADVISORY-framed correction obsolete. The human should answer decision-3 (or decision-13's option C/D, whose option labels are still informative); decision-13's premise about today's criticality is now wrong and the human can leave it unresolved or pick "Other (explain in reply): superseded by #2139 — answer decision-3 instead."

## Current Behavior

### Implement phase orchestration

- Entry point: `orchestrator/routes/pipelines.py::_run_pipeline()` at **line 11443** iterates pipeline phases (refine → plan → implement → pr) and calls `_run_concurrent_phase()` per phase.
- The phase-runner calls `ConcurrentPhaseExecutor.spawn_all()` (`orchestrator/concurrent_executor.py:266`), which:
  1. Resolves the roster via `ConcurrentPhaseExecutor.get_agent_roles()` at `concurrent_executor.py:177` (which delegates to `get_roles_for_phase("implement", include_reviewers=True)` at `shared/egg_contracts/agent_roles.py:1287`).
  2. Registers all roles in the `PeerConsensusTracker`.
  3. Spawns every role concurrently via a `ThreadPoolExecutor`.
- BRC consensus is driven entirely by `orchestrator/peer_consensus.py`: `PeerConsensusTracker` (line 69) keys on `pipeline_id` (set in `__init__` at line 90, threaded into ~30 emit sites). Module singletons are managed via `_trackers[pipeline_id]` (`get_peer_consensus_tracker()` at line 1744, `remove_peer_consensus_tracker()` at line 1761). **No `slice_id` field exists anywhere in the repo today** — this would be a net-new key.
- Phase advancement is gated on consensus completion + health checks + unresolved HITL decisions (`orchestrator/routes/phases.py::advance_phase()` at line 229).

### Current implement-phase roster (post-#2152, `shared/egg_contracts/agent_roles.py`)

| Class | Roles | Criticality (per `orchestrator/review_graph.py:215-260`) |
| --- | --- | --- |
| Producers (`_PHASE_ROLES["implement"]`, line 1110) | `CODER`, `TESTER`, `DOCUMENTER` | n/a |
| CRITICAL reviewers (`_PHASE_REVIEWERS["implement"]`, lines 1116-1122) | `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`, `reviewer_concurrency` | All CRITICAL — NACKs deadlock consensus |
| Dual-role | `tester` reviews `coder` | CRITICAL |
| ADVISORY edge | `reviewer_code → documenter` (line 252) | ADVISORY (only ADVISORY edge in implement post-#2139) |

`reviewer_code` survived #2152 — only its **subagent fan-out paths** were removed; the role itself remains as a single-pass line-by-line code reviewer. `reviewer_code_holistic` (added in #2126) remains as the always-on holistic reviewer.

### Dependency graph machinery — built but unused at runtime

- `shared/egg_contracts/dependency_graph.py` provides `DependencyNode` (line 28), `ExecutionWave` (line 51), `ExecutionPlan` (line 73), `DependencyGraph` (line 114) with `topological_sort()` (line 194) + `compute_waves()` (line 229) already implemented. Module-level `build_dependency_graph(roles)` at line 282; `compute_execution_plan(roles)` at line 296.
- **Critical caveat**: it is keyed on `AgentRole`, not on plan phases / slices. `DependencyGraph.build_from_roles()` (line 139) reads `role_def.dependencies` (which encode "coder depends on plan output"), not the plan's `Phase.dependencies` field.
- The graph is built but **never used for runtime scheduling**. `ConcurrentPhaseExecutor.spawn_all()` ignores it and dispatches everything in one wave; `OrchestrationState.can_agent_run()` performs ad-hoc dependency checking via `role_def.dependencies` directly.
- **Reuse plan**: the wave-computation algorithm (Kahn / max-dep-wave) is reusable as-is. The data model needs either a parallel `SliceDependencyGraph` (slice-keyed) or generification of `DependencyGraph` to accept arbitrary node IDs — the plan phase should pick (likely the latter, since the algorithm is already pure topology).

### Contract model

- `shared/egg_contracts/models.py:189-216` — `Phase` has eleven serialized fields: `id` (192), `name` (193), `status` (194), **`review_cycles: int`** (195, default 0), `max_cycles` (196, default 3), `escalated: bool` (197), **`escalation_reason: str | None`** (198), `tasks: list[Task]` (199), `dependencies: list[str]` (200-203), `commit: str | None` (204-208), `review_feedback` (209-211), plus `validate_commit` validator (213-216). All eleven fields are part of the rename surface area.
- `Contract.phases: list[Phase]` (line 478) — the orchestrator currently does not iterate `phases[]` to scope implement work; it just runs all tasks against one big diff.
- The plan parser populates `Phase.dependencies` correctly: `ParsedPhase.to_contract_phase()` (`shared/egg_contracts/plan_parser.py:109`) and the collection wrapper `ParseResult.to_contract_phases()` (line 170) normalize values like `"phase 1"` → `"phase-1"`. **`ParsedPhase.dependencies` today is a `str` (line 106), parsed into a list inside `to_contract_phase()`** — a quirk worth knowing if the plan phase wants to redesign the parsing schema.
- `ParsedTask` (line 75) has `files_affected: list[str]` (line 83). **`ParsedPhase` does NOT have `files_affected`** — files-affected is per-task, not per-phase. The `files_affected` clustering heuristic the issue suggests for auto-serialization (and decision-17) needs to either aggregate per-slice from `ParsedTask.files_affected` or extend `ParsedPhase` with a slice-level field.
- **Hard prereq #2134**: the post-plan ingestion path has a wrapper at `orchestrator/routes/pipelines.py::_populate_contract_from_plan_safe` (line **10832**) that swallows all exceptions, calling the inner `_populate_contract_from_plan` (line **10860**). The inner function has 5 silent early-return paths plus the wrapper's outer `try/except`. For pipeline `issue-1931`, this path silently failed and `phases[]` shipped empty — meaning even if the orchestrator started reading `phases[]`/`slices[]` today, it would intermittently see an empty list. **#2134 is currently OPEN** (verified at refine time).

### Branch / merge / PR machinery

- **Branch creation**: `gateway/worktree_manager.py::create_worktree()` (line 237) creates per-container branches `egg/{container_id}/work` (set at lines 295 and 983). Issue-keyed PR branches use `egg/issue-{issue}` (concurrent_executor.py line 236). `create_phase_worktree(container_id, phase_id, base_branch)` (line 848) composes IDs to produce sub-worktrees keyed on `phase_id` — designed for closed #732's Tier 3 phases, **never wired into runtime**. This is a natural fit for slice worktrees.
- **Per-role staging branches** already exist for babysit-pr (`orchestrator/concurrent_executor.py:198-236`): `egg/babysit-pr/{pr}/{short-sha}/{role}` is a precedent for parallel branches. Slice branches would follow a similar shape.
- **Merge primitives**: `git merge` is allowlisted in `gateway/git_client.py:615` for *agent* use only (allowed flags lines 617-633: `--no-commit`, `--no-ff`, `--ff-only`, `--squash`, `--abort`, `--continue`, `--quit`, `--message`, `--no-edit`, `--strategy-option`, `--verbose`, `--quiet`, `-X`, `-m`, `-v`, `-q`; absent: `--ff`, `--allow-unrelated-histories`, `-s`/`--strategy`). Under the revised issue text **the orchestrator does not need to merge** — slices stack via PRs and merge through the normal GitHub flow. The agent allowlist suffices for any in-slice rebase-onto-base operations the coder performs in its own worktree.
- **Pipeline-branch rebase**: `_rebase_pipeline_branch_onto_base()` lives at `orchestrator/routes/pipelines.py:5324`; useful as a reference for any new "rebase child slice onto parent's new base" helper if decision-16 picks orchestrator-driven rebase.
- **PR creation**: today's PR phase opens one PR per pipeline. With slicing, the implement phase itself becomes a PR-emitter (one per slice). The plan phase needs to decide whether per-slice PR creation lives in a new PR-helper role per slice (cheap), in the orchestrator's run loop (more direct), or piggybacks on a refactored PR phase per slice (heavier).

### Failure handling

- Single-agent failure → `ConcurrentPhaseExecutor.handle_agent_failure()` creates a HITL decision with retry/abort/continue options.
- 2+ failures within 60s → immediate phase abort.
- Pending HITL decisions block phase advancement (`_collect_unresolved_phase_decisions()`).
- Stuck-phase detection: `overseer_stuck_phase_transition_seconds` (default 180s) emits OVERSEER_ALERT.
- Heartbeat timeout for implement: `orchestrator_implement_heartbeat_timeout_seconds` (default 600s).

### What's *not* yet built

| Capability | Status |
| --- | --- |
| `contract.slices[]` schema field | Not present; `phases[]` is the closest analogue |
| Slice-keyed (or ID-generic) `DependencyGraph` | Not present; existing graph is reusable but role-keyed |
| Forest constraint validation at plan ingestion | Not present |
| Auto-serialization of would-be multi-parent slices | Not present |
| Orchestrator slice scheduler (wave loop driving `ConcurrentPhaseExecutor` per slice) | Not present |
| Per-slice PR creation | Not present (today's PR phase emits one per pipeline) |
| Stacked-PR rebase / retarget on parent merge | Not present |
| Slice-aware coder prompt scoping | Not present |
| BRC tracker namespacing for concurrent slice consensus | Not present (today: `pipeline_id`-keyed only) |
| Per-slice branch (e.g., `egg/issue-N/slice-M`) | Not present (closest precedents: `egg/issue-{issue}` and `egg/babysit-pr/{pr}/{sha}/{role}`) |

## Constraints

### Technical

- **Hard prereq on #2134**: `phases[]` ingestion has silent-failure paths. Slice scheduling cannot be reliable until #2134 is fixed (audit-logged populate outcomes + regression test).
- **Existing `DependencyGraph` is role-keyed, not slice-keyed**. Either extend it to accept arbitrary node IDs (slice IDs) or introduce a parallel `SliceDependencyGraph`. The wave algorithm is reusable.
- **Forest constraint is a real schema validation, not just guidance.** The planner must emit a forest; ingestion must reject non-forests. No precedent for plan-shape validation today — this is a new code path.
- **Auto-serialization is a planner-side responsibility.** The orchestrator should not silently rewrite the DAG; the planner emits the serialized chain and the human can override during plan approval. `ParsedPhase` lacks a `files_affected` field today — either derive it from `tasks[].files_affected` aggregation or add a new field.
- **BRC tracker keys exclusively on `pipeline_id`.** Concurrent slice BRCs need either nested IDs (`issue-2137/slice-1`) or a `slice_id` field on every message. Surveyed code confirms zero existing `slice_id` references — net-new field. Existing audit/heartbeat/overseer code needs to keep up with whichever choice (decision-14).
- **Worktree manager already supports phase-keyed worktrees** (`create_phase_worktree`, line 848) but the orchestrator never calls it. Per-slice agent containers can reuse this scaffolding without inventing it.
- **Branch-name length / GitHub ref limits** are practical caps on slice IDs (~250 bytes for refs, much less for human readability). `egg/issue-N/slice-M` keeps it under control. Branch-naming convention recorded as feedback-1 Q3.
- **GitHub auto-retarget caveat**: GitHub auto-retargets stacked PRs when the parent PR merges via the GitHub UI / `gh pr merge` only — force-pushes or out-of-band branch deletion can break the chain. Decision-16 picks the policy.

### Cost / token budget

- Today's monolithic implement spawns ~6-8 agents (3 producers + 3 CRITICAL reviewers + 2 lens reviewers, with reviewer_code merged into reviewer_code_holistic-style coverage post-#2152). For a 5-slice ticket with the issue's mandate of "full implement-phase reviewer roster per slice" (≈ 8 roles per slice), the gross multiplier is ~5× the agent count. Slice diffs are smaller per agent, but total token spend per pipeline is meaningfully higher than today.
- Compaction empirically kicks in around tickets ≳ 33K LOC / 41 files (background context from #2105). Smaller tickets won't see compaction and don't strictly need slicing — but the proposed change applies to *all* implement phases. Whether to gate slicing on a complexity threshold is **not** in the issue's scope; raised as a non-blocking observation for the plan phase.

### Operational

- Container/sandbox concurrency may already be capped by gateway resource limits; spawning N×roster_size containers per wave can exhaust them. The issue says "no concurrency cap — DAG width drives parallelism," but operational ceilings (gateway, container, GHA queue, Anthropic rate limits) still apply. Captured as feedback-1 Q4.
- **Stacked PRs change reviewer ergonomics.** Today's reviewers see one PR per pipeline. With slicing they see N PRs that depend on each other; merging requires landing them in DAG order. CI cost multiplies (each PR runs its own checks). Operational guidance for human reviewers should be in the plan/PR phases.
- The orchestrator's **CI integration** assumes one branch per pipeline. Slice branches multiply the running-CI surface; if checks block PR merge, the parent PR cannot land until checks finish, blocking children.

### Out of scope (per current issue text)

- Refine and plan phases unchanged.
- `babysit_pr` pipelines unchanged (#2063 stays separate; decision-8 asks how permanent this is).
- Reviewer roster cleanup landed in #2139 (PR #2152, merged).
- Silent-failure ingestion fixes land in #2134.
- Multi-parent slices (diamond DAGs) deferred to a follow-up; MVP enforces forest constraint.

## Options Considered

### Option A: Issue-as-written (slice scheduler + stacked PRs + forest constraint)

**Approach**: Rename `Phase` → `Slice` in the contract; build a generic `DependencyGraph` (or a slice-keyed parallel); drive a wave-by-wave loop in the orchestrator that spawns one `ConcurrentPhaseExecutor`-equivalent per slice, each on its own branch (`egg/issue-N/slice-M` or similar — decision feedback-1 Q3). Each slice's coder opens a PR stacked on its parent slice's branch (root targets pipeline branch). The planner emits a forest DAG; multi-parent slices are auto-serialized at plan-emission time. Failure of one slice does not cancel siblings.

**Pros**:
- Matches the issue verbatim, including stacked-PR delivery shape and forest constraint.
- Reuses ~80% of existing scaffolding: `ExecutionWave`/`compute_waves()`, `create_phase_worktree`, BRC tracker, HITL machinery.
- **Eliminates the orchestrator-side merge surface entirely** — no new privileged code path; merging is a normal GitHub operation triggered by humans (or auto-merge bot).
- Enables per-slice early-abort (a flawed slice surfaces before later slices burn tokens).
- Slice-scoped reviewers see small, comprehensible diffs — addresses the *cause* of compaction, not the symptom.
- **Lifts the one-PR-per-pipeline constraint** as a side effect, unblocking #1557's epic pipeline.

**Cons**:
- Cross-slice architectural defects (the `__checkout__` synthetic-key class of bugs that #2126 specifically targeted) may be invisible to per-slice CRITICAL reviewers (`reviewer_code_holistic` only sees one slice's diff at a time). Mitigation registered as decision-3/decision-13 (lens scope) — but the issue's mandate of "no per-slice roster customization" partially closes off the option of running a holistic pass on the merged-equivalent state. The plan phase will need to reconcile.
- Stacked PRs add reviewer load (N PRs to read / land instead of 1) and CI multiplier.
- Forest constraint may force unnatural serializations that slow the pipeline (a 5-slice DAG with two diamonds collapses to a chain of 3+2; latency reverts toward monolithic).
- Stacked-PR rebase on parent merge is a known sharp edge in GitHub; decision-16 picks the recovery policy.
- BRC tracker namespacing needs careful redesign (decision-14).

### Option B: Schema-only rename now, runtime change deferred

**Approach**: In this PR, only rename `phases` → `slices` everywhere (per the issue's "Renames" section), fix #2134's silent-failure paths (or take it as a hard prereq landing first), and emit `slices[]` from the planner with `dependencies[]` populated. *Don't* change runtime scheduling yet. Follow-up PR(s) build the slice scheduler, the per-slice BRC, the stacked-PR machinery, and the forest validation.

**Pros**:
- Minimum-viable first PR; concentrates risk on the schema change (which is well-bounded — `phases[]` is currently written but not read).
- Lets reviewer/operator confidence accrue before the runtime change.
- Aligns with how risky orchestrator changes have landed historically (decompose into reviewable chunks).

**Cons**:
- Doesn't satisfy the issue's acceptance criteria as written — the "End-to-end: a previously-oversized ticket completes without compaction and is delivered as a stack of PRs" criterion explicitly requires the runtime scheduler.
- Two-PR landing means the schema rename ships without anyone reading it; slightly *increases* the #2134 risk surface in the interim.
- Defers the issue's primary value (eliminating compaction).

### Option C: Slice-as-batch, single shared branch (no per-slice branches, no stacked PRs)

**Approach**: Same DAG and per-slice BRC, but every slice's coder pushes to the *same* pipeline branch using the babysit-PR rebase pattern. One PR per ticket as today; slices are an internal scheduling detail.

**Pros**:
- No new branch / PR machinery; reuses the proven `_sync_worktree_with_remote` rebase reconciliation.
- No conflict-resolution policy needed at the orchestrator layer; agents handle their own rebases.

**Cons**:
- Concurrent rebases on the same branch are inherently fragile at slice scale (5+ pushers).
- Loses the audit value of slice-scoped commits (you can't say "slice 3 made these changes" — every push is just on `egg/issue-N`).
- Conflicts between slice waves are implicit and only surface as rebase failures.
- Misses the explicit acceptance criterion about per-slice PRs and stacked delivery.
- Misses the side-benefit of unblocking #1557.

### Option D: Issue-as-written but with a final cross-slice holistic pass

**Approach**: Same as Option A, but **before** the implement phase advances to PR phase, run `reviewer_code_holistic` (CRITICAL) once on a *synthetic* merged state — e.g., the orchestrator opens a draft PR that combines all slice branches and runs holistic review on that diff. If holistic NACKs, escalate to HITL or require a fixup slice.

**Pros**:
- Restores cross-slice architectural review without reintroducing orchestrator merging.
- Catches the `__checkout__`-class bugs that motivated `reviewer_code_holistic` in the first place.

**Cons**:
- Conflicts directly with the issue's "no per-slice roster customization" mandate (this *is* a roster customization — running holistic on a non-slice surface).
- Re-creates the large-context surface that motivated slicing; if compaction returns at the holistic step, the gain is partially lost.
- Adds latency at the tail of the pipeline.
- Synthetic merge of multiple slice branches needs a venue (orchestrator merge endpoint or temp branch); reintroduces a merge surface that the revised issue text explicitly removed.

## Recommended Approach

**Option A (issue-as-written, slice scheduler + stacked PRs + forest constraint) is the recommended approach**, with these caveats made explicit for the plan phase:

1. **#2134 must land first** (or as part of the same change set). The issue calls #2134 a hard prereq; this analysis confirms it: silent-failure ingestion of `phases[]`/`slices[]` would intermittently produce empty slice arrays, masquerading as a scheduling bug. Fixing #2134 also gives the slice scheduler the structured-logging surface that the implement-phase debug story badly needs.

2. **Schema rename should be additive in implementation, atomic in the contract.** Internal Python types can be renamed in one go (`Phase` → `Slice`, `to_contract_phases` → `to_contract_slices`); the on-disk contract JSON should accept the old `phases[]` shape during a brief migration window so any in-flight pipelines aren't bricked. Decision-7 picks the strategy.

3. **Forest validation lives at plan ingestion** (decision-18 option A or C). Validating only at the orchestrator scheduler is too late; a non-forest plan would already be on disk and committed.

4. **Lens reviewer scope (decisions 3 / 13) needs explicit resolution** post-#2139. The "no per-slice roster customization" clause in the AC closes off some prior options — but it doesn't preclude a per-slice run *and* a non-slice cross-cutting run if the human asks for it. Recommended starting point: per-slice CRITICAL lenses today; revisit post-MVP if cross-slice security regressions surface in production.

5. **Stacked-PR rebase on parent merge (decision-16) should default to GitHub's auto-retarget plus a periodic reconciler.** GitHub's auto-retarget covers the common case; a reconciler catches force-push / out-of-band-deletion edge cases. A webhook listener is cleaner but is net-new orchestrator infrastructure.

6. **Auto-serialization heuristic (decision-17) should be planner-side.** The orchestrator should not rewrite the DAG silently. The planner emits the serialized chain in the plan draft, the refiner spot-checks it during plan review, and the human can override during plan approval (per the issue's literal text). The exact ordering rule is the decision; the issue's `files_affected` clustering + descending fan-out is a strong starting point.

7. **Recommended PR sequence (informational; final shape via feedback-1 Q2):**
   - **PR-1**: #2134 silent-failure fix (already a separate ticket; lands first).
   - **PR-2**: Schema rename `contract.phases[]` → `contract.slices[]`, plan-parser update, plan-template update, doc updates. Pure rename; no runtime change. Validates that nothing reads `phases[]` today.
   - **PR-3**: `DependencyGraph` generification + slice-keyed `ExecutionWave` builder + forest validation in `_populate_contract_from_plan`.
   - **PR-4**: Orchestrator slice scheduler + per-slice BRC tracker namespacing (decision-14) + per-slice branch creation via `create_phase_worktree`.
   - **PR-5**: Per-slice PR creation + stacked-PR rebase reconciler (decision-16).
   - **PR-6**: Auto-serialization heuristic in the planner + plan-prompt update for slice sizing guidance (decision-6).

Option B is rejected as the *whole* answer because it doesn't satisfy acceptance criteria — but its decomposition discipline is folded into PR-2 above.

Option C is rejected because concurrent rebases on a shared branch are fundamentally unsafe at slice scale.

Option D is rejected because it conflicts with the "no per-slice roster customization" AC and re-creates the merge surface the revised issue text explicitly removed. If cross-slice architectural coverage becomes a real problem in production, a follow-up issue can revisit it once we have per-slice empirical data.

## Open Questions

> **Note:** every question below is registered with `egg-contract` so it appears as a checkbox/comment on the GitHub issue. These are **not** advisory — the answers shape the plan phase output. Decisions 1, 4, and 15 are **obsolete** as a result of the issue's revision (no merge step) and #2152's landing (roster cleanup); the human can pick "Other (explain in reply)" with a moot-marker note or leave them unresolved. Decision-13 is **superseded by decision-3** post-#2139 (lenses are CRITICAL again).

### Multiple-choice decisions (registered as `decision-N`)

- **decision-1** — *Slice merge strategy*: **OBSOLETE** in revised issue text (no merge step; stacked PRs replace merging). Retain on contract but treat as no-op.
- **decision-2** — *Slice failure semantics*: partially answered by issue ("siblings keep running"); options remain on contract for human to override or confirm.
- **decision-3** — *Lens reviewer scope* (lenses CRITICAL today post-#2139): now correctly framed.
- **decision-4** — *Reviewer roster removal*: **resolved by #2152**. Human can mark this complete or pick "Other: handled by #2139/#2152".
- **decision-5** — *Slice scheduling concurrency cap*: partially answered by issue ("no cap"). Operational ceilings (feedback-1 Q4) still apply.
- **decision-6** — *Plan-phase slice sizing guidance*: still open.
- **decision-7** — *Renames vs. additive schema*: still open; recommendation in caveat 2 above.
- **decision-8** — *Babysit-PR pipelines*: still open (issue still says #2063 stays separate).
- **decision-9** — *Slice-level retry / `max_cycles`*: still open.
- **decision-10** — *Deadlock detection latency*: still open.
- **decision-11** — *Contract task → slice mapping*: still open; the issue's `files_affected` clustering hints at a hybrid 1:1+files-affected-informed option.
- **decision-12** — *Per-slice agent team identity*: answered by issue ("no per-slice roster customization" → option A).
- **decision-13** — ⚠️ *Superseded by decision-3* post-#2139 (lenses CRITICAL again). Pick "Other (explain in reply): superseded — answer decision-3" or leave unresolved.
- **decision-14** — *BRC tracker namespacing for concurrent slice consensus*: still open; net-new field per code survey.
- **decision-15** — *Orchestrator merge-endpoint authorization*: **OBSOLETE** in revised issue text (no merge endpoint).
- **decision-16** — ⭐ *Stacked-PR rebase mechanics on parent merge* (NEW this cycle).
- **decision-17** — ⭐ *Auto-serialization heuristic for would-be multi-parent slices* (NEW this cycle).
- **decision-18** — ⭐ *Forest constraint enforcement point* (NEW this cycle).

### Open-ended feedback (registered as `feedback-1`)

- **Q1** — Expected typical / worst-case slice count per ticket (drives operational concurrency planning).
- **Q2** — Single PR vs. multi-PR delivery preference for #2137 itself? (Recommended caveat 7 above suggests 5 PRs.)
- **Q3** — Branch naming: `egg/issue-N/slice-M` (slash) vs. `egg/issue-N-slice-M` (dash)?
- **Q4** — Operational concurrency caps (gateway, container, GHA queue, Anthropic rate limits)?
- **Q5** — Salvage anything from `reviewer_code` fan-out? **Resolved by #2152** as a clean tear-out.
- **Q6** — Specific past ticket / pipeline to use as the "completes without compaction" regression benchmark?

## Complexity Assessment

**high**

Justification:
- Touches the orchestrator's run loop (`pipelines.py::_run_pipeline`), the BRC tracker namespacing (`peer_consensus.py`), the contract schema (`models.py`), the plan parser output (`plan_parser.py`), the dependency graph (`dependency_graph.py`), the worktree/gateway interaction, and the implement-phase scheduler (`concurrent_executor.py`).
- Introduces forest-constraint validation, planner-side auto-serialization, and stacked-PR retargeting — three net-new code paths with no precedent in the codebase.
- Has a hard prereq (#2134) that itself has silent-failure paths needing audit logging + regression coverage.
- Involves cross-cutting redefinition of "phase" terminology in code, prompts, and operator-facing UX (the renames list).
- Has 18 multi-choice decisions + 6 open-ended feedback items the plan phase must absorb before producing tasks; the issue's revision since the prior refine cycle already closed off some prior options but opened three new ones.
- **Lifts the one-PR-per-pipeline constraint** for the first time, which is itself a load-bearing architectural change.

The issue is explicitly the kind of architectural change the plan phase decomposes into multiple PRs/slices. Recursive note: this issue may itself be a candidate first user of slicing once #2137 ships.

---

*Authored-by: egg*
