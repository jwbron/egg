# refiner BRC memory — issue-3229 (refine)

## CRITICAL CORRECTION (v2) — the issue's premise is FALSE
- The issue body (lines 226-228) and contract task_description ("## History") claim
  "#3064 never merged to main … Nothing from either attempt is on main; clean re-run."
  **This is factually WRONG.** Verified against origin/main @74838edb4:
  ALL SIX #3064 slices are MERGED — PRs #3167 (slice-1 flag), #3169 (slice-2 event loop),
  #3181 (slice-3 JobSupervisor), #3192 (slice-4 worktree/session), #3198 (slice-5 health-mode),
  + docs #3202/#3204/#3205. With test coverage (test_event_loop.py + orchestrator-mode tests
  in test_concurrent_executor/test_consensus_wrapper/test_kubernetes_spawner/test_heartbeat).
- The full orchestrator-owned on-demand spawning mechanism #3229 describes BUILDING already
  EXISTS on main, behind EGG_EVENT_LOOP_OWNER (default "pod"): event_loop.py (OrchestratorEventLoop,
  JobSupervisor, compute_dedupe_key on proposal_commit_sha, restart reconcile, convergence-stall);
  kubernetes_spawner.spawn_event_job + LABEL_EVENT_DEDUPE adoption + pre-spawn worktree cleanup;
  concurrent_executor._start_event_loop/_ExecutorEventSpawner/orchestrator-mode branch (spawns 0
  agents up front)/_teardown_exhausted_session; env_config.get_event_loop_owner (default "pod");
  health_monitor.set_orchestrator_mode/_orchestrator_skip_tripwire; consensus_wrapper ONE_SHOT_OWNER.
- The ONLY undone piece is the default flip + live-BRC proving run, which the issue itself defers
  to #3164 (out of #3229 scope). So #3229 has no obvious in-scope structural work vs main.

## Status
- v1 (commit a24748b) was greenfield-scoped ("build the mechanism behind a flag") — WRONG; both
  reviewers NACked v1 on the false "nothing on main" premise (reviewer_refine B1, reviewer_agent_design).
- v2: draft rewritten to reconcile against main (inventory landed #3064 as foundation, re-derive real
  gap, reframe scope+ACs to adopt/verify/gap-fill, fix build_consensus_wrapped_command attribution
  → defined at consensus_wrapper.py:1216 not concurrent_executor). HITL cq-1 registered. Re-proposed v2.

## HITL — cq-1 (registered, mcp__sdlc__register_open_question)
- adopt-vs-reimplement conflict: issue premise vs git state. Options:
  opt-1 adopt #3064 + verify/gap-fill only genuine delta; opt-2 operator names specific defect;
  opt-3 collapse #3229 into #3164. MUST be resolved by operator before plan. Do NOT re-litigate.

## Verdict / position (v2)
- Scope is now PROVISIONAL and CONDITIONAL on cq-1 — NOT a greenfield Option-B build.
- Do NOT plan a fresh build of the mechanism (adopt-vs-reimplement hazard; operator directive forbids).
- Hard constraint (spawner-first / guard-depends-on-spawner from scrapped #3023) is ALREADY satisfied
  on main (flag defaults to in-pod, spawner present).

## If NACKed again
- Address each blocker; edit draft in place, re-commit, re-propose (version bump). Keep the reconciled
  current-state (mechanism is ON MAIN) and the cq-1 HITL — those are reviewer-required and verified.
  Do NOT revert to greenfield framing. Cite file:line for disputed claims.

## Decision log
- 2026-06-24 v1: greenfield Option-B analysis, proposed a24748b — NACked by both reviewers (false premise).
- 2026-06-24 v2: verified #3064 fully merged on main; rewrote draft to adopt/verify/gap-fill framing;
  registered HITL cq-1; re-committed + re-proposed.
