"""Contract/task/phase/criterion/decision/feedback command handlers.

The ``cmd_*`` functions backing the non-agent egg-contract subcommands,
extracted verbatim from the monolithic ``contract_cli.py`` (#3312,
slice-1). No behaviour change. Each handler still does its own lazy
``from egg_agent_tools.handlers import ...`` so the CLI and the MCP tools
share a single handler implementation.
"""

import argparse
import json
import sys
from typing import Any

from ._config import get_contract_identifier, get_repo_path
from ._decisions import format_decision_markdown
from ._errors import GatewayError, HandlerError
from ._gateway import _render_gateway_error_and_exit


def cmd_show(args: argparse.Namespace) -> int:
    """Display current contract state.

    Delegates to :func:`egg_agent_tools.handlers.sdlc.show_contract`
    so the CLI and the ``mcp__sdlc__show_contract`` MCP tool share a
    handler. Stdout/stderr shape is byte-compatible with the prior
    hand-rolled implementation (summary for TTY, ``--json`` for
    machine consumption, ``--audit`` to include audit-log).
    """
    from egg_agent_tools.handlers import sdlc as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "repo_path": args.repo_path or get_repo_path(),
        "audit": bool(getattr(args, "audit", False)),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier

    try:
        resp = _handlers.show_contract(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code

    contract = resp.get("contract", {}) or {}
    if args.json:
        print(json.dumps(contract, indent=2))
    else:
        _print_contract_summary(contract)
    return 0


def _print_contract_summary(contract: dict[str, Any]) -> None:
    """Print a human-readable contract summary."""
    issue = contract.get("issue")
    if issue and issue.get("number"):
        print(f"Issue: #{issue.get('number')} - {issue.get('title')}")
    else:
        pipeline_id = contract.get("pipeline_id")
        if pipeline_id:
            print(f"Pipeline: {pipeline_id}")
        else:
            print("Contract:")
    print(f"Phase: {contract.get('current_phase', 'unknown')}")
    print()

    # Surface `task_description` so the CLI summary doesn't silently drop
    # the full free-text task that `mcp__sdlc__show_contract` returns (#3033).
    if task_description := contract.get("task_description"):
        print(f"Task:\n{task_description}\n")

    # Read modern ``slices``; fall back to legacy ``phases`` for un-migrated
    # raw JSON. Same shape as ``_tasks_for_role`` / ``task_mark_gap`` (#3029).
    phases = contract.get("slices") or contract.get("phases") or []
    if phases:
        print("Phases:")
        for phase in phases:
            status_icon = {"pending": "○", "in_progress": "◐", "complete": "●", "blocked": "⊘"}.get(
                phase.get("status", "pending"), "?"
            )
            print(f"  {status_icon} {phase.get('id')}: {phase.get('name')} [{phase.get('status')}]")

            for task in phase.get("tasks", []):
                task_icon = {
                    "pending": "○",
                    "in_progress": "◐",
                    "complete": "●",
                    "incomplete": "✗",
                    "blocked": "⊘",
                }.get(task.get("status", "pending"), "?")
                commit_info = f" ({task.get('commit')[:7]})" if task.get("commit") else ""
                print(f"    {task_icon} {task.get('id')}: {task.get('description')}{commit_info}")
        print()

    # Show agent executions if present (multi-agent mode)
    agent_executions = contract.get("agent_executions", [])
    if agent_executions:
        print("Agent Executions:")
        for execution in agent_executions:
            status_icon = {
                "pending": "○",
                "running": "◐",
                "complete": "●",
                "failed": "✗",
                "skipped": "⊘",
                "blocked": "⊘",
            }.get(execution.get("status", "pending"), "?")
            role = execution.get("role", "unknown")
            status = execution.get("status", "pending")
            commit_info = f" ({execution.get('commit')[:7]})" if execution.get("commit") else ""
            error_info = f" - {execution.get('error')}" if execution.get("error") else ""
            print(f"  {status_icon} {role}: {status}{commit_info}{error_info}")
        print()

    # Show pending decisions
    decisions = [d for d in contract.get("decisions", []) if not d.get("resolved")]
    if decisions:
        print("Pending Decisions:")
        for decision in decisions:
            print(f"  [{decision.get('id')}] {decision.get('question')}")


def cmd_add_commit(args: argparse.Namespace) -> int:
    """Link a commit to a task.

    Delegates to :func:`egg_agent_tools.handlers.task.task_add_commit`
    so the CLI and the ``mcp__task__add_commit`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import task as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "task": args.task,
        "commit": args.commit,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier

    try:
        _handlers.task_add_commit(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    print(f"Linked commit {args.commit[:7]} to {args.task}")
    return 0


def cmd_update_notes(args: argparse.Namespace) -> int:
    """Add implementation notes to a task.

    Delegates to :func:`egg_agent_tools.handlers.task.task_update_notes`
    so the CLI and the ``mcp__task__update_notes`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import task as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "task": args.task,
        "notes": args.notes,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier

    try:
        _handlers.task_update_notes(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    print(f"Updated notes for {args.task}")
    return 0


def cmd_complete_task(args: argparse.Namespace) -> int:
    """Mark a task as complete, optionally linking a commit.

    Delegates to :func:`egg_agent_tools.handlers.task.task_complete`
    so the MCP ``mcp__task__complete`` tool and the shell CLI share a
    single handler.  Stdout text and exit code are byte-identical to
    the pre-refactor CLI behaviour.

    The handler raises :class:`GatewayError` with a message prefixed
    ``"Task marked complete but failed to link commit: "`` on
    commit-link failure; we catch that and render the legacy stderr
    *without* the generic ``"Error:"`` prefix.  Status-mutation
    failures render as ``"Error setting status: <msg>"``.
    """
    from egg_agent_tools.handlers import task as _handlers
    from egg_agent_tools.handlers.errors import GatewayError

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "task": args.task,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier
    if args.commit:
        req["commit"] = args.commit

    try:
        resp = _handlers.task_complete(req)
    except GatewayError as err:
        msg = err.message or str(err)
        if msg.startswith("Task marked complete but failed to link commit: "):
            # Preserve the original no-"Error:"-prefix "Warning:" wording
            print(f"Warning: {msg}", file=sys.stderr)
        else:
            print(f"Error setting status: {msg}", file=sys.stderr)
        return err.exit_code

    commit = resp.get("commit")
    if commit:
        print(f"Completed {args.task} (commit {commit[:7]})")
    else:
        print(f"Completed {args.task}")
    return 0


def cmd_complete_phase(args: argparse.Namespace) -> int:
    """Mark a phase as complete, optionally linking a commit.

    Delegates to :func:`egg_agent_tools.handlers.phase.phase_complete_phase`
    so the CLI and the ``mcp__phase__complete_phase`` MCP tool share a
    handler (iter-2 drift gate).

    Handler ordering changed in response to reviewer_code NACK #6: the
    commit-link happens BEFORE the status flip, so a mid-way failure
    leaves the phase not-complete-yet with the commit already
    populated, and callers can retry the same request to progress.
    The stderr phrasing "Error setting status:" is preserved from the
    legacy CLI surface so scripts that grep the exit messages keep
    working.
    """
    from egg_agent_tools.handlers import phase as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "phase": args.phase,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier
    if args.commit:
        req["commit"] = args.commit

    try:
        _handlers.phase_complete_phase(req)
    except GatewayError as err:
        # The handler raises two distinct GatewayError shapes now: a
        # "phase commit link failed" (when supplied) or a bare status
        # error.  Both land here; the CLI maps all gateway failures to
        # the legacy "Error setting status:" prefix so exit-grep
        # scripts keep working.
        msg = err.message or str(err)
        print(f"Error setting status: {msg}", file=sys.stderr)
        return err.exit_code
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code

    if args.commit:
        print(f"Completed {args.phase} (commit {args.commit[:7]})")
    else:
        print(f"Completed {args.phase}")
    return 0


def cmd_verify_criterion(args: argparse.Namespace) -> int:
    """Mark an acceptance criterion as verified.

    Note: This operation requires REVIEWER role. Agents running as IMPLEMENTER
    will receive a role authorization error from the gateway. This command is
    used by contract verification reviewers to mark criteria as verified.

    Delegates to :func:`egg_agent_tools.handlers.sdlc.verify_criterion`
    so the CLI and the ``mcp__sdlc__verify_criterion`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import sdlc as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "criterion": args.criterion,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier

    try:
        _handlers.verify_criterion(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    print(f"Verified criterion {args.criterion}")
    return 0


def cmd_add_decision(args: argparse.Namespace) -> int:
    """Create a HITL decision point.

    Delegates to :func:`egg_agent_tools.handlers.sdlc.register_open_question`
    so the MCP ``mcp__sdlc__register_open_question`` tool and the CLI share
    a single handler.  Note: the TOCTOU race on the decision ID is
    inherited from the handler; the gateway rejects duplicate indices
    server-side.
    """
    from egg_agent_tools.handlers import sdlc as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    req: dict[str, Any] = {
        "question": args.question,
        "options": list(args.options) if args.options else [],
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier
    if args.phase:
        req["phase"] = args.phase

    try:
        resp = _handlers.register_open_question(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    decision = resp.get("decision", {})

    output_format = getattr(args, "format", "json")
    if output_format == "markdown":
        markdown = format_decision_markdown(
            decision["id"],
            args.question,
            decision.get("options", []),
        )
        print(markdown)
    else:
        print(f"Created decision {decision['id']}: {args.question}")
    return 0


def cmd_add_feedback(args: argparse.Namespace) -> int:
    """Create a feedback comment for open-ended questions.

    Delegates to :func:`egg_agent_tools.handlers.sdlc.request_feedback`
    so the MCP ``mcp__sdlc__request_feedback`` tool and the CLI share a
    single handler.
    """
    from egg_agent_tools.handlers import sdlc as _handlers

    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    if not args.question:
        print("Error: At least one --question is required", file=sys.stderr)
        return 1

    req: dict[str, Any] = {
        "questions": list(args.question),
        "repo_path": args.repo_path or get_repo_path(),
    }
    if isinstance(identifier, int):
        req["issue"] = identifier
    else:
        req["pipeline_id"] = identifier

    try:
        resp = _handlers.request_feedback(req)
    except GatewayError as err:
        return _render_gateway_error_and_exit(err)
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code
    feedback_id = resp.get("id")
    questions = resp.get("questions", [])
    warning = resp.get("warning")
    if warning:
        print(f"Warning: {warning}", file=sys.stderr)

    output_format = getattr(args, "format", "json")
    if output_format == "markdown":
        print(resp.get("markdown", ""))
    else:
        print(f"Created feedback {feedback_id} with {len(questions)} question(s)")
        for q in questions:
            print(f"  {q['id']}: {q['question']}")
    return 0
