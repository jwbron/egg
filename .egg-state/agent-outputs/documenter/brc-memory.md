## Codebase / change model

Issue #3023 splits the consensus-wrapper retirement and the orchestrator-
side phase-level idle-budget alert into three slices:

- **slice-1** ("Orchestrator-side phase-level idle-budget alert") —
  introduces `orchestrator/phase_idle_budget.py` + wiring through
  `routes/pipelines.py` and a coexistence env (`EGG_PHASE_IDLE_BUDGET_OWNER`)
  in `consensus_wrapper.py`. **All three slice-1 tasks (task-1-1, task-1-2,
  task-1-3) carry `role: null`.** No documenter assignment in this slice.
- **slice-2** ("On-demand spawner + worktree-PVC and gateway-session reuse") —
  the OnDemandSpawner, session keep-alive, per-event spawn entry point,
  orphan-commit detector, atomic brc-memory writes, etc. No documenter
  assignment.
- **slice-3** ("`consensus_wrapper.py` retirement + entrypoint
  simplification") — **`task-3-4` is the only documenter task in the
  whole issue.** Deliverable:
  - Refresh `docs/architecture/orchestrator.md` (and `lifecycle.md` if
    present) to remove the long-lived in-pod event-pump section and add
    a per-event on-demand-spawn ASCII diagram.
  - Update `docs/guides/concurrent-execution.md` runbook: replace
    per-role stuck-phase-transition guidance with phase-level guidance
    pointing at the `per_role_state` payload + `pending_hitl_ids`
    reason; add a per-spawn pod-log retention section (last N=20).
  - PR-body deliverables: cq-6 overseer-unification follow-up issue
    link; `spawn_all` rename follow-up issue link; the
    drain-then-revert protocol verbatim (AC-R7); before/after grep
    output (AC-R10).
  - Gated on slices 1 and 2 having produced the implementation it
    documents.

## Per-producer assessment

### documenter

- producer: documenter
- last_reviewed_commit_sha: 8d51b1a97d67021ee7182e3bef52af0afca441db
- prior_verdict: WITHDRAWN
- prior_nack_reasons:
  - reviewer_code v2: empty proposal (zero producer commits ahead of
    `origin/main`), `docs/architecture/lifecycle.md` listed but absent
    from tree, slice-3 deliverables proposed while in slice-1,
    documenter has no slice-1 task assignment.
- prior_conditional_obligation: (none)
- summary_of_assessment: The v2 proposal was filed in slice-1 with no
  commits and slice-3-scope artifact references. Reviewer NACK was
  100% correct against the contract. Proposal withdrawn via
  `egg-orch consensus withdraw`. **Do not re-propose in slice-1.**
  Wait for orchestrator to spawn the role in slice-3 (or for the
  operator to amend the slice-1 contract to add a documenter task).
  If re-spawned for slice-1 with no contract change, re-withdraw
  immediately with the same reason.

## Decision log

- 2026-06-09T02:34Z WITHDRAW documenter v2: empty proposal, no
  slice-1 work assigned, slice-3 docs cannot begin until slices 1-2
  land their implementation; concurring with reviewer_code NACK.
