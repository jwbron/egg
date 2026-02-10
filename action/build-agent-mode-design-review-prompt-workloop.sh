#!/usr/bin/env bash
# build-agent-mode-design-review-prompt-workloop.sh — Build agent-mode design review prompt for work loop
#
# This script creates a prompt for agent-mode design review that works within the SDLC
# work loop. Unlike the PR-triggered version, this operates on drafts and diffs without
# requiring a PR number.
#
# Environment variables:
#   GITHUB_REPOSITORY   — owner/repo
#   EGG_ISSUE_NUMBER    — GitHub issue number
#   EGG_PIPELINE_PHASE  — SDLC phase (refine, plan, implement)
#   RUNNER_TEMP         — Temp directory for prompt file
#   REVIEW_CYCLE        — Current review cycle number
#   PRIOR_FEEDBACK      — Prior review feedback (optional, for re-reviews)
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT
#   Writes verdict to .egg-state/reviews/{ISSUE}-{PHASE}-agent-design.json

set -euo pipefail

# ---------------------------------------------------------------------------
# Phase-specific context
# ---------------------------------------------------------------------------

get_phase_context() {
    local phase="$1"
    local issue_number="$2"

    case "$phase" in
        refine)
            echo "The analysis document is at \`.egg-state/drafts/${issue_number}-analysis.md\`.
Read the analysis and verify it follows agent-mode design principles:
- Does it describe agent objectives rather than rigid step-by-step procedures?
- Are prompts oriented toward the agent's capabilities rather than constraining them?"
            ;;
        plan)
            echo "The implementation plan is at \`.egg-state/drafts/${issue_number}-plan.md\`.
Read the plan and verify it follows agent-mode design principles:
- Are task descriptions focused on outcomes rather than prescriptive procedures?
- Does the plan leverage agent capabilities for exploration and problem-solving?
- Are structured outputs only required where machine parsing is needed?"
            ;;
        implement)
            echo "Review the implementation using \`git diff origin/main..HEAD\`.
Examine prompt scripts, workflows, and agent-related code for agent-mode design alignment:
- Do prompts give agents room to explore, or do they micromanage every step?
- Is context provided to orient the agent, not to pre-fetch everything it might need?
- Are outputs structured only when consumed by machines, not humans?"
            ;;
        *)
            echo "Unknown phase. Review any agent-related changes for design alignment."
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local issue_number="${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
    local phase="${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
    local review_cycle="${REVIEW_CYCLE:-1}"

    local phase_context
    phase_context=$(get_phase_context "$phase" "$issue_number")

    local review_file=".egg-state/reviews/${issue_number}-${phase}-agent-design.json"

    local prompt
    local is_rereview=false

    if [[ "$review_cycle" -gt 1 ]]; then
        is_rereview=true
    fi

    if [[ "$is_rereview" == "true" ]]; then
        prompt="Re-review the ${phase} phase output for issue #${issue_number} (agent-mode design alignment).

This is **review cycle ${review_cycle}** — the agent has revised the output based on prior feedback.

## Scope

This is a specialized **agent-mode design review**. Focus ONLY on agent-mode design principles.
Do NOT review general code quality, security, or correctness — other reviewers handle those.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

### Phase Context
${phase_context}

"
    else
        prompt="Review the ${phase} phase output for issue #${issue_number} (agent-mode design alignment).

## Scope

This is a specialized **agent-mode design review**. Focus ONLY on agent-mode design principles.
Do NOT review general code quality, security, or correctness — other reviewers handle those.

**Only flag issues if you find clear agent-mode design anti-patterns.** If the output has no agent-mode concerns, approve with a brief note like \"No agent-mode design concerns.\"

### Phase Context
${phase_context}

"
    fi

    prompt+="## Review Philosophy

The guidelines in \`docs/guides/agent-mode-design.md\` are **guidelines, not absolute rules**. Apply them with judgment:

- **Orienting vs constraining**: Small metadata, task context, and summaries that orient the agent are fine. The concern is with large pre-fetched data that prevents the agent from seeing what it needs.

- **Practical balance**: A design that's 80% aligned but works well is better than 100% pure but fragile.

- **Benefit of the doubt**: If a design choice could be interpreted as either helpful or problematic, lean toward the charitable interpretation.

## What to Look For

Flag these **clear** anti-patterns:

1. **Excessive pre-fetching**: Baking large diffs (10KB+) or full file contents into prompts
2. **Structured output for humans**: Requiring JSON when output goes directly to humans
3. **Post-processing pipelines**: Scripts that parse agent output to take actions the agent could take directly
4. **Rigid procedures**: Micromanaging step-by-step procedures when objectives would suffice
5. **Prompt-level security**: Using instructions for constraints that should be sandbox-enforced

## Verdict Format

Write your verdict to \`${review_file}\`:

\`\`\`json
{
  \"reviewer\": \"agent-design\",
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the output PASSES review (no agent-mode concerns):

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"agent-design\",
  \"verdict\": \"approved\",
  \"summary\": \"No agent-mode design concerns found.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Agent-mode design review: approved (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

### If the output NEEDS REVISION (clear anti-patterns found):

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"agent-design\",
  \"verdict\": \"needs_revision\",
  \"summary\": \"Agent-mode design issues found.\",
  \"feedback\": \"### Agent-Mode Design Issues\\n\\n1. **[Anti-pattern]**: [Specific issue]\\n\\n### Suggestions\\n\\n- [Actionable suggestion]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Agent-mode design review: needs revision (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

## Important Notes

- Be thorough but fair. Only flag **clear** anti-patterns — not borderline cases.
- Provide specific, actionable feedback when requesting revision.
- Escape newlines in the feedback field as \\\\n for valid JSON.
- **Do NOT post to the issue** — your review is internal.
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/agent-design-review-prompt-${issue_number}.txt"
    echo "$prompt" > "$prompt_file"

    # Use sonnet for faster reviews (this is a specialized check)
    local model="sonnet"

    # Write outputs
    {
        echo "prompt-file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    local review_type="initial"
    if [[ "$is_rereview" == "true" ]]; then
        review_type="re-review (cycle ${review_cycle})"
    fi
    echo "Agent-mode design review prompt built for ${phase} phase: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
