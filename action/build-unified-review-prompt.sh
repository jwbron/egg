#!/usr/bin/env bash
# build-unified-review-prompt.sh — Build review prompt for any SDLC phase
#
# Unified review prompt builder that replaces phase-specific review prompt
# scripts (build-refine-review-prompt.sh, build-plan-review-prompt.sh).
#
# Environment variables:
#   EGG_ISSUE_NUMBER    — GitHub issue number
#   EGG_PHASE           — Current phase (refine, plan, implement)
#   GITHUB_REPOSITORY   — owner/repo
#   REVIEW_CYCLE        — Current review cycle number
#   PRIOR_FEEDBACK      — Feedback from prior cycle (optional)
#   RUNNER_TEMP         — Temp directory for prompt file
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

find_repo_path() {
    if [[ -n "${EGG_REPO_PATH:-}" ]]; then
        echo "$EGG_REPO_PATH"
    elif [[ -d "/home/egg/repos" ]]; then
        find /home/egg/repos -maxdepth 1 -type d ! -name repos | head -1
    else
        pwd
    fi
}

get_draft_file() {
    local issue_number="$1"
    local phase="$2"
    local repo_path
    repo_path=$(find_repo_path)

    case "$phase" in
        refine)
            echo "${repo_path}/.egg-state/drafts/${issue_number}-analysis.md"
            ;;
        plan)
            echo "${repo_path}/.egg-state/drafts/${issue_number}-plan.md"
            ;;
        implement)
            echo "${repo_path}/.egg-state/drafts/${issue_number}-implementation.md"
            ;;
        *)
            echo ""
            ;;
    esac
}

get_review_file() {
    local issue_number="$1"
    local phase="$2"
    local repo_path
    repo_path=$(find_repo_path)

    echo "${repo_path}/.egg-state/reviews/${issue_number}-${phase}-review.json"
}

# ---------------------------------------------------------------------------
# Phase-specific criteria
# ---------------------------------------------------------------------------

get_refine_review_criteria() {
    cat <<'EOF'
## Review Criteria for Analysis (Refine Phase)

Review the analysis document at `.egg-state/drafts/{issue_number}-analysis.md` against these criteria:

### Required Sections
1. **Problem Statement**: Clear description of what needs to be solved
2. **Analysis**: Investigation of the current state and constraints
3. **Recommended Approach**: Specific recommendation with justification
4. **Open Questions** (if any): Clearly articulated unknowns

### Quality Checks
- [ ] Problem is correctly understood from the issue description
- [ ] Analysis demonstrates understanding of the codebase
- [ ] Recommendation is specific and actionable (not vague)
- [ ] Tradeoffs and alternatives were considered
- [ ] No major gaps in reasoning or analysis
- [ ] Assumptions are explicitly stated

### Verdict Criteria
- **approved**: Analysis is complete, accurate, and actionable
- **needs_revision**: Analysis has gaps, errors, or needs more depth
EOF
}

get_plan_review_criteria() {
    cat <<'EOF'
## Review Criteria for Plan

Review the plan document at `.egg-state/drafts/{issue_number}-plan.md` against these criteria:

### Required Sections
1. **Summary**: High-level description of the implementation
2. **Implementation Plan**: Detailed phases and tasks
3. **Test Strategy**: How changes will be verified
4. **Risk Assessment** (if applicable): Potential issues and mitigations

### YAML Tasks Block
The plan MUST include a valid YAML block marked with `# yaml-tasks` containing:
```yaml
# yaml-tasks
phases:
  - id: phase-1
    name: "Phase Name"
    tasks:
      - id: task-1
        description: "Task description"
```

### Quality Checks
- [ ] Plan covers all requirements from the analysis
- [ ] Tasks are specific, atomic, and testable
- [ ] Dependencies between tasks are clear
- [ ] Acceptance criteria are defined
- [ ] Test strategy is comprehensive
- [ ] YAML block parses correctly
- [ ] No overly complex or risky approaches without justification

### Verdict Criteria
- **approved**: Plan is complete, well-structured, and implementable
- **needs_revision**: Plan has missing tasks, unclear scope, or invalid YAML
EOF
}

get_implement_review_criteria() {
    cat <<'EOF'
## Review Criteria for Implementation

Review the implementation against the plan and these criteria:

### Code Quality
- [ ] Code follows existing codebase patterns
- [ ] No security vulnerabilities introduced
- [ ] Error handling is appropriate
- [ ] Tests are included and pass
- [ ] No debug code or TODOs left behind

### Completeness
- [ ] All tasks in the plan are implemented
- [ ] All acceptance criteria are met
- [ ] Documentation is updated if needed

### Verdict Criteria
- **approved**: Implementation is complete and high quality
- **needs_revision**: Implementation has bugs, missing features, or quality issues
EOF
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_review_prompt() {
    local issue_number="${EGG_ISSUE_NUMBER}"
    local phase="${EGG_PHASE}"
    local cycle="${REVIEW_CYCLE:-1}"
    local prior_feedback="${PRIOR_FEEDBACK:-}"
    local repo="${GITHUB_REPOSITORY}"

    # Get phase-specific criteria
    local criteria
    case "$phase" in
        refine)
            criteria=$(get_refine_review_criteria)
            ;;
        plan)
            criteria=$(get_plan_review_criteria)
            ;;
        implement)
            criteria=$(get_implement_review_criteria)
            ;;
        *)
            echo "Unknown phase: ${phase}" >&2
            exit 1
            ;;
    esac

    # Get draft and review file paths
    local draft_file review_file
    draft_file=$(get_draft_file "$issue_number" "$phase")
    review_file=$(get_review_file "$issue_number" "$phase")

    # Build the prompt
    cat <<PROMPT
You are a code reviewer for the egg SDLC pipeline, reviewing work from the **${phase}** phase.

## Context

- **Issue**: #${issue_number}
- **Repository**: ${repo}
- **Review Cycle**: ${cycle}
- **Phase**: ${phase}

## Your Task

Review the ${phase} output and produce a verdict.

${criteria}

## Input Files

- Draft document: \`${draft_file}\`
- Previous review (if any): \`${review_file}\`

## Prior Feedback
$(if [[ -n "$prior_feedback" ]]; then
    echo "The previous review cycle identified these issues:"
    echo ""
    echo "$prior_feedback"
    echo ""
    echo "Verify whether these issues have been addressed."
else
    echo "This is the first review cycle."
fi)

## Instructions

1. Read the draft document at \`${draft_file}\`
2. Evaluate against the criteria above
3. Write your verdict to \`${review_file}\` as JSON:

\`\`\`json
{
  "verdict": "approved" | "needs_revision",
  "feedback": "Detailed feedback explaining issues (only if needs_revision)",
  "criteria_met": ["list", "of", "met", "criteria"],
  "criteria_failed": ["list", "of", "failed", "criteria"],
  "cycle": ${cycle}
}
\`\`\`

4. Use the Write tool to save the review JSON file

## Important

- Be thorough but fair - don't reject for minor issues
- Provide specific, actionable feedback for revisions
- Consider whether the ${phase} output achieves its goals
- If this is cycle 3+, be more lenient for non-critical issues
PROMPT
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    local prompt_file="${RUNNER_TEMP}/review-prompt.md"

    # Build and write the prompt
    build_review_prompt > "$prompt_file"

    # Output for GitHub Actions
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${EGG_REVIEW_MODEL:-sonnet}"  # Use sonnet for reviews by default (fast, good quality)
    } >> "$GITHUB_OUTPUT"

    echo "Review prompt written to ${prompt_file}"
}

main "$@"
