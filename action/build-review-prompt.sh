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

# Source the base framework
# shellcheck source=review-bot-base.sh
source "$(dirname "$0")/review-bot-base.sh"

# ---------------------------------------------------------------------------
# Bot configuration (used by review-bot-base.sh)
# ---------------------------------------------------------------------------

# shellcheck disable=SC2034  # Variables used by sourced script
BOT_NAME="review"

# shellcheck disable=SC2034  # Used by sourced script
BOT_DEFAULT_RULES='## Default Review Rules

Focus on:
- Security issues (vulnerabilities, unsafe patterns, credential leaks)
- Correctness (logic errors, edge cases, error handling gaps)
- Code quality (readability, maintainability, naming)

Skip:
- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)'

# shellcheck disable=SC2034  # Used by sourced script
BOT_TASK_DESCRIPTION='Perform a thorough code review. Check for security vulnerabilities, logic errors,
edge cases, and code quality issues. Look at how the changed code interacts with
the rest of the codebase.'

# shellcheck disable=SC2034  # Used by sourced script
BOT_CONVENTIONS_FILE="$(dirname "$0")/review-conventions.md"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

build_bot_prompt
