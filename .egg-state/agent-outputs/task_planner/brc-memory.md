# task_planner BRC memory — issue-3077 (plan phase)

## Verdict / state
- Proposed plan v1: `.egg-state/drafts/3077-plan.md` (3 serialized slices, 16 tasks).
- Validated locally: `parse_plan` success, 0 warnings; `validate_plan_preflight` OK
  (yaml-tasks + pr.title/description/test_plan/manual_steps all present).

## Plan shape (for consistency across re-invocations)
- Slice 1 — foundations: R1 non-silent sync (consensus_wrapper outcome → event_prompt
  warning), artifact spec module `shared/egg_contracts/artifact_spec.py`,
  `_get_draft_path` re-derivation + phase_filter/phase_patterns consistency exposure,
  fail-loud memory-backend signal. Tasks 1-1..1-8 (5 coder, 3 tester).
- Slice 2 — spec consumers (deps: 1): all-producer propose validation in signals.py,
  gateway `POST /api/v1/artifact/get` (strict, spec-registered names only — HITL Q2),
  `sandbox/scripts/egg-artifact` helper. Tasks 2-1..2-5 (3 coder, 2 tester).
- Slice 3 — prose retirement + ratchet (deps: 2): delete REVIEWER-SYNC fetch prose +
  event_prompt fallback, `docs/architecture/coordination-state.md` invariant entry,
  ratchet test `orchestrator/tests/test_prompt_sync_ratchet.py`. Tasks 3-1..3-3.

## Anchors honored (defend these on NACK unless reviewer shows them wrong)
- HITL Q1: Option C full scope. Q2: strict name-only endpoint (no raw-path escape).
  Q3: fail-loud only; `auto` backend selection unchanged; deeper durability → #3070.
- Slices strictly serialized: spec before consumers; prose deletion after served reads.
- Designed `_clear_concurrent_state()` phase-boundary wipe stays; only accidental
  mid-phase memory-backend loss is treated as defect.
- PR title kept under 70 chars (parser warning otherwise).
