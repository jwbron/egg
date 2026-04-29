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

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from egg_contracts.agent_roles import AgentRole, get_role_definition

from egg_lib.config import GATEWAY_PORT

# Shared exception types so handlers (and this CLI) can raise instead of
# calling sys.exit.  See sandbox/egg_agent_tools/handlers/errors.py.
try:
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError
except ImportError:  # pragma: no cover - only during partial bootstraps

    class HandlerError(Exception):  # type: ignore[no-redef]
        def __init__(self, message: str, *, details: Any = None, exit_code: int = 1) -> None:
            super().__init__(message)
            self.message = message
            self.details = details or {}
            self.exit_code = exit_code

    class GatewayError(HandlerError):  # type: ignore[no-redef]
        def __init__(
            self,
            message: str,
            *,
            status_code: int | None = None,
            details: Any = None,
            hint: str | None = None,
        ) -> None:
            super().__init__(message, details=details)
            self.status_code = status_code
            self.hint = hint


# Regex for validating git commit SHAs (7-40 hex characters)
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def get_gateway_url() -> str:
    """Get the gateway URL from environment or default."""
    return os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")


def get_issue_number() -> int | None:
    """Get the current issue number from environment."""
    issue_str = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


def get_pipeline_id() -> str | None:
    """Get the pipeline ID from environment.

    Used when running in pipeline mode with JIRA tickets
    instead of GitHub issues.
    """
    return os.environ.get("EGG_PIPELINE_ID") or None


def get_contract_identifier(args: argparse.Namespace) -> int | str | None:
    """Resolve the contract identifier from args and environment.

    Priority (highest to lowest):
    1. --issue flag (int, for backward compatibility)
    2. --pipeline-id flag (str)
    3. EGG_PIPELINE_ID env var (str) — preferred because contracts are
       keyed by pipeline_id on disk; this also covers qualified pipelines
       (e.g. ``issue-1759-v2``) where the bare issue number can't
       disambiguate between multiple pipelines for the same issue.
    4. EGG_ISSUE_NUMBER env var (int) — legacy fallback.

    Returns:
        int for issue numbers, str for pipeline IDs, None if nothing found
    """
    issue_arg: int | None = args.issue
    if issue_arg is not None:
        return issue_arg
    pipeline_id_arg: str | None = getattr(args, "pipeline_id", None)
    if pipeline_id_arg is not None:
        return pipeline_id_arg
    pipeline_id = get_pipeline_id()
    if pipeline_id is not None:
        return pipeline_id
    return get_issue_number()


def get_repo_path() -> str:
    """Get the repository path from environment or default."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


def get_session_token() -> str | None:
    """Get the session token for gateway authentication."""
    # Try environment variable first
    token = os.environ.get("EGG_SESSION_TOKEN")
    if token:
        return token

    # Try reading from file (used in container)
    token_file = Path.home() / ".egg-session-token"
    if token_file.exists():
        return token_file.read_text().strip()

    return None


def get_container_id() -> str:
    """Get the container ID from environment."""
    return os.environ.get("CONTAINER_ID", "")


def _container_id_field() -> dict[str, str]:
    """Return a dict with container_id only when the env var is set.

    Used with ``**`` unpacking in POST data dicts so that an empty
    container_id is never sent over the wire, matching the conditional
    GET-parameter pattern used elsewhere in this module.
    """
    cid = get_container_id()
    return {"container_id": cid} if cid else {}


def parse_task_id(task_id: str) -> tuple[int, int]:
    """Parse task ID and return (phase_idx, task_idx).

    Args:
        task_id: Task ID in format "task-N" or "task-P-T"

    Returns:
        Tuple of (phase_idx, task_idx) as 0-based indices

    Raises:
        ValueError: If task ID format is invalid or numbers are out of range
    """
    lower = task_id.lower()
    stripped = lower.removeprefix("task-")
    if stripped == lower:
        raise ValueError(f"Invalid task ID '{task_id}': expected format 'task-N' or 'task-P-T'")
    task_parts = stripped.split("-")
    try:
        if len(task_parts) == 1:
            # Simple format: task-N (assumes phase-1)
            phase_idx = 0
            task_idx = int(task_parts[0]) - 1
        elif len(task_parts) == 2:
            # Full format: task-P-T
            phase_idx = int(task_parts[0]) - 1
            task_idx = int(task_parts[1]) - 1
        else:
            raise ValueError(f"Invalid task ID format: {task_id}")

        if phase_idx < 0 or task_idx < 0:
            raise ValueError(f"Task/phase numbers must be >= 1: {task_id}")
        return phase_idx, task_idx
    except ValueError as e:
        if "Invalid task ID" in str(e) or "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid task ID '{task_id}': expected numeric values") from e


def parse_criterion_id(criterion_id: str) -> int:
    """Parse criterion ID and return criterion_idx.

    Args:
        criterion_id: Criterion ID in format "ac-N"

    Returns:
        Criterion index as 0-based

    Raises:
        ValueError: If criterion ID format is invalid or number is out of range
    """
    try:
        criterion_num = int(criterion_id.lower().replace("ac-", ""))
        if criterion_num < 1:
            raise ValueError(f"Criterion number must be >= 1: {criterion_id}")
        return criterion_num - 1
    except ValueError as e:
        if "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid criterion ID '{criterion_id}': expected format 'ac-N'") from e


def parse_phase_id(phase_id: str) -> int:
    """Parse phase ID and return phase_idx.

    Args:
        phase_id: Phase ID in format "phase-N"

    Returns:
        Phase index as 0-based

    Raises:
        ValueError: If phase ID format is invalid or number is out of range
    """
    lower = phase_id.lower()
    stripped = lower.removeprefix("phase-")
    if stripped == lower:
        # prefix was not present
        raise ValueError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'")
    try:
        phase_num = int(stripped)
        if phase_num < 1:
            raise ValueError(f"Phase number must be >= 1: {phase_id}")
        return phase_num - 1
    except ValueError as e:
        if "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'") from e


def validate_commit_sha(commit: str) -> str:
    """Validate a git commit SHA.

    Args:
        commit: Git commit SHA (7-40 hex characters)

    Returns:
        The validated commit SHA

    Raises:
        ValueError: If the commit SHA format is invalid
    """
    if not COMMIT_SHA_PATTERN.match(commit):
        raise ValueError(f"Invalid commit SHA '{commit}': expected 7-40 hexadecimal characters")
    return commit


def make_gateway_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a request to the gateway API.

    Args:
        endpoint: API endpoint (e.g., "/api/v1/contract/123")
        method: HTTP method
        data: Request body data (for POST requests)

    Returns:
        Response data as dictionary

    Raises:
        GatewayError: On HTTP/URL/timeout failure.  The caller (either a
            ``cmd_*`` shim or a pure handler in ``egg_agent_tools``) is
            responsible for rendering the error — ``make_gateway_request``
            itself no longer calls ``sys.exit``.  Callers that want the
            legacy print-and-exit behaviour should catch ``GatewayError``
            and call :func:`_render_gateway_error_and_exit`.
    """
    gateway_url = get_gateway_url()
    url = f"{gateway_url}{endpoint}"

    headers = {"Content-Type": "application/json"}

    # Add session token if available
    token = get_session_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode() if data else None

    try:
        request = Request(url, data=body, headers=headers, method=method)
        with urlopen(request, timeout=30) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        try:
            error_data = json.loads(e.read().decode())
            message = error_data.get("message", str(e))
            details = error_data.get("details") or {}
        except json.JSONDecodeError, Exception:
            message = str(e)
            details = {}
        raise GatewayError(
            message,
            status_code=getattr(e, "code", None),
            details=details if isinstance(details, dict) else {"raw": details},
        ) from e
    except URLError as e:
        raise GatewayError(
            f"connecting to gateway: {e.reason}",
            hint="Is the gateway running?",
        ) from e
    except TimeoutError as e:
        raise GatewayError("Request to gateway timed out") from e


def _render_gateway_error_and_exit(err: GatewayError) -> int:
    """Render a GatewayError on stderr in the legacy make_gateway_request shape.

    Kept as a helper so cmd_* shims that historically exited from inside
    ``make_gateway_request`` continue to produce byte-identical output.
    """
    # Legacy shape — message first, then optional indented details, then
    # the hint (for URL errors).
    print(f"Error: {err.message}", file=sys.stderr)
    if err.details:
        try:
            print(f"Details: {json.dumps(err.details, indent=2)}", file=sys.stderr)
        except TypeError, ValueError:
            pass
    if err.hint:
        print(err.hint, file=sys.stderr)
    return err.exit_code


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

    phases = contract.get("phases", [])
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


def validate_decision_id(decision_id: str) -> None:
    """Validate decision_id matches the expected format.

    The workflow regex expects [a-z0-9-]+ and the ID must end at a valid
    boundary (space or '>') in the HTML comment.

    Args:
        decision_id: The decision ID to validate

    Raises:
        ValueError: If decision_id contains invalid characters
    """
    if not re.match(r"^[a-z0-9-]+$", decision_id):
        raise ValueError(
            f"Invalid decision_id '{decision_id}': must contain only "
            "lowercase letters, numbers, and hyphens"
        )


def format_decision_markdown(decision_id: str, question: str, options: list[dict[str, Any]]) -> str:
    """Format a HITL decision as markdown with proper markers.

    The output format matches what the HITL decision handler expects:
    - HTML comment marker with decision ID for detection
    - Checkbox list for options

    Args:
        decision_id: The decision ID (e.g., "decision-1"). Must match [a-z0-9-]+
        question: The decision question
        options: List of option dicts with 'label' keys

    Returns:
        Formatted markdown string ready for GitHub comment

    Raises:
        ValueError: If decision_id contains invalid characters
    """
    validate_decision_id(decision_id)

    lines = [
        f"<!-- egg-hitl-decision id={decision_id} -->",
        "",
        f"**{question}**",
        "",
    ]

    for opt in options:
        lines.append(f"- [ ] {opt['label']}")

    return "\n".join(lines)


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


VALID_AGENT_ROLES = ["coder", "tester", "documenter"]
VALID_AGENT_STATUSES = ["pending", "running", "complete", "failed", "skipped", "blocked"]


def cmd_agent_status(args: argparse.Namespace) -> int:
    """Show agent execution status for multi-agent orchestration."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    result = make_gateway_request(endpoint)

    if not result.get("success"):
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1

    contract = result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    if args.json:
        # Output JSON format
        output = {
            "agent_executions": agent_executions,
        }
        print(json.dumps(output, indent=2))
    else:
        # Human-readable format
        if not agent_executions:
            print("No agent executions found. Multi-agent mode may not be enabled.")
            return 0

        print(f"Agent Executions for {identifier}:")
        print()

        for execution in agent_executions:
            role = execution.get("role", "unknown")
            status = execution.get("status", "pending")
            commit = execution.get("commit")
            error = execution.get("error")
            retry_count = execution.get("retry_count", 0)

            status_icon = {
                "pending": "○",
                "running": "◐",
                "complete": "●",
                "failed": "✗",
                "skipped": "⊘",
                "blocked": "⊘",
            }.get(status, "?")

            line = f"  {status_icon} {role}: {status}"
            if commit:
                line += f" (commit: {commit[:7]})"
            if retry_count > 0:
                line += f" [retries: {retry_count}]"
            print(line)

            if error:
                print(f"      Error: {error}")

        # Show summary
        print()
        pending = sum(1 for e in agent_executions if e.get("status") == "pending")
        running = sum(1 for e in agent_executions if e.get("status") == "running")
        complete = sum(1 for e in agent_executions if e.get("status") == "complete")
        failed = sum(1 for e in agent_executions if e.get("status") == "failed")

        print(
            f"Summary: {complete} complete, {running} running, {pending} pending, {failed} failed"
        )

    return 0


def cmd_agent_start(args: argparse.Namespace) -> int:
    """Mark an agent as started (running)."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Update status to running
    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "running",
            "actor": "egg",
            "reason": f"Started {role} agent",
            **_container_id_field(),
        },
    )

    if result.get("success"):
        print(f"Started agent: {role}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_agent_complete(args: argparse.Namespace) -> int:
    """Mark an agent as complete."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Build updates
    updates = [
        {
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "complete",
        }
    ]

    # Add commit if provided
    if args.commit:
        try:
            validate_commit_sha(args.commit)
            updates.append(
                {
                    "field_path": f"agent_executions.{exec_idx}.commit",
                    "new_value": args.commit,
                }
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Apply updates
    for update in updates:
        result = make_gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": args.repo_path or get_repo_path(),
                "field_path": update["field_path"],
                "new_value": update["new_value"],
                "actor": "egg",
                "reason": f"Completed {role} agent",
                **_container_id_field(),
            },
        )

        if not result.get("success"):
            print(f"Error: {result.get('message')}", file=sys.stderr)
            return 1

    commit_info = f" (commit: {args.commit[:7]})" if args.commit else ""
    print(f"Completed agent: {role}{commit_info}")
    return 0


def cmd_agent_fail(args: argparse.Namespace) -> int:
    """Mark an agent as failed."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Update status and error
    updates = [
        {
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "failed",
        },
        {
            "field_path": f"agent_executions.{exec_idx}.error",
            "new_value": args.error,
        },
    ]

    for update in updates:
        result = make_gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": args.repo_path or get_repo_path(),
                "field_path": update["field_path"],
                "new_value": update["new_value"],
                "actor": "egg",
                "reason": f"Failed {role} agent: {args.error[:50]}",
                **_container_id_field(),
            },
        )

        if not result.get("success"):
            print(f"Error: {result.get('message')}", file=sys.stderr)
            return 1

    print(f"Marked agent as failed: {role}")
    print(f"  Error: {args.error}")
    return 0


def cmd_agent_next(args: argparse.Namespace) -> int:
    """Get the next wave of agents to dispatch."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    if not agent_executions:
        print("No agent executions configured. Multi-agent mode may not be enabled.")
        return 0

    # Compute next dispatch locally
    # This mirrors the logic in orchestrator.py but runs client-side
    pending = []
    running = []
    complete = []
    failed = []

    for execution in agent_executions:
        status = execution.get("status", "pending")
        role = execution.get("role")
        if status == "pending":
            pending.append(role)
        elif status == "running":
            running.append(role)
        elif status == "complete":
            complete.append(role)
        elif status == "failed":
            failed.append(role)

    if failed:
        output: dict[str, Any] = {
            "status": "failed",
            "agents": [],
            "reason": f"Agents failed: {', '.join(failed)}",
            "failed_agents": failed,
        }
    elif not pending and not running:
        output = {
            "status": "complete",
            "agents": [],
            "reason": "All agents have completed",
        }
    elif running:
        output = {
            "status": "waiting",
            "agents": [],
            "reason": f"Waiting for running agents: {', '.join(running)}",
            "running_agents": running,
        }
    else:
        # Determine which pending agents can run based on dependencies
        # Use canonical dependency definitions from agent_roles module
        runnable = []
        for role in pending:
            try:
                role_enum = AgentRole(role)
                role_def = get_role_definition(role_enum)
                # Check if all dependencies are complete
                deps_satisfied = all(dep.value in complete for dep in role_def.dependencies)
                if deps_satisfied:
                    runnable.append(role)
            except ValueError, KeyError:
                # Unknown role - skip
                pass

        if runnable:
            output = {
                "status": "dispatch",
                "agents": runnable,
                "parallel": len(runnable) > 1,
                "reason": f"Ready to dispatch: {', '.join(runnable)}",
            }
        else:
            output = {
                "status": "blocked",
                "agents": [],
                "reason": "Pending agents blocked by dependencies",
                "pending_agents": pending,
            }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"Status: {output['status']}")
        if output.get("agents"):
            print(f"Agents to dispatch: {', '.join(output['agents'])}")
            if output.get("parallel"):
                print("  (can run in parallel)")
        print(f"Reason: {output['reason']}")

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


if __name__ == "__main__":
    sys.exit(main())
