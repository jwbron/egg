# refiner BRC memory — issue-3312 (refine)

## Task
Refresh of #3111→#2817→#2261: decompose ALL 19 oversize Python files below
the 1,500-line / 100KB cap and EMPTY `scripts/file-size-allowlist.yaml`'s
`files:` map. Pattern landed in PR #2335 (merged); no slice has ever landed.

## Status
- v1 analysis written to `.egg-state/drafts/3312-analysis.md`. Grounded all
  issue claims vs live worktree (HEAD 46f74f8b7, 2026-06-26). Committed +
  proposed v1. NO HITL registered (scope is operator-locked).

## Verdict / position
- **SCOPE IS LOCKED — no descope, no HITL.** All 19 files in scope incl.
  `pipelines.py` (~27k) + `gateway.py` (~10k) + `_run_pipeline` directly
  (non-neg #7). Acceptance = empty allowlist. Slice DAG/ordering/PR packaging
  = planner's, NOT refine. Do NOT register a scope-reduction HITL.
- Defend grounded facts; the issue is heavily author-specified. Don't invent
  scope.

## Grounded facts (verified 2026-06-26)
- 19 files confirmed; live counts drifted UP from issue table (authoritative:
  pipelines.py 27,211 lines/1.27MB; gateway.py 10,385/408KB). 9 over byte cap
  (same 9 issue names). All 19 have live allowlist entries.
- `docs/guides/decomposition-pattern.md` present (13,292B); `scripts/select_tests/`
  present worked ref (__init__ barrel + __main__ + _cli/_constants/_graph/_io),
  already out of allowlist. PR #2335 is template.
- **STALE-CLAIM CORRECTION:** issue says CLAUDE.md seam tables carry stale
  `#2261 slice-14/15` TBD rows. FALSE on live tree — orchestrator/CLAUDE.md &
  gateway/CLAUDE.md have NO #2261 refs, NO TBD decomposition rows; only a
  component table + one generic decompose paragraph. #2261 refs live in docs/.
  → seam work is CREATE concrete rows, not retag. sandbox/CLAUDE.md has no seam
  table; shared/CLAUDE.md ABSENT (must be created for plan_parser.py).
- Back-compat surface re-derived: ~57 distinct `routes.pipelines.*` patch
  targets across ~137 test files; ~12 `gateway.gateway.*`. Barrel re-exports
  must preserve these.

## If NACKed
- Edit `.egg-state/drafts/3312-analysis.md` in place, re-commit, re-propose
  (version bumps). Defend grounded file:line facts. Keep "scope locked / no
  HITL" stance unless a reviewer shows a factual error. The seam-table
  correction is verified — hold it.

## Decision log
- 2026-06-26: grounded #3312 live body vs worktree; confirmed 19 files/sizes/
  allowlist/pattern inputs; re-derived patch surface (57/12); corrected stale
  #2261 seam-table claim; no HITL (scope locked); wrote + proposed v1.
