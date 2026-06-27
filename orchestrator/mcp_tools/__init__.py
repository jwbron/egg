"""
MCP tool definitions for pipeline management.

Provides tool schemas and handlers that proxy to orchestrator APIs,
enabling external Claude Code sessions to manage SDLC pipelines
via the MCP protocol.
"""

import os
import re
import sys
from pathlib import Path

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Ensure the repo root is on sys.path so that `from orchestrator.*` imports
# work when this module is loaded from the MCP server sidecar (which runs
# inside the orchestrator/ directory, not from the repo root).
_repo_root_path = Path(__file__).parent.parent
if (_repo_root_path / "orchestrator" / "__init__.py").exists() and str(
    _repo_root_path
) not in sys.path:
    sys.path.insert(0, str(_repo_root_path))

try:
    from egg_config import GATEWAY_PORT
except ImportError:
    GATEWAY_PORT = 9848  # noqa: EGG002

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:
        return logging.getLogger(name)


# Canonical slice id shape (``slice-<N>``). Imported from the shared
# validator so this seam stays in lockstep with the orchestrator-side
# ``extract_slice_id`` regex and the sandbox-side ``resolve_slice_id``.
try:
    from slice_id_validation import SLICE_ID_PATTERN as _SLICE_ID_PATTERN
except ImportError:
    _SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")

try:
    from egg_tool_output import cap_result_dict
except ImportError:  # pragma: no cover - shared path always present in prod

    def cap_result_dict(result, **_kwargs):  # type: ignore[no-redef]
        return result


# Per-tool "how to narrow this call" hints, surfaced in the truncation
# marker when a result exceeds the tool-output cap (#2805). These tools'
# output scales with cluster/repo state rather than the caller's params, so
# they are the at-risk set; anything not listed gets a generic hint and, in
# practice, never trips the cap because its output is bounded by design.
_TOOL_NARROW_HINTS: dict[str, str] = {
    "get_service_logs": (
        "scope with `pipeline_id`, `level`, or `pattern` (applied before truncation), "
        "lower `lines` or set a smaller `since_seconds` window, or fetch a single service at a time"
    ),
    "get_container_logs": (
        "lower `lines` or set a smaller `since_seconds` window, or target a single container"
    ),
    "list_containers": (
        "request a single pipeline/phase if the API supports it; otherwise "
        "this result is proportional to the running container count"
    ),
    "list_tasks": "use `limit` (and any status/phase filter) to page results",
    "list_agent_local_commits": "target a single agent/branch or use `limit`",
}


logger = get_logger("orchestrator.mcp_tools")


def _is_timeout_error(exc: BaseException) -> bool:
    """Check if an OSError/URLError is a timeout.

    On Python 3.14, ``socket.timeout`` is an alias for ``TimeoutError`` so
    the explicit check is redundant — but it's kept to keep the call sites
    obvious and to remain robust against urllib wrapping the real timeout
    in ``URLError.reason``.
    """
    import socket

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if hasattr(exc, "reason") and isinstance(exc.reason, (TimeoutError, socket.timeout)):
        return True
    return False


# Tool definitions following MCP protocol schema


# PIPELINE_TOOLS lives in _tool_defs.py; re-exported here as the
# stable public API (#3312 slice-13).
# Method bodies live in underscore-prefixed private submodules and are
# bound back onto PipelineToolHandler below (method-modules-on-class).
from . import (  # noqa: E402
    _consensus,
    _deployment,
    _dispatch,
    _health,
    _lifecycle,
    _request,
    _snapshot,
    _status,
    _submit,
    _tasks,
)
from ._tool_defs import PIPELINE_TOOLS


class PipelineToolHandler:
    """Handles MCP tool calls by proxying to orchestrator APIs."""

    def __init__(
        self,
        orchestrator_url: str = "http://localhost:9849",
        gateway_url: str | None = None,
    ):
        self.orchestrator_url = orchestrator_url
        self.gateway_url = gateway_url or os.environ.get(
            "GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}"
        )
        self._gateway_session_token: str | None = None

    # -- method bindings (bodies in private submodules, #3312 slice-13) --
    # _dispatch
    handle_tool_call = _dispatch.handle_tool_call
    # _request
    _make_request = _request._make_request
    _get_gateway_client = _request._get_gateway_client
    _ensure_gateway_session = _request._ensure_gateway_session
    _make_gateway_request = _request._make_gateway_request
    # _submit
    _handle_submit_task = _submit._handle_submit_task
    _handle_validate_config = _submit._handle_validate_config
    _handle_update_pipeline_config = _submit._handle_update_pipeline_config
    _handle_populate_contract = _submit._handle_populate_contract
    # _status
    _handle_get_status = _status._handle_get_status
    _live_running_agents_fallback = _status._live_running_agents_fallback
    _build_status_snapshot = _status._build_status_snapshot
    _enrich_pending_decisions = _status._enrich_pending_decisions
    _read_reviewer_feedback = _status._read_reviewer_feedback
    _handle_get_phase = _status._handle_get_phase
    # _tasks
    _handle_provide_input = _tasks._handle_provide_input
    _handle_answer_feedback = _tasks._handle_answer_feedback
    _handle_list_tasks = _tasks._handle_list_tasks
    _handle_cancel_task = _tasks._handle_cancel_task
    # _health
    _handle_check_health = _health._handle_check_health
    _handle_list_containers = _health._handle_list_containers
    _handle_get_container_logs = _health._handle_get_container_logs
    _handle_send_message = _health._handle_send_message
    # _consensus
    _handle_get_consensus_status = _consensus._handle_get_consensus_status
    _infer_consensus_from_messages = _consensus._infer_consensus_from_messages
    # _snapshot
    _handle_get_pipeline_snapshot = _snapshot._handle_get_pipeline_snapshot
    _handle_get_contract = _snapshot._handle_get_contract
    # _lifecycle
    _handle_restart_agent = _lifecycle._handle_restart_agent
    _handle_restart_phase = _lifecycle._handle_restart_phase
    _handle_list_agent_local_commits = _lifecycle._handle_list_agent_local_commits
    _handle_salvage_agent_commits = _lifecycle._handle_salvage_agent_commits
    _handle_advance_phase = _lifecycle._handle_advance_phase
    _handle_start_pipeline = _lifecycle._handle_start_pipeline
    _handle_start_phase = _lifecycle._handle_start_phase
    _handle_complete_phase = _lifecycle._handle_complete_phase
    # _deployment
    _handle_get_deployment_context = _deployment._handle_get_deployment_context
    _handle_validate_deployment_manifests = _deployment._handle_validate_deployment_manifests
    _handle_prune_stale_worktrees = _deployment._handle_prune_stale_worktrees
    _handle_validate_network_isolation = _deployment._handle_validate_network_isolation
    _handle_get_service_logs = _deployment._handle_get_service_logs
    _handle_rebuild_and_rollout = _deployment._handle_rebuild_and_rollout


__all__ = [
    "PIPELINE_TOOLS",
    "PipelineToolHandler",
]
