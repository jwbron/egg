#!/usr/bin/env bash
# entrypoint.sh — Orchestrate the egg stack (gateway + sandbox) inside GitHub Actions
#
# Steps:
#   1. Pull pre-built images from GHCR
#   2. Generate config (via generate-config.sh)
#   3. Run Python orchestration (gha_exec) — handles networks, gateway,
#      session, sandbox container, and cleanup
#   4. Capture GHA-specific output (GITHUB_OUTPUT, GITHUB_STEP_SUMMARY)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${GITHUB_RUN_ID:-$$}"

IMAGE_TAG="${INPUT_IMAGE_TAG:-latest}"
GATEWAY_IMAGE="ghcr.io/jwbron/egg-gateway:${IMAGE_TAG}"
SANDBOX_IMAGE="ghcr.io/jwbron/egg-sandbox:${IMAGE_TAG}"

MODEL="${INPUT_MODEL:-opus}"
LOG_FILE="${RUNNER_TEMP:-/tmp}/egg-output-${RUN_ID}.log"

# ---------------------------------------------------------------------------
# Safety-net cleanup (bash-level fallback)
# ---------------------------------------------------------------------------
# Python handles its own cleanup via ctx.ephemeral, but if the process is
# killed mid-flight these docker commands serve as a last resort.

cleanup() {
  echo "=== Bash cleanup ==="
  local exit_code=$?
  docker rm -f "egg-gha-gateway-${RUN_ID}" 2>/dev/null || true
  docker rm -f "egg-gha-sandbox-${RUN_ID}" 2>/dev/null || true
  docker network rm "egg-gha-isolated-${RUN_ID}" 2>/dev/null || true
  docker network rm "egg-gha-external-${RUN_ID}" 2>/dev/null || true
  return "$exit_code"
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 1: Pull images
# ---------------------------------------------------------------------------

echo "=== Step 1: Pull images ==="
echo "${INPUT_GITHUB_TOKEN}" | docker login ghcr.io -u "${GITHUB_ACTOR}" --password-stdin
docker pull "$GATEWAY_IMAGE"
docker pull "$SANDBOX_IMAGE"

# ---------------------------------------------------------------------------
# Step 2: Generate config
# ---------------------------------------------------------------------------

echo "=== Step 2: Generate config ==="

export INPUT_ANTHROPIC_OAUTH_TOKEN="${INPUT_ANTHROPIC_OAUTH_TOKEN:?anthropic-oauth-token is required}"
export INPUT_GITHUB_TOKEN="${INPUT_GITHUB_TOKEN:?github-token is required}"
export INPUT_BOT_APP_ID="${INPUT_BOT_APP_ID:-}"
export INPUT_BOT_APP_PRIVATE_KEY="${INPUT_BOT_APP_PRIVATE_KEY:-}"
export INPUT_BOT_APP_INSTALLATION_ID="${INPUT_BOT_APP_INSTALLATION_ID:-}"
export INPUT_BOT_USERNAME="${INPUT_BOT_USERNAME:-}"

"$SCRIPT_DIR/generate-config.sh"

CONFIG_DIR="${RUNNER_TEMP:-/tmp}/egg-config-${RUN_ID}"

# ---------------------------------------------------------------------------
# Step 2b: Resolve prompt from file if needed
# ---------------------------------------------------------------------------

if [[ -n "${INPUT_PROMPT_FILE:-}" && -f "${INPUT_PROMPT_FILE}" ]]; then
  echo "Reading prompt from file: ${INPUT_PROMPT_FILE}"
  export INPUT_PROMPT
  INPUT_PROMPT=$(cat "${INPUT_PROMPT_FILE}")
  echo "Prompt loaded: ${#INPUT_PROMPT} chars"
elif [[ -z "${INPUT_PROMPT:-}" ]]; then
  echo "ERROR: Either prompt or prompt-file input is required" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: Run Python orchestration
# ---------------------------------------------------------------------------

echo "=== Step 3: Python orchestration ==="

# Set EGG_* environment variables consumed by RuntimeContext.from_environment()
export EGG_ISOLATED_NETWORK="egg-gha-isolated-${RUN_ID}"
export EGG_EXTERNAL_NETWORK="egg-gha-external-${RUN_ID}"
export EGG_ISOLATED_SUBNET="auto"
export EGG_EXTERNAL_SUBNET="auto"
export EGG_GATEWAY_CONTAINER_NAME="egg-gha-gateway-${RUN_ID}"
export EGG_GATEWAY_IMAGE="${GATEWAY_IMAGE}"
export EGG_SANDBOX_IMAGE="${SANDBOX_IMAGE}"
export EGG_SKIP_BUILD="true"
export EGG_EPHEMERAL="true"
export EGG_PUBLISH_GATEWAY_PORTS="false"
export EGG_CONFIG_DIR="$CONFIG_DIR"
EGG_LAUNCHER_SECRET="$(cat "$CONFIG_DIR/launcher-secret")"
export EGG_LAUNCHER_SECRET

# Pass through EGG_BOT_NAME if set (used by gh wrapper for review markers)
if [[ -n "${EGG_BOT_NAME:-}" ]]; then
  export EGG_BOT_NAME
fi

# Pass through EGG_COMMIT_SHA if set (used by gh wrapper for review markers
# to pin the marker to the commit that was actually reviewed, avoiding races
# with commits pushed during the review)
if [[ -n "${EGG_COMMIT_SHA:-}" ]]; then
  export EGG_COMMIT_SHA
fi

# Add egg_lib and shared modules to Python path
export PYTHONPATH="${SCRIPT_DIR}/../sandbox:${SCRIPT_DIR}/../shared${PYTHONPATH:+:$PYTHONPATH}"

set +e
python3 -c "from egg_lib.cli import gha_exec; import sys; sys.exit(gha_exec())" \
  2>&1 | tee "$LOG_FILE"
SANDBOX_EXIT_CODE=${PIPESTATUS[0]}
set -e

echo "Python orchestration exited with code: $SANDBOX_EXIT_CODE"

# ---------------------------------------------------------------------------
# Step 4: Capture output
# ---------------------------------------------------------------------------

echo "=== Step 4: Capture output ==="

# Write outputs to GITHUB_OUTPUT
{
  echo "exit-code=${SANDBOX_EXIT_CODE}"
  echo "log-file=${LOG_FILE}"
} >> "${GITHUB_OUTPUT:-/dev/null}"

# Extract PR URL from output (look for GitHub PR URLs in the log)
PR_URL=$(grep -oP 'https://github\.com/[^/]+/[^/]+/pull/\d+' "$LOG_FILE" | tail -1 || true)
if [[ -n "$PR_URL" ]]; then
  echo "pr-url=${PR_URL}" >> "${GITHUB_OUTPUT:-/dev/null}"
  echo "PR created: $PR_URL"
fi

# Write to job summary
# Resolve mode the same way Python does (auto → public/private)
RESOLVED_MODE="${INPUT_MODE:-auto}"
if [[ "$RESOLVED_MODE" == "auto" ]]; then
  REPO_VIS="${GITHUB_EVENT_REPOSITORY_VISIBILITY:-public}"
  if [[ "$REPO_VIS" == "private" || "$REPO_VIS" == "internal" ]]; then
    RESOLVED_MODE="private"
  else
    RESOLVED_MODE="public"
  fi
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## egg Run Summary"
    echo ""
    echo "**Exit code:** \`${SANDBOX_EXIT_CODE}\`"
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
  } >> "$GITHUB_STEP_SUMMARY"
fi

# Exit with sandbox exit code
exit "$SANDBOX_EXIT_CODE"
