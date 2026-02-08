#!/usr/bin/env bash
# build-refine-review-prompt.sh — Build a reviewer prompt for refine phase analysis
#
# This script creates a prompt for the reviewer agent to evaluate the quality
# of the analysis produced by the refine phase. The reviewer checks whether
# the analysis adequately addresses the issue and meets quality standards.
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
        prompt="Re-review the analysis for issue #${issue_number} in ${GITHUB_REPOSITORY}.

This is **review cycle ${review_cycle}** — the refiner has revised the analysis based on prior feedback.

## Your Task

Review the **revised analysis** and determine if the issues from your previous review have been adequately addressed.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

### Steps

1. **Read the draft analysis**: The analysis is in \`.egg-state/drafts/${issue_number}-analysis.md\`. Use the Read tool or \`cat\` to read it.

2. **Compare against prior feedback**: Check that each concern raised has been properly addressed, not just superficially acknowledged.

3. **Apply review criteria** (below) to the revised analysis.

4. **Write your verdict** to the review file (format specified below).

"
    else
        prompt="Review the analysis for issue #${issue_number} in ${GITHUB_REPOSITORY}.

## Your Task

Evaluate the quality of the refine phase analysis. The analysis should provide a solid foundation for implementation planning.

### Steps

1. **Read the draft analysis**: The analysis is in \`.egg-state/drafts/${issue_number}-analysis.md\`. Use the Read tool or \`cat\` to read it.

2. **Review the original issue** for context:

**Issue Title:** ${issue_title}

**Issue Description:**
${issue_body}

3. **Apply the review criteria** below systematically.

4. **Write your verdict** to the review file (format specified below).

"
    fi

    prompt+="## Review Criteria

Evaluate the analysis against these criteria:

### 1. Problem Understanding
- Does the analysis correctly identify the core problem or feature request?
- Is the current behavior (if applicable) accurately described?
- Are the goals and desired outcomes clear?

### 2. Research Quality
- Has the agent explored the relevant parts of the codebase?
- Are existing patterns and conventions identified?
- Is the technical context accurate?

### 3. Options Analysis
- Are the options meaningfully different? (not just variations of the same approach)
- Are trade-offs clearly articulated for each option?
- Is the reasoning for each option logical and well-founded?

### 4. Constraints and Dependencies
- Are technical constraints identified (performance, compatibility, etc.)?
- Are dependencies on other code or systems noted?
- Are potential risks or complications surfaced?

### 5. Open Questions
- Are open questions specific enough for a human to answer?
- Do questions address genuine ambiguities (not things the agent could figure out)?
- Are questions actionable (answering them would unblock the work)?

### 6. Recommendation Quality
- Is there a clear recommended approach?
- Is the recommendation justified with specific reasons?
- Does the recommendation align with the analysis findings?

## Verdict Format

After your review, write your verdict to a JSON file. **Do NOT post to the issue** — the verdict is internal.

### JSON Schema

Write your verdict to \`.egg-state/reviews/${issue_number}-refine-review.json\`:

\`\`\`json
{
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the analysis PASSES review:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .egg-state/reviews/${issue_number}-refine-review.json << REVIEW_EOF
{
  \"verdict\": \"approved\",
  \"summary\": \"The analysis meets quality standards and is ready for the plan phase.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add .egg-state/reviews/${issue_number}-refine-review.json
git commit -m \"Refine review: approved for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

### If the analysis NEEDS REVISION:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > .egg-state/reviews/${issue_number}-refine-review.json << REVIEW_EOF
{
  \"verdict\": \"needs_revision\",
  \"summary\": \"The analysis requires revision before proceeding.\",
  \"feedback\": \"### Issues Found\\n\\n1. **[Category]**: [Specific issue]\\n2. **[Category]**: [Specific issue]\\n\\n### Suggestions\\n\\n- [Actionable suggestion]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add .egg-state/reviews/${issue_number}-refine-review.json
git commit -m \"Refine review: needs revision for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

## Important Notes

- Be thorough but fair. The goal is to catch quality issues before human review.
- Provide specific, actionable feedback when requesting revision.
- Do not request changes for minor style issues — focus on substantive problems.
- **Do NOT post to the issue** — your review is internal and will only be shared with the refiner agent if revision is needed.
- Escape newlines in the feedback field as \\\\n for valid JSON.
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/refine-review-prompt-${issue_number}.txt"
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
    echo "Refine review prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
