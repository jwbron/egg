### Task Analysis

**Problem statement**: The restart PR #1577 introduced agent-level and phase-level restart functionality, but review identified 7 non-blocking issues ranging from data corruption risks (P0) to latent footguns (P3).

**Source context**: GitHub issue #1695 tracks these deferred review items. The issue provides a clear breakdown with suggested implementation order: 1 → 4 → 2+3 → 5 → 6 → 7 (grouping items that touch the same files).

**System context**: Agent restart flows through two paths: (1) MCP/REST API via `routes/pipelines.py:restart_agent` → `ContainerSpawner.restart_agent_container()`, and (2) overseer automated restart via `monitor.py:_execute_restart_agent()` → REST API → same route. Both paths track restart counts independently — the spawner in `_restart_counts` keyed by `(pipeline_id, role)`, and the overseer in `_agent_restart_counts` keyed by `role` alone. Consensus state is managed by both `peer_consensus` and legacy `consensus` modules, reset in the route handler before the container spawn attempt.

**Technical root cause** (per issue):
1. **P0 — No concurrency guard**: `_restart_counts` dict is read-modify-written without locking in `container_spawner.py`. Concurrent callers (overseer + human MCP) can both read count=0, both pass the limit check, both spawn. The `ContainerSpawner` class has no threading imports at all.
2. **P0 — Dual counters**: Spawner tracks `_restart_counts` keyed by `(pipeline_id, role)` (L155), overseer tracks `_agent_restart_counts` keyed by `role` only (L121). MCP restart increments spawner only; overseer stays at 0. Max 4 restarts possible instead of intended 2.
3. **P1 — Not phase-keyed**: Overseer's `_agent_restart_counts` (monitor.py:120-122) keyed by `agent_role` only, never reset on phase transition. After 2 restarts in `plan`, agent permanently blocked in `implement`. Phase transition detection at L1623-1637 does NOT reset this counter, unlike `container_spawner.reset_restart_counts()` at L891-899.
4. **P1 — Count on success only**: `container_spawner.py:867` increments after `spawn_agent_container()` succeeds. If Docker consistently fails (e.g., out of disk), count stays 0 → infinite retries via the limit check at L796.
5. **P1 — Non-atomic consensus reset**: `routes/pipelines.py:1249-1292` wipes consensus (both peer_consensus tracker and legacy evaluator) before the restart attempt at L1386. If `restart_agent_container()` raises `ContainerSpawnError`, consensus is gone but no container exists — broken pipeline state.
6. **P2 — Infra errors → HITL only**: `decision_maker.py:59-66` unconditionally returns `"action": "hitl"` for all `infrastructure_error` classifications, bypassing the LLM decision process at L68-91 which includes `restart_agent` as an option. Hung/crashed agents never get auto-restarted. The same pattern exists in `escalate_redirect_decision` at L140-177.
7. **P3 — Default mode="public"**: `container_spawner.py:748` has `mode: str = "public"`. Both existing callers (routes/pipelines.py L1390) pass `mode=gateway_mode` explicitly via `_compute_gateway_mode()`, but a future caller omitting `mode` silently gets public mode for private repos.

**Files affected**:
- `orchestrator/container_spawner.py` — Issues 1, 4, 7: add per-key lock, increment before spawn, remove default mode
- `orchestrator/overseer/monitor.py` — Issues 2, 3: remove shadow counter, read from REST API response
- `orchestrator/routes/pipelines.py` — Issue 5: move consensus reset after successful spawn
- `orchestrator/overseer/decision_maker.py` — Issue 6: route restartable infra errors to restart_agent
- `orchestrator/tests/test_restart_agent.py` — Updated tests
- `orchestrator/tests/test_restart_overseer.py` — Updated tests

**Risks / edge cases**:
- Issue 1: Lock granularity matters — per-(pipeline_id, role) avoids contention across unrelated restarts
- Issue 4: Incrementing before spawn means a transient Docker failure burns a restart attempt. This is the correct tradeoff — the alternative (infinite retries) is worse
- Issue 5: If we move consensus reset after spawn, a successful spawn with a failed consensus reset leaves stale consensus state — but this is recoverable (agent re-enters consensus), unlike the current failure mode (orphaned consensus deletion)
- Issue 6: Need to be conservative about which infra errors are "restartable" — only unresponsive/crashed/OOM/timeout, not permission/filesystem errors