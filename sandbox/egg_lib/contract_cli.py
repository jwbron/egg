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
    egg-contract mark-task --task <id> --status <status>
                                               Mark task status (reviewer only)
    egg-contract mark-phase --phase <id> --passed <bool>
                                               Mark phase status (reviewer only)
    egg-contract add-decision --question <text>
                                               Create HITL decision point
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

# Regex for validating git commit SHAs (7-40 hex characters)
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def get_gateway_url() -> str:
    """Get the gateway URL from environment or default."""
    return os.environ.get("GATEWAY_URL", "http://egg-gateway:9848")


def get_issue_number() -> int | None:
    """Get the current issue number from environment."""
    issue_str = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


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


def parse_task_id(task_id: str) -> tuple[int, int]:
    """Parse task ID and return (phase_idx, task_idx).

    Args:
        task_id: Task ID in format "task-N" or "task-P-T"

    Returns:
        Tuple of (phase_idx, task_idx) as 0-based indices

    Raises:
        ValueError: If task ID format is invalid or numbers are out of range
    """
    task_parts = task_id.lower().replace("task-", "").split("-")
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
    try:
        phase_num = int(phase_id.lower().replace("phase-", ""))
        if phase_num < 1:
            raise ValueError(f"Phase number must be >= 1: {phase_id}")
        return phase_num - 1
    except ValueError as e:
        if "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid phase ID '{phase_id}': expected numeric value") from e


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
        SystemExit: On request failure
    """
    gateway_url = get_gateway_url()
    url = f"{gateway_url}{endpoint}"

    headers = {"Content-Type": "application/json"}

    # Add session token if available
    token = get_session_token()
    if token:
        headers["X-Egg-Session-Token"] = token

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
            details = error_data.get("details", {})
            print(f"Error: {message}", file=sys.stderr)
            if details:
                print(f"Details: {json.dumps(details, indent=2)}", file=sys.stderr)
        except (json.JSONDecodeError, Exception):
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Error connecting to gateway: {e.reason}", file=sys.stderr)
        print("Is the gateway running?", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print("Error: Request to gateway timed out", file=sys.stderr)
        sys.exit(1)


def cmd_show(args: argparse.Namespace) -> int:
    """Display current contract state."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required. Set EGG_ISSUE_NUMBER or use --issue", file=sys.stderr)
        return 1

    params: dict[str, str] = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    if args.audit:
        params["include_audit_log"] = "true"

    endpoint = f"/api/v1/contract/{issue_number}"
    if params:
        endpoint += "?" + urlencode(params)

    result = make_gateway_request(endpoint)

    if result.get("success"):
        contract = result.get("data", {})
        if args.json:
            print(json.dumps(contract, indent=2))
        else:
            _print_contract_summary(contract)
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1

    return 0


def _print_contract_summary(contract: dict[str, Any]) -> None:
    """Print a human-readable contract summary."""
    print(
        f"Issue: #{contract.get('issue', {}).get('number')} - {contract.get('issue', {}).get('title')}"
    )
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

    # Show pending decisions
    decisions = [d for d in contract.get("decisions", []) if not d.get("resolved")]
    if decisions:
        print("Pending Decisions:")
        for decision in decisions:
            print(f"  [{decision.get('id')}] {decision.get('question')}")
        print()

    # Show circuit breaker status
    cb = contract.get("circuit_breaker", {})
    if cb.get("status") == "open":
        print(
            f"⚠ Circuit Breaker: OPEN (cycles: {cb.get('total_cycles')}/{cb.get('max_total_cycles')})"
        )


def cmd_add_commit(args: argparse.Namespace) -> int:
    """Link a commit to a task."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    try:
        phase_idx, task_idx = parse_task_id(args.task)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        validate_commit_sha(args.commit)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    field_path = f"phases.{phase_idx}.tasks.{task_idx}.commit"

    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": field_path,
            "new_value": args.commit,
            "actor": "egg",
            "reason": f"Linked commit {args.commit[:7]} to {args.task}",
        },
    )

    if result.get("success"):
        print(f"Linked commit {args.commit[:7]} to {args.task}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_update_notes(args: argparse.Namespace) -> int:
    """Add implementation notes to a task."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    try:
        phase_idx, task_idx = parse_task_id(args.task)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    field_path = f"phases.{phase_idx}.tasks.{task_idx}.notes"

    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": field_path,
            "new_value": args.notes,
            "actor": "egg",
            "reason": f"Updated notes for {args.task}",
        },
    )

    if result.get("success"):
        print(f"Updated notes for {args.task}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_mark_task(args: argparse.Namespace) -> int:
    """Mark task status.

    Note: This operation requires REVIEWER role. Agents running as IMPLEMENTER
    will receive a role authorization error from the gateway. This command is
    included for reviewer agents or human reviewers using the CLI.
    """
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    try:
        phase_idx, task_idx = parse_task_id(args.task)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    field_path = f"phases.{phase_idx}.tasks.{task_idx}.status"

    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": field_path,
            "new_value": args.status,
            "actor": "egg",
            "reason": f"Marked {args.task} as {args.status}",
        },
    )

    if result.get("success"):
        print(f"Marked {args.task} as {args.status}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_mark_phase(args: argparse.Namespace) -> int:
    """Mark phase status.

    Note: This operation requires REVIEWER role. Agents running as IMPLEMENTER
    will receive a role authorization error from the gateway. This command is
    included for reviewer agents or human reviewers using the CLI.
    """
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    try:
        phase_idx = parse_phase_id(args.phase)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    field_path = f"phases.{phase_idx}.status"
    new_status = "complete" if args.passed else "incomplete"

    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": field_path,
            "new_value": new_status,
            "actor": "egg",
            "reason": f"Marked {args.phase} as {new_status}",
        },
    )

    if result.get("success"):
        print(f"Marked {args.phase} as {new_status}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


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

    The output format matches what sdlc-hitl.yml expects:
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
    """
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    try:
        criterion_idx = parse_criterion_id(args.criterion)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    field_path = f"acceptance_criteria.{criterion_idx}.verified"

    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": field_path,
            "new_value": True,
            "actor": "egg",
            "reason": f"Verified criterion {args.criterion}",
        },
    )

    if result.get("success"):
        print(f"Verified criterion {args.criterion}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_add_decision(args: argparse.Namespace) -> int:
    """Create a HITL decision point.

    Note: There is a potential race condition between reading the current
    decision count and submitting the mutation. If concurrent agents both
    call add-decision simultaneously, they may compute the same decision ID.
    The gateway should handle conflicts by rejecting duplicate indices or
    assigning IDs server-side. This is documented as a known limitation.
    """
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Issue number required", file=sys.stderr)
        return 1

    # Get the current contract to determine the next decision ID
    # NOTE: TOCTOU race condition exists here - concurrent calls may get same ID.
    # The gateway should handle conflicts appropriately.
    endpoint = f"/api/v1/contract/{issue_number}"
    if args.repo_path:
        endpoint += "?" + urlencode({"repo_path": args.repo_path})

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    decisions = contract.get("decisions", [])
    next_id = len(decisions) + 1

    # Build the new decision
    new_decision = {
        "id": f"decision-{next_id}",
        "question": args.question,
        "type": "hitl",
        "options": [],
        "resolved": False,
        "resolution": None,
        "resolved_by": None,
        "resolved_at": None,
        "debounce_until": None,
    }

    # Parse options if provided, and auto-append "Other" option
    if args.options:
        for i, opt in enumerate(args.options):
            new_decision["options"].append(
                {"id": f"opt-{i + 1}", "label": opt, "description": None}
            )
        # Auto-append "Other (explain in reply)" as the last option
        other_idx = len(args.options) + 1
        new_decision["options"].append(
            {"id": f"opt-{other_idx}", "label": "Other (explain in reply)", "description": None}
        )

    # Add the decision to the array
    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "issue_number": issue_number,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": f"decisions.{len(decisions)}",
            "new_value": new_decision,
            "actor": "egg",
            "reason": f"Created HITL decision: {args.question[:50]}{'...' if len(args.question) > 50 else ''}",
        },
    )

    if result.get("success"):
        # Output based on format
        output_format = getattr(args, "format", "json")
        if output_format == "markdown":
            markdown = format_decision_markdown(
                new_decision["id"],
                args.question,
                new_decision["options"],
            )
            print(markdown)
        else:
            print(f"Created decision {new_decision['id']}: {args.question}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


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

    # mark-task command (requires REVIEWER role)
    mark_task_parser = subparsers.add_parser(
        "mark-task", help="Mark task status (requires REVIEWER role)"
    )
    mark_task_parser.add_argument("--task", required=True, help="Task ID")
    mark_task_parser.add_argument(
        "--status",
        required=True,
        choices=["pending", "in_progress", "complete", "incomplete", "blocked"],
        help="Task status",
    )
    mark_task_parser.set_defaults(func=cmd_mark_task)

    # mark-phase command (requires REVIEWER role)
    mark_phase_parser = subparsers.add_parser(
        "mark-phase", help="Mark phase status (requires REVIEWER role)"
    )
    mark_phase_parser.add_argument("--phase", required=True, help="Phase ID (e.g., phase-1)")
    mark_phase_parser.add_argument(
        "--passed",
        required=True,
        type=lambda x: x.lower() in ("true", "1", "yes"),
        help="Whether phase passed (true/false)",
    )
    mark_phase_parser.set_defaults(func=cmd_mark_phase)

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
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format: json (default) or markdown (for GitHub comments)",
    )
    decision_parser.set_defaults(func=cmd_add_decision)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
