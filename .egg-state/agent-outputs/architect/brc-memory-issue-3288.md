# BRC memory — architect (issue-3288, plan phase)

## My proposal (v1)
- Artifact: `.egg-state/drafts/3288-plan-architect-analysis.json`
- Design: Two coupled work streams.
  - **WS1 (documenter retrain, low risk, 2 files):** edit the documenter `## Your Task`
    block in `orchestrator/routes/pipelines.py` (~14781 implement; ~6761 phase summary;
    ~14157 plan orientation) and `DOCUMENTER_ROLE` in `shared/egg_contracts/agent_roles.py`
    to inject the snapshot doctrine: current-state not change-log; NEVER emit
    slice/TASK/phase/HITL ids into any doc/docstring/comment; history only when tangibly
    valuable (rationale over chronology); fold-and-remove stale ledger entries (replace,
    don't append); keep the no-op propose path.
  - **WS2 (corpus cleanup, large, planner-sliced):** bounded by HITL cq-1 opt-1 — named
    targets + bounded high-density sweep, long tail deferred + logged. Two edit classes:
    mechanical line-edits vs. total-refactors of load-bearing pages
    (gateway-auto-filter.md, coordination-state.md, slice-dag.md). Slice by package/doc-area
    so slices are file-disjoint.
- Recommended DAG: S1 (WS1) first; S2..S6 (WS2 by package) file-disjoint, parallel after S1.
  Planner owns final slicing.

## Key invariants I will DEFEND in review (NACK if violated)
1. Documenter gateway write-boundaries UNCHANGED (allowed_write docs/, **/README.md,
   **/*.md, .egg-state/agent-outputs/; blocked_write code/tests/.github/). Only wording changes.
2. BRC no-op `--no-changes-needed` propose path preserved in BOTH ~14157 (plan) and
   ~14800 (implement) branches.
3. NOT "delete all issue references" — rationale links stay (reframed). Test:
   chronology-as-default = strip; rationale-where-valuable = keep.
4. No new lint/CI guard (HITL cq-2 opt-1).
5. WS2 scope bounded (cq-1 opt-1); deferred long tail enumerated+logged, not silently dropped.
6. Snapshot doctrine applies to THIS pipeline's own doc touches too (no self-referential
   slice/TASK refs reintroduced).

## Grounding facts (verified 2026-06-26 against tree + live issue #3288)
- No test asserts the literal documenter prompt strings (grep hit only the 2 source files)
  → WS1 blast radius bounded.
- kubernetes_spawner.py = 67 ledger refs (densest single offender).
- Self-evidencing: orchestrator/CLAUDE.md + gateway/CLAUDE.md seam tables are themselves
  slice-keyed — a WS2 target (re-key by current submodule structure).
- ~260 files carry ledger refs across docs/, gateway/, orchestrator/, shared/.

## Peer state
- No peer proposals reviewed yet (first invocation, 2026-06-26). task_planner / risk_analyst
  / simplifier all WORKING, not yet proposed.
