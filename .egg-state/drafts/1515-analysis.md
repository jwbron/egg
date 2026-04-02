### Task Analysis

**Problem statement**: When a user calls `cancel_task` with `cleanup: true`, the MCP call consistently times out even though the cancellation succeeds. The caller sees a timeout error and must make a follow-up `get_status` call to confirm.

**Source context**: Issue #1515 — observed on a pipeline with 4 running containers. The pattern is consistent: "the first cancel always seems to time out."

**Workarounds**: Call `get_status` after the timeout to confirm cancellation went through.

**System context**: The `cancel_task` MCP handler (`orchestrator/mcp_tools.py:948`) makes two sequential synchronous HTTP calls:
1. **PATCH** `/api/v1/pipelines/{task_id}` (120s timeout) — marks pipeline cancelled, then synchronously cleans up decisions, containers, sessions, and worktrees inline before returning
2. **DELETE** `/api/v1/pipelines/{task_id}` (120s timeout) — re-cleans containers, deletes remote branches, clears Redis, deletes state file

The PATCH handler (`orchestrator/routes/pipelines.py:822-875`) does the heavy lifting synchronously: for each container it calls `docker.remove_container()` (blocks on Docker daemon), `gateway.delete_session_by_container()` (HTTP call), then iterates worktrees calling `gateway.delete_worktrees()` (HTTP call each). With 4+ containers, this chain of synchronous I/O easily exceeds the MCP client's timeout.

**Technical root cause**: The PATCH handler at `pipelines.py:841-863` calls `spawner.cleanup_pipeline()` synchronously before returning the HTTP response. `cleanup_pipeline()` (`container_spawner.py:637`) iterates containers sequentially, calling `docker.remove_container(force=True)` + `gateway.delete_session_by_container()` for each, then iterates worktrees calling `gateway.delete_worktrees()` for each. Each Docker remove can take seconds (especially with force-kill), and each gateway call adds network latency. With 4 containers + worktrees, this easily takes 30-60+ seconds. The DELETE handler then repeats container cleanup again.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Move container/worktree cleanup in the PATCH handler to a background thread, return response immediately after marking status + cancelling decisions + syncing state
- `orchestrator/routes/pipelines.py` — In DELETE handler, container cleanup can stay synchronous (it's expected to be slower), but skip if containers were already cleaned by the PATCH