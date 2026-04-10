### Task Analysis

**Problem statement**: During stuck pipeline recovery (e.g., #1570), operators must use raw `curl` calls, `docker stop`, and file manipulation to manage phase transitions and contract population. Four REST endpoints that already exist (`advance_phase`, `start_phase`, `complete_phase`) or need to be created (`populate_contract`) are not exposed as MCP tools, forcing manual intervention.

**Source context**: Issue #1646, motivated by the #1570 incident where plan phase consensus completed but the HITL gate wasn't created. Recovery required 5+ manual steps across REST, Docker, and Python APIs. Also references #1641 (CLI import bug discovered during the same run).

**System context**: MCP tools are defined in `orchestrator/mcp_tools.py` as entries in the `PIPELINE_TOOLS` list (schema dicts) with corresponding `_handle_*` methods on `PipelineToolHandler`. The handler methods proxy to REST endpoints via `_make_request()`. The MCP server in `mcp_server.py` auto-registers all tools from `PIPELINE_TOOLS` — no additional wiring needed. REST endpoints for `advance_phase`, `start_phase`, and `complete_phase` already exist in `orchestrator/routes/phases.py`. The `_populate_contract_from_plan()` function exists in `orchestrator/routes/pipelines.py` (line 6758) but has no REST endpoint — it's called internally during phase transitions.

**Technical root cause**: The MCP tool layer (`mcp_tools.py`) simply lacks entries for these four operations. Three have REST endpoints ready (`routes/phases.py`); one (`populate_contract`) needs a new REST endpoint to wrap the internal function.

**Files affected**:
- `orchestrator/mcp_tools.py` — Add 4 tool schemas to `PIPELINE_TOOLS` + 4 `_handle_*` methods on `PipelineToolHandler`
- `orchestrator/routes/phases.py` — Add `populate_contract` REST endpoint (POST `/<pipeline_id>/phase/populate-contract`)
- `orchestrator/tests/test_mcp_tools.py` — Add tests for the 4 new tool handlers

**Risks / edge cases**:
- The `advance_phase` with `force=true` should also stop running containers per the issue. The current REST endpoint does NOT stop containers on force-advance — this is a behavior gap. The MCP tool handler should call the container stop endpoint before calling advance_phase when `force=true`, or the REST endpoint itself needs enhancement.
- `populate_contract` requires resolving the correct repo/worktree path — needs the same `resolve_worktree_path` logic used elsewhere in `routes/pipelines.py`.