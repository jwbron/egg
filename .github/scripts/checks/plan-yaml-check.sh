#!/usr/bin/env bash
# plan-yaml-check.sh — Validate and extract YAML tasks from plan document
#
# This check extracts the YAML task block from the plan document, validates
# its structure, and optionally outputs the parsed tasks.
#
# Environment variables:
#   EGG_ISSUE_NUMBER — Issue number (required)
#   PLAN_PATH        — Override path to plan file (optional)
#   OUTPUT_TASKS     — If "true", output parsed tasks JSON (optional)
#
# Exit codes:
#   0 - YAML is valid and tasks extracted
#   1 - YAML is invalid or missing
#   2 - Missing required environment variables
#   3 - Plan file not found

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

if [[ -z "${EGG_ISSUE_NUMBER:-}" ]]; then
    echo "[plan-yaml-check] ERROR: EGG_ISSUE_NUMBER is required" >&2
    exit 2
fi

PLAN_PATH="${PLAN_PATH:-.egg-state/drafts/${EGG_ISSUE_NUMBER}-plan.md}"

# ---------------------------------------------------------------------------
# Check plan file exists
# ---------------------------------------------------------------------------

echo "[plan-yaml-check] Checking plan: ${PLAN_PATH}"

if [[ ! -f "$PLAN_PATH" ]]; then
    echo "[plan-yaml-check] ERROR: Plan file not found: ${PLAN_PATH}" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Extract YAML block
# ---------------------------------------------------------------------------

# Find the YAML block with # yaml-tasks marker
yaml_content=""
in_yaml_block=false
found_marker=false

while IFS= read -r line; do
    if [[ "$line" == '```yaml' ]] || [[ "$line" == '```yml' ]]; then
        in_yaml_block=true
        continue
    fi

    if [[ "$in_yaml_block" == true ]]; then
        if [[ "$line" == '```' ]]; then
            in_yaml_block=false
            # Only keep if we found the marker
            if [[ "$found_marker" == true ]]; then
                break
            fi
            yaml_content=""
            continue
        fi

        # Check for yaml-tasks marker
        if [[ "$line" == *"# yaml-tasks"* ]]; then
            found_marker=true
        fi

        yaml_content+="$line"$'\n'
    fi
done < "$PLAN_PATH"

if [[ -z "$yaml_content" ]] || [[ "$found_marker" != true ]]; then
    echo "[plan-yaml-check] ERROR: No YAML block with '# yaml-tasks' marker found" >&2
    echo "[plan-yaml-check] Expected format:" >&2
    echo '```yaml' >&2
    echo '# yaml-tasks' >&2
    echo 'phases:' >&2
    echo '  - id: 1' >&2
    echo '    ...' >&2
    echo '```' >&2
    exit 1
fi

echo "[plan-yaml-check] Found YAML block (${#yaml_content} characters)"

# ---------------------------------------------------------------------------
# Validate YAML structure
# ---------------------------------------------------------------------------

# Write YAML to temp file for parsing
yaml_file=$(mktemp)
echo "$yaml_content" > "$yaml_file"
trap 'rm -f "$yaml_file"' EXIT

# Check if Python is available for proper YAML parsing
if command -v python3 &> /dev/null; then
    # Use Python for robust YAML validation
    validation_result=$(python3 << PYTHON_EOF
import sys
import yaml
import json

try:
    with open("$yaml_file", 'r') as f:
        data = yaml.safe_load(f)

    if data is None:
        print("ERROR: YAML is empty", file=sys.stderr)
        sys.exit(1)

    errors = []
    warnings = []

    # Check for required top-level keys
    if 'phases' not in data:
        errors.append("Missing 'phases' key")

    # Validate phases structure
    if 'phases' in data:
        phases = data['phases']
        if not isinstance(phases, list):
            errors.append("'phases' must be a list")
        else:
            for i, phase in enumerate(phases):
                if not isinstance(phase, dict):
                    errors.append(f"Phase {i} must be an object")
                    continue

                # Check required phase fields
                if 'id' not in phase:
                    errors.append(f"Phase {i} missing 'id'")
                if 'name' not in phase:
                    warnings.append(f"Phase {i} missing 'name'")
                if 'tasks' not in phase:
                    warnings.append(f"Phase {i} missing 'tasks'")

                # Validate tasks
                if 'tasks' in phase:
                    tasks = phase['tasks']
                    if not isinstance(tasks, list):
                        errors.append(f"Phase {i} 'tasks' must be a list")
                    else:
                        for j, task in enumerate(tasks):
                            if not isinstance(task, dict):
                                errors.append(f"Phase {i} Task {j} must be an object")
                                continue
                            if 'id' not in task:
                                errors.append(f"Phase {i} Task {j} missing 'id'")
                            if 'description' not in task:
                                warnings.append(f"Phase {i} Task {j} missing 'description'")

    # Check for PR metadata
    if 'pr' in data:
        pr = data['pr']
        if isinstance(pr, dict):
            if 'title' not in pr:
                warnings.append("PR metadata missing 'title'")

    if errors:
        print("ERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    if warnings:
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)

    # Output task count
    task_count = sum(len(p.get('tasks', [])) for p in data.get('phases', []))
    phase_count = len(data.get('phases', []))
    print(f"Found {phase_count} phases with {task_count} total tasks")

    # Optionally output parsed data
    if "$OUTPUT_TASKS" == "true":
        print("---TASKS_JSON---")
        print(json.dumps(data, indent=2))

except yaml.YAMLError as e:
    print(f"ERROR: Invalid YAML syntax: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

sys.exit(0)
PYTHON_EOF
) || exit_code=$?

    echo "$validation_result"

    if [[ ${exit_code:-0} -ne 0 ]]; then
        echo "[plan-yaml-check] YAML validation failed" >&2
        exit 1
    fi
else
    # Fallback: basic validation without Python
    echo "[plan-yaml-check] WARNING: Python not available, using basic validation" >&2

    # Check for basic YAML structure using grep
    if ! echo "$yaml_content" | grep -qE '^phases:'; then
        echo "[plan-yaml-check] ERROR: Missing 'phases:' key" >&2
        exit 1
    fi

    # Count indented items
    task_count=$(echo "$yaml_content" | grep -cE '^\s+-\s+id:' || echo "0")
    echo "[plan-yaml-check] Found approximately ${task_count} tasks (basic validation)"
fi

echo "[plan-yaml-check] YAML validation passed"
exit 0
