#!/usr/bin/env bash
# build-contract-verification-prompt.sh — Build prompt for contract verification review
#
# This script creates a prompt for a contract verification reviewer agent. The agent
# verifies that the implementation matches the contract (tasks completed, acceptance
# criteria met), marks verified items via egg-contract CLI, and identifies gaps.
#
# Environment variables:
#   PR_NUMBER          — Pull request number
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   LAST_REVIEW_COMMIT — (Optional) Commit SHA of last review, for incremental verification
#   COMMIT_SHA         — Current PR head commit SHA (for review marker)
#   EGG_ISSUE_NUMBER   — Issue number for the contract
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch contract rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_contract_rules() {
    local rules_file=".egg/contract-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        # Default contract verification rules when no repo-specific rules exist
        cat <<'EOF'
## Default Contract Verification Rules

### Task Verification

For each task in the contract, verify:

1. **Implementation exists**: The described functionality is present in the code
2. **Acceptance criteria met**: The specific acceptance criteria for the task is satisfied
3. **Commit linked**: If a commit is linked, verify it relates to the task
4. **Tests present**: Where applicable, tests cover the new functionality

### Phase Consistency

Check that:
- All tasks in completed phases are actually implemented
- Phase status matches task completion state
- No orphaned code exists that isn't covered by any task

### Acceptance Criteria Verification

For each acceptance criterion in the contract:
1. Read the criterion description
2. Examine the implementation to verify it meets the criterion
3. If verified, mark it using: `egg-contract verify-criterion --criterion <id>`
4. If not verified, note the gap in your review

### Contract Integrity

Verify:
- No implementation changes violate previously verified criteria
- New changes don't break existing contract compliance
- All required files listed in tasks are present
EOF
    fi
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local contract_rules
    contract_rules=$(fetch_contract_rules)

    # Load review conventions if available
    local conventions_file
    conventions_file="$(dirname "$0")/review-conventions.md"
    local conventions=""
    if [[ -f "$conventions_file" ]]; then
        conventions=$(cat "$conventions_file")
    fi

    local prompt
    local is_rereview=false

    # Check if this is a re-review (we have a previous review commit)
    if [[ -n "${LAST_REVIEW_COMMIT:-}" ]]; then
        is_rereview=true
        prompt="Re-verify contract compliance for PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

This is a **re-review** — you previously verified this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Your Task

Perform **incremental contract verification**. Focus on changes since your last review and verify they don't violate the contract.

1. **Review the delta**: Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see what changed since your last review.
2. **Check contract state**: Run \`egg-contract show\` to see the current contract with all tasks and acceptance criteria.
3. **Verify new changes comply**: Ensure new code doesn't break any previously verified acceptance criteria.
4. **Verify newly completed tasks**: If any tasks were completed since last review, verify their implementation.
5. **Mark verified criteria**: For any acceptance criteria now fully verified, run:
   \`egg-contract verify-criterion --criterion ac-N\`
6. **Identify gaps**: Note any contract violations or incomplete implementations.

### CLI Commands (REVIEWER role)

You are running with REVIEWER role, which allows you to mark criterion status:

\`\`\`bash
# View contract state
egg-contract show

# Mark acceptance criterion as verified
egg-contract verify-criterion --criterion ac-1
\`\`\`

For full PR context if needed: \`gh pr diff ${PR_NUMBER}\`

## Contract Rules

${contract_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\` with \`--body-file\`. Always write your review to a temp file first, then use --body-file to post it. Do NOT use --body with inline content — long reviews will fail due to shell escaping. Use --approve, --request-changes, or --comment as appropriate. Sign your review with: — Authored by egg}

## Review Marker

Your review MUST include this HTML comment at the end of your review body for tracking:

\`\`\`
<!-- egg-automated-review bot=contract-verification commit=${COMMIT_SHA:-\$(git rev-parse HEAD)} verdict=<approve|request-changes|comment> -->
\`\`\`
"
    else
        prompt="Verify contract compliance for PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Your Task

Perform **comprehensive contract verification**. Verify that the implementation matches the contract and all acceptance criteria are met.

### How to Proceed

1. **Get the contract**: Run \`egg-contract show\` to see all tasks and acceptance criteria.
2. **Get the PR diff**: Run \`gh pr diff ${PR_NUMBER}\` to see all code changes.
3. **Verify each task**:
   - For each task, check that the implementation matches the task description
   - Verify the acceptance criteria for each task is satisfied
   - Check that linked commits relate to their tasks
4. **Mark verified criteria**: For each acceptance criterion that is fully verified, run:
   \`egg-contract verify-criterion --criterion ac-N\`
5. **Identify gaps**: Note any tasks that are incomplete or don't meet their criteria.
6. **Check for violations**: Ensure no code changes violate contract requirements.

### CLI Commands (REVIEWER role)

You are running with REVIEWER role, which allows you to mark criterion status:

\`\`\`bash
# View contract state
egg-contract show

# Mark acceptance criterion as verified
egg-contract verify-criterion --criterion ac-1
\`\`\`

### Be Thorough

Verify every task and every acceptance criterion. This is the last check before human review. If something doesn't match the contract, flag it clearly.

## Contract Rules

${contract_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\` with \`--body-file\`. Always write your review to a temp file first, then use --body-file to post it. Do NOT use --body with inline content — long reviews will fail due to shell escaping. Use --approve, --request-changes, or --comment as appropriate. Sign your review with: — Authored by egg}

## Review Marker

Your review MUST include this HTML comment at the end of your review body for tracking:

\`\`\`
<!-- egg-automated-review bot=contract-verification commit=${COMMIT_SHA:-\$(git rev-parse HEAD)} verdict=<approve|request-changes|comment> -->
\`\`\`
"
    fi

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/contract-verification-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Always use opus for contract verification (needs thorough reasoning)
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    local review_type="initial"
    if [[ "$is_rereview" == "true" ]]; then
        review_type="re-review (since ${LAST_REVIEW_COMMIT:0:7})"
    fi
    echo "Contract verification prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
