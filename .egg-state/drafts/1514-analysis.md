### Task Analysis

**Problem statement**: The `list_checkpoints` and `search_checkpoints` MCP tools always fail with `{"error": "Missing repos list"}`, making them unusable.

**System context**: These MCP tools live in `orchestrator/mcp_tools.py` and call gateway HTTP endpoints via `_make_gateway_request()`. Before making any gateway request, the MCP server registers a gateway session via `_ensure_gateway_session()` (line 1014). This calls `client.register_session(container_id="mcp-server", container_ip=..., mode="public")` — passing **no `repos` and no `pipeline_id`**.

**Technical root cause**: The gateway's session registration endpoint (`gateway/gateway.py:3752`) requires at least one of `repos`, `local_only_repos`, or `pipeline_id`:
```python
if not repos and not local_only_repos and not pipeline_id:
    return make_error("Missing repos list")
```
The MCP server provides none of these, so session registration fails before the checkpoint request is even made. The error propagates back as the tool's response.

The gateway already has a carve-out for orchestrator-internal sessions: "repos can be omitted for orchestrator-internal sessions that have a pipeline_id". The MCP server is exactly this kind of internal component but doesn't use the exemption.

**Files affected**:
- `orchestrator/mcp_tools.py` — Fix `_ensure_gateway_session()` to pass `pipeline_id="mcp-server"` and add optional `repo` param to both checkpoint tool schemas for explicit checkpoint repo targeting
- `orchestrator/tests/test_mcp_tools.py` — Update/add tests

**Risks / edge cases**: The `get_contract` tool also uses `_make_gateway_request` and is affected by the same bug — fixing session registration fixes all three gateway-backed tools. The gateway checkpoint endpoint still needs to resolve `repo_path` and `checkpoint_repo` after session auth succeeds — this works via `EGG_REPO_PATH` env var fallback and auto-detection from git remotes, which are available in the gateway's runtime context.