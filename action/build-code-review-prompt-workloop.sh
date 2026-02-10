#!/usr/bin/env bash
# build-code-review-prompt-workloop.sh — Build code review prompt for work loop
#
# This script creates a prompt for code review that works within the SDLC work loop.
# Unlike the PR-triggered version, this operates on the implementation diff without
# requiring a PR number.
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
#   Writes verdict to .egg-state/reviews/{ISSUE}-{PHASE}-code.json

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch review rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_review_rules() {
    local rules_file=".egg/review-rules.md"

    if [[ -f "$rules_file" ]]; then
        cat "$rules_file"
    else
        # Default review rules when no repo-specific rules exist
        cat <<'EOF'
## Default Review Rules

**Be extremely thorough.** This is critical infrastructure. Identify ALL issues in the first pass—do not stop after finding a few. A false negative (missing a bug) is far worse than extra scrutiny.

### What to Review

**Security** (highest priority):
- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)
- Authentication/authorization flaws
- Credential exposure, hardcoded secrets
- Insecure cryptography or randomness
- SSRF, open redirects, unsafe deserialization

**Correctness**:
- Logic errors, off-by-one, boundary conditions
- Race conditions, deadlocks, concurrency bugs
- Null/undefined handling, missing error paths
- Resource leaks (connections, file handles, memory)
- Incorrect algorithm complexity for data size

**Robustness**:
- Missing input validation at trust boundaries
- Unhandled exceptions that could crash the system
- Missing retry logic for transient failures
- Inadequate timeouts for external calls
- State corruption scenarios

**Design issues**:
- Violations of existing codebase patterns
- Breaking changes to public interfaces
- Missing or incorrect abstractions
- Tight coupling that will hinder future changes

### How to Review

1. **Examine every changed file systematically**. Do not skim.
2. **Read surrounding context**—check how changed code integrates with the rest of the codebase. Use file reads and grep liberally.
3. **Trace data flow** from input to output, especially for security-sensitive paths.
4. **Consider edge cases** the author may not have tested.
5. **Research when uncertain**—look up library behavior, check documentation, verify assumptions.

### Skip

- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)
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

    local review_rules
    review_rules=$(fetch_review_rules)

    local review_file=".egg-state/reviews/${issue_number}-${phase}-code.json"

    local prompt
    local is_rereview=false

    if [[ "$review_cycle" -gt 1 ]]; then
        is_rereview=true
    fi

    # Code review is primarily for implement phase
    if [[ "$phase" != "implement" ]]; then
        # For non-implement phases, provide a minimal pass-through
        prompt="Code review is not applicable for the ${phase} phase.

Write an approval verdict to \`${review_file}\`:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"code\",
  \"verdict\": \"approved\",
  \"summary\": \"Code review not applicable for ${phase} phase.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Code review: approved (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`
"
    elif [[ "$is_rereview" == "true" ]]; then
        prompt="Re-review the implementation for issue #${issue_number}.

This is **review cycle ${review_cycle}** — the implementation has been revised based on prior feedback.

## Scope

This is a **comprehensive code review**. Focus on security, correctness, and robustness.
Agent-mode design alignment is handled by another reviewer.

### Prior Feedback
${PRIOR_FEEDBACK:-No prior feedback available}

## Your Task

Perform a **thorough review of all changes**. Find ALL issues—do not stop after identifying a few problems.

1. **Review recent changes**: Use \`git log --oneline -10\` and \`git diff HEAD~5..HEAD\` to see recent changes
2. **Verify issues addressed**: Confirm that concerns from prior feedback have been properly fixed
3. **Examine new code thoroughly**: Apply the same rigorous scrutiny to all changes

For full implementation context: \`git diff origin/main..HEAD\`

### Be Direct

Do not soften feedback. State issues clearly and explain why they matter. This is infrastructure review.

## Review Rules

${review_rules}

"
    else
        prompt="Review the implementation for issue #${issue_number}.

## Scope

This is a **comprehensive code review**. Focus on security, correctness, and robustness.
Agent-mode design alignment is handled by another reviewer.

## Your Task

Perform a **comprehensive, thorough code review**. This is critical infrastructure—your review is the last line of defense before human review. **Find ALL issues on the first pass.** Do not stop after identifying a few problems.

### How to Proceed

1. **Get the full diff**: Run \`git diff origin/main..HEAD\` to see all changes
2. **Review every file systematically**: Go through each changed file, examining every modified line
3. **Read surrounding context**: Use file reads and grep to understand how changes integrate with the existing codebase
4. **Trace data flow**: Follow inputs through the system, especially for security-sensitive operations
5. **Research when needed**: Look up library behavior, check documentation, verify assumptions
6. **Consider edge cases**: Think about what the author might not have tested

### Be Direct

Do not soften feedback. State issues clearly and explain why they matter. This is infrastructure review.

## Review Rules

${review_rules}

"
    fi

    prompt+="## Verdict Format

Write your verdict to \`${review_file}\`:

\`\`\`json
{
  \"reviewer\": \"code\",
  \"verdict\": \"approved\" | \"needs_revision\",
  \"summary\": \"Brief summary of review findings\",
  \"feedback\": \"Detailed feedback if needs_revision, empty string if approved\",
  \"timestamp\": \"ISO 8601 timestamp\"
}
\`\`\`

### If the implementation PASSES review:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"code\",
  \"verdict\": \"approved\",
  \"summary\": \"Implementation passes code review. No blocking issues found.\",
  \"feedback\": \"\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Code review: approved (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

### If the implementation NEEDS REVISION:

\`\`\`bash
mkdir -p .egg-state/reviews
TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat > ${review_file} << REVIEW_EOF
{
  \"reviewer\": \"code\",
  \"verdict\": \"needs_revision\",
  \"summary\": \"Implementation requires changes.\",
  \"feedback\": \"### Security Issues\\n\\n[If any]\\n\\n### Correctness Issues\\n\\n[If any]\\n\\n### Robustness Issues\\n\\n[If any]\\n\\n### Suggestions\\n\\n- [Actionable fix]\",
  \"timestamp\": \"\$TIMESTAMP\"
}
REVIEW_EOF
git add ${review_file}
git commit -m \"Code review: needs revision (${phase} phase, issue #${issue_number})\"
git push origin \$(git branch --show-current)
\`\`\`

## Important Notes

- Be thorough — examine every changed file and trace data flow
- Prioritize security issues — they are the highest priority
- Provide specific, actionable feedback when requesting revision
- Reference exact file and line when reporting issues
- Escape newlines in the feedback field as \\\\n for valid JSON
- **Do NOT post to the issue** — your review is internal
"

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/code-review-prompt-${issue_number}.txt"
    echo "$prompt" > "$prompt_file"

    # Use opus for code reviews (needs thorough analysis)
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
    echo "Code review prompt built for ${phase} phase: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${EGG_ISSUE_NUMBER:?EGG_ISSUE_NUMBER is required}"
: "${EGG_PIPELINE_PHASE:?EGG_PIPELINE_PHASE is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
