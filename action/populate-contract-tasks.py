#!/usr/bin/env python3
"""
Populate contract tasks from the plan document.

Fetches the plan comment from the GitHub issue, parses it with plan_parser,
and writes the extracted phases/tasks into the contract JSON.

This script is idempotent: if the contract already has tasks, it exits
without changes.

Environment variables:
    ISSUE_NUMBER    — GitHub issue number (required)
    GITHUB_REPOSITORY — owner/repo (required)
    GH_TOKEN        — GitHub token for API access (required)

Exit codes:
    0 — Tasks populated (or already present)
    1 — Error (no plan found, parse failure, etc.)
"""

import json
import os
import re
import subprocess
import sys

from egg_contracts.models import Contract
from egg_contracts.plan_parser import parse_plan
from pydantic import ValidationError


def get_issue_comments(repo: str, issue_number: str, token: str) -> list[str]:
    """Fetch all comments on a GitHub issue.

    Returns comment bodies as a list of strings. Each string is the full
    body of a single comment, preserving newlines within the comment.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{issue_number}/comments",
            "--paginate",
            "--slurp",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_TOKEN": token},
    )
    if result.returncode != 0:
        print(f"Failed to fetch comments: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # With --slurp, gh outputs all pages as a single JSON array of arrays.
    # Each inner array is one page of results.
    output = result.stdout.strip()
    if not output:
        return []

    try:
        pages = json.loads(output)
    except json.JSONDecodeError as e:
        print(f"Failed to parse comments JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Flatten pages and extract comment bodies
    bodies: list[str] = []
    for page in pages:
        if isinstance(page, list):
            bodies.extend(c.get("body", "") for c in page if c.get("body"))

    return bodies


def find_plan_comment(comments: list[str]) -> str | None:
    """Find the plan document comment by looking for task markers."""
    for comment in reversed(comments):
        if "[TASK-" in comment and ("## Phase" in comment or "Phase 1:" in comment):
            return comment
    return None


def extract_acceptance_criteria(plan_content: str) -> list[dict]:
    """Extract acceptance criteria from plan content.

    Looks for:
    - Exit criteria from phase sections (e.g., "**Exit criteria**: ...")
    - Test strategy sections (unit tests, integration tests, manual testing)

    Returns a list of acceptance criterion dicts matching the contract schema:
    [{"id": "ac-1", "description": "...", "verified": False}, ...]
    """
    criteria: list[dict] = []
    criterion_id = 1

    # Pattern for exit criteria (handles both ** and non-bold variants)
    # Matches: "**Exit criteria**:" or "Exit criteria:" followed by content
    # Captures until double newline, end of string, or next section header
    # Uses a more permissive pattern that captures multi-line content
    exit_criteria_pattern = re.compile(
        r"\*{0,2}Exit criteria\*{0,2}\s*:\s*(.+?)(?=\n\n|\n#{2,}|\Z)",
        re.IGNORECASE | re.DOTALL,
    )

    for match in exit_criteria_pattern.finditer(plan_content):
        criterion_text = match.group(1).strip()
        # Clean up the criterion text - remove leading dashes/bullets
        criterion_text = criterion_text.lstrip("- ").strip()
        if criterion_text and len(criterion_text) > 5:  # Skip empty or tiny matches
            criteria.append(
                {
                    "id": f"ac-{criterion_id}",
                    "description": criterion_text,
                    "verified": False,
                }
            )
            criterion_id += 1

    # Also extract from Test Strategy section if present
    # Use negative lookbehind to ensure we match exactly ## (not ### or more)
    test_strategy_pattern = re.compile(
        r"(?:^|\n)##(?!#)\s*Test Strategy\s*\n(.*?)(?=\n##(?!#)|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    test_match = test_strategy_pattern.search(plan_content)
    if test_match:
        test_section = test_match.group(1)
        # Look for bullet points in the test strategy
        for line in test_section.split("\n"):
            line = line.strip()
            if line.startswith("-") or line.startswith("*"):
                # Extract the test item
                test_item = line.lstrip("-* ").strip()
                # Skip template placeholders (lines starting with "[")
                if test_item and not test_item.startswith("[") and len(test_item) > 10:
                    criteria.append(
                        {
                            "id": f"ac-{criterion_id}",
                            "description": f"Test: {test_item}",
                            "verified": False,
                        }
                    )
                    criterion_id += 1

    return criteria


def main() -> None:
    issue_number = os.environ.get("ISSUE_NUMBER")
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN")

    if not all([issue_number, repo, token]):
        print("Missing required environment variables", file=sys.stderr)
        sys.exit(1)

    contract_path = f".egg-state/contracts/{issue_number}.json"

    if not os.path.exists(contract_path):
        print(f"Contract not found: {contract_path}", file=sys.stderr)
        sys.exit(1)

    with open(contract_path) as f:
        contract = json.load(f)

    # Check if tasks already exist
    total_tasks = sum(len(phase.get("tasks", [])) for phase in contract.get("phases", []))
    if total_tasks > 0:
        print(f"Contract already has {total_tasks} tasks, skipping population")
        sys.exit(0)

    # Fetch plan comment
    print("Fetching issue comments to find plan document...")
    comments = get_issue_comments(repo, issue_number, token)

    plan_content = find_plan_comment(comments)
    if not plan_content:
        print("No plan document found in issue comments", file=sys.stderr)
        sys.exit(1)

    # Parse plan
    print("Parsing plan document...")
    result = parse_plan(plan_content)

    if not result.success:
        print(f"Plan parsing failed: {result.error}", file=sys.stderr)
        sys.exit(1)

    if result.warnings:
        for warning in result.warnings:
            print(f"  Warning: {warning.message}")

    # Convert to contract format
    phases_data = []
    total_tasks = 0
    for phase in result.phases:
        contract_phase = phase.to_contract_phase()
        phase_dict = {
            "id": contract_phase.id,
            "name": contract_phase.name,
            "status": str(contract_phase.status),
            "tasks": [],
            "review_cycles": 0,
            "max_cycles": 3,
            "escalated": False,
            "escalation_reason": None,
            "review_feedback": [],
        }
        for task in contract_phase.tasks:
            task_dict = {
                "id": task.id,
                "description": task.description,
                "status": str(task.status),
                "acceptance_criteria": task.acceptance_criteria,
                "files_affected": task.files_affected,
                "commit": None,
                "review_cycles": 0,
                "max_cycles": 3,
                "escalated": False,
                "notes": "",
            }
            phase_dict["tasks"].append(task_dict)
            total_tasks += 1
        phases_data.append(phase_dict)

    if total_tasks == 0:
        print("No tasks extracted from plan document", file=sys.stderr)
        sys.exit(1)

    # Extract acceptance criteria from the plan
    acceptance_criteria = extract_acceptance_criteria(plan_content)
    if acceptance_criteria:
        print(f"Extracted {len(acceptance_criteria)} acceptance criteria from plan")

    # Update contract and validate
    contract["phases"] = phases_data
    if acceptance_criteria:
        contract["acceptance_criteria"] = acceptance_criteria

    try:
        Contract.model_validate(contract)
    except ValidationError as e:
        print(f"Generated contract is invalid: {e}", file=sys.stderr)
        sys.exit(1)

    with open(contract_path, "w") as f:
        json.dump(contract, f, indent=2)
        f.write("\n")

    print(f"Populated contract with {len(phases_data)} phases and {total_tasks} tasks")
    if acceptance_criteria:
        print(f"  and {len(acceptance_criteria)} acceptance criteria")


if __name__ == "__main__":
    main()
