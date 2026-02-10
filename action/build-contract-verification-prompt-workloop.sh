#!/usr/bin/env bash
# build-contract-verification-prompt-workloop.sh — Build contract verification prompt for work loop
#
# This script creates a prompt for contract verification review that works within the SDLC
# work loop. Unlike the PR-triggered version, this operates during the implement phase
# without requiring a PR number.
#
# Environment variables:
#   GITHUB_REPOSITORY   — owner/repo
#   EGG_ISSUE_NUMBER    — GitHub issue number
#   EGG_PIPELINE_PHASE  — SDLC phase (should be implement for this reviewer)
#   RUNNER_TEMP         — Temp directory for prompt file
#   REVIEW_CYCLE        — Current review cycle number
#   PRIOR_FEEDBACK      — Prior review feedback (optional, for re-reviews)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT
#   Writes verdict to .egg-state/reviews/{ISSUE}-{PHASE}-contract.json

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
    local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
    local phase="${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
    local review_cycle="${REVIEW_CYCLE:-1}"

    local contract_rules
    contract_rules=$(fetch_contract_rules)

    local review_file=".egg-state/reviews/${issue_number}-${phase}-contract.json"

    local prompt
    local is_rereview=false

    if [[ "$review_cycle" -gt 1 ]]; then
        is_rereview=true
    fi

    # Contract verification is primarily for implement phase
    if [[ "$phase" != "implement" ]]; then
        # For non-implement phases, provide a minimal pass-through
        prompt="Contract verification is not applicable for the ${phase} phase.

Write an approval verdict to \`${review_file}\`:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"contract\",
  \"verdict\": \"approved\",
  \"summary\": \"Contract verification not applicable for ${phase} phase.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Contract review: approved (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`
"
    elif [[ "$is_rereview" == "true" ]]; then
        prompt="Re-verify contract compliance for issue #${issue_number} implementation.

This is **review cycle ${review_cycle}** — the implementation has been revised based on prior feedback.

## Scope

This is a **contract verification review**. Verify that the implementation matches the contract and all acceptance criteria are met. Do NOT review general code quality or security — other reviewers handle those.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

## Your Task

Perform **incremental contract verification**. Focus on changes since the last review.

1. **Review the delta**: Use \`git log --oneline -10\` and \`git diff HEAD~5..HEAD\` to see recent changes
2. **Check contract state**: Run \`egg-contract show\` to see the current contract with all tasks
3. **Verify new changes comply**: Ensure changes don't break any previously verified acceptance criteria
4. **Verify newly completed tasks**: If any tasks were completed, verify their implementation
5. **Identify gaps**: Note any contract violations or incomplete implementations

### CLI Commands (REVIEWER role)

You are running with REVIEWER role, which allows you to mark task and criterion status:

\`\`\`bash
# View contract state
egg-contract show

# Mark acceptance criterion as verified
egg-contract verify-criterion --criterion ac-1

# Mark task status (complete, incomplete, blocked)
egg-contract mark-task --task task-1-1 --status complete

# Mark phase status
egg-contract mark-phase --phase phase-1 --passed true
\`\`\`

## Contract Rules

${contract_rules}

"
    else
        prompt="Verify contract compliance for issue #${issue_number} implementation.

## Scope

This is a **contract verification review**. Verify that the implementation matches the contract and all acceptance criteria are met. Do NOT review general code quality or security — other reviewers handle those.

## Your Task

Perform **comprehensive contract verification**.

### How to Proceed

1. **Get the contract**: Run \`egg-contract show\` to see all tasks and acceptance criteria
2. **Get the implementation diff**: Run \`git diff origin/main..HEAD\` to see all code changes
3. **Verify each task**:
   - For each task, check that the implementation matches the task description
   - Verify the acceptance criteria for each task is satisfied
   - Check that linked commits relate to their tasks
4. **Mark verified criteria**: For each acceptance criterion that is fully verified, run:
   \`egg-contract verify-criterion --criterion ac-N\`
5. **Identify gaps**: Note any tasks that are incomplete or don't meet their criteria
6. **Check for violations**: Ensure no code changes violate contract requirements

### CLI Commands (REVIEWER role)

You are running with REVIEWER role, which allows you to mark task and criterion status:

\`\`\`bash
# View contract state
egg-contract show

# Mark acceptance criterion as verified
egg-contract verify-criterion --criterion ac-1

# Mark task status (complete, incomplete, blocked)
egg-contract mark-task --task task-1-1 --status complete

# Mark phase status
egg-contract mark-phase --phase phase-1 --passed true
\`\`\`

### Be Thorough

Verify every task and every acceptance criterion. This is the last check before human review. If something doesn't match the contract, flag it clearly.

## Contract Rules

${contract_rules}

"
    fi

    prompt+="## Verdict Format

Write your verdict to \`${review_file}\`:

\`\`\`json
{
  \"reviewer\": \"contract\",
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the implementation PASSES contract verification:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"contract\",
  \"verdict\": \"approved\",
  \"summary\": \"Implementation meets all contract requirements.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Contract review: approved (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

### If the implementation NEEDS REVISION (contract violations found):

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"contract\",
  \"verdict\": \"needs_revision\",
  \"summary\": \"Contract violations or incomplete tasks found.\",
  \"feedback\": \"### Contract Issues\\n\\n1. **[Task ID]**: [Specific issue]\\n\\n### Unverified Criteria\\n\\n- ac-N: [What's missing]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Contract review: needs revision (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

## Important Notes

- Be thorough — verify every task and criterion
- Provide specific, actionable feedback when requesting revision
- Escape newlines in the feedback field as \\\\n for valid JSON
- **Do NOT post to the issue** — your review is internal
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/contract-verification-review-prompt-${issue_number}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for contract verification (needs thorough reasoning)
    local model="opus"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    local review_type="initial"
    if [[ "$is_rereview" == "true" ]]; then
        review_type="re-review (cycle ${review_cycle})"
    fi
    echo "Contract verification review prompt built for ${phase} phase: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
