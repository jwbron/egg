#!/usr/bin/env python3
"""
Lint check: Detect untrusted script execution with secrets in GitHub Actions.

Catches the pattern where a workflow step:
  1. Runs a script file from the repo (bash <path>, sh <path>, ./<path>, etc.)
  2. Has secrets/tokens in its env: block
  3. The most recent checkout was NOT from a trusted ref (main or fixed SHA)

This prevents a malicious PR from replacing a script to exfiltrate tokens.

Inline run: commands (not script files) are allowed because they are defined
in the workflow YAML itself, which lives on the trusted default branch.

Usage:
    python3 scripts/check-workflow-secrets.py

Exit codes:
    0 - No issues found
    1 - Found untrusted script execution with secrets
"""

import re
import sys
from pathlib import Path

import yaml

# Patterns that indicate a run: step is executing a script file (not inline)
SCRIPT_PATTERNS = [
    # bash/sh/zsh <path>
    re.compile(r"^\s*(?:bash|sh|zsh)\s+(\S+)"),
    # python/python3 <path>
    re.compile(r"^\s*(?:python3?)\s+(\S+)"),
    # node <path>
    re.compile(r"^\s*node\s+(\S+)"),
    # ./<path> or source <path>
    re.compile(r"^\s*(?:\./|source\s+)(\S+)"),
]

# Env value patterns that indicate secrets/tokens
SECRET_PATTERNS = [
    re.compile(r"\$\{\{\s*secrets\."),
    re.compile(r"\$\{\{\s*steps\.[^}]*\.outputs\.token"),
    re.compile(r"\$\{\{\s*steps\.[^}]*\.outputs\.private-key"),
]

# Env key names that indicate tokens
SECRET_KEY_NAMES = {"GH_TOKEN", "GITHUB_TOKEN"}


def is_script_execution(run_cmd: str) -> str | None:
    """Return the script path if run: executes a file, else None."""
    first_line = run_cmd.strip().split("\n")[0]
    for pattern in SCRIPT_PATTERNS:
        m = pattern.match(first_line)
        if m:
            path = m.group(1).strip('"').strip("'")
            # Filter out flags (e.g. -c, -e) and env vars
            if path.startswith("-") or path.startswith("$"):
                continue
            return path
    return None


def has_secret_env(step: dict) -> list[str]:
    """Return list of env keys that contain secrets/tokens."""
    env = step.get("env", {})
    if not env or not isinstance(env, dict):
        return []
    flagged = []
    for key, value in env.items():
        value_str = str(value)
        if key in SECRET_KEY_NAMES:
            flagged.append(key)
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(value_str):
                flagged.append(key)
                break
    return flagged


def is_trusted_checkout(step: dict) -> bool:
    """Check if a checkout step uses a trusted ref (main or omitted)."""
    uses = step.get("uses", "")
    if "actions/checkout" not in uses:
        return False
    with_block = step.get("with", {})
    if not with_block:
        # No 'with' block means default checkout (triggered ref for push/PR)
        # For pull_request triggers this is the merge ref, not trusted
        return False
    ref = with_block.get("ref", "")
    ref_str = str(ref)
    # Trusted if ref is 'main', 'master', or a full SHA
    if ref_str in ("main", "master"):
        return True
    # 40-char hex SHA
    if re.match(r"^[0-9a-f]{40}$", ref_str):
        return True
    return False


def is_checkout_step(step: dict) -> bool:
    """Check if a step is an actions/checkout step."""
    uses = step.get("uses", "")
    return "actions/checkout" in str(uses)


def check_workflow(workflow_path: Path) -> list[str]:
    """Check a single workflow file for untrusted script + secrets issues."""
    violations = []
    content = workflow_path.read_text()
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as e:
        print(f"Warning: Could not parse {workflow_path}: {e}", file=sys.stderr)
        return []

    if not doc or "jobs" not in doc:
        return []

    for job_name, job in doc["jobs"].items():
        steps = job.get("steps", [])
        # Track whether the last checkout was trusted
        last_checkout_trusted: bool | None = None

        for step in steps:
            if not isinstance(step, dict):
                continue

            # Track checkout state
            if is_checkout_step(step):
                last_checkout_trusted = is_trusted_checkout(step)
                continue

            # Check run: steps
            run_cmd = step.get("run")
            if not run_cmd:
                continue

            script_path = is_script_execution(str(run_cmd))
            if not script_path:
                continue

            secret_keys = has_secret_env(step)
            if not secret_keys:
                continue

            # If last checkout was trusted (or no checkout yet), it's fine
            if last_checkout_trusted is True:
                continue
            if last_checkout_trusted is None:
                # No checkout happened yet — step runs from default checkout
                # which for pull_request triggers is the merge ref (untrusted)
                pass

            step_name = step.get("name", "(unnamed)")
            violations.append(
                f'  {workflow_path}  job={job_name}  step="{step_name}"\n'
                f"    Script: {script_path}\n"
                f"    Secrets in env: {', '.join(secret_keys)}\n"
                f"    Last checkout was NOT trusted (not ref: main or fixed SHA)\n"
                f"    Fix: Copy the script to $RUNNER_TEMP before the untrusted "
                f"checkout, then run from $RUNNER_TEMP"
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
        all_violations.extend(check_workflow(wf))

    if all_violations:
        print("ERROR: Found script execution with secrets after untrusted checkout!\n")
        print("=" * 70)
        print("A run: step executes a script FILE from the repo with secrets/tokens")
        print("in env:, but the most recent checkout is NOT from a trusted ref.")
        print("")
        print("A malicious PR could replace the script to exfiltrate tokens.")
        print("=" * 70)
        print()
        for v in all_violations:
            print(v)
            print()
        print("How to fix:")
        print("  1. Before the untrusted checkout, copy the script to $RUNNER_TEMP:")
        print('     run: cp <script> "$RUNNER_TEMP/<script>"')
        print("  2. After the untrusted checkout, run from the saved copy:")
        print('     run: bash "$RUNNER_TEMP/<script>"')
        print()
        return 1
    else:
        print("OK: No untrusted script execution with secrets found in workflows")
        return 0


if __name__ == "__main__":
    sys.exit(main())
