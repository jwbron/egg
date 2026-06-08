# Refiner BRC memory — issue #3023

## Context

Issue #3023: Orchestrator-driven on-demand agent spawning. Lift the BRC event-pump loop out of the agent pod into the orchestrator so the orchestrator spawns a pod only when a role has an actionable event.

## Proposal v1 — submitted

Artifact: `.egg-state/drafts/3023-analysis.md`

### Recommended approach

**Option A**: Orchestrator-side per-event spawner with per-role worktree PVC reuse + per-role gateway-session reuse for the phase. Reuses every existing primitive (`_derive_next_action` in `orchestrator/routes/consensus.py:280-406`, `compose_event_prompt` in `orchestrator/routes/event_prompt.py:337-447`, durable BRC memory at `.egg-state/agent-outputs/<role>/brc-memory.md`, `spawn_specific_roles` in `orchestrator/concurrent_executor.py:378-400`, `resolve_agent_model` from #2769). New code lands in a dedicated submodule rather than the in-flight #2261 slice-15 barrel.

### Options enumerated

- **A**: Orchestrator-side per-event spawner + per-spawn worktree/session reuse (**recommended**)
- **B**: Lift only the wait (push notifications instead of long-poll) — fails the primary goal (pods still long-lived)
- **C**: Status quo (do nothing) — fails both goals
- **D**: Per-event spawn with fresh worktree+session — blows cold-start budget

### HITL decisions registered

- **cq-1** — cold-start latency budget (≤3s / ≤8s / ≤20s / no fixed budget)
- **cq-2** — gateway session lifecycle (per-role-phase / per-spawn / orchestrator-wide)
- **cq-3** — idle-budget alert owner (orchestrator per-role / phase-level / drop)
- **cq-4** — rollout shape (per-pipeline flag / per-phase flag / big-bang)
- **cq-5** — consensus_wrapper.py retirement scope (delete / thin wrapper / env-flag coexist)
- **cq-6** — overseer agents in scope? (out / in / follow-up issue)

### Open feedback registered

- **feedback-1** — Q1-Q5 covering resource-saving target, debug-shell affordance loss, in-flight PVC work (#2866, #3017), #2958 streaming-commits adjacency, and idle-budget tuning.

## Foundation noted (already in place — #2908 slice 3+4)

- `compose_event_prompt` is per-event stateless (file:`orchestrator/routes/event_prompt.py:337-447`)
- Per-role BRC memory at `.egg-state/agent-outputs/<role>/brc-memory.md` survives pod restart
- Server-side prefix cache (Anthropic / LiteLLM), ≥60min TTL, pod-independent
- `_derive_next_action` already enumerates the spawn-trigger verbs (`routes/consensus.py:77`)
- `spawn_specific_roles` is the existing seam for partial spawns (`concurrent_executor.py:378-400`)

## Tracking

| Producer | Last reviewed commit SHA |
|---|---|

*(No upstream producers in refine — refiner is the only producer. This table will populate as reviewers ACK/NACK and re-reviews happen.)*

## Notes for re-propose if NACKed

- Decisions section follows analysis.md template guidance: scope/intent only, no slice-DAG decomposition (architect's call), no API-shape decisions (planner's call).
- Slice-decomposition is mentioned in the Complexity Assessment as advisory but is explicitly deferred to the architect.
- File-size discipline (#2261 slice-15 for `routes/pipelines.py`) is flagged in Constraints so the plan picks the right module location.
