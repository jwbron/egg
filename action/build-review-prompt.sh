#!/usr/bin/env bash
# build-review-prompt.sh — Build a minimal review prompt for agent-driven code review
#
# This script creates a minimal prompt that tells Claude to fetch what it needs
# and post its own review directly via `gh pr review`. This replaces the old
# approach of pre-fetching all PR data and parsing structured JSON output.
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   LAST_REVIEW_COMMIT — (Optional) Commit SHA of last bot review, for re-reviews
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Fetch review rules (or use defaults)
# ---------------------------------------------------------------------------

fetch_review_rules() {
    local rules_file=".egg/review-rules.md"
    local script_dir
    script_dir="$(dirname "$0")"
    local shared_file="${script_dir}/../shared/prompts/code-review-criteria.md"

    if [[ -f "$rules_file" ]]; then
        # User override takes priority
        cat "$rules_file"
    elif [[ -f "$shared_file" ]]; then
        # Shared criteria (anchored to trusted checkout via script dir)
        cat "$shared_file"
    else
        # Inline fallback for rollout safety
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
    local review_rules
    review_rules=$(fetch_review_rules)

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
        prompt="Re-review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

This is a **re-review** — you previously reviewed this PR at commit \`${LAST_REVIEW_COMMIT}\`.

## Your Task

Perform a **thorough review of all new changes**. Find ALL issues in the new code—do not stop after identifying a few problems.

1. **Review the delta**: Use \`git diff ${LAST_REVIEW_COMMIT}..HEAD\` to see what changed since your last review.
2. **Check previous feedback**: Use \`gh pr view ${PR_NUMBER} --comments\` to see previous review comments.
3. **Verify issues addressed**: Confirm that concerns from your previous review have been properly fixed, not just superficially addressed.
4. **Examine new code thoroughly**: Apply the same rigorous scrutiny to new changes as you would to an initial review. Read surrounding context, trace data flow, research when uncertain.

For full PR context if needed: \`gh pr diff ${PR_NUMBER}\`

### Be Direct

Do not soften feedback. State issues clearly and explain why they matter. This is infrastructure review.

## Review Rules

${review_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\` with \`--body-file\`. Always write your review to a temp file first, then use --body-file to post it. Do NOT use --body with inline content — long reviews will fail due to shell escaping. Example: \`cat > /tmp/review-body.md << 'REVIEW_EOF'\` then \`gh pr review ${PR_NUMBER} --request-changes --body-file /tmp/review-body.md\`. Use --approve, --request-changes, or --comment as appropriate. Sign your review with: — Authored by egg}
"
    else
        prompt="Review PR #${PR_NUMBER} in ${GITHUB_REPOSITORY}.

## Your Task

Perform a **comprehensive, thorough code review**. This is critical infrastructure—your review is the last line of defense before code reaches production. **Find ALL issues on the first pass.** Do not stop after identifying a few problems.

### How to Proceed

1. **Get the full diff**: Run \`gh pr diff ${PR_NUMBER}\` to see all changes.
2. **Review every file systematically**: Go through each changed file, examining every modified line.
3. **Read surrounding context**: Use file reads and grep to understand how changes integrate with the existing codebase. Don't review in isolation.
4. **Trace data flow**: Follow inputs through the system, especially for security-sensitive operations.
5. **Research when needed**: Look up library behavior, check documentation, verify assumptions about APIs or language semantics.
6. **Consider edge cases**: Think about what the author might not have tested—boundary conditions, error paths, concurrent access.

### Be Direct

Do not soften feedback. State issues clearly and explain why they matter. Suggest specific fixes where possible. This is infrastructure review, not a code chat.

## Review Rules

${review_rules}

## Review Conventions

${conventions:-Post your review using \`gh pr review ${PR_NUMBER}\` with \`--body-file\`. Always write your review to a temp file first, then use --body-file to post it. Do NOT use --body with inline content — long reviews will fail due to shell escaping. Example: \`cat > /tmp/review-body.md << 'REVIEW_EOF'\` then \`gh pr review ${PR_NUMBER} --request-changes --body-file /tmp/review-body.md\`. Use --approve, --request-changes, or --comment as appropriate. Sign your review with: — Authored by egg}
"
    fi

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/review-prompt-${PR_NUMBER}.txt"
    echo "$prompt" > "$prompt_file"

    # Always use opus for reviews
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
    echo "Review prompt built: ${#prompt} chars, model=${model}, type=${review_type}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
