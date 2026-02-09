#!/usr/bin/env bash
# build-unified-review-prompt.sh — Build a unified reviewer prompt for any SDLC phase
#
# This script creates a prompt for the reviewer agent that works across all phases
# (refine, plan, implement). It generates phase-appropriate review criteria and
# supports re-review with prior feedback context.
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   EGG_ISSUE_NUMBER   — GitHub issue number
#   EGG_PIPELINE_PHASE — SDLC phase (refine, plan, implement)
#   RUNNER_TEMP        — Temp directory for prompt file
#   REVIEW_CYCLE       — Current review cycle number (for re-reviews)
#   PRIOR_FEEDBACK     — Prior review feedback (optional, for re-reviews)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Safe API wrapper
# ---------------------------------------------------------------------------

# Wrapper around gh api that warns on failure and returns empty JSON object
gh_api_safe() {
  local stderr_file
  stderr_file=$(mktemp)
  # Ensure temp file is cleaned up on exit (including signals)
  trap 'rm -f "$stderr_file"' RETURN
  local output
  if output=$(gh api "$@" 2>"$stderr_file"); then
    echo "$output"
  else
    local rc=$?
    echo "ERROR: 'gh api $1' failed (exit $rc): $(cat "$stderr_file")" >&2
    echo "{}"
  fi
}

# ---------------------------------------------------------------------------
# Phase-specific review criteria
# ---------------------------------------------------------------------------

get_refine_criteria() {
    cat <<'EOF'
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
EOF
}

get_plan_criteria() {
    cat <<'EOF'
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
- Are acceptance criteria specific (not vague like "works correctly")?
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
- Is there a YAML code block with the `# yaml-tasks` marker?
- Does the YAML accurately reflect the tasks in the prose?
- Are all required fields present (id, description, acceptance, files)?
EOF
}

get_implement_criteria() {
    cat <<'EOF'
### 1. Task Completion
- Are all tasks from the plan implemented?
- Does each implementation match its acceptance criteria?
- Are all files listed in the plan modified or created?

### 2. Code Quality
- Does the code follow existing patterns in the codebase?
- Is the code readable and maintainable?
- Are there appropriate comments where logic is non-obvious?

### 3. Security
- Are there any injection vulnerabilities (SQL, command, XSS)?
- Is input validation present at trust boundaries?
- Are credentials or secrets properly handled (not hardcoded)?

### 4. Error Handling
- Are errors handled gracefully?
- Are failure paths tested or at least considered?
- Do operations that can fail have appropriate retry or fallback logic?

### 5. Testing
- Are there tests for new functionality?
- Do existing tests still pass?
- Are edge cases covered?

### 6. Documentation
- Are significant changes documented?
- Are new functions/methods documented with docstrings?
- Is the README updated if user-facing behavior changes?
EOF
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
    local phase="${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
    local review_cycle="${REVIEW_CYCLE:-1}"

    # Fetch issue details for context
    local issue_data
    issue_data=$(gh_api_safe "repos/${GITHUB_REPOSITORY}/issues/${issue_number}")

    local issue_title issue_body
    issue_title=$(echo "$issue_data" | jq -r '.title // "Unknown"')
    issue_body=$(echo "$issue_data" | jq -r '.body // ""')

    # Truncate issue body if too long
    if [[ ${#issue_body} -gt 5000 ]]; then
        issue_body="${issue_body:0:5000}... (truncated)"
    fi

    # Get phase-specific criteria
    local review_criteria
    local artifact_type
    local artifact_file
    local review_file

    case "$phase" in
        refine)
            review_criteria=$(get_refine_criteria)
            artifact_type="analysis"
            artifact_file=".egg-state/drafts/${issue_number}-analysis.md"
            review_file=".egg-state/reviews/${issue_number}-refine-review.json"
            ;;
        plan)
            review_criteria=$(get_plan_criteria)
            artifact_type="implementation plan"
            artifact_file=".egg-state/drafts/${issue_number}-plan.md"
            review_file=".egg-state/reviews/${issue_number}-plan-review.json"
            ;;
        implement)
            review_criteria=$(get_implement_criteria)
            artifact_type="implementation"
            artifact_file=""  # No single file - review the git diff
            review_file=".egg-state/reviews/${issue_number}-implement-review.json"
            ;;
        *)
            echo "ERROR: Unknown phase: $phase" >&2
            exit 1
            ;;
    esac

    local prompt
    local is_rereview=false

    if [[ "$review_cycle" -gt 1 ]]; then
        is_rereview=true
    fi

    if [[ "$is_rereview" == "true" ]]; then
        prompt="Re-review the ${artifact_type} for issue #${issue_number} in ${GITHUB_REPOSITORY}.

This is **review cycle ${review_cycle}** — the agent has revised the ${artifact_type} based on prior feedback.

## Your Task

Review the **revised ${artifact_type}** and determine if the issues from your previous review have been adequately addressed.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

### Steps

1. **Read the ${artifact_type}**: "
        if [[ -n "$artifact_file" ]]; then
            prompt+="The ${artifact_type} is in \`${artifact_file}\`. Use the Read tool or \`cat\` to read it."
        else
            prompt+="Review the recent commits using \`git log --oneline -10\` and \`git diff origin/main..HEAD\`."
        fi

        prompt+="

2. **Compare against prior feedback**: Check that each concern raised has been properly addressed, not just superficially acknowledged.

3. **Apply review criteria** (below) to the revised ${artifact_type}.

4. **Write your verdict** to the review file (format specified below).

"
    else
        prompt="Review the ${artifact_type} for issue #${issue_number} in ${GITHUB_REPOSITORY}.

## Your Task

Evaluate the quality of the ${phase} phase output. "

        if [[ "$phase" == "refine" ]]; then
            prompt+="The analysis should provide a solid foundation for implementation planning."
        elif [[ "$phase" == "plan" ]]; then
            prompt+="The plan should provide a clear roadmap for implementation."
        else
            prompt+="The implementation should be complete, correct, and ready for human review."
        fi

        prompt+="

### Steps

1. **Read the ${artifact_type}**: "
        if [[ -n "$artifact_file" ]]; then
            prompt+="The ${artifact_type} is in \`${artifact_file}\`. Use the Read tool or \`cat\` to read it."
        else
            prompt+="Review the implementation using \`git log --oneline -10\` and \`git diff origin/main..HEAD\`."
        fi

        # Track step number for proper sequencing
        local step_num=2

        if [[ "$phase" == "plan" ]]; then
            prompt+="

${step_num}. **Read the prior analysis**: The analysis is in \`.egg-state/drafts/${issue_number}-analysis.md\` to verify alignment."
            step_num=$((step_num + 1))
        fi

        prompt+="

${step_num}. **Review the original issue** for context:

**Issue Title:** ${issue_title}

**Issue Description:**
${issue_body}

$((step_num + 1)). **Apply the review criteria** below systematically.

$((step_num + 2)). **Write your verdict** to the review file (format specified below).

"
    fi

    prompt+="## Review Criteria

Evaluate the ${artifact_type} against these criteria:

${review_criteria}

## Verdict Format

After your review, write your verdict to a JSON file. **Do NOT post to the issue** — the verdict is internal.

### JSON Schema

Write your verdict to \`${review_file}\`:

\`\`\`json
{
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the ${artifact_type} PASSES review:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"verdict\": \"approved\",
  \"summary\": \"The ${artifact_type} meets quality standards and is ready for the next phase.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"${phase^} review: approved for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

### If the ${artifact_type} NEEDS REVISION:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"verdict\": \"needs_revision\",
  \"summary\": \"The ${artifact_type} requires revision before proceeding.\",
  \"feedback\": \"### Issues Found\\n\\n1. **[Category]**: [Specific issue]\\n2. **[Category]**: [Specific issue]\\n\\n### Suggestions\\n\\n- [Actionable suggestion]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"${phase^} review: needs revision for issue #${issue_number}\"
git push origin \\\${EGG_BRANCH_NAME}
\`\`\`

## Important Notes

- Be thorough but fair. The goal is to catch quality issues before human review.
- Provide specific, actionable feedback when requesting revision.
- Do not request changes for minor style issues — focus on substantive problems."

    if [[ "$phase" == "plan" ]]; then
        prompt+="
- Pay special attention to the YAML appendix — incorrect YAML will cause extraction failures."
    fi

    prompt+="
- **Do NOT post to the issue** — your review is internal and will only be shared with the agent if revision is needed.
- Escape newlines in the feedback field as \\\\n for valid JSON.
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/unified-review-prompt-${issue_number}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for reviews (consistent with existing behavior)
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
    echo "Unified review prompt built for ${phase} phase: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
