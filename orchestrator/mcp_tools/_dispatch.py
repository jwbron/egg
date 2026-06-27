"""PipelineToolHandler tool-call dispatch (handle_tool_call) (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

from typing import Any

from mcp_tools import (
    _TOOL_NARROW_HINTS,
    cap_result_dict,
    logger,
)


def handle_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to the appropriate handler.

    Args:
        tool_name: Name of the MCP tool
        arguments: Tool arguments

    Returns:
        Tool result dictionary
    """
    handlers = {
        "submit_task": self._handle_submit_task,
        "get_status": self._handle_get_status,
        "provide_input": self._handle_provide_input,
        "answer_feedback": self._handle_answer_feedback,
        "list_tasks": self._handle_list_tasks,
        "cancel_task": self._handle_cancel_task,
        "check_health": self._handle_check_health,
        "list_containers": self._handle_list_containers,
        "get_container_logs": self._handle_get_container_logs,
        "send_message": self._handle_send_message,
        "get_consensus_status": self._handle_get_consensus_status,
        "get_phase": self._handle_get_phase,
        "get_pipeline_snapshot": self._handle_get_pipeline_snapshot,
        "get_contract": self._handle_get_contract,
        "validate_config": self._handle_validate_config,
        "update_pipeline_config": self._handle_update_pipeline_config,
        "restart_agent": self._handle_restart_agent,
        "restart_phase": self._handle_restart_phase,
        "list_agent_local_commits": self._handle_list_agent_local_commits,
        "salvage_agent_commits": self._handle_salvage_agent_commits,
        "advance_phase": self._handle_advance_phase,
        "start_pipeline": self._handle_start_pipeline,
        "start_phase": self._handle_start_phase,
        "complete_phase": self._handle_complete_phase,
        "populate_contract": self._handle_populate_contract,
        "get_deployment_context": self._handle_get_deployment_context,
        "validate_deployment_manifests": self._handle_validate_deployment_manifests,
        "prune_stale_worktrees": self._handle_prune_stale_worktrees,
        "validate_network_isolation": self._handle_validate_network_isolation,
        "rebuild_and_rollout": self._handle_rebuild_and_rollout,
        "get_service_logs": self._handle_get_service_logs,
    }

    handler = handlers.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        result = handler(arguments)
    except Exception as e:
        logger.error("Tool call failed", tool=tool_name, error=str(e))
        return {"error": str(e)}
    # Bound the result before mcp_server.py serializes it across the
    # operator's Agent SDK 1 MB reader buffer (#2805). Oversized output
    # is replaced with a head-preview marker naming how to narrow the
    # call; bounded tools never trip this. ``indent=2`` matches
    # mcp_server.py's ``json.dumps(result, indent=2)`` so the cap is
    # measured against the real on-wire size, not compact JSON.
    if isinstance(result, dict):
        return cap_result_dict(
            result,
            tool=tool_name,
            narrow_hint=_TOOL_NARROW_HINTS.get(tool_name),
            indent=2,
        )
    return result
