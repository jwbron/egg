#!/usr/bin/env python3
"""
Contract CLI for SDLC pipeline operations.

This CLI provides commands for agents to interact with the contract state
during the SDLC pipeline. All mutations route through the gateway endpoint
for role-based enforcement.

Commands:
    egg-contract show                           Display current contract state
    egg-contract add-commit --task <id> --commit <sha>
                                               Link commit to task
    egg-contract update-notes --task <id> --notes <text>
                                               Add implementation notes
    egg-contract add-decision --question <text>
                                               Create HITL decision point
    egg-contract add-feedback --question <text> [--question <text>...]
                                               Create feedback comment for open-ended questions
"""

# ── Sub-package barrel (#3312, slice-1) ─────────────────────────────────────
# Decomposed from a single 1,501-line ``contract_cli.py`` following the
# pattern in ``docs/guides/decomposition-pattern.md``. This barrel is the
# stable public API: external callers and test patch targets keep using
# ``egg_lib.contract_cli.<symbol>``. Submodules:
#   _errors.py         — GatewayError / HandlerError shim
#   _config.py         — env/config getters, id parsers, validators
#   _gateway.py        — make_gateway_request + legacy error renderer
#   _decisions.py      — HITL decision id validation + markdown
#   _commands.py       — contract/task/phase/criterion/decision/feedback cmds
#   _agent_commands.py — multi-agent orchestration cmds
import argparse
import sys

# Eager submodule imports so attribute-access monkeypatching of helpers at
# their definition site (e.g. ``contract_cli._gateway.get_session_token``)
# resolves without an explicit import in the test.
from . import (  # noqa: F401 — re-export targets
    _agent_commands,
    _commands,
    _config,
    _decisions,
    _errors,
    _gateway,
)
from ._agent_commands import (
    VALID_AGENT_ROLES,
    VALID_AGENT_STATUSES,
    cmd_agent_complete,
    cmd_agent_fail,
    cmd_agent_next,
    cmd_agent_start,
    cmd_agent_status,
)
from ._commands import (
    _print_contract_summary,
    cmd_add_commit,
    cmd_add_decision,
    cmd_add_feedback,
    cmd_complete_phase,
    cmd_complete_task,
    cmd_show,
    cmd_update_notes,
    cmd_verify_criterion,
)
from ._config import (
    COMMIT_SHA_PATTERN,
    get_container_id,
    get_contract_identifier,
    get_gateway_url,
    get_issue_number,
    get_pipeline_id,
    get_repo_path,
    get_session_token,
    parse_criterion_id,
    parse_phase_id,
    parse_task_id,
    validate_commit_sha,
)
from ._decisions import format_decision_markdown, validate_decision_id
from ._errors import GatewayError, HandlerError
from ._gateway import _render_gateway_error_and_exit, make_gateway_request


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-contract",
        description="Contract CLI for SDLC pipeline operations",
    )
    parser.add_argument(
        "--issue",
        type=int,
        help="Issue number (defaults to EGG_ISSUE_NUMBER env var)",
    )
    parser.add_argument(
        "--pipeline-id",
        type=str,
        help="Pipeline ID for JIRA-ticket pipelines (defaults to EGG_PIPELINE_ID env var)",
    )
    parser.add_argument(
        "--repo-path",
        help="Repository path (defaults to EGG_REPO_PATH or cwd)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # show command
    show_parser = subparsers.add_parser("show", help="Display current contract state")
    show_parser.add_argument("--json", action="store_true", help="Output as JSON")
    show_parser.add_argument("--audit", action="store_true", help="Include audit log")
    show_parser.set_defaults(func=cmd_show)

    # add-commit command
    commit_parser = subparsers.add_parser("add-commit", help="Link commit to task")
    commit_parser.add_argument("--task", required=True, help="Task ID (e.g., task-1 or task-1-2)")
    commit_parser.add_argument("--commit", required=True, help="Git commit SHA")
    commit_parser.set_defaults(func=cmd_add_commit)

    # update-notes command
    notes_parser = subparsers.add_parser("update-notes", help="Add implementation notes")
    notes_parser.add_argument("--task", required=True, help="Task ID")
    notes_parser.add_argument("--notes", required=True, help="Implementation notes")
    notes_parser.set_defaults(func=cmd_update_notes)

    # complete-task command
    complete_task_parser = subparsers.add_parser("complete-task", help="Mark a task as complete")
    complete_task_parser.add_argument(
        "--task", required=True, help="Task ID (e.g., task-1 or task-1-2)"
    )
    complete_task_parser.add_argument("--commit", help="Git commit SHA to link to the task")
    complete_task_parser.set_defaults(func=cmd_complete_task)

    # complete-phase command
    complete_phase_parser = subparsers.add_parser("complete-phase", help="Mark a phase as complete")
    complete_phase_parser.add_argument("--phase", required=True, help="Phase ID (e.g., phase-1)")
    complete_phase_parser.add_argument("--commit", help="Git commit SHA to link to the phase")
    complete_phase_parser.set_defaults(func=cmd_complete_phase)

    # verify-criterion command (requires REVIEWER role)
    verify_criterion_parser = subparsers.add_parser(
        "verify-criterion", help="Mark acceptance criterion as verified (requires REVIEWER role)"
    )
    verify_criterion_parser.add_argument(
        "--criterion", required=True, help="Criterion ID (e.g., ac-1)"
    )
    verify_criterion_parser.set_defaults(func=cmd_verify_criterion)

    # add-decision command
    decision_parser = subparsers.add_parser("add-decision", help="Create HITL decision point")
    decision_parser.add_argument("--question", required=True, help="Decision question")
    decision_parser.add_argument(
        "--options",
        nargs="*",
        help="Optional: decision options",
    )
    decision_parser.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Pipeline phase (defaults to contract's current_phase)",
    )
    decision_parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format: json (default) or markdown (for GitHub comments)",
    )
    decision_parser.set_defaults(func=cmd_add_decision)

    # add-feedback command
    feedback_parser = subparsers.add_parser(
        "add-feedback", help="Create feedback comment for open-ended questions"
    )
    feedback_parser.add_argument(
        "--question",
        action="append",
        required=True,
        help="Open-ended question (can be specified multiple times)",
    )
    feedback_parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format: json (default) or markdown (for GitHub comments)",
    )
    feedback_parser.set_defaults(func=cmd_add_feedback)

    # Agent orchestration commands
    # agent-status command
    agent_status_parser = subparsers.add_parser(
        "agent-status", help="Show agent execution status for multi-agent orchestration"
    )
    agent_status_parser.add_argument("--json", action="store_true", help="Output as JSON")
    agent_status_parser.set_defaults(func=cmd_agent_status)

    # agent-start command
    agent_start_parser = subparsers.add_parser(
        "agent-start", help="Mark an agent as started (running)"
    )
    agent_start_parser.add_argument(
        "--role",
        required=True,
        choices=VALID_AGENT_ROLES,
        help="Agent role to start",
    )
    agent_start_parser.set_defaults(func=cmd_agent_start)

    # agent-complete command
    agent_complete_parser = subparsers.add_parser(
        "agent-complete", help="Mark an agent as complete"
    )
    agent_complete_parser.add_argument(
        "--role",
        required=True,
        choices=VALID_AGENT_ROLES,
        help="Agent role to mark complete",
    )
    agent_complete_parser.add_argument(
        "--commit",
        help="Git commit SHA if agent made changes",
    )
    agent_complete_parser.set_defaults(func=cmd_agent_complete)

    # agent-fail command
    agent_fail_parser = subparsers.add_parser("agent-fail", help="Mark an agent as failed")
    agent_fail_parser.add_argument(
        "--role",
        required=True,
        choices=VALID_AGENT_ROLES,
        help="Agent role to mark failed",
    )
    agent_fail_parser.add_argument(
        "--error",
        required=True,
        help="Error message describing the failure",
    )
    agent_fail_parser.set_defaults(func=cmd_agent_fail)

    # agent-next command
    agent_next_parser = subparsers.add_parser(
        "agent-next", help="Get the next wave of agents to dispatch"
    )
    agent_next_parser.add_argument("--json", action="store_true", help="Output as JSON")
    agent_next_parser.set_defaults(func=cmd_agent_next)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point.

    Wraps ``args.func(args)`` in a try/except for :class:`GatewayError`
    / :class:`HandlerError` so the raise-don't-exit behaviour of
    ``make_gateway_request`` (and the shared handlers in
    ``egg_agent_tools``) is rendered with the legacy stderr + exit-code
    surface humans and scripts expect.
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        result: int = args.func(args)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    return result


__all__ = [
    # error types
    "GatewayError",
    "HandlerError",
    # config / parsers / validators
    "COMMIT_SHA_PATTERN",
    "get_container_id",
    "get_contract_identifier",
    "get_gateway_url",
    "get_issue_number",
    "get_pipeline_id",
    "get_repo_path",
    "get_session_token",
    "parse_criterion_id",
    "parse_phase_id",
    "parse_task_id",
    "validate_commit_sha",
    # gateway
    "make_gateway_request",
    # decisions
    "format_decision_markdown",
    "validate_decision_id",
    # commands
    "cmd_show",
    "cmd_add_commit",
    "cmd_update_notes",
    "cmd_complete_task",
    "cmd_complete_phase",
    "cmd_verify_criterion",
    "cmd_add_decision",
    "cmd_add_feedback",
    "_print_contract_summary",
    # agent orchestration
    "VALID_AGENT_ROLES",
    "VALID_AGENT_STATUSES",
    "cmd_agent_status",
    "cmd_agent_start",
    "cmd_agent_complete",
    "cmd_agent_fail",
    "cmd_agent_next",
    # parser / entrypoint
    "create_parser",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
