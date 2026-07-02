# task_planner BRC memory — issue-3393 (plan phase)

## Verdict / state
- Proposed plan: `.egg-state/drafts/3393-plan.md` — 6-slice SINGLE LINEAR CHAIN
  (slice-1 → … → slice-6), 18 tasks (coder source, tester tests, documenter docs).
- Validated locally (PYTHONPATH=shared): `parse_plan` success (6 slices);
  `validate_plan_preflight` OK (yaml-tasks + pr.title/description/test_plan +
  manual_steps present); `validate_forest`=[], `validate_slice_file_overlap`=[],
  `validate_task_role_alignment`=[].
- Reviewers = reviewer_plan, risk_analyst, simplifier (architect is a PEER
  producer, not my reviewer). At propose time no producer had proposed;
  simplifier's plan-draft-human was explicitly waiting on my 3393-plan.md.

## Why a single linear chain (defend on NACK)
Five slices edit `orchestrator/routes/pipelines.py`; slices 1 & 3 both edit
`orchestrator/models.py`. Per #3046 overlap validator, overlapping slices need
a transitive-ancestor ordering → one linear `dependencies` chain. Do NOT
re-parallelize 2–6 into independent roots (trips #3046).

## Slice shape (for consistency across re-invocations)
- slice-1 (root): repo dimension in persisted schema. `Slice.repo` +
  Pipeline repo-list + schemaVersion 1.3→1.4 migration (absent ⇒ primary;
  singleton ⇒ 1-elem list) [shared/egg_contracts/models.py, orchestrator/models.py]
  + tests. Chain root: nothing writes Slice.repo until it lands.
- slice-2 (dep 1): list-shaped submission (_submit.py + pipelines route) +
  uniform-visibility & uniform-auth validation (repo_visibility.py,
  _credentials.py). Same-name sets NOT rejected (ruling #6).
- slice-3 (dep 2): kill all THREE repos[0] collapse sites (_spawn.py:452/464/523,
  commit_authorship_store.py:932-933, pipelines.py:732) + owner/repo re-key of
  worktree map (_worktree.py + gateway.py) + ratchet test/grep sweep
  (sdlc_hitl.py:82 allowlisted, guarded not a collapse).
- slice-4 (dep 3): slice-PR routing to slice.repo (_pr.py) + lazy-per-repo work
  branch + context PR (_open_context_pr_at_implement_start) + sibling cross-refs.
- slice-5 (dep 4): cq-1 two-tier hold. Tier A automated draft→ready on upstream
  PR merge (poll, no release detection); Tier B HITL for beyond-merge-state.
  Deps gate merge-readiness NOT development.
- slice-6 (dep 5): per-repo test-gate + reviewer-diff scoping + per-repo
  conventions (cwd = slice's repo worktree, its CLAUDE.md/linters) + docs.

## Anchors honored (defend on NACK unless reviewer shows them wrong)
- All 8 refine ACs mapped (AC table in plan). All 6 operator rulings + cq-1.
- Ruling #1 lazy-per-repo context PR; #3 per-repo gate scope; #5 per-repo
  conventions; #6 owner/repo re-key (reject-same-name FORBIDDEN; prohibitive
  fan-out ⇒ new HITL, never silent fallback).
- cq-1: two hold kinds, two release paths; NO release/version auto-detection.
- Migration lands BEFORE any producer writes Slice.repo (chain-root ordering).
- N=1 regression baseline asserted in every tester task; AC-8 make lint+test-all.
- orchestrator/models.py `primary_repo` property is the INTENTIONAL primary
  accessor — explicitly NOT one of the 3 collapse sites (distinguish on NACK).
- All slices are repo=jwbron/egg (this pipeline builds multi-repo; doesn't span).
