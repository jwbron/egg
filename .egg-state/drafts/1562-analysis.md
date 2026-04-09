### Task Analysis

**Problem statement**: When the overseer container crashes and is auto-respawned during a pipeline phase, the restart is invisible to external monitoring. The SDLC dashboard (`get_status`) shows no indication that the overseer restarted, the old container's diagnostic info (exit code, log tail) is lost when the container ID is replaced, and there's no message bus event to alert the user. More critically, the root cause is that `max_turns=500` is too low for the overseer's long-running monitoring loop.

**Source context**: Issue #1562 documents pipeline `issue-1558` where the overseer restarted ~24 minutes into execution. The respawned overseer's first poll detected 5 missed progress events from the critical consensus negotiation period (NACKs at 15:14, re-proposals at 15:14-15:15). The pipeline completed but with unexplained delay.

**System context**: The overseer lifecycle works as follows:
- Spawned at phase start in `_execute_pipeline_phases()` (`pipelines.py:7270-7290`) before regular agents
- Monitored by a health poll thread (`_health_monitor_poll`, `pipelines.py:7135-7175`) that runs every 30 seconds
- The poll thread calls `_check_and_respawn_overseer()` (`pipelines.py:123-204`) which queries Docker for container status
- If the container is EXITED/FAILED/REMOVED, and `overseer_respawn_count < max_overseer_respawns` (default 3), a new overseer is spawned
- The respawn count resets to 0 at each phase start (line 7284)
- The `MessageStore` (`message_store.py`) provides `OVERSEER_ALERT` message type, and `_get_message_store()` (line 3479) is the standard accessor pattern in `pipelines.py`

**Technical root cause**: Two issues:

1. `max_turns=500` is hardcoded in `spawn_overseer_container()` (`container_spawner.py:1062`). The overseer runs a continuous poll-classify-act loop using ~2-10 Agent SDK turns per 30-second cycle depending on alert activity. At 24 minutes with active consensus negotiation (NACKs, re-proposals, corrective actions), that's ~48 cycles × ~10 turns = ~480 turns, hitting the 500 limit. The SDK stream ends, the agent exits cleanly (code 0), and the health monitor respawns it. There is no `overseer_max_turns` config — it's hardcoded.

2. `_check_and_respawn_overseer()` (lines 168-196) performs the respawn but only logs to the orchestrator's Python logger. It does not:
   - Broadcast a message bus event — the `MessageStore` is not used, so `get_status`/`recent_messages` shows nothing
   - Capture the old container's log tail — `spawner.docker.get_container_logs()` is available and works on exited containers, but is not called before the container ID is replaced
   - Record structured respawn metadata

**Files affected**:
- `orchestrator/models.py` — Add `overseer_max_turns` field to PipelineConfig
- `orchestrator/container_spawner.py` — Add `max_turns` parameter to `spawn_overseer_container()`, use it instead of hardcoded 500
- `orchestrator/routes/pipelines.py` — Pass `overseer_max_turns` at both spawn sites; in `_check_and_respawn_overseer()`, capture old container log tail and broadcast OVERSEER_ALERT on respawn
- `orchestrator/tests/test_overseer_spawn.py` — Tests for new config field, max_turns passthrough, respawn broadcast
- `orchestrator/tests/test_phase_scoped_overseer.py` — Update affected respawn tests

**Risks / edge cases**:
- Log capture on a `ContainerNotFoundError` container will fail — handle gracefully (skip log capture, note "container deleted" in broadcast)
- `_get_message_store()` can return `None` if imports fail — broadcast must be best-effort, not block the respawn
- Log capture adds ~100ms latency to respawn path — acceptable since overseer is already down