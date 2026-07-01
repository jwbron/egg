# BRC memory — refiner — issue-3393 (refine phase)

## Status

- CURRENT: v4 proposed (iteration 1) — sole change: restored
  `3393-analysis-human.md` byte-exact to the simplifier's `e88c16d61`
  rendering, un-doing my v3 clobber. `3393-analysis.md` untouched (both
  reviewers called it ACK-ready / "do not touch it further").
- v3 (`c2a3a8e80`) folded in operator's cq-1 resolution correctly in the
  analysis, but CLOBBERED the simplifier-owned human summary: my rebase
  conflict "resolution" ran `git checkout --theirs` — in a REBASE, --theirs
  is YOUR OWN commit being replayed, --ours is upstream — so I took my
  stale copy wholesale: deleted their per-repo house-rules bullet and
  replaced their hard-bit #1 (dropping the development-blocks element and
  overstating "work proceeds in parallel either way"). Two NACKs
  (reviewer_refine, reviewer_agent_design), both verified correct.
- HARD RULE going forward: `3393-analysis-human.md` is the SIMPLIFIER's
  artifact — never edit it, even when directives say "update the refine
  document(s)"; hand wording suggestions to the simplifier instead.
- v2 analysis (`4bb71004b`) addressed all three v1 NACKs in one round-trip.
  Artifacts: `.egg-state/drafts/3393-analysis.md`, `3393-analysis-human.md`.
- HITL decision cq-1 registered: v1 merge-sequencing gate semantics
  (poll+auto-release / HITL release / hybrid / other).
- v1 → three NACKs (reviewer_refine, reviewer_agent_design,
  first_principles_reviewer); all conceded after live re-verification.
- v2 → ACKed by all three named reviewers (simplifier→refiner was pending);
  operator then resolved HITL cq-1 at the phase gate and kicked back with
  iteration feedback → v3.
- **cq-1 RESOLVED (operator, custom answer, BINDING):** two-tier
  merge-sequencing — (a) plain merge ordering AUTOMATED: dependent slice
  developed in parallel, PR held in draft, orchestrator auto-marks ready
  when upstream PR merges; (b) beyond-merge-state blocks (release/publish
  waits, version-pin choices, genuine development blocks) are HITL-resolved,
  never programmatically detected. Folded into analysis (design rec #2,
  hard part #2, AC-6, new "HITL Resolution (cq-1)" section) and the human
  summary's hard-bit #1. No new HITL decisions induced.

## v1 NACK resolutions (do not re-litigate — I verified reviewers were right)

1. **Correction #1 was inverted (mine, not the issue's, error).** Client
   method IS `create_worktrees` (plural, gateway_client/_worktree.py:13);
   singular `create_worktree` is gateway-internal
   (gateway/worktree_manager/_create.py:115). `repo_volumes` is the live
   spawner param (_spawn.py:45, _concurrent.py:117) fed from
   `WorktreeResult.worktrees` (_spawn.py:283). v2 rewrote correction #1 as a
   two-layer naming map: client `create_worktrees` → `worktrees` → spawner
   `repo_volumes`.
2. **THREE `repos[0]` collapse sites** (verified by my own grep):
   kubernetes_spawner/_spawn.py:452/:464 (+:523 EGG_PIPELINE_REPO);
   commit_authorship_store.py:932-933; routes/pipelines.py:732
   (overseer_repo). sandbox/egg_lib/sdlc_hitl.py:82 is guarded by
   `len(repos)==1` — NOT a collapse; noted in analysis so nobody re-flags it.
3. **Per-repo conventions entailment added** (reviewer_agent_design ask):
   design recommendation #5 + AC-7 — slice agent cwd = slice's repo
   worktree; that repo's CLAUDE.md/linters/check commands govern.
4. Human summary "two spots" → "three spots". NOTE: the human summary was
   also externally edited (work-branch/umbrella-PR bullet added) — those
   edits are intentional, preserve them.

## My verdict (stable across events)

Issue #3393's concentrated-gap thesis HOLDS (grounded 2026-07-01; v1 at
`20b476173`, v2 commit follows). Scope operator-locked: arbitrary N repos,
slice↔repo 1:1 (`Slice.repo`), uniform visibility + uniform auth per run,
workable merge-sequencing hold. No descope; mixed auth + richer sequencing
deferred.

## Grounding facts I will defend (file:line in analysis)

- `Slice` (shared/egg_contracts/models.py:322-459) has NO repo field;
  `dependencies: list[str]` exists; Contract schemaVersion 1.3, four
  migration precedents.
- Worktree map keyed by BARE repo name → owner-collision risk; v1 re-keys to
  owner/repo or rejects same-name sets (reviewers confirmed at
  gateway.py:7740-7767).
- No submission-time visibility validation exists; private mode is global
  gateway posture; per-repo `get_repo_visibility` exists.
- Session mode single-per-pipeline (gateway_client/_session.py:16-39) —
  uniform-auth v1 maps onto it.
- `check_branch_ownership` repo-agnostic in logic, global-bot in config —
  non-issue under v1 uniform auth.

## Positions taken (keep consistent if NACKed again)

- Lazy per-repo work branches/context PRs (only repos with ≥1 slice).
- Test gate + reviewer diff + agent conventions scope = slice's repo only.
- Merge-sequencing semantics = operator's call (cq-1); nothing pre-decided.
- Exactly ONE HITL question; the rest of the issue's design questions are
  planner-phase mechanics.

## If re-proposing again

reviewer_agent_design said "expect ACK on v2 with just these two fixes";
reviewer_refine and first_principles_reviewer requested only the naming +
enumeration fixes. Any NEW objection on v2 should be narrow — address it
without expanding scope, and re-verify disputed facts live before conceding.
