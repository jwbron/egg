# task_planner BRC memory — issue-3077 (plan phase)

## Verdict / state
- Proposed plan: `.egg-state/drafts/3077-plan.md` (3 serialized slices, 17 tasks).
- v1 propose rejected by server role↔files validation: TASK-3-1 mixed
  `shared/prompts/REVIEWER-SYNC.md` (documenter-owned) with coder files. Fixed by
  splitting: TASK-3-1 (coder, event_prompt.py only) + TASK-3-4 (documenter,
  shared/prompts). All task file lists now verified via check_file_restriction
  (coder/tester/documenter, phase=implement) — all can_write=true.
- Validated locally: `parse_plan` success, 0 warnings; `validate_plan_preflight` OK
  (yaml-tasks + pr.title/description/test_plan/manual_steps all present).

## Plan shape (for consistency across re-invocations)
RESTRUCTURED to the architect's 6-slice DAG
(`.egg-state/agent-outputs/3077-architect-slices.yaml`) after it landed on the
shared branch — slices 1-5 serialized chain, slice 6 parallel root. 17 tasks:
- Slice 1: R1 non-silent sync — wrapper records per-SHA outcomes
  (merged/already-ancestor/unresolvable/merge-failed) and PREPENDS the banner to
  the fetched event prompt (wrapper-side, not event_prompt rendering); event_prompt
  empty-delta caution cross-references it. 2 coder + 1 tester.
- Slice 2: artifact spec module (frozen rows: analysis-draft, plan-draft,
  architect-output, architect-slices, risk-analyst-output; resolve_artifact_path/
  specs_for/spec_by_name) + MANDATORY consistency tests (phase gates, mirror,
  _get_draft_path both identifier shapes, pipelines.py literals). No consumer
  rewiring yet. 1 coder + 1 tester.
- Slice 3: spec-derived validation for all refine/plan producers (reuse #3081
  branch_verified degradation; delete _validate_producer_draft_present; re-derive
  pipelines.py literals). 1 coder + 1 tester.
- Slice 4: orchestrator/routes/artifacts.py + gateway/artifact_api.py blueprint
  (strict: no path field, 400 lists registered names, hex-validated ref,
  truncated flag) + sandbox/scripts/egg-artifact. 3 coder + 1 tester.
- Slice 5 (serialized_chain_order: slice-1..slice-4): event_prompt self-fetch
  prose deletion (coder), REVIEWER-SYNC prose (documenter — shared/prompts is
  documenter-owned), docs/architecture/coordination-state.md (documenter),
  ratchet test (tester). #3046 overlap with slice 1 on event_prompt.py noted.
- Slice 6 (parallel root): message_store fail-loud (auto→memory = error +
  degraded health flag; explicit memory = warning) + Redis restart-semantics
  test distinguishing designed _clear_concurrent_state wipe. 1 coder + 1 tester.

## Anchors honored (defend these on NACK unless reviewer shows them wrong)
- HITL Q1: Option C full scope. Q2: strict name-only endpoint (no raw-path escape).
  Q3: fail-loud only; `auto` backend selection unchanged; deeper durability → #3070.
- Slices strictly serialized: spec before consumers; prose deletion after served reads.
- Designed `_clear_concurrent_state()` phase-boundary wipe stays; only accidental
  mid-phase memory-backend loss is treated as defect.
- PR title kept under 70 chars (parser warning otherwise).
