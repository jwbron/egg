#!/usr/bin/env python3
"""
Orchestrator CLI for interacting with the egg orchestrator API.

Provides commands for pipeline management, signal sending, phase transitions,
decision queuing, and container operations. Usable by both the egg agent
and humans.

Commands:
    egg-orch health                              Check orchestrator + gateway health
    egg-orch pipeline list                       List all pipelines
    egg-orch pipeline get <id>                   Get pipeline details
    egg-orch pipeline create --repo <r> ...      Create a pipeline
    egg-orch pipeline status <id>                Get pipeline status
    egg-orch pipeline delete <id>                Delete a pipeline
    egg-orch signal complete <pipeline_id> ...   Signal completion
    egg-orch signal progress <pipeline_id> ...   Signal progress
    egg-orch signal error <pipeline_id> ...      Signal error
    egg-orch signal heartbeat <pipeline_id> ...  Send heartbeat
    egg-orch phase get <pipeline_id>             Get current phase
    egg-orch phase advance <pipeline_id>         Advance to next phase
    egg-orch phase start <pipeline_id>           Start current phase
    egg-orch phase complete <pipeline_id>        Complete current phase
    egg-orch decision list <pipeline_id>         List decisions
    egg-orch decision create <pipeline_id> ...   Queue a decision
    egg-orch decision resolve <pid> <did> ...    Resolve a decision
    egg-orch decision status <pipeline_id>       Decision queue status
    egg-orch container list <pipeline_id>        List containers
    egg-orch container spawn <pipeline_id> ...   Spawn a container
    egg-orch container get <pid> <cid>           Get container info
    egg-orch container stop <pid> <cid>          Stop a container
    egg-orch container logs <pid> <cid>          Get container logs
    egg-orch message send <pid> --to <role> ...  Send inter-agent message (concurrent mode)
    egg-orch message poll <pid> ...              Poll for messages (concurrent mode)
    egg-orch message wait <pid> --for TYPE ...   Block until typed event arrives (wrapper-internal)
    egg-orch message wait-loop <pid> --for TYPE  Loop message wait until match / cap (wrapper-internal)
    egg-orch message heartbeat <pid> --state X   Emit structured HEARTBEAT
    egg-orch message status <pid>                Get message bus status (concurrent mode)
    egg-orch signal readiness <pid> --state ...  Signal readiness state (concurrent mode)
    egg-orch consensus propose <pid> ...         Send BRC consensus proposal
    egg-orch consensus ack <producer> ...        ACK a producer's proposal
    egg-orch consensus nack <producer> ...       NACK a producer's proposal
    egg-orch consensus withdraw <pid> ...        Withdraw proposal
    egg-orch consensus confirmed <pid>           Confirm after all reviewers ACK
    egg-orch consensus status <pid>              Show BRC consensus status
    egg-orch overseer alert <pid> ...            Broadcast OVERSEER_ALERT to human operator
"""

# ── Sub-package barrel (#3312, slice-17) ────────────────────────────────────
# Decomposed from a 5,012-line ``orch_cli.py`` per
# ``docs/guides/decomposition-pattern.md``. This barrel is the stable public
# API: external callers and test patch targets keep using
# ``egg_lib.orch_cli.<symbol>``. The two live patch seams ``orch_request`` and
# ``get_agent_role_from_env`` are re-exported here, and the command submodules
# reach them via ``import egg_lib.orch_cli as _pkg`` (live barrel attribute
# lookup) so ``patch("egg_lib.orch_cli.orch_request")`` keeps intercepting.
#
# Eager submodule imports so attribute-access monkeypatching of a helper at its
# definition site resolves without an explicit import in the test.
from . import (  # noqa: F401 — re-export submodules for attr patching
    _brc,
    _common,
    _consensus,
    _container,
    _decision,
    _health,
    _http,
    _message,
    _overseer,
    _parser,
    _phase,
    _pipeline,
    _progress,
    _signal,
)
from ._brc import (
    _VALID_BRC_HISTORY_PHASES,
    cmd_brc_get_state,
    cmd_brc_list_blocking,
    cmd_brc_next_action,
    cmd_brc_read_peer_artifact,
    cmd_brc_resolve_obligation,
)
from ._common import (
    _emit_argv_prose_deprecation,
    _ProseArgError,
    _render_handler_error,
    _require_role,
    _resolve_files_reviewed_arg,
    _resolve_prose_arg,
)
from ._consensus import (
    _consensus_push,
    _render_stale_version_rejection,
    cmd_consensus_ack,
    cmd_consensus_confirmed,
    cmd_consensus_nack,
    cmd_consensus_propose,
    cmd_consensus_status,
    cmd_consensus_withdraw,
)
from ._container import (
    cmd_container_get,
    cmd_container_list,
    cmd_container_logs,
    cmd_container_spawn,
    cmd_container_stop,
)
from ._decision import (
    cmd_decision_create,
    cmd_decision_list,
    cmd_decision_resolve,
    cmd_decision_status,
)
from ._health import (
    cmd_gateway_health,
    cmd_gateway_permissions,
    cmd_gateway_phase,
    cmd_health,
    cmd_health_alerts,
    cmd_health_resolve,
)
from ._http import (
    _SAFE_ID_PATTERN,
    _SLICE_ID_PATTERN,
    ApiError,
    _proposal_version_type,
    api_request,
    api_request_or_exit,
    gateway_request,
    get_agent_role_from_env,
    get_gateway_url,
    get_issue_number,
    get_orchestrator_url,
    get_pipeline_id_from_env,
    get_session_token,
    get_slice_id_from_env,
    orch_request,
    print_json,
    require_pipeline_id,
    resolve_slice_id,
    validate_id,
)
from ._message import (
    _classify_gateway_error_rc,
    _delete_cursor_file,
    _read_cursor_file,
    _resolve_from_producer_arg,
    _wait_cursor_path,
    _write_cursor_file,
    cmd_message_heartbeat,
    cmd_message_poll,
    cmd_message_send,
    cmd_message_status,
    cmd_message_wait,
    cmd_message_wait_loop,
)
from ._overseer import (
    _OVERSEER_BODY_MAX_BYTES,
    _OVERSEER_TITLE_MAX_CHARS,
    _OVERSEER_VALID_LABEL_PRIORITIES,
    cmd_overseer_alert,
    cmd_overseer_consult_advisor,
    cmd_overseer_file_issue,
)
from ._parser import (
    _add_json_flag,
    _non_negative_int,
    create_parser,
)
from ._phase import (
    cmd_phase_advance,
    cmd_phase_complete,
    cmd_phase_get,
    cmd_phase_get_context,
    cmd_phase_start,
)
from ._pipeline import (
    _WAIT_STATUS_TERMINAL_EVENTS,
    _WAIT_STATUS_TERMINAL_STATUSES,
    _WAIT_STATUS_TO_EVENT_TYPE,
    cmd_pipeline_create,
    cmd_pipeline_delete,
    cmd_pipeline_get,
    cmd_pipeline_list,
    cmd_pipeline_status,
    cmd_pipeline_wait_status,
)
from ._progress import (
    cmd_env,
    cmd_progress_emit,
    cmd_progress_query,
)
from ._signal import (
    cmd_signal_complete,
    cmd_signal_error,
    cmd_signal_heartbeat,
    cmd_signal_progress,
    cmd_signal_readiness,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Handle subcommand groups that need their own help
    func = getattr(args, "func", None)
    if func is None:
        # No subcommand selected within the group
        sub = args.command
        # Re-parse to show the right help
        parser.parse_args([sub, "--help"])
        return 1

    result: int = func(args)
    return result


__all__ = [
    "ApiError",
    "_OVERSEER_BODY_MAX_BYTES",
    "_OVERSEER_TITLE_MAX_CHARS",
    "_OVERSEER_VALID_LABEL_PRIORITIES",
    "_ProseArgError",
    "_SAFE_ID_PATTERN",
    "_SLICE_ID_PATTERN",
    "_VALID_BRC_HISTORY_PHASES",
    "_WAIT_STATUS_TERMINAL_EVENTS",
    "_WAIT_STATUS_TERMINAL_STATUSES",
    "_WAIT_STATUS_TO_EVENT_TYPE",
    "_add_json_flag",
    "_classify_gateway_error_rc",
    "_consensus_push",
    "_delete_cursor_file",
    "_emit_argv_prose_deprecation",
    "_non_negative_int",
    "_proposal_version_type",
    "_read_cursor_file",
    "_render_handler_error",
    "_render_stale_version_rejection",
    "_require_role",
    "_resolve_files_reviewed_arg",
    "_resolve_from_producer_arg",
    "_resolve_prose_arg",
    "_wait_cursor_path",
    "_write_cursor_file",
    "api_request",
    "api_request_or_exit",
    "cmd_brc_get_state",
    "cmd_brc_list_blocking",
    "cmd_brc_next_action",
    "cmd_brc_read_peer_artifact",
    "cmd_brc_resolve_obligation",
    "cmd_consensus_ack",
    "cmd_consensus_confirmed",
    "cmd_consensus_nack",
    "cmd_consensus_propose",
    "cmd_consensus_status",
    "cmd_consensus_withdraw",
    "cmd_container_get",
    "cmd_container_list",
    "cmd_container_logs",
    "cmd_container_spawn",
    "cmd_container_stop",
    "cmd_decision_create",
    "cmd_decision_list",
    "cmd_decision_resolve",
    "cmd_decision_status",
    "cmd_env",
    "cmd_gateway_health",
    "cmd_gateway_permissions",
    "cmd_gateway_phase",
    "cmd_health",
    "cmd_health_alerts",
    "cmd_health_resolve",
    "cmd_message_heartbeat",
    "cmd_message_poll",
    "cmd_message_send",
    "cmd_message_status",
    "cmd_message_wait",
    "cmd_message_wait_loop",
    "cmd_overseer_alert",
    "cmd_overseer_consult_advisor",
    "cmd_overseer_file_issue",
    "cmd_phase_advance",
    "cmd_phase_complete",
    "cmd_phase_get",
    "cmd_phase_get_context",
    "cmd_phase_start",
    "cmd_pipeline_create",
    "cmd_pipeline_delete",
    "cmd_pipeline_get",
    "cmd_pipeline_list",
    "cmd_pipeline_status",
    "cmd_pipeline_wait_status",
    "cmd_progress_emit",
    "cmd_progress_query",
    "cmd_signal_complete",
    "cmd_signal_error",
    "cmd_signal_heartbeat",
    "cmd_signal_progress",
    "cmd_signal_readiness",
    "create_parser",
    "gateway_request",
    "get_agent_role_from_env",
    "get_gateway_url",
    "get_issue_number",
    "get_orchestrator_url",
    "get_pipeline_id_from_env",
    "get_session_token",
    "get_slice_id_from_env",
    "main",
    "orch_request",
    "print_json",
    "require_pipeline_id",
    "resolve_slice_id",
    "validate_id",
]
