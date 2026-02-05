#!/usr/bin/env bash
# entrypoint.sh — Orchestrate the egg stack (gateway + sandbox) inside GitHub Actions
#
# This script replicates the orchestration flow from sandbox/egg_lib/runtime.py
# and sandbox/egg_lib/gateway.py in bash, for use inside a GHA runner.
#
# Steps:
#   1. Pull pre-built images from GHCR
#   2. Create Docker networks with dynamic subnet allocation
#   3. Detect mode (auto/public/private)
#   4. Generate config (via generate-config.sh)
#   5. Start gateway container (dual-homed)
#   6. Health check
#   7. Allocate container IP
#   8. Create session via gateway API
#   9. Start sandbox container
#  10. Capture output
#  11. Cleanup (trap EXIT)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_PORT=9848
GATEWAY_PROXY_PORT=3129
CONTAINER_HOME="/home/egg"
RUN_ID="${GITHUB_RUN_ID:-$$}"

GATEWAY_CONTAINER="egg-gha-gateway-${RUN_ID}"
SANDBOX_CONTAINER="egg-gha-sandbox-${RUN_ID}"
ISOLATED_NETWORK="egg-gha-isolated-${RUN_ID}"
EXTERNAL_NETWORK="egg-gha-external-${RUN_ID}"

IMAGE_TAG="${INPUT_IMAGE_TAG:-latest}"
GATEWAY_IMAGE="ghcr.io/jwbron/egg-gateway:${IMAGE_TAG}"
SANDBOX_IMAGE="ghcr.io/jwbron/egg-sandbox:${IMAGE_TAG}"

TIMEOUT_MINUTES="${INPUT_TIMEOUT:-30}"
MODEL="${INPUT_MODEL:-opus}"
LOG_FILE="${RUNNER_TEMP:-/tmp}/egg-output-${RUN_ID}.log"

# State tracking for cleanup
GATEWAY_STARTED=false
SANDBOX_STARTED=false
NETWORKS_CREATED=false
SESSION_TOKEN=""
LAUNCHER_SECRET=""
GATEWAY_IP_ISOLATED=""
GATEWAY_IP_EXTERNAL=""

# ---------------------------------------------------------------------------
# Cleanup handler
# ---------------------------------------------------------------------------

cleanup() {
  echo "=== Cleanup ==="
  local exit_code=$?

  # Delete session if we have a token
  if [[ -n "$SESSION_TOKEN" && -n "$GATEWAY_IP_ISOLATED" ]]; then
    echo "Deleting session..."
    curl -sf -X DELETE \
      "http://${GATEWAY_IP_ISOLATED}:${GATEWAY_PORT}/api/v1/sessions/${SESSION_TOKEN}" \
      -H "Authorization: Bearer ${LAUNCHER_SECRET}" \
      2>/dev/null || echo "Session cleanup failed (non-fatal)"
  fi

  # Stop and remove containers
  if [[ "$SANDBOX_STARTED" == "true" ]]; then
    echo "Stopping sandbox container..."
    docker stop -t 10 "$SANDBOX_CONTAINER" 2>/dev/null || true
    docker rm -f "$SANDBOX_CONTAINER" 2>/dev/null || true
  fi

  if [[ "$GATEWAY_STARTED" == "true" ]]; then
    echo "Stopping gateway container..."
    docker stop -t 5 "$GATEWAY_CONTAINER" 2>/dev/null || true
    docker rm -f "$GATEWAY_CONTAINER" 2>/dev/null || true
  fi

  # Remove networks
  if [[ "$NETWORKS_CREATED" == "true" ]]; then
    echo "Removing networks..."
    docker network rm "$ISOLATED_NETWORK" 2>/dev/null || true
    docker network rm "$EXTERNAL_NETWORK" 2>/dev/null || true
  fi

  echo "Cleanup complete (exit code: $exit_code)"
  return "$exit_code"
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helper: find an unused 172.x.0.0/24 subnet
# ---------------------------------------------------------------------------

allocate_subnet() {
  # Collect subnets already in use by Docker into an associative array for O(1) lookups
  local -A used_subnets
  while read -r subnet; do
    [[ -n "$subnet" ]] && used_subnets["$subnet"]=1
  done < <(docker network ls --format '{{.ID}}' | while read -r net_id; do
    docker network inspect "$net_id" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null
  done)

  # Scan 172.28-172.63 for an unused /24
  for major in $(seq 28 63); do
    for minor in $(seq 0 255); do
      local candidate="172.${major}.${minor}.0/24"
      if [[ -z "${used_subnets[$candidate]+x}" ]]; then
        echo "$candidate"
        return 0
      fi
    done
  done

  echo "ERROR: No unused subnet found" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Helper: allocate next available IP from a Docker network
# ---------------------------------------------------------------------------

allocate_container_ip() {
  local network="$1"
  local subnet

  subnet=$(docker network inspect "$network" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')

  # Get assigned IPs
  local assigned_ips
  assigned_ips=$(docker network inspect "$network" \
    --format '{{range .Containers}}{{.IPv4Address}} {{end}}' 2>/dev/null | tr ' ' '\n' | cut -d/ -f1)

  # Parse subnet base (e.g., 172.28.0)
  local base
  base=$(echo "$subnet" | cut -d. -f1-3)

  # .1 is typically the gateway, .2 is reserved for our gateway container
  # Start allocating from .10
  for host in $(seq 10 254); do
    local candidate="${base}.${host}"
    if ! echo "$assigned_ips" | grep -qxF "$candidate"; then
      echo "$candidate"
      return 0
    fi
  done

  echo "ERROR: No available IPs in $network" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Step 1: Pull images
# ---------------------------------------------------------------------------

echo "=== Step 1: Pull images ==="
echo "${INPUT_GITHUB_TOKEN}" | docker login ghcr.io -u "${GITHUB_ACTOR}" --password-stdin
docker pull "$GATEWAY_IMAGE"
docker pull "$SANDBOX_IMAGE"

# ---------------------------------------------------------------------------
# Step 2: Create networks with dynamic subnets
# ---------------------------------------------------------------------------

echo "=== Step 2: Create Docker networks ==="

ISOLATED_SUBNET=$(allocate_subnet)
echo "Isolated network subnet: $ISOLATED_SUBNET"

ISOLATED_BASE=$(echo "$ISOLATED_SUBNET" | cut -d. -f1-3)
GATEWAY_IP_ISOLATED="${ISOLATED_BASE}.2"

# Create isolated network first so the next allocate_subnet sees it as used
docker network create \
  --driver bridge \
  --subnet "$ISOLATED_SUBNET" \
  "$ISOLATED_NETWORK"

# Allocate external subnet (now sees isolated subnet as taken)
EXTERNAL_SUBNET=$(allocate_subnet)
echo "External network subnet: $EXTERNAL_SUBNET"

EXTERNAL_BASE=$(echo "$EXTERNAL_SUBNET" | cut -d. -f1-3)
GATEWAY_IP_EXTERNAL="${EXTERNAL_BASE}.2"

docker network create \
  --driver bridge \
  --subnet "$EXTERNAL_SUBNET" \
  "$EXTERNAL_NETWORK"

NETWORKS_CREATED=true

# ---------------------------------------------------------------------------
# Step 3: Detect mode
# ---------------------------------------------------------------------------

echo "=== Step 3: Detect mode ==="

MODE="${INPUT_MODE:-auto}"

if [[ "$MODE" == "auto" ]]; then
  REPO_VISIBILITY="${GITHUB_EVENT_REPOSITORY_VISIBILITY:-}"
  if [[ -z "$REPO_VISIBILITY" ]]; then
    echo "WARNING: Repository visibility not available in event context, defaulting to public"
    REPO_VISIBILITY="public"
  fi

  case "$REPO_VISIBILITY" in
    private|internal)
      MODE="private"
      ;;
    *)
      MODE="public"
      ;;
  esac
  echo "Auto-detected mode: $MODE (repo visibility: $REPO_VISIBILITY)"
else
  echo "Using configured mode: $MODE"
fi

# ---------------------------------------------------------------------------
# Step 4: Generate config
# ---------------------------------------------------------------------------

echo "=== Step 4: Generate config ==="

# Export inputs for generate-config.sh
export INPUT_ANTHROPIC_OAUTH_TOKEN="${INPUT_ANTHROPIC_OAUTH_TOKEN:?anthropic-oauth-token is required}"
export INPUT_GITHUB_TOKEN="${INPUT_GITHUB_TOKEN:?github-token is required}"
export INPUT_BOT_APP_ID="${INPUT_BOT_APP_ID:-}"
export INPUT_BOT_APP_PRIVATE_KEY="${INPUT_BOT_APP_PRIVATE_KEY:-}"
export INPUT_BOT_APP_INSTALLATION_ID="${INPUT_BOT_APP_INSTALLATION_ID:-}"
export INPUT_BOT_USERNAME="${INPUT_BOT_USERNAME:-egg}"

"$SCRIPT_DIR/generate-config.sh"

CONFIG_DIR="${RUNNER_TEMP:-/tmp}/egg-config-${RUN_ID}"
LAUNCHER_SECRET=$(cat "$CONFIG_DIR/launcher-secret")

# ---------------------------------------------------------------------------
# Step 5: Start gateway container
# ---------------------------------------------------------------------------

echo "=== Step 5: Start gateway ==="

REPO_NAME="${GITHUB_REPOSITORY#*/}"
WORKSPACE="${GITHUB_WORKSPACE:-.}"
WORKTREES_DIR="${RUNNER_TEMP:-/tmp}/egg-worktrees-${RUN_ID}"
STATE_DIR="${RUNNER_TEMP:-/tmp}/egg-state-${RUN_ID}"
CERTS_DIR="${RUNNER_TEMP:-/tmp}/egg-certs-${RUN_ID}"

mkdir -p "$WORKTREES_DIR" "$STATE_DIR" "$CERTS_DIR"

# Start gateway on isolated network first
docker run -d \
  --name "$GATEWAY_CONTAINER" \
  --network "$ISOLATED_NETWORK" \
  --ip "$GATEWAY_IP_ISOLATED" \
  --security-opt label=disable \
  -v "$CONFIG_DIR/repositories.yaml:/config/repositories.yaml:ro" \
  -v "$CONFIG_DIR:${CONTAINER_HOME}/.config/egg:ro" \
  -v "$CONFIG_DIR:/secrets:ro" \
  -v "$WORKSPACE:${CONTAINER_HOME}/repos/${REPO_NAME}" \
  -v "$WORKTREES_DIR:${CONTAINER_HOME}/.egg-worktrees" \
  -v "$STATE_DIR:${CONTAINER_HOME}/.egg-state" \
  -v "$CERTS_DIR:/shared/certs" \
  -e "EGG_REPO_CONFIG=/config/repositories.yaml" \
  -e "HOME=${CONTAINER_HOME}" \
  -e "HOST_UID=$(id -u)" \
  -e "HOST_GID=$(id -g)" \
  -e "GITHUB_USER_TOKEN=${INPUT_GITHUB_TOKEN}" \
  -e "CLAUDE_CODE_OAUTH_TOKEN=${INPUT_ANTHROPIC_OAUTH_TOKEN}" \
  -e "GATEWAY_BOT_NAME=${INPUT_BOT_USERNAME:-egg}" \
  -e "GATEWAY_BOT_BRANCH_PREFIX=${INPUT_BOT_USERNAME:-egg}" \
  -e "EGG_USER_GIT_NAME=${GITHUB_ACTOR}" \
  -e "EGG_USER_GIT_EMAIL=${GITHUB_ACTOR_ID:-0}+${GITHUB_ACTOR}@users.noreply.github.com" \
  ${INPUT_BOT_APP_ID:+-e "GITHUB_APP_ID=${INPUT_BOT_APP_ID}"} \
  ${INPUT_BOT_APP_INSTALLATION_ID:+-e "GITHUB_APP_INSTALLATION_ID=${INPUT_BOT_APP_INSTALLATION_ID}"} \
  "$GATEWAY_IMAGE"

GATEWAY_STARTED=true

# Connect gateway to external network (dual-homed)
echo "Connecting gateway to external network..."
docker network connect \
  --ip "$GATEWAY_IP_EXTERNAL" \
  "$EXTERNAL_NETWORK" \
  "$GATEWAY_CONTAINER"

# ---------------------------------------------------------------------------
# Step 6: Health check
# ---------------------------------------------------------------------------

echo "=== Step 6: Health check ==="

HEALTH_URL="http://${GATEWAY_IP_ISOLATED}:${GATEWAY_PORT}/api/v1/health"
HEALTH_TIMEOUT=60
SECONDS=0

while (( SECONDS < HEALTH_TIMEOUT )); do
  if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
    echo "Gateway healthy after ${SECONDS}s"
    break
  fi
  sleep 1
done

if (( SECONDS >= HEALTH_TIMEOUT )); then
  echo "ERROR: Gateway failed health check after ${HEALTH_TIMEOUT}s"
  echo "Gateway logs:"
  docker logs "$GATEWAY_CONTAINER" 2>&1 | tail -50
  exit 1
fi

# In private mode, also verify that the Squid proxy is ready.
# Claude Code's non-gateway traffic routes through Squid, so it must be
# accepting connections before the sandbox starts. Accept any HTTP response
# (including 403/407) as proof of connectivity.
if [[ "$MODE" == "private" ]]; then
  echo "Checking proxy readiness..."
  SECONDS=0
  while (( SECONDS < 15 )); do
    if curl -sf --proxy "http://${GATEWAY_IP_ISOLATED}:${GATEWAY_PROXY_PORT}" \
         -o /dev/null -w '' https://api.anthropic.com/ 2>/dev/null; then
      echo "Proxy healthy after ${SECONDS}s"
      break
    fi
    # Squid may return 403/407 — any response means it's up
    HTTP_CODE=$(curl -s --proxy "http://${GATEWAY_IP_ISOLATED}:${GATEWAY_PROXY_PORT}" \
                  -o /dev/null -w '%{http_code}' https://api.anthropic.com/ 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" != "000" ]]; then
      echo "Proxy healthy after ${SECONDS}s (HTTP $HTTP_CODE)"
      break
    fi
    sleep 1
  done
  if (( SECONDS >= 15 )); then
    echo "WARNING: Proxy health check timed out — sandbox may experience connectivity issues"
  fi
fi

# ---------------------------------------------------------------------------
# Step 7: Allocate container IP
# ---------------------------------------------------------------------------

echo "=== Step 7: Allocate container IP ==="

if [[ "$MODE" == "private" ]]; then
  SANDBOX_NETWORK="$ISOLATED_NETWORK"
  SANDBOX_GATEWAY_IP="$GATEWAY_IP_ISOLATED"
else
  SANDBOX_NETWORK="$EXTERNAL_NETWORK"
  SANDBOX_GATEWAY_IP="$GATEWAY_IP_EXTERNAL"
fi

CONTAINER_IP=$(allocate_container_ip "$SANDBOX_NETWORK")
echo "Allocated container IP: $CONTAINER_IP on $SANDBOX_NETWORK"

# ---------------------------------------------------------------------------
# Step 8: Create session
# ---------------------------------------------------------------------------

echo "=== Step 8: Create session ==="

SESSION_RESPONSE=$(curl -sf -X POST \
  "http://${GATEWAY_IP_ISOLATED}:${GATEWAY_PORT}/api/v1/sessions/create" \
  -H "Authorization: Bearer ${LAUNCHER_SECRET}" \
  -H "Content-Type: application/json" \
  -d "{
    \"container_id\": \"${SANDBOX_CONTAINER}\",
    \"container_ip\": \"${CONTAINER_IP}\",
    \"mode\": \"${MODE}\",
    \"repos\": [\"${GITHUB_REPOSITORY}\"],
    \"uid\": $(id -u),
    \"gid\": $(id -g)
  }")

SESSION_TOKEN=$(echo "$SESSION_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['data']['session_token'])")
WORKTREE_PATH=$(echo "$SESSION_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)['data']
worktrees = data.get('worktrees', {})
# Get the first (and only) worktree path
for path in worktrees.values():
    print(path)
    break
")

echo "Session token: ${SESSION_TOKEN:0:8}..."
echo "Worktree path: $WORKTREE_PATH"

# ---------------------------------------------------------------------------
# Step 9: Start sandbox container
# ---------------------------------------------------------------------------

echo "=== Step 9: Start sandbox ==="

# Build sandbox docker run command
SANDBOX_CMD=(
  docker run
  --name "$SANDBOX_CONTAINER"
  --network "$SANDBOX_NETWORK"
  --ip "$CONTAINER_IP"
  --security-opt label=disable
  --stop-timeout 30
  --add-host "egg-gateway:${SANDBOX_GATEWAY_IP}"
  -e "RUNTIME_UID=$(id -u)"
  -e "RUNTIME_GID=$(id -g)"
  -e "CONTAINER_ID=${SANDBOX_CONTAINER}"
  -e "EGG_SESSION_TOKEN=${SESSION_TOKEN}"
  -e "GATEWAY_URL=http://egg-gateway:${GATEWAY_PORT}"
  -e "ANTHROPIC_AUTH_METHOD=oauth"
  -e "EGG_QUIET=1"
)

# Mode-specific network settings
if [[ "$MODE" == "private" ]]; then
  SANDBOX_CMD+=(
    --dns 0.0.0.0
    -e "PRIVATE_MODE=true"
    -e "NO_PROXY=localhost,127.0.0.1,egg-gateway"
    -e "no_proxy=localhost,127.0.0.1,egg-gateway"
  )
  # NOTE: We intentionally do NOT pass HTTP_PROXY/HTTPS_PROXY to the sandbox
  # container. The sandbox entrypoint's run_exec() path (used by GHA) does not
  # strip proxy vars before exec'ing the command, unlike run_interactive().
  # If set, Claude Code (Node.js) would route all traffic — including calls to
  # ANTHROPIC_BASE_URL (egg-gateway:9848) — through Squid, which only allows
  # api.anthropic.com. Network isolation is still enforced via --dns 0.0.0.0
  # and the isolated network topology. The proper fix is to add proxy stripping
  # to run_exec() in sandbox/entrypoint.py (tracked separately).
else
  SANDBOX_CMD+=(-e "PRIVATE_MODE=false")
fi

# Mount worktree and shadow .git
SANDBOX_CMD+=(
  -v "${WORKTREE_PATH}:${CONTAINER_HOME}/repos/${REPO_NAME}:rw"
  --mount "type=bind,source=/dev/null,destination=${CONTAINER_HOME}/repos/${REPO_NAME}/.git,readonly"
)

# Mount shared certs for CA trust
SANDBOX_CMD+=(-v "${CERTS_DIR}:/shared/certs:ro")

# Add model configuration
if [[ -n "${MODEL:-}" ]]; then
  SANDBOX_CMD+=(-e "CLAUDE_MODEL=${MODEL}")
fi

# Image and command — run Claude Code in non-interactive exec mode
SANDBOX_CMD+=(
  "$SANDBOX_IMAGE"
  claude
  --dangerously-skip-permissions
  --print
  --verbose
  --output-format stream-json
  --model "$MODEL"
  "$INPUT_PROMPT"
)

# Start timeout watchdog in background
(
  sleep $((TIMEOUT_MINUTES * 60))
  echo "WARNING: Timeout reached (${TIMEOUT_MINUTES}m), stopping sandbox..."
  docker stop -t 30 "$SANDBOX_CONTAINER" 2>/dev/null || true
) &
TIMEOUT_PID=$!

# Run sandbox and capture output
echo "Running Claude Code with prompt: ${INPUT_PROMPT:0:100}..."
SANDBOX_STARTED=true

set +e
"${SANDBOX_CMD[@]}" 2>&1 | tee "$LOG_FILE"
SANDBOX_EXIT_CODE=${PIPESTATUS[0]}
set -e

# Kill timeout watchdog
kill "$TIMEOUT_PID" 2>/dev/null || true
wait "$TIMEOUT_PID" 2>/dev/null || true

echo "Sandbox exited with code: $SANDBOX_EXIT_CODE"

# ---------------------------------------------------------------------------
# Step 10: Capture output
# ---------------------------------------------------------------------------

echo "=== Step 10: Capture output ==="

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
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## egg Run Summary"
    echo ""
    echo "**Exit code:** \`${SANDBOX_EXIT_CODE}\`"
    echo "**Mode:** ${MODE}"
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
