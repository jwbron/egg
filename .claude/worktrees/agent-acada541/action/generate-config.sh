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
#   INPUT_BOT_APP_ID             — GitHub App ID for bot identity
#   INPUT_BOT_APP_PRIVATE_KEY    — GitHub App private key PEM content
#   INPUT_BOT_APP_INSTALLATION_ID — GitHub App installation ID
#   INPUT_BOT_USERNAME           — Bot username (REQUIRED for bot mode)
#   INPUT_REVIEWER_APP_ID        — Reviewer GitHub App ID (for posting reviews)
#   INPUT_REVIEWER_APP_PRIVATE_KEY — Reviewer GitHub App private key PEM content
#   INPUT_REVIEWER_APP_INSTALLATION_ID — Reviewer GitHub App installation ID
#   INPUT_REVIEWER_BOT_NAME      — Reviewer bot username
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

BOT_USERNAME="${INPUT_BOT_USERNAME:-}"
RUN_ID="${GITHUB_RUN_ID:-$$}"

# ---------------------------------------------------------------------------
# Create config directory
# ---------------------------------------------------------------------------

CONFIG_DIR="${RUNNER_TEMP:-/tmp}/egg-config-${RUN_ID}"
mkdir -p "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# Generate repositories.yaml
# ---------------------------------------------------------------------------

# Determine auth_mode based on whether bot App credentials are provided
if [[ -n "${INPUT_BOT_APP_ID:-}" && -n "${INPUT_BOT_APP_PRIVATE_KEY:-}" && -n "${INPUT_BOT_APP_INSTALLATION_ID:-}" ]]; then
  AUTH_MODE="bot"
else
  AUTH_MODE="user"
fi

# Build optional repo_settings lines
CHECKPOINT_REPO_LINE=""
if [[ -n "${INPUT_CHECKPOINT_REPO:-}" ]]; then
  # Validate owner/repo format before writing to config
  if [[ ! "${INPUT_CHECKPOINT_REPO}" =~ ^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$ ]]; then
    echo "ERROR: checkpoint-repo must be in 'owner/repo' format, got: ${INPUT_CHECKPOINT_REPO}" >&2
    exit 1
  fi
  CHECKPOINT_REPO_LINE="    checkpoint_repo: ${INPUT_CHECKPOINT_REPO}"
fi

cat > "$CONFIG_DIR/repositories.yaml" <<YAML
github_username: ${GITHUB_ACTOR}
bot_username: ${BOT_USERNAME}

writable_repos:
  - ${GITHUB_REPOSITORY}

repo_settings:
  ${GITHUB_REPOSITORY}:
    auth_mode: ${AUTH_MODE}
${CHECKPOINT_REPO_LINE}

user_mode:
  github_user: ${GITHUB_ACTOR}
  git_name: ${GITHUB_ACTOR}
  git_email: ${GITHUB_ACTOR_ID:-0}+${GITHUB_ACTOR}@users.noreply.github.com

local_repos:
  paths:
    - ${GITHUB_WORKSPACE}
YAML

# ---------------------------------------------------------------------------
# Generate secrets.env
# ---------------------------------------------------------------------------

cat > "$CONFIG_DIR/secrets.env" <<ENV
CLAUDE_CODE_OAUTH_TOKEN=${INPUT_ANTHROPIC_OAUTH_TOKEN}
GITHUB_USER_TOKEN=${INPUT_GITHUB_TOKEN}
ENV

# Add bot GitHub App credentials if provided
if [[ "$AUTH_MODE" == "bot" ]]; then
  echo "GITHUB_APP_ID=${INPUT_BOT_APP_ID}" >> "$CONFIG_DIR/secrets.env"
  echo "GITHUB_APP_INSTALLATION_ID=${INPUT_BOT_APP_INSTALLATION_ID}" >> "$CONFIG_DIR/secrets.env"

  # Write private key PEM to file for the gateway's token refresher.
  # Normalize literal \n sequences to real newlines — common when PEM keys
  # are pasted as a single line into CI secret UIs.
  PEM_CONTENT="${INPUT_BOT_APP_PRIVATE_KEY//\\n/$'\n'}"
  printf '%s\n' "$PEM_CONTENT" > "$CONFIG_DIR/github-app.pem"
  chmod 600 "$CONFIG_DIR/github-app.pem"

  # Validate PEM structure
  if ! grep -q -- "-----BEGIN" "$CONFIG_DIR/github-app.pem"; then
    echo "ERROR: bot-app-private-key does not appear to be valid PEM." >&2
    echo "  Expected '-----BEGIN RSA PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'." >&2
    echo "  Paste the full .pem file contents (with newlines) into the GitHub secret." >&2
    exit 1
  fi
fi

# Add bot identity config
# GATEWAY_BOT_NAME = GitHub identity (for PR author checks)
# GATEWAY_BOT_BRANCH_PREFIX = branch namespace (for push ownership checks)
# These are independent: bot name and branch prefix can differ
BOT_BRANCH_PREFIX="${INPUT_BOT_BRANCH_PREFIX:-}"
echo "GATEWAY_BOT_NAME=${BOT_USERNAME}" >> "$CONFIG_DIR/secrets.env"
echo "GATEWAY_BOT_BRANCH_PREFIX=${BOT_BRANCH_PREFIX}" >> "$CONFIG_DIR/secrets.env"

# Add reviewer bot credentials if provided
# The reviewer bot is a separate GitHub App used for posting code reviews.
# This allows reviews to use the full GitHub Reviews API (approve/request-changes)
# since the reviewer is not the same account as the PR author.
if [[ -n "${INPUT_REVIEWER_APP_ID:-}" && -n "${INPUT_REVIEWER_APP_PRIVATE_KEY:-}" && -n "${INPUT_REVIEWER_APP_INSTALLATION_ID:-}" ]]; then
  echo "REVIEWER_APP_ID=${INPUT_REVIEWER_APP_ID}" >> "$CONFIG_DIR/secrets.env"
  echo "REVIEWER_APP_INSTALLATION_ID=${INPUT_REVIEWER_APP_INSTALLATION_ID}" >> "$CONFIG_DIR/secrets.env"

  # Write reviewer private key PEM to file (same pattern as bot PEM).
  # Multiline PEM keys cannot be stored in secrets.env (line-based parsing).
  REVIEWER_PEM="${INPUT_REVIEWER_APP_PRIVATE_KEY//\\n/$'\n'}"
  printf '%s\n' "$REVIEWER_PEM" > "$CONFIG_DIR/reviewer-app.pem"
  chmod 600 "$CONFIG_DIR/reviewer-app.pem"

  # Validate PEM structure
  if ! grep -q -- "-----BEGIN" "$CONFIG_DIR/reviewer-app.pem"; then
    echo "ERROR: reviewer-app-private-key does not appear to be valid PEM." >&2
    echo "  Expected '-----BEGIN RSA PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'." >&2
    echo "  Paste the full .pem file contents (with newlines) into the GitHub secret." >&2
    exit 1
  fi

  # Add reviewer bot name for identity checks
  if [[ -n "${INPUT_REVIEWER_BOT_NAME:-}" ]]; then
    echo "GATEWAY_REVIEWER_BOT_NAME=${INPUT_REVIEWER_BOT_NAME}" >> "$CONFIG_DIR/secrets.env"
  fi
fi

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
