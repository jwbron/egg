#!/usr/bin/env python3
"""
Lint check: Ensure reviewer workflows use the standard job naming convention.

All workflows that use reusable-review.yml must have job names prefixed with
"egg-review /" to ensure the wait-for-checks filter correctly excludes them.

This prevents infinite loops where reviewers wait for each other indefinitely.

Usage:
    python3 scripts/check-reviewer-job-names.py

Exit codes:
    0 - All reviewer jobs follow naming convention
    1 - Found reviewer jobs without the required prefix
"""

import sys
from pathlib import Path

import yaml

REQUIRED_PREFIX = "egg-review /"
REUSABLE_WORKFLOW = "./.github/workflows/reusable-review.yml"


def check_workflow(workflow_path: Path) -> list[str]:
    """Check a single workflow file for reviewer naming convention."""
    violations = []
    content = workflow_path.read_text()
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as e:
        print(f"Warning: Could not parse {workflow_path}: {e}", file=sys.stderr)
        return []

    if not doc or "jobs" not in doc:
        return []

    for job_id, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue

        uses = job.get("uses", "")
        if REUSABLE_WORKFLOW not in str(uses):
            continue

        # This job uses reusable-review.yml - check its name
        job_name = job.get("name", "")
        if not job_name:
            violations.append(
                f"  {workflow_path}  job={job_id}\n"
                f"    Job uses reusable-review.yml but has no 'name:' field\n"
                f"    Add: name: {REQUIRED_PREFIX}<description>"
            )
        elif not job_name.startswith(REQUIRED_PREFIX):
            violations.append(
                f"  {workflow_path}  job={job_id}\n"
                f"    Job name: {job_name!r}\n"
                f"    Required prefix: {REQUIRED_PREFIX!r}\n"
                f"    Change to: name: {REQUIRED_PREFIX}{job_name}"
            )

    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    workflows_dir = repo_root / ".github" / "workflows"

    if not workflows_dir.exists():
        print("Warning: .github/workflows/ directory not found")
        return 0

    workflow_files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))

    if not workflow_files:
        print("Warning: No workflow files found")
        return 0

    all_violations: list[str] = []
    for wf in workflow_files:
        # Skip the reusable workflow itself
        if wf.name == "reusable-review.yml":
            continue
        all_violations.extend(check_workflow(wf))

    if all_violations:
        print("ERROR: Found reviewer jobs without required naming prefix!\n")
        print("=" * 70)
        print("All jobs using reusable-review.yml must have names starting with")
        print(f"'{REQUIRED_PREFIX}' to prevent self-deadlock in wait-for-checks.")
        print("=" * 70)
        print()
        for v in all_violations:
            print(v)
            print()
        print("Why this matters:")
        print("  The wait-for-checks job filters out checks matching 'egg-review /'")
        print("  to prevent reviewers from waiting on each other indefinitely.")
        print("  Without this prefix, a new reviewer will cause infinite loops.")
        print()
        return 1
    else:
        print("OK: All reviewer jobs use the required naming prefix")
        return 0


if __name__ == "__main__":
    sys.exit(main())
