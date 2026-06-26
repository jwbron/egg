# task_planner BRC memory — issue-2270-overhaul (plan phase)

## Pipeline identity (do NOT confuse with leftovers)
- THIS pipeline = **issue-2270** "Overseer overhaul (open season)". The sibling
  `.egg-state/agent-outputs/task_planner/brc-memory.md` is **STALE** (issue-3077) — ignore it.

## Status
- v2 plan (ADOPTS architect DAG) at `.egg-state/drafts/issue-2270-overhaul-plan.md`, committed + proposed.
- Validated locally (PYTHONPATH=shared; no .venv present): parse_plan success, **0 warnings**;
  preflight OK; forest=[]; overlap=[]; role_alignment(repo=jwbron/egg)=[]. **9 slices, 30 tasks.**
- v1 (my own ordering) was SUPERSEDED: rebased onto origin after architect + risk_analyst pushed;
  re-aligned to the architect's slices.yaml.

## Plan shape (defend on NACK unless reviewer shows it wrong)
- **ADOPTED the architect's 9-slice DAG** (`issue-2270-overhaul-architect-slices.yaml`) verbatim
  (numbering/names/goals). Architect DAG is multi-parent (s4←[1,3], s7←[1,4], s8←[4,7], s9←[3,5,6,8]).
- The #2137 forest validator forbids >1 parent, so contract `dependencies` is encoded as the **linear
  chain slice-1→…→slice-9** — a VERIFIED topological sort of the architect DAG (script-checked). It
  preserves every architect ordering edge incl. the hard invariant "detection plane (s4) + corpus (s1)
  live before respawn deletion (s5)", is a forest, and makes all #3046 overlaps transitively ordered.
- Architect's ORDER (key difference from my v1): s2 model-tiering + s3 spawn-norm come EARLY (the s4
  detection plane needs normalized spawn + resolver to spawn the on-demand adjudicator); s4 plane built
  BEFORE s5 deletes the pod; s7 signal-fixes plug INTO the s4 plane.
- s1 corpus doc → `docs/architecture/overseer-calibration-corpus.md` (NOT tests/ — a .md under tests/
  is unwritable by every role; this was the only role-align trap, fixed).
- Slice map: 1 corpus+harness(§2 deliverable#1) · 2 model tiering(§1,#2813) · 3 spawn-norm(§1.5) ·
  4 detection plane + adjudicator (Option C core) · 5 lifecycle/respawn(§3) · 6 authority/corrective
  vocab(§4) · 7 signal fixes(§2) · 8 coverage survey(§5 all-in-one) · 9 cleanup+docs(§6).
- §4 real enforcement = gateway/agent_restrictions.py + contract RBAC (NOT stale roles.py:can_modify).
  §7b: RETAIN #3123 brc-confirmation-timeout nudge (golden-file). §6: issue_filer.py IS used — keep;
  monitor.py decomposition rides #2817 (out).

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
