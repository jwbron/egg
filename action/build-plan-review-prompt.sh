#!/usr/bin/env bash
# build-plan-review-prompt.sh — Build a reviewer prompt for plan phase review
#
# This script creates a prompt for the reviewer agent to evaluate the quality
# of the implementation plan produced by the plan phase. The reviewer checks
# whether the plan aligns with the approved analysis and meets quality standards.
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   EGG_ISSUE_NUMBER   — GitHub issue number
#   RUNNER_TEMP        — Temp directory for prompt file
#   REVIEW_CYCLE       — Current review cycle number (for re-reviews)
#   PRIOR_FEEDBACK     — Prior review feedback (optional, for re-reviews)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
    local review_cycle="${REVIEW_CYCLE:-1}"

    # Fetch issue details for context
    local issue_data
    issue_data=$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue_number}" 2>/dev/null || echo "{}")

    local issue_title issue_body
    issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
    issue_body=$(echo "$issue_data" | jq -r '.body // ""')

    # Truncate issue body if too long
    if [[ ${#issue_body} -gt 5000 ]]; then
        issue_body="${issue_body:0:5000}... (truncated)"
    fi

    local prompt
    local is_rereview=false

    if [[ "$review_cycle" -gt 1 ]]; then
        is_rereview=true
    fi

    if [[ "$is_rereview" == "true" ]]; then
        prompt="Re-review the implementation plan for issue #${issue_number} in ${GITHUB_REPOSITORY}.

This is **review cycle ${review_cycle}** — the planner has revised the plan based on prior feedback.

## Your Task

Review the **revised plan** and determine if the issues from your previous review have been adequately addressed.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

### Steps

1. **Read the draft plan**: The plan is in \`.egg-state/drafts/${issue_number}-plan.md\`. Use the Read tool or \`cat\` to read it.

2. **Compare against prior feedback**: Check that each concern raised has been properly addressed, not just superficially acknowledged.

3. **Apply review criteria** (below) to the revised plan.

4. **Write your verdict** to the review file (format specified below).

"
    else
        prompt="Review the implementation plan for issue #${issue_number} in ${GITHUB_REPOSITORY}.

## Your Task

Evaluate the quality of the plan phase output. The plan should provide a clear roadmap for implementation.

### Steps

1. **Read the draft plan**: The plan is in \`.egg-state/drafts/${issue_number}-plan.md\`. Use the Read tool or \`cat\` to read it.

2. **Read the prior analysis**: The analysis is in \`.egg-state/drafts/${issue_number}-analysis.md\` to verify alignment.

3. **Review the original issue** for context:

**Issue Title:** ${issue_title}

**Issue Description:**
${issue_body}

4. **Apply the review criteria** below systematically.

5. **Write your verdict** to the review file (format specified below).

"
    fi

    prompt+="## Review Criteria

Evaluate the plan against these criteria:

### 1. Alignment with Analysis
- Does the plan implement the recommended approach from the analysis?
- Are all requirements from the issue addressed?
- If the plan deviates from the analysis, is the reason explained?

### 2. Task Breakdown
- Are tasks specific and actionable?
- Are tasks appropriately sized (not too large, not too granular)?
- Are task IDs in the correct format ([TASK-P-N])?
- Is the order of tasks logical?

### 3. Acceptance Criteria
- Does each task have clear, verifiable acceptance criteria?
- Are acceptance criteria specific (not vague like \"works correctly\")?
- Can the criteria be objectively verified?

### 4. Dependencies
- Are dependencies between tasks identified?
- Is the phase structure logical (dependencies within phases, ordering between phases)?
- Are external dependencies (libraries, APIs, etc.) noted?

### 5. Test Strategy
- Is there a test strategy section?
- Does it cover unit tests, integration tests as appropriate?
- Are edge cases and error scenarios considered?

### 6. Risk Assessment
- Are potential risks identified?
- Is there a rollback plan or mitigation strategy?
- Are technical challenges acknowledged?

### 7. YAML Appendix (Machine-Readable)
- Is there a YAML code block with the \`# yaml-tasks\` marker?
- Does the YAML accurately reflect the tasks in the prose?
- Are all required fields present (id, description, acceptance, files)?

## Verdict Format

After your review, write your verdict to a JSON file. **Do NOT post to the issue** — the verdict is internal.

### JSON Schema

Write your verdict to \`.egg-state/reviews/${issue_number}-plan-review.json\`:

\`\`\`json
{
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the plan PASSES review:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .egg-state/reviews/${issue_number}-plan-review.json << REVIEW_EOF
{
  \"verdict\": \"approved\",
  \"summary\": \"The implementation plan meets quality standards and is ready for the implement phase.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add .egg-state/reviews/${issue_number}-plan-review.json
git commit -m \"Plan review: approved for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

### If the plan NEEDS REVISION:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .egg-state/reviews/${issue_number}-plan-review.json << REVIEW_EOF
{
  \"verdict\": \"needs_revision\",
  \"summary\": \"The plan requires revision before proceeding.\",
  \"feedback\": \"### Issues Found\\n\\n1. **[Category]**: [Specific issue]\\n2. **[Category]**: [Specific issue]\\n\\n### Suggestions\\n\\n- [Actionable suggestion]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add .egg-state/reviews/${issue_number}-plan-review.json
git commit -m \"Plan review: needs revision for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

## Important Notes

- Be thorough but fair. The goal is to catch quality issues before human review.
- Provide specific, actionable feedback when requesting revision.
- Do not request changes for minor style issues — focus on substantive problems.
- Pay special attention to the YAML appendix — incorrect YAML will cause extraction failures.
- **Do NOT post to the issue** — your review is internal and will only be shared with the planner agent if revision is needed.
- Escape newlines in the feedback field as \\\\n for valid JSON.
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/plan-review-prompt-${issue_number}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for reviews (consistent with PR reviews)
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
    echo "Plan review prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
