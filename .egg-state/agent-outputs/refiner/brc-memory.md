# refiner BRC memory — issue-3064 (refine)

## IMPORTANT: prior memory was stale
- Earlier memory in this file referenced issue-3077 (analysis at `.egg-state/drafts/3077-analysis.md`, HITL cq-1/cq-2 about served-state scope). That belonged to a DIFFERENT pipeline. This pipeline is **issue-3064** ("Orchestrator-driven on-demand agent spawning"). The gateway rejected the 3077-path proposal; contract confirmed pipeline_id=issue-3064. Do not act on 3077 content here.

## Status
- v1 analysis written to `.egg-state/drafts/3064-analysis.md`, HITL cq-1 (scope A/B/C; recommended B) and cq-2 (failure supervision; recommended bounded respawn + alert) registered on the issue-3064 contract. Committed + proposed (see decision log).

## Verdict / position
- Recommended scope (Option B): on-demand spawner for propose|ack|nack + ownership flag defaulting to in-pod loop + spawn dedupe (role + proposal_commit_sha / nack-version) + bounded respawn supervision + confirm/complete orchestrator-side, PLUS worktree re-attach & session reuse, idle-budget/stall alerts re-homed orchestrator-side, lifecycle-aware health-monitor thresholds, #2806 signaling relocated. Default flip = gated follow-up after a live BRC cycle (issue's own bar).
- Hard constraint (from scrapped #3023): guard + spawner land together or spawner-first; no rollback flag exists since #2908 slice-4 deleted EGG_BRC_EVENT_PUMP.
- Key grounded facts: spawn-up-front at concurrent_executor.py:311-349 / kubernetes_spawner.py:491-940; in-pod loop consensus_wrapper.py:110-916 (wait-loop ≈379, heartbeat 30s ≈209-230, idle budget alert-only ≈702-720, streak backoff ≈897-901); _derive_next_action routes/consensus.py:296-422 (proposal_commit_sha in pending_reviews ≈220-221); confirm/complete already agent-free in wrapper; durable memory brc_memory.py atomic-write; tracker rebuilt from message store (#2761); worktrees hostPath-persistent (#3005/#2403).

## If NACKed
- Address reviewer points by editing `.egg-state/drafts/3064-analysis.md` in place, re-commit, re-propose (version bumps). Keep scope options A/B/C unless a reviewer shows a factual error. Cite file:line for any disputed claim.

## Decision log
- 2026-06-12: discovered stale 3077 memory; rebuilt analysis for issue-3064 from issue body (re-verified 2026-06-11 by author) + codebase exploration; registered cq-1/cq-2; proposed v1.
