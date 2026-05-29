#!/usr/bin/env bash
# entrypoint.sh — Run the egg agent as a bare process in the GitHub Actions runner.
#
# There is no Docker, no gateway, and no sandbox container in this path. The
# runner is already ephemeral and the GitHub token (scoped by the calling job's
# permissions: block) is the capability boundary. Steps:
#   1. Configure git/gh auth + identity for the agent's git/gh operations
#   2. Resolve the prompt (inline or from file) and the network mode
#   3. Run `python3 -m egg_agent` against the checked-out repo
#   4. Capture GHA-specific output (GITHUB_OUTPUT, GITHUB_STEP_SUMMARY)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${GITHUB_RUN_ID:-$$}"
MODEL="${INPUT_MODEL:-opus}"
TIMEOUT_MINUTES="${INPUT_TIMEOUT:-30}"
LOG_FILE="${RUNNER_TEMP:-/tmp}/egg-output-${RUN_ID}.log"

# ---------------------------------------------------------------------------
# Step 1: Auth + identity
# ---------------------------------------------------------------------------

echo "=== Step 1: Configure git/gh ==="

: "${INPUT_ANTHROPIC_OAUTH_TOKEN:?anthropic-oauth-token is required}"
: "${INPUT_GITHUB_TOKEN:?github-token is required}"

# The Claude CLI (driven by claude-agent-sdk) reads this from the environment.
export CLAUDE_CODE_OAUTH_TOKEN="${INPUT_ANTHROPIC_OAUTH_TOKEN}"

# `gh` reads GH_TOKEN; `git push`/`git fetch` use gh as the credential helper.
export GH_TOKEN="${INPUT_GITHUB_TOKEN}"
gh auth setup-git

# Install the slim `gh` review-marker shim ahead of the real gh on PATH so the
# agent's `gh pr review` calls carry the egg-automated-review marker the review
# workflows parse. Resolve the real gh FIRST (before the prepend) so the shim
# can delegate to it without recursing. See action/bin/gh.
EGG_REAL_GH="$(command -v gh)"
export EGG_REAL_GH
export PATH="${SCRIPT_DIR}/bin:${PATH}"

# Commit identity. github-token is a GitHub App installation token, so commits
# the agent makes should be authored by the App's bot user (<name>[bot]) with
# its numeric-id noreply email, matching how GitHub attributes App commits.
# Falls back to github-actions[bot] when no bot-username is supplied (e.g.
# read-only bots that never commit, or non-App callers).
BOT_USERNAME="${INPUT_BOT_USERNAME:-}"
if [[ -n "$BOT_USERNAME" ]]; then
  BOT_LOGIN="${BOT_USERNAME}[bot]"
  # Look up the bot user's numeric id for the noreply email; fall back to a
  # plain noreply address if the lookup fails (e.g. restricted token).
  BOT_ID="$(gh api "users/${BOT_LOGIN}" --jq '.id' 2>/dev/null || true)"
  if [[ -n "$BOT_ID" ]]; then
    BOT_EMAIL="${BOT_ID}+${BOT_LOGIN}@users.noreply.github.com"
  else
    BOT_EMAIL="${BOT_LOGIN}@users.noreply.github.com"
  fi
  git config --global user.name "$BOT_LOGIN"
  git config --global user.email "$BOT_EMAIL"
else
  git config --global user.name "github-actions[bot]"
  git config --global user.email "41898282+github-actions[bot]@users.noreply.github.com"
fi

# ---------------------------------------------------------------------------
# Step 2: Resolve prompt + mode
# ---------------------------------------------------------------------------

echo "=== Step 2: Resolve prompt + mode ==="

if [[ -n "${INPUT_PROMPT_FILE:-}" && -f "${INPUT_PROMPT_FILE}" ]]; then
  echo "Reading prompt from file: ${INPUT_PROMPT_FILE}"
  INPUT_PROMPT="$(cat "${INPUT_PROMPT_FILE}")"
  echo "Prompt loaded: ${#INPUT_PROMPT} chars"
elif [[ -z "${INPUT_PROMPT:-}" ]]; then
  echo "ERROR: Either prompt or prompt-file input is required" >&2
  exit 1
fi

# Resolve network mode (auto → public/private from repo visibility). In private
# mode egg_agent disables WebFetch/WebSearch at the SDK level (see
# shared/egg_agent/client.py); EGG_PRIVATE_MODE is the signal it reads.
RESOLVED_MODE="${INPUT_MODE:-auto}"
if [[ "$RESOLVED_MODE" == "auto" ]]; then
  REPO_VIS="${GITHUB_EVENT_REPOSITORY_VISIBILITY:-public}"
  if [[ "$REPO_VIS" == "private" || "$REPO_VIS" == "internal" ]]; then
    RESOLVED_MODE="private"
  else
    RESOLVED_MODE="public"
  fi
fi
if [[ "$RESOLVED_MODE" == "private" ]]; then
  export EGG_PRIVATE_MODE="true"
fi
echo "Resolved mode: $RESOLVED_MODE"

# Land the agent in the checked-out repo on its first tool call (see
# shared/egg_agent/client.py cwd resolution).
export EGG_REPO_PATH="${GITHUB_WORKSPACE:-$PWD}"

# ---------------------------------------------------------------------------
# Step 3: Run the agent
# ---------------------------------------------------------------------------
# EGG_AGENT_ROLE, EGG_BOT_NAME, EGG_COMMIT_SHA, EGG_ISSUE_NUMBER and
# EGG_PR_NUMBER are set by the calling workflow on the action step's env and are
# inherited here, so egg_agent picks them up from os.environ directly.

echo "=== Step 3: Run agent ==="

# --max-turns 200: enough turns for explore + implement + test + comment.
# Default (100) was observed insufficient for the PR-bot tasks.
TIMEOUT_SECONDS=$((TIMEOUT_MINUTES * 60))

set +e
printf '%s' "$INPUT_PROMPT" | python3 -m egg_agent \
  --model "$MODEL" \
  --max-turns 200 \
  --timeout "$TIMEOUT_SECONDS" \
  2>&1 | tee "$LOG_FILE"
AGENT_EXIT_CODE=${PIPESTATUS[1]}
set -e

echo "Agent exited with code: $AGENT_EXIT_CODE"

# ---------------------------------------------------------------------------
# Step 4: Capture output
# ---------------------------------------------------------------------------

echo "=== Step 4: Capture output ==="

{
  echo "exit-code=${AGENT_EXIT_CODE}"
  echo "log-file=${LOG_FILE}"
} >>"${GITHUB_OUTPUT:-/dev/null}"

# Extract PR URL from output (look for GitHub PR URLs in the log)
PR_URL=$(grep -oP 'https://github\.com/[^/]+/[^/]+/pull/\d+' "$LOG_FILE" | tail -1 || true)
if [[ -n "$PR_URL" ]]; then
  echo "pr-url=${PR_URL}" >>"${GITHUB_OUTPUT:-/dev/null}"
  echo "PR created: $PR_URL"
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## egg Run Summary"
    echo ""
    echo "**Exit code:** \`${AGENT_EXIT_CODE}\`"
    echo "**Mode:** ${RESOLVED_MODE}"
    echo "**Model:** ${MODEL}"
    if [[ -n "$PR_URL" ]]; then
      echo "**PR:** ${PR_URL}"
    fi
    echo ""
    echo "<details><summary>Output log</summary>"
    echo ""
    echo '```'
    # Truncate to avoid exceeding step summary limits (1MB)
    head -c 500000 "$LOG_FILE"
    echo '```'
    echo "</details>"
  } >>"$GITHUB_STEP_SUMMARY"
fi

exit "$AGENT_EXIT_CODE"
