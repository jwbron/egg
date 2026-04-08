### Task Analysis

**Problem statement**: The overseer container runs for the entire pipeline lifetime, sitting idle between phases and during HITL review windows. This wastes resources and accumulates state across phase boundaries.

**Source context**: Issue #1560 proposes making the overseer phase-scoped — spawn at phase start, tear down at phase completion. The issue notes the Tier 1 deterministic health checks (`orchestrator/overseer/monitor.py`) can continue independently; only the Tier 2 LLM-assisted overseer agent needs phase-scoping.

**System context**: The pipeline execution engine lives in `orchestrator/routes/pipelines.py`. The main execution function has a `while True` phase loop (line 6258) that iterates through phases. Currently:
- The overseer is spawned **once** before the phase loop (line 6161) via `spawner.spawn_overseer_container()`
- A health monitor polling thread runs `_check_and_respawn_overseer()` every 30s (line 6224) to detect and restart crashed overseers
- The overseer is torn down **once** in the `finally` block after the loop exits (line 7147)
- The container is named `egg-{pipeline_id}-overseer` (fixed per pipeline)

Phase transitions happen at line 7068-7072 where `pipeline.current_phase = next_phase`. The `_clear_concurrent_state()` helper (line 84) already clears message/consensus state at transitions — a useful pattern for the overseer lifecycle.

**Technical root cause**: The overseer is spawned at pipeline scope rather than phase scope. The spawn call at line 6161 runs once before the phase loop; the teardown at line 7147 runs once after it exits. There is no spawn/teardown at phase boundaries.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Move overseer spawn from pre-loop (line 6161) into the phase-start block (around line 6287). Add overseer teardown before phase advance (around line 7068). Update `_health_monitor_poll` to not respawn overseer when it's intentionally absent between phases.
- `orchestrator/container_spawner.py:828` — Update docstring/comment referencing "entire pipeline lifetime" to reflect phase-scoped lifecycle
- `orchestrator/tests/test_overseer_spawn.py` — Update tests for the new phase-scoped spawn behavior
- `orchestrator/tests/test_pipeline_failure_path.py` — Update assertion at line 1425 that expects overseer spawn at pipeline level

**Risks / edge cases**:
- The `_check_and_respawn_overseer` respawn logic must be aware that the overseer is intentionally absent between phases. A `phase_overseer_active` flag gates this.
- Container naming (`egg-{pipeline_id}-overseer`) is fixed, so the same name is reused across phases. The existing cleanup-before-spawn logic in `spawn_agent_container` (lines 246-272) handles this already.
- Graceful shutdown mid-poll: `stop_agent_container(timeout=10)` handles this.
- Cross-phase overseer state is intentionally lost (per-issue design goal).