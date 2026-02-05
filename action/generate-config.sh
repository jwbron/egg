#!/usr/bin/env bash
# generate-config.sh — Generate ephemeral gateway configuration for GitHub Actions
#
# Creates a temp directory containing the three files the gateway needs:
#   - repositories.yaml  (repo config for config/repo_config.py)
#   - secrets.env         (Anthropic credentials for anthropic_credentials.py)
#   - launcher-secret     (auth token for launcher API calls)
#
# Required environment variables:
#   GITHUB_REPOSITORY   — owner/repo (e.g., "jwbron/egg")
#   GITHUB_ACTOR        — GitHub username triggering the workflow
#   GITHUB_ACTOR_ID     — Numeric ID for noreply email
#   INPUT_ANTHROPIC_OAUTH_TOKEN — Anthropic OAuth token
#   INPUT_GITHUB_TOKEN  — GitHub token for git operations
#
# Optional environment variables:
#   INPUT_BOT_GITHUB_TOKEN — Bot GitHub App token
#   INPUT_BOT_USERNAME     — Bot username (default: "egg")
#
# Outputs:
#   EGG_CONFIG_DIR — path to the generated config directory (written to GITHUB_OUTPUT)

set -euo pipefail

# ---------------------------------------------------------------------------
# Validate required inputs
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_ACTOR:?GITHUB_ACTOR is required}"
: "${INPUT_ANTHROPIC_OAUTH_TOKEN:?anthropic-oauth-token input is required}"
: "${INPUT_GITHUB_TOKEN:?github-token input is required}"

REPO_NAME="${GITHUB_REPOSITORY#*/}"
BOT_USERNAME="${INPUT_BOT_USERNAME:-egg}"
RUN_ID="${GITHUB_RUN_ID:-$$}"

# ---------------------------------------------------------------------------
# Create config directory
# ---------------------------------------------------------------------------

CONFIG_DIR="${RUNNER_TEMP:-/tmp}/egg-config-${RUN_ID}"
mkdir -p "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# Generate repositories.yaml
# ---------------------------------------------------------------------------

# Determine auth_mode based on whether a bot token is provided
if [[ -n "${INPUT_BOT_GITHUB_TOKEN:-}" ]]; then
  AUTH_MODE="bot"
else
  AUTH_MODE="user"
fi

cat > "$CONFIG_DIR/repositories.yaml" <<YAML
github_username: ${GITHUB_ACTOR}
bot_username: ${BOT_USERNAME}

writable_repos:
  - ${GITHUB_REPOSITORY}

repo_settings:
  ${GITHUB_REPOSITORY}:
    auth_mode: ${AUTH_MODE}

user_mode:
  github_user: ${GITHUB_ACTOR}
  git_name: ${GITHUB_ACTOR}
  git_email: ${GITHUB_ACTOR_ID:-0}+${GITHUB_ACTOR}@users.noreply.github.com

local_repos:
  paths:
    - /home/egg/repos/${REPO_NAME}
YAML

# ---------------------------------------------------------------------------
# Generate secrets.env
# ---------------------------------------------------------------------------

cat > "$CONFIG_DIR/secrets.env" <<ENV
CLAUDE_CODE_OAUTH_TOKEN=${INPUT_ANTHROPIC_OAUTH_TOKEN}
ENV
# NOTE: GITHUB_USER_TOKEN is intentionally omitted from secrets.env — the
# gateway reads it from the environment variable (passed via docker run -e),
# not from this file.

# Add bot token if provided
if [[ -n "${INPUT_BOT_GITHUB_TOKEN:-}" ]]; then
  echo "BOT_GITHUB_TOKEN=${INPUT_BOT_GITHUB_TOKEN}" >> "$CONFIG_DIR/secrets.env"
fi

# Add bot identity config
echo "GATEWAY_BOT_NAME=${BOT_USERNAME}" >> "$CONFIG_DIR/secrets.env"
echo "GATEWAY_BOT_BRANCH_PREFIX=${BOT_USERNAME}" >> "$CONFIG_DIR/secrets.env"

chmod 600 "$CONFIG_DIR/secrets.env"

# ---------------------------------------------------------------------------
# Generate launcher-secret
# ---------------------------------------------------------------------------

openssl rand -base64 32 > "$CONFIG_DIR/launcher-secret"
chmod 600 "$CONFIG_DIR/launcher-secret"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

echo "EGG_CONFIG_DIR=$CONFIG_DIR" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "Config directory: $CONFIG_DIR"
echo "  repositories.yaml: $(wc -l < "$CONFIG_DIR/repositories.yaml") lines"
echo "  secrets.env: $(wc -l < "$CONFIG_DIR/secrets.env") lines"
echo "  launcher-secret: generated"
