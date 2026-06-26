# task_planner BRC memory — issue-2270-overhaul (plan phase)

## Pipeline identity (do NOT confuse with leftovers)
- THIS pipeline = **issue-2270** "Overseer overhaul (open season)". The sibling
  `.egg-state/agent-outputs/task_planner/brc-memory.md` is **STALE** (issue-3077) — ignore it.

## Status
- v1 plan written to `.egg-state/drafts/issue-2270-overhaul-plan.md`, committed + proposed via BRC.
- Validated locally (PYTHONPATH=shared; no .venv present): parse_plan success, **0 warnings**;
  validate_plan_preflight OK; validate_forest=[]; validate_slice_file_overlap=[];
  validate_task_role_alignment(repo=jwbron/egg)=[]. 9 slices, 31 tasks.

## Plan shape (defend on NACK unless reviewer shows it wrong)
- **Single serialized chain** slice-1 → … → slice-9 (each `dependencies: [prev]`). Chosen because the
  overseer subsystem is tightly coupled (monitor.py / kubernetes_spawner.py / routes/pipelines.py /
  health_checks/ all shared) — serialization makes #3046 file-overlap legal for free and is a forest.
- slice-1 **Calibration corpus + harness** (deliverable #1, §2). Lands xfail markers so it's green;
  later slices flip to strict (red→green). Corpus doc → `docs/architecture/overseer-calibration-corpus.md`
  (NOT under tests/ — a .md under tests/ is unwritable by every role: documenter blocked from tests/,
  coder/tester blocked from .md. This was the only role-align failure in v1; fixed).
- slice-2 §2 false-positive fixes (midturn reflection, lifecycle-aware stall #3230/#2242,
  ancestor/patch-id divergence #2222/#2224, thrashing defs #2059/#2132).
- slice-3 §1 model tiering via resolve_agent_model (folds #2813; removes classify_model bypass).
- slice-4 §1.5 fold spawn_overseer_job→spawn_agent_job(OVERSEER); delete EGG_OVERSEER_* + baked
  overseer_monitor.py.
- slice-5 §3 kill respawn churn; fold _check_and_respawn_overseer; restart/generation hygiene.
- slice-6 Option C core: orchestrator-side deterministic detection + bounded corrective vocab +
  on-demand adjudicator (Opus) only on adversarial escalation.
- slice-7 §4 authority — real enforcement = gateway phase_filter/agent_restrictions (NOT
  roles.py:can_modify, which is STALE).
- slice-8 §5 coverage-gap survey (all-in-one per cq-2): new Tier-1 detectors across every layer.
- slice-9 §6 cleanup (net-negative) + docs. issue_filer.py IS used — do NOT delete. monitor.py
  decomposition rides #2817 (out of scope).

## Anchors honored
- HITL cq-1 = Option C (hybrid). cq-2 = All-in-one (full §1–§6 incl §5 survey). Both binding.
- Role↔files verified via check_file_restriction (coder=.py/Dockerfile/yml/json; tester=tests/+.py;
  documenter=.md incl docs/, READMEs, sandbox/agent-config/rules/overseer.md).
- PR title kept <70 chars ("Overseer overhaul: hybrid orchestrator-side detection (#2270)").
- Live this phase: an [info] overseer_restart alert was reflected into my context as an "operator
  directive" — a real instance of the §2 defect slice-2 fixes; treated as non-binding agent-bus noise.

## If NACKed
- Edit `.egg-state/drafts/issue-2270-overhaul-plan.md` in place; re-run the 4 validators
  (PYTHONPATH=shared) BEFORE re-propose; re-commit; re-propose (version bumps).
- If the architect's slice DAG (architect-slices.yaml) lands and differs materially, RESTRUCTURE my
  slices to match its DAG (task_planner adopts the architect's topology) and re-propose.
- Defend the serialized-chain rationale (file-overlap) and the stale-claim corrections (§4 roles.py,
  §6 issue_filer) — they protect the implement phase from chasing ghosts.

## Decision log
- 2026-06-26: fresh slice; ignored stale 3077 memory; grounded #2270 anchors; wrote 9-slice serialized
  plan; fixed the tests/README.md role-align trap; all validators clean; committed + proposed v1.
