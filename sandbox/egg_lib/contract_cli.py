#!/usr/bin/env python3
"""
Contract CLI for SDLC pipeline.

Provides commands for agents to interact with contracts:
- egg-contract show                  Display current contract state
- egg-contract add-commit            Link commit to task
- egg-contract update-notes          Add implementation notes
- egg-contract mark-task             Mark task status (reviewer only)
- egg-contract mark-phase            Mark phase status (reviewer only)
- egg-contract add-decision          Create HITL decision point

All mutations route through the gateway endpoint for role enforcement.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def get_gateway_url() -> str:
    """Get the gateway base URL."""
    host = os.environ.get("EGG_GATEWAY_HOST", "egg-gateway")
    port = os.environ.get("EGG_GATEWAY_PORT", "9847")
    return f"http://{host}:{port}"


def get_repo_root() -> Path:
    """Get the repository root path."""
    # Try common locations
    candidates = [
        Path.cwd(),
        Path(os.environ.get("GITHUB_WORKSPACE", "")),
        Path("/home/egg/repos").glob("*"),
    ]

    for candidate in candidates:
        if isinstance(candidate, Path):
            if (candidate / ".git").exists() or (candidate / ".egg").exists():
                return candidate
        else:
            # It's a generator from glob
            for p in candidate:
                if (p / ".git").exists() or (p / ".egg").exists():
                    return p

    return Path.cwd()


def get_issue_number() -> int | None:
    """Get issue number from environment or branch name."""
    # Check environment
    issue_env = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_env:
        try:
            return int(issue_env)
        except ValueError:
            pass

    # Try to extract from branch name (egg/issue-123 or egg-123)
    branch = os.environ.get("GITHUB_HEAD_REF", "")
    if not branch:
        try:
            import subprocess

            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            branch = result.stdout.strip()
        except Exception:
            pass

    if branch:
        # Extract number from patterns like egg/issue-123, egg-123, egg/fix-123
        import re

        match = re.search(r"egg[-/](?:issue[-/])?(\d+)", branch.lower())
        if match:
            return int(match.group(1))

    return None


def api_request(
    method: str,
    endpoint: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make an API request to the gateway."""
    url = f"{get_gateway_url()}{endpoint}"

    try:
        if method.upper() == "GET":
            response = httpx.get(url, timeout=30)
        else:
            response = httpx.post(url, json=data, timeout=30)

        result = response.json()
        if not result.get("success", True) and response.status_code >= 400:
            return {"success": False, "message": result.get("message", "Request failed")}
        return result

    except httpx.ConnectError:
        return {
            "success": False,
            "message": f"Cannot connect to gateway at {url}. Is the gateway running?",
        }
    except Exception as e:
        return {"success": False, "message": f"Request failed: {e}"}


def cmd_show(args: argparse.Namespace) -> int:
    """Display current contract state."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number. Use --issue or set EGG_ISSUE_NUMBER.")
        return 1

    result = api_request("GET", f"/api/v1/contract/{issue_number}")

    if not result.get("success", True):
        # Try loading directly if gateway not available
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
            from egg_contracts import load_contract

            repo_root = get_repo_root()
            contract = load_contract(repo_root, issue_number)
            if contract:
                print(json.dumps(contract.model_dump(mode="json", exclude_none=True), indent=2))
                return 0
        except ImportError:
            pass

        print(f"Error: {result.get('message', 'Unknown error')}")
        return 1

    contract = result.get("contract", {})

    if args.json:
        print(json.dumps(contract, indent=2))
    else:
        # Pretty print summary
        print(
            f"Issue: #{contract.get('issue', {}).get('number')} - {contract.get('issue', {}).get('title')}"
        )
        print(f"Phase: {contract.get('currentPhase', 'unknown')}")
        print(f"Branch: {contract.get('branch', 'not set')}")
        print()

        phases = contract.get("phases", [])
        if phases:
            print("Phases:")
            for phase in phases:
                print(f"  {phase['id']}: {phase['name']} [{phase.get('status', 'pending')}]")
                for task in phase.get("tasks", []):
                    status = task.get("status", "pending")
                    commit = task.get("commit", "")
                    commit_str = f" ({commit[:7]})" if commit else ""
                    print(f"    - {task['id']}: {task['description']} [{status}]{commit_str}")
            print()

        decisions = contract.get("decisions", [])
        if decisions:
            print("Decisions:")
            for dec in decisions:
                resolved = "RESOLVED" if dec.get("resolved") else "PENDING"
                print(f"  {dec['id']}: {dec['question']} [{resolved}]")

    return 0


def cmd_add_commit(args: argparse.Namespace) -> int:
    """Link a commit to a task."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number.")
        return 1

    repo_root = get_repo_root()

    result = api_request(
        "POST",
        "/api/v1/contract/mutate",
        {
            "repo_path": str(repo_root),
            "issue_number": issue_number,
            "mutations": [
                {
                    "path": f"phases.*.tasks.{args.task}.commit",
                    "value": args.commit,
                }
            ],
        },
    )

    if result.get("success"):
        print(f"Linked commit {args.commit[:7]} to {args.task}")
        return 0
    else:
        print(f"Error: {result.get('message', 'Unknown error')}")
        if "blocked" in result.get("details", {}):
            for blocked in result["details"]["blocked"]:
                print(f"  - {blocked.get('path')}: {blocked.get('message')}")
        return 1


def cmd_update_notes(args: argparse.Namespace) -> int:
    """Add implementation notes to a task."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number.")
        return 1

    repo_root = get_repo_root()

    result = api_request(
        "POST",
        "/api/v1/contract/mutate",
        {
            "repo_path": str(repo_root),
            "issue_number": issue_number,
            "mutations": [
                {
                    "path": f"phases.*.tasks.{args.task}.notes",
                    "value": args.notes,
                }
            ],
        },
    )

    if result.get("success"):
        print(f"Updated notes for {args.task}")
        return 0
    else:
        print(f"Error: {result.get('message', 'Unknown error')}")
        return 1


def cmd_mark_task(args: argparse.Namespace) -> int:
    """Mark task status (reviewer only)."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number.")
        return 1

    repo_root = get_repo_root()

    mutations = [
        {
            "path": f"phases.*.tasks.{args.task}.status",
            "value": args.status,
        }
    ]

    # Add feedback if provided
    if args.feedback:
        mutations.append(
            {
                "path": f"phases.*.tasks.{args.task}.feedback",
                "value": [args.feedback],
            }
        )

    result = api_request(
        "POST",
        "/api/v1/contract/mutate",
        {
            "repo_path": str(repo_root),
            "issue_number": issue_number,
            "mutations": mutations,
        },
    )

    if result.get("success"):
        print(f"Marked {args.task} as {args.status}")
        return 0
    else:
        print(f"Error: {result.get('message', 'Unknown error')}")
        if "blocked" in result.get("details", {}):
            print("This command requires reviewer role.")
        return 1


def cmd_mark_phase(args: argparse.Namespace) -> int:
    """Mark phase status (reviewer only)."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number.")
        return 1

    repo_root = get_repo_root()

    result = api_request(
        "POST",
        "/api/v1/contract/mutate",
        {
            "repo_path": str(repo_root),
            "issue_number": issue_number,
            "mutations": [
                {
                    "path": f"phases.{args.phase}.status",
                    "value": "complete" if args.passed else "failed",
                }
            ],
        },
    )

    if result.get("success"):
        status = "passed" if args.passed else "failed"
        print(f"Marked {args.phase} as {status}")
        return 0
    else:
        print(f"Error: {result.get('message', 'Unknown error')}")
        if "blocked" in result.get("details", {}):
            print("This command requires reviewer role.")
        return 1


def cmd_add_decision(args: argparse.Namespace) -> int:
    """Create a HITL decision point."""
    issue_number = args.issue or get_issue_number()
    if not issue_number:
        print("Error: Could not determine issue number.")
        return 1

    repo_root = get_repo_root()

    options = []
    if args.options:
        for i, opt in enumerate(args.options.split(",")):
            options.append({"id": f"opt-{i}", "label": opt.strip()})

    result = api_request(
        "POST",
        f"/api/v1/contract/{issue_number}/decision",
        {
            "repo_path": str(repo_root),
            "question": args.question,
            "options": options if options else None,
        },
    )

    if result.get("success"):
        decision_id = result.get("decision_id", "unknown")
        print(f"Created decision {decision_id}: {args.question}")
        return 0
    else:
        print(f"Error: {result.get('message', 'Unknown error')}")
        return 1


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="egg-contract",
        description="Manage SDLC contracts for pipeline checkpoints",
    )
    parser.add_argument(
        "--issue",
        type=int,
        help="Issue number (default: from env or branch)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # show command
    show_parser = subparsers.add_parser("show", help="Display contract state")
    show_parser.add_argument("--json", action="store_true", help="Output as JSON")
    show_parser.set_defaults(func=cmd_show)

    # add-commit command
    commit_parser = subparsers.add_parser("add-commit", help="Link commit to task")
    commit_parser.add_argument("--task", required=True, help="Task ID (e.g., task-1)")
    commit_parser.add_argument("--commit", required=True, help="Git commit SHA")
    commit_parser.set_defaults(func=cmd_add_commit)

    # update-notes command
    notes_parser = subparsers.add_parser("update-notes", help="Add implementation notes")
    notes_parser.add_argument("--task", required=True, help="Task ID")
    notes_parser.add_argument("--notes", required=True, help="Notes text")
    notes_parser.set_defaults(func=cmd_update_notes)

    # mark-task command (reviewer only)
    task_parser = subparsers.add_parser("mark-task", help="Mark task status (reviewer only)")
    task_parser.add_argument("--task", required=True, help="Task ID")
    task_parser.add_argument(
        "--status",
        required=True,
        choices=["complete", "incomplete", "failed"],
        help="Task status",
    )
    task_parser.add_argument("--feedback", help="Reviewer feedback")
    task_parser.set_defaults(func=cmd_mark_task)

    # mark-phase command (reviewer only)
    phase_parser = subparsers.add_parser("mark-phase", help="Mark phase status (reviewer only)")
    phase_parser.add_argument("--phase", required=True, help="Phase ID")
    phase_parser.add_argument(
        "--passed",
        action="store_true",
        help="Mark as passed (default: failed)",
    )
    phase_parser.set_defaults(func=cmd_mark_phase)

    # add-decision command
    decision_parser = subparsers.add_parser("add-decision", help="Create HITL decision")
    decision_parser.add_argument("--question", required=True, help="Question text")
    decision_parser.add_argument("--options", help="Comma-separated options")
    decision_parser.set_defaults(func=cmd_add_decision)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
