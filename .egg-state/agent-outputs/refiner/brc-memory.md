# refiner BRC memory — issue-3258 (refine)

## IMPORTANT: prior memory was stale
- Earlier contents of this file referenced issue-3064 / issue-3077 — DIFFERENT pipelines. This
  pipeline is **issue-3258**: "Complete #3200 slice-10 — emit-only BRC context-discipline
  measurement surfaces (AC-4+AC-5 of #3200)." Base branch = `main`. Ignore all 3064/3077 content.

## Status
- v1 analysis written to `.egg-state/drafts/issue-3258-analysis.md`. Registered BLOCKING HITL
  OQ-1 (substrate dependency, options A/B/C/D; recommended A-if-stacking-else-D). Committed + proposed.

## Verdict / position
- Scope is emit-only: emit 6 per-event metrics (window occupancy = cache_read+cache_creation+input;
  peak context util under resume; single-event working set vs real backend window
  [recursion-escalation signal]; reseed freq per phase; root-cache hit rate; tokens/event) through
  EXISTING progress/heartbeat/metrics surfaces. NOTHING gated — a test must assert no decision
  branches on emitted values. No measurement run / A/B / aggregation-to-verdict (owned by #3249).

## CRITICAL grounded fact (drives OQ-1)
- The "slices 1-9 already merged" premise is FALSE for base=main. slice-1 occupancy (#3236, OPEN,
  base egg/issue-3200/work) and slice-8 reseed (#3251, OPEN, UNSTABLE) are UNMERGED; origin/main has
  no `occupanc`/`reseed` code. `AgentResult` (shared/egg_agent/result.py:7-34) has only
  cost_usd/num_turns/duration_ms/session_id/metadata — no occupancy field. Token data exists only
  downstream in config/litellm/cost_callback.py:173-206.
- Existing surfaces to reuse: progress_emit (sandbox/egg_agent_tools/handlers/progress.py:35-84 →
  orchestrator/routes/progress.py:56-131, EventType.PROGRESS_EMITTED); heartbeat
  (handlers/progress.py:114-132; orchestrator/heartbeat.py:45-168 HeartbeatCoordinator);
  metrics registry (orchestrator/routes/metrics.py:1-81, counters/gauges/histograms, post-hoc only).
- Per-event seam: orchestrator one-shot event loop (orchestrator/event_loop.py,
  consensus_wrapper.py:~93-100,429); AgentResult NOT currently read back post-event by orchestrator.

## If NACKed
- Edit `.egg-state/drafts/issue-3258-analysis.md` in place, re-commit, re-propose (version bumps).
  Hold the emit-only line and the unmerged-substrate finding unless a reviewer shows a factual error
  (cite file:line). Keep OQ-1 options A/B/C/D until operator resolves.

## Decision log
- 2026-06-25: rebuilt memory for issue-3258 (was stale 3064); grounded codebase; discovered
  slice-1/slice-8 substrate UNMERGED on main; wrote v1 analysis; registered HITL OQ-1; proposing v1.
