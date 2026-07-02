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

## v2 (addressed risk_analyst R1 NACK on slice-1 — CONCEDED, aligned to architect aeb3528)
Original TASK-1-1 wrongly told the coder to fill Slice.repo + normalize the
pipeline repo INSIDE the contract-model migration. Impossible: the Contract
model has NO repo field and cannot see the orchestrator Pipeline. Realigned to
the two-layer resolver design:
- Contract layer (shared/egg_contracts/models.py): Slice.repo:str|None + PURE
  ADDITIVE _migrate_schema_version_to_1_4 stamp (mirror _migrate_schema_version_to_1_3
  verbatim; guard =="1.3"; idempotent; NO field mutation; Slice.repo stays None on
  legacy load). Migration does NOT fill repo, does NOT reference pipeline, no repo
  list on Contract.
- Orchestrator layer (orchestrator/models.py): RepoSpec + Pipeline.repos +
  validator synthesizes list from legacy singleton & mirrors repos[0] back +
  primary_repo property + RUNTIME resolve_slice_repo(slice,pipeline)=slice.repo or
  primary. absent⇒primary lives HERE, not in the contract migration.
- TASK-1-3 tests split across both layers (contract test_models.py +
  orchestrator/tests/test_models.py). Re-validated clean; re-proposed v2.

## v3 (addressed reviewer_plan R3 NACK on slice-5 — CONCEDED, aligned to architect layer_6)
TASK-5-1 pinned the three failure semantics reviewer_plan required:
(a) merge detection = merged boolean / mergedAt (gh pr view --json state,mergedAt),
NOT head-SHA equality (squash/rebase changes SHA); on merged⇒ NEW gateway verb
mark_pr_ready(repo,pr_number) wrapping existing gh pr ready (github_client.py:124),
exposed via gateway/gateway.py + gateway_client/_pr.py; poll on existing reconcile
cadence (extend stacked_pr_reconciler.py or add cross_repo_merge_gate.py).
(b) CLOSED-UNMERGED terminal ⇒ HITL hold (not auto-ready), surfaced on status —
distinct from Tier B beyond-merge-state.
(c) BOUND/TIMEOUT ⇒ never-merging escalates to HITL, not indefinite draft.
TASK-5-1 files expanded: +stacked_pr_reconciler.py, +gateway_client/_pr.py,
+gateway/gateway.py, +gateway/github_client.py (all coder-writable; overlap OK —
slice-5 is transitive descendant of slices 3&4 which own gateway.py/_pr.py).
TASK-5-3 adds test cases: squash-merge SHA≠head, closed-unmerged⇒HITL,
never-merging⇒HITL bound. Re-validated clean; re-proposed v3.

## Slice shape (for consistency across re-invocations)
- slice-1 (root): repo dimension across TWO model layers (see v2 above).
  Chain root: nothing writes Slice.repo until it lands.
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
