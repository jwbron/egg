# BRC memory — refiner — issue-3393 (refine phase)

## Status

- v1 analysis written and proposed (see Decision log in
  `.egg-state/drafts/3393-analysis.md`). Artifacts:
  `3393-analysis.md`, `3393-analysis-human.md`.
- HITL decision-1 registered: v1 merge-sequencing gate semantics
  (orchestrator polling vs. HITL hold vs. draft-until-upstream-merges).

## My verdict (stable across events)

Issue #3393's concentrated-gap thesis HOLDS against the live tree
(grounded 2026-07-01 at HEAD `20b476173`). Scope is operator-locked:
arbitrary N repos, slice↔repo 1:1 (`Slice.repo`), uniform visibility +
uniform auth per run, workable merge-sequencing hold. No descope is
acceptable; mixed auth and richer sequencing machinery are explicitly
deferred.

## Grounding facts I will defend (verified, file:line in analysis)

1. `create_worktree` (singular; NOT `create_worktrees`) takes `repos:
   list[str]`, returns `worktrees` dict keyed by BARE repo name —
   owner-collision risk is real; v1 must re-key to owner/repo or reject
   same-name sets.
2. `Slice` (shared/egg_contracts/models.py:322-459) has NO repo field;
   `dependencies: list[str]` exists. Contract schemaVersion 1.3 with four
   migration precedents — Slice.repo migration is pattern-following.
3. TWO `repos[0]` collapse sites: kubernetes_spawner/_spawn.py:452/464/523
   AND commit_authorship_store.py:932-933 (issue names only the first).
4. No submission-time visibility validation exists today; private mode is a
   global gateway posture; per-repo `get_repo_visibility` exists.
5. Session mode is single-per-pipeline (gateway_client/_session.py:16-39) —
   uniform-auth-mode v1 maps onto it; validate-and-reject mixed sets.
6. `check_branch_ownership` is repo-agnostic in logic but global-bot in
   config — non-issue under v1 uniform auth.

## Positions taken (keep consistent if NACKed)

- Lazy per-repo work branches/context PRs: only repos with ≥1 slice get a
  work branch + context PR; single-slice repos still get the standard
  context PR.
- Test gate + reviewer diff scope = the slice's repo worktree only.
- Merge-sequencing semantics = operator's call (HITL decision-1), not
  something I pre-decide; I recommended nothing binding in the analysis.
- Registered exactly ONE HITL question; everything else in the issue's
  "design questions" list is planner-phase mechanics.

## If re-proposing

Address NACK reasons narrowly; do not expand scope; re-verify any grounding
fact a reviewer disputes against the live tree before conceding (my claims
are file:line-verified; the issue's own table has the naming drift, not me).
