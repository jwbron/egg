# documenter BRC memory — pipeline issue-3023 slice-3

## Last reviewed commit SHA

_(none yet — initial proposal)_

## Decision log

### 2026-06-09 — initial slice-3 documenter proposal

Context: slice-3 of #3023 is "consensus_wrapper.py retirement + entrypoint
simplification". My task is task-3-4 — update docs/architecture/orchestrator.md
and docs/guides/concurrent-execution.md to describe the new on-demand spawn
model after `orchestrator/consensus_wrapper.py` is deleted, plus add a new
ASCII architecture diagram and prepare the PR-body deliverables.

Substrate state at proposal time: slice-1 has been merged to main
(commit `330e37836b` — phase-level idle-budget alert + wrapper coexistence
guard). Slice-2 has TASK-2-1 committed (commit `068388f8c0` — derive_next_action
exposed as module-level callable). Slice-3 implementation tasks (3-1, 3-2,
3-3, 3-5) have `role=null` in the contract and have not been started.
My docs describe the planned post-#3023 architecture per the contract; the
docs will land alongside the slice-3 code changes in the same PR.

### Decisions made by this proposal

1. **Section title.** Renamed `## BRC Consensus Wrapper` in
   `docs/architecture/orchestrator.md` to `## BRC On-Demand Agent Spawning`
   to reflect the new design center. All in-doc cross-references (e.g. from
   the BRC Per-Event Prompt Composer section, the env-vars table) were
   updated to the new anchor.
2. **Section title.** Renamed `## Consensus Wrapper` in
   `docs/guides/concurrent-execution.md` to `## On-Demand Agent Spawning`
   for the same reason.
3. **ASCII diagram.** Added a new ASCII flowchart at the top of the
   concurrent-execution.md section showing the orchestrator per-phase tick
   driving derive_next_action → coalesce → compose_event_prompt → spawn_fn
   → one-shot pod. The diagram puts the per-PVC + per-(pipeline,role) gateway
   session in the pod box so the reuse model is visible at a glance.
4. **Runbook content (AC-R4 + AC-R5).** Added three runbook subsections in
   concurrent-execution.md: triaging `stuck-phase-transition` (with
   per_role_state payload + pending_hitl_ids interpretation), triaging
   `orphan_commit_post_spawn`, and triaging `missing_session_token`. Added a
   per-spawn pod-log retention subsection explaining the
   `.egg-state/agent-outputs/<role>/spawn-<N>.log` path and the
   `PER_ROLE_LOG_RETENTION = 20` / `EGG_SPAWN_LOG_RETENTION` semantics.
5. **Env-vars table.** Updated `EGG_BRC_EVENT_PUMP` and
   `EGG_BRC_IDLE_BUDGET_MIN` rows to reflect #3023 ownership; added
   `EGG_PHASE_IDLE_BUDGET_OWNER`, `EGG_EVENT_LOOP_OWNER`,
   `EGG_ON_DEMAND_SPAWN`, `EGG_SPAWN_INDEX`, `EGG_SPAWN_LOG_RETENTION` rows
   per slice-1 + slice-2 acceptance criteria.
6. **Cross-doc consistency.** Lines 52, 68, 70 of orchestrator.md
   (pre-existing startup-reconciliation narrative) were updated to drop
   anachronistic "consensus wrapper exhausted restarts" framing and replace
   it with on-demand-spawn-accurate language. The composer / preamble
   sections kept their substance (the composer code did not change) but had
   their dispatcher framing updated from "wrapper invokes" to "orchestrator
   invokes via OnDemandSpawner.tick" and the broken anchor link from
   `#brc-consensus-wrapper` to `#brc-on-demand-agent-spawning` was fixed.
7. **lifecycle.md is intentionally not added.** The task description says
   "same for docs/architecture/lifecycle.md if present" — the file does not
   exist in the codebase, so no edits there.

### Deferred to PR-phase (not in this slice-3 implement proposal)

Per task-3-4 acceptance criteria:

- **Before/after grep output** for `consensus_wrapper|build_consensus_wrapped_command|EVENT_PUMP_|is_buffer_overflow|is_transient_crash|is_startup_failure` over `orchestrator/`, `sandbox/`, `shared/` — to be captured at PR-body composition time, after slice-3 tasks 3-1/3-2/3-3 have actually deleted the wrapper. Cannot be produced now because the wrapper still exists in the codebase.
- **cq-6 overseer-unification follow-up issue link** — to be filed and linked at PR closeout.
- **spawn_all rename follow-up issue link** — to be filed and linked at PR closeout.
- **Drain-then-revert protocol in the PR body** — already drafted verbatim in `docs/architecture/orchestrator.md` ("Rollback plan — drain-then-revert (cq-4 big-bang)" subsection); the PR-body copy will lift from that subsection.

## NACK history

_(none yet)_

## Style / convention notes

- Cite issue numbers as `[#NNNN](https://github.com/jwbron/egg/issues/NNNN)` on
  first mention in a section; bare `#NNNN` on subsequent mentions.
- Cross-reference within `docs/architecture/orchestrator.md` via
  `[label](#anchor)`; cross-reference between files via the relative path.
- Preserve the existing `> blockquote` style for section preambles.
- Prefer function-name navigation anchors over hard line numbers when
  citing source (line numbers drift; function names don't).
