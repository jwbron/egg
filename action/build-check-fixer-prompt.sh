#!/usr/bin/env bash
# build-check-fixer-prompt.sh — Build a per-check focused prompt for autofixing
#
# Replaces build-autofixer-prompt.sh with a per-check fixer approach:
# - Reads check-fixers.yml config for per-job settings
# - Reads autofix state from PR comment to track retry counts
# - Identifies which jobs failed in the triggering workflow run
# - Outputs a focused prompt listing ONLY failed jobs
# - Outputs non-LLM fix commands for applicable checks
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   FAILED_WORKFLOW    — Name of the workflow that failed
#   FAILED_RUN_ID      — Run ID of the failed workflow
#   FAILED_JOBS        — JSON array of failed job names (from caller)
#   AUTOFIX_STATE      — JSON object of retry counts (from caller)
#   CONFIG_FILE        — Path to check-fixers.yml (from caller, optional)
#   RUNNER_TEMP        — Temp directory for prompt file
#
# Output (via $GITHUB_OUTPUT):
#   prompt-file          — Path to the focused prompt for the LLM fixer
#   model                — Model to use
#   non-llm-fixes        — JSON array of {job, command} objects
#   has-non-llm-fixes    — true/false
#   needs-llm            — true/false
#   max-retries-reached  — true/false (escalation needed)
#   escalation-details   — JSON array of {job, attempts, max} for exceeded checks
#   non-llm-jobs         — JSON array of job names attempted by non-LLM fixes
#   llm-jobs             — JSON array of job names attempted by LLM fixer

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse check-fixers.yml config (simple YAML parser using grep/sed)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."

load_config() {
    local config_file=""

    # Use CONFIG_FILE from environment if set, otherwise discover
    if [[ -n "${CONFIG_FILE:-}" && -f "${CONFIG_FILE}" ]]; then
        config_file="${CONFIG_FILE}"
    elif [[ -f ".egg/check-fixers.yml" ]]; then
        config_file=".egg/check-fixers.yml"
    elif [[ -f "${REPO_ROOT}/shared/check-fixers.yml" ]]; then
        config_file="${REPO_ROOT}/shared/check-fixers.yml"
    fi

    if [[ -z "$config_file" ]]; then
        echo "::warning::No check-fixers.yml found, using defaults"
        echo "{}"
        return
    fi

    cat "$config_file"
}

# Get a job-specific config value from the YAML. Falls back to defaults.
# Uses Python for reliable YAML parsing.
get_job_config() {
    local workflow="$1"
    local job="$2"
    local field="$3"
    local config_file="$4"

    CFG_PATH="$config_file" CFG_WORKFLOW="$workflow" CFG_JOB="$job" CFG_FIELD="$field" \
    python3 -c "
import yaml, sys, os
with open(os.environ['CFG_PATH']) as f:
    cfg = yaml.safe_load(f) or {}
defaults = cfg.get('defaults', {})
workflows = cfg.get('workflows', {})
wf = workflows.get(os.environ['CFG_WORKFLOW'], {})
job_cfg = wf.get(os.environ['CFG_JOB'], {})
val = job_cfg.get(os.environ['CFG_FIELD'], defaults.get(os.environ['CFG_FIELD'], ''))
print(val if val else '')
" 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local config_file=""
    if [[ -n "${CONFIG_FILE:-}" && -f "${CONFIG_FILE}" ]]; then
        config_file="${CONFIG_FILE}"
    elif [[ -f ".egg/check-fixers.yml" ]]; then
        config_file=".egg/check-fixers.yml"
    elif [[ -f "${REPO_ROOT}/shared/check-fixers.yml" ]]; then
        config_file="${REPO_ROOT}/shared/check-fixers.yml"
    fi

    # Parse failed jobs (JSON array from caller)
    local failed_jobs_json="${FAILED_JOBS:-[]}"
    local autofix_state_json="${AUTOFIX_STATE:-{}}"

    # If no failed jobs provided, we can't build a focused prompt
    if [[ "$failed_jobs_json" == "[]" ]]; then
        echo "::warning::No failed jobs provided, building generic prompt"
        failed_jobs_json='["unknown"]'
    fi

    # Determine non-LLM fixes, model, and retry state per job
    local non_llm_fixes="[]"
    local needs_llm="false"
    local has_non_llm="false"
    local max_retries_reached="false"
    local escalation_details="[]"
    local model=""
    local failed_job_list=""

    if [[ -n "$config_file" ]]; then
        # Use Python to process all job configs at once.
        # Data is passed via environment variables (not shell interpolation)
        # to prevent code injection from crafted JSON payloads.
        local result
        result=$(CONFIG_PATH="$config_file" \
            WORKFLOW_NAME="${FAILED_WORKFLOW}" \
            JOBS_JSON="${failed_jobs_json}" \
            STATE_JSON="${autofix_state_json}" \
            python3 -c "
import yaml, json, sys, os

config_file = os.environ['CONFIG_PATH']
workflow = os.environ['WORKFLOW_NAME']
failed_jobs = json.loads(os.environ['JOBS_JSON'])
state = json.loads(os.environ['STATE_JSON'])

with open(config_file) as f:
    cfg = yaml.safe_load(f) or {}

defaults = cfg.get('defaults', {})
workflows = cfg.get('workflows', {})
wf = workflows.get(workflow, {})

non_llm_fixes = []
escalation = []
model = defaults.get('model', 'sonnet')
needs_llm = False
has_non_llm = False
max_retries_reached = False
jobs_for_llm = []

for job in failed_jobs:
    job_cfg = wf.get(job, {})
    job_max = job_cfg.get('max_retries', defaults.get('max_retries', 3))
    job_model = job_cfg.get('model', defaults.get('model', 'sonnet'))
    state_key = f'{workflow}/{job}'
    attempts = state.get(state_key, 0)

    # Check max retries
    if attempts >= job_max:
        max_retries_reached = True
        escalation.append({'job': job, 'attempts': attempts, 'max': job_max})
        continue

    # Check for non-LLM fix (only on first attempt)
    non_llm_cmd = job_cfg.get('non_llm_fix', '')
    if non_llm_cmd and attempts == 0:
        has_non_llm = True
        non_llm_fixes.append({'job': job, 'command': non_llm_cmd.strip()})
    else:
        needs_llm = True
        jobs_for_llm.append(job)
        # Use the highest-tier model among failed jobs
        if job_model == 'opus':
            model = 'opus'

result = {
    'non_llm_fixes': non_llm_fixes,
    'needs_llm': needs_llm,
    'has_non_llm': has_non_llm,
    'max_retries_reached': max_retries_reached,
    'escalation': escalation,
    'model': model,
    'jobs_for_llm': jobs_for_llm,
    'non_llm_jobs': [f['job'] for f in non_llm_fixes],
}
print(json.dumps(result))
" 2>/dev/null || echo '{"non_llm_fixes":[],"needs_llm":true,"has_non_llm":false,"max_retries_reached":false,"escalation":[],"model":"sonnet","jobs_for_llm":[]}')

        non_llm_fixes=$(echo "$result" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['non_llm_fixes']))")
        needs_llm=$(echo "$result" | python3 -c "import json,sys; print(str(json.load(sys.stdin)['needs_llm']).lower())")
        has_non_llm=$(echo "$result" | python3 -c "import json,sys; print(str(json.load(sys.stdin)['has_non_llm']).lower())")
        max_retries_reached=$(echo "$result" | python3 -c "import json,sys; print(str(json.load(sys.stdin)['max_retries_reached']).lower())")
        escalation_details=$(echo "$result" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['escalation']))")
        model=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin)['model'])")
        failed_job_list=$(echo "$result" | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['jobs_for_llm']))")
        local non_llm_jobs llm_jobs
        non_llm_jobs=$(echo "$result" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['non_llm_jobs']))")
        llm_jobs=$(echo "$result" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['jobs_for_llm']))")
    else
        # No config file — all jobs need LLM, default model
        needs_llm="true"
        model="sonnet"
        failed_job_list=$(echo "$failed_jobs_json" | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)))")
        local non_llm_jobs="[]"
        local llm_jobs="$failed_jobs_json"
    fi

    # Build the failed checks section for the prompt
    local failed_checks_section=""
    if [[ -n "$failed_job_list" ]]; then
        while IFS= read -r job; do
            [[ -z "$job" ]] && continue
            failed_checks_section="${failed_checks_section}
- **${job}**"
        done <<< "$failed_job_list"
    fi

    # Load conventions (per-check fixer specific)
    local conventions_file="${SCRIPT_DIR}/autofixer-conventions.md"
    local conventions=""
    if [[ -f "$conventions_file" ]]; then
        conventions=$(cat "$conventions_file")
    fi

    # Build the focused prompt.
    # NOTE: We intentionally do NOT include shared/prompts/autofixer-rules.md here
    # because it instructs the agent to run checks locally, which conflicts with the
    # per-check CI-driven model. The conventions file contains the relevant rules.
    local run_log_cmd=""
    if [[ -n "${FAILED_RUN_ID:-}" ]]; then
        run_log_cmd="gh run view ${FAILED_RUN_ID} --log-failed"
    else
        run_log_cmd="gh pr checks ${PR_NUMBER}"
    fi

    local prompt
    prompt="Fix failing checks in the **${FAILED_WORKFLOW}** workflow on PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Failed Checks
${failed_checks_section}

## Instructions

1. **Investigate the failure**: Run \`${run_log_cmd}\` to see the failure output.
2. **Fix the issues** causing the failures listed above.
3. **Commit and push** your fixes.

**CRITICAL: Do NOT run checks locally.** CI will re-run automatically after you push.
Fix only the issues listed above. Do not fix unrelated code.

If you cannot fix an issue without human guidance, post a PR comment explaining
what's needed and why.

## Auto-fixable vs Report-only

**Auto-fixable (commit fixes directly):**
- Lint errors (formatting, import order, code style)
- Type errors with clear fixes
- Simple test failures with obvious fixes
- Missing or outdated dependencies in lock files

**Report only (explain what's needed):**
- Complex logic errors requiring design decisions
- Security issues requiring architectural changes
- Failures that require understanding business requirements to resolve correctly

## Conventions

${conventions:-Use git commit and git push to push fixes. Sign comments with: -- Authored by egg}
"

    # Write prompt to temp file
    local prompt_dir="${RUNNER_TEMP:-/tmp}"
    mkdir -p "$prompt_dir"
    local prompt_file="${prompt_dir}/check-fixer-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
        echo "non-llm-fixes=${non_llm_fixes}"
        echo "has-non-llm-fixes=${has_non_llm}"
        echo "needs-llm=${needs_llm}"
        echo "max-retries-reached=${max_retries_reached}"
        echo "escalation-details=${escalation_details}"
        echo "non-llm-jobs=${non_llm_jobs}"
        echo "llm-jobs=${llm_jobs}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Check fixer prompt built: ${#prompt} chars, model=${model}, has_non_llm=${has_non_llm}, needs_llm=${needs_llm}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
