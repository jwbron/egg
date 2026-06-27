<!--
HANDOFF → documenter (#3312 slice-13).
coder is role-blocked from orchestrator/CLAUDE.md (shared/egg_restrictions/patterns.py;
alternative_role=documenter). This is the drafted seam-table subsection for the
`mcp_tools/` decomposition. Append it to orchestrator/CLAUDE.md's "## Decomposition
seams" section, AFTER the `peer_consensus/` subsection, and add `mcp_tools/` to the
trailing "landed orchestrator decompositions" sentence.
-->

### `mcp_tools/` — MCP tool schemas + PipelineToolHandler ([#3312](https://github.com/jwbron/egg/issues/3312), slice 13)

`mcp_tools.py` (2,948 lines / 130,445 bytes — **over the byte cap**) → `mcp_tools/` (largest submodule `_tool_defs.py`, 1,069 lines / 49KB). Class-dominated module: follows the **method-modules-on-class** shape ([pattern](../docs/guides/decomposition-pattern.md) §c) rather than the Flask-blueprint shape. The `PipelineToolHandler` class definition + `__init__` stay in the barrel and keep the class identity on the `mcp_tools` module path; each of its ~40 method bodies moves to a responsibility-grouped private submodule as a module-level function taking `self` explicitly, bound back onto the class in the barrel. The `PIPELINE_TOOLS` schema list (pure MCP-protocol data) moves to `_tool_defs.py` and is re-exported. **Dockerfile (binding, NOT packaging-neutral):** `mcp_tools.py` was a top-level `orchestrator/` module shipped by the non-recursive `COPY orchestrator/*.py ./` glob (Dockerfile:44); once it becomes a directory the glob stops matching it, so this slice adds an explicit `COPY orchestrator/mcp_tools/ ./mcp_tools/` (Dockerfile) to keep `import mcp_tools` resolving for the in-process MCP server — same as slice-3 `state_store/` + slice-10 `peer_consensus/`.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 196 lines) | Stable public API: the `PipelineToolHandler` class definition + `__init__` + every method binding; re-exports `PIPELINE_TOOLS`; keeps the module-level `logger` / `cap_result_dict` / `_TOOL_NARROW_HINTS` / `_is_timeout_error` / `GATEWAY_PORT` / `_SLICE_ID_PATTERN` bindings the submodules import | `PIPELINE_TOOLS`, `PipelineToolHandler` |
| `_tool_defs.py` (largest, 1,069 lines) | The `PIPELINE_TOOLS` MCP tool-schema list (pure data) | `PIPELINE_TOOLS` |
| `_dispatch.py` (86 lines) | Tool-name → handler dispatch + result capping | `handle_tool_call` |
| `_request.py` (134 lines) | Orchestrator + gateway HTTP request plumbing | `_make_request`, `_get_gateway_client`, `_ensure_gateway_session`, `_make_gateway_request` |
| `_submit.py` (266 lines) | Pipeline creation + config validation / update + contract population | `_handle_submit_task`, `_handle_validate_config`, `_handle_update_pipeline_config`, `_handle_populate_contract` |
| `_status.py` (397 lines) | Status snapshot + pending-decision / reviewer-feedback enrichment | `_handle_get_status`, `_live_running_agents_fallback`, `_build_status_snapshot`, `_enrich_pending_decisions`, `_read_reviewer_feedback`, `_handle_get_phase` |
| `_tasks.py` (143 lines) | Input / feedback / list / cancel task handlers | `_handle_provide_input`, `_handle_answer_feedback`, `_handle_list_tasks`, `_handle_cancel_task` |
| `_health.py` (153 lines) | Health-check / container-list / container-logs / send-message handlers | `_handle_check_health`, `_handle_list_containers`, `_handle_get_container_logs`, `_handle_send_message` |
| `_consensus.py` (142 lines) | Consensus-status handler + message-inference helper | `_handle_get_consensus_status`, `_infer_consensus_from_messages` |
| `_snapshot.py` (115 lines) | Pipeline-snapshot + contract read handlers | `_handle_get_pipeline_snapshot`, `_handle_get_contract` |
| `_lifecycle.py` (286 lines) | Agent/phase restart, commit salvage, advance/start/complete lifecycle handlers | `_handle_restart_agent`, `_handle_restart_phase`, `_handle_list_agent_local_commits`, `_handle_salvage_agent_commits`, `_handle_advance_phase`, `_handle_start_pipeline`, `_handle_start_phase`, `_handle_complete_phase` |
| `_deployment.py` (223 lines) | Deployment context / manifest validation / network isolation / service logs / rebuild handlers | `_handle_get_deployment_context`, `_handle_validate_deployment_manifests`, `_handle_prune_stale_worktrees`, `_handle_validate_network_isolation`, `_handle_get_service_logs`, `_handle_rebuild_and_rollout` |

Pure refactor, no behaviour change: the ~40 method bodies are extracted verbatim from the pre-split file (AST-identical modulo docstring re-indentation — non-docstring multi-line strings are byte-identical, proven by AST diff). Patch seams preserved: `PipelineToolHandler` + its method bindings resolve on the barrel, so `patch.object(handler, "_make_request")` (the suite's only handler-internal patch) and `patch("urllib.request.build_opener")` (external) keep working; there are **no** `patch("mcp_tools.X")` module-global seams, so the private submodules import the barrel globals (`logger`, `cap_result_dict`, `_TOOL_NARROW_HINTS`, `_is_timeout_error`, `GATEWAY_PORT`, `_SLICE_ID_PATTERN`) from the package and keep a single binding — mirroring `peer_consensus/`. `make lint` clean; 219 mcp_tools + 126 cross-tree importer tests pass.

<!-- Also append `mcp_tools/` to the trailing sentence listing landed orchestrator decompositions. -->
