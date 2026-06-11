# BRC Memory — risk_analyst — pipeline issue-3077 (plan phase)

## Producer state
- **Proposed**: risk assessment at `.egg-state/drafts/3077-plan-risk-analyst.json` (v1).
- Verdict: **MEDIUM / PROCEED_WITH_MITIGATIONS** for HITL-approved Option C scope.
- 7 risks: R1 spec-as-fourth-replica (HIGH/MED), R2 validator generalization bricks-or-silently-degrades, #3081 both edges (HIGH/HIGH), R3 artifact-read endpoint ref/scoping surface — requires_human_review (HIGH/MED), R4 sync-warning process-boundary placement, wrapper is a format-string template (MED/HIGH), R5 prose-deletion sequencing + ratchet false-positives (MED/MED), R6 spec module placement vs Dockerfile COPY lists — verified both containers already ship egg_contracts + egg_restrictions (MED/MED), R7 fail-loud trigger placement / alert fatigue (LOW/MED).
- Grounded against working tree: consensus_wrapper.py:487-539, signals.py:1067-1200, message_store.py:589-650, phase_filter.py:595-655, both Dockerfiles.

## Reviewer state (risk_analyst also reviews in plan)
- Not yet reviewed anyone. When task_planner/architect propose, check their plan against:
  1. R1: each hardcoding marked DERIVED vs ASSERTED + containment test task present.
  2. R2: explicit per-(role,condition) degradation matrix; no_changes_needed + sentinel-SHA exemptions; #3081 invariant preserved; observable skips.
  3. R3: hex-validated ref, name-only resolution, session-bound template params, explicit truncation, honest 404.
  4. R4: warning injected sandbox-side (wrapper), not orchestrator-side event_prompt.
  5. R5: phase-3 deletion ordered last; ratchet test has allowlist.
  6. R6: spec lives in shared/egg_contracts (or Dockerfile updates are an explicit task).
  7. depends_on edges: task2 → task3,task4; task5 last.
- NACK if the plan omits the R2 degradation matrix or puts the R4 warning orchestrator-side; otherwise ACK with notes.
