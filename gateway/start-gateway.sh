#!/bin/bash
# Dynamic startup script for gateway
# Generates container mounts at startup rather than relying on stale config files
#
# This script runs in network lockdown mode with dual network architecture:
# - egg-isolated: Internal network for egg container (no external route)
# - egg-external: Gateway's external network for filtered internet access
#
# All network traffic from egg container is routed through Squid proxy for filtering.

set -e

# Get home directory (works with systemd %h substitution)
HOME_DIR="${HOME:-$(eval echo ~)}"

# Container paths - must match what egg containers use (fixed /home/egg user)
# This is critical for path consistency between egg containers and gateway
CONTAINER_HOME="/home/egg"

# Network names and IPs for lockdown mode
ISOLATED_NETWORK="egg-isolated"
EXTERNAL_NETWORK="egg-external"
GATEWAY_ISOLATED_IP="172.32.0.2"
GATEWAY_EXTERNAL_IP="172.33.0.2"

# Load secrets from secrets.env if it exists
# This file contains sensitive environment variables like GITHUB_USER_TOKEN
SECRETS_ENV_FILE="$HOME_DIR/.config/egg/secrets.env"
if [ -f "$SECRETS_ENV_FILE" ]; then
    set -a  # Automatically export all variables
    # shellcheck source=/dev/null
    source "$SECRETS_ENV_FILE"
    set +a
fi

# Note: PRIVATE_MODE env file is no longer used for gateway configuration.
# Gateway always runs with locked-down Squid (PRIVATE_MODE=true).
# Per-container mode determines whether containers use the proxy or not.

CONFIG_FILE="$HOME_DIR/.config/egg/repositories.yaml"
CONFIG_DIR="$HOME_DIR/.config/egg"
REPOS_DIR="$HOME_DIR/repos"
WORKTREES_DIR="$HOME_DIR/.egg-worktrees"
STATE_DIR="$HOME_DIR/.egg-state"
GIT_MAIN_DIR="$HOME_DIR/.git-main"
LOCAL_OBJECTS_DIR="$HOME_DIR/.egg-local-objects"
SHARED_CERTS_DIR="$HOME_DIR/.egg-shared-certs"

# Verify required files exist
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Configuration file not found: $CONFIG_FILE" >&2
    echo "Run 'egg --setup' to create the configuration." >&2
    exit 1
fi

if [ ! -d "$CONFIG_DIR" ]; then
    echo "ERROR: Config directory not found: $CONFIG_DIR" >&2
    echo "Run 'egg --setup' to create it." >&2
    exit 1
fi

# Verify launcher secret exists
if [ ! -f "$CONFIG_DIR/launcher-secret" ]; then
    echo "ERROR: Launcher secret not found: $CONFIG_DIR/launcher-secret" >&2
    echo "Run 'egg --setup' or gateway/setup.sh to generate it." >&2
    exit 1
fi

# Build mount arguments as an array (handles paths with spaces safely)
MOUNTS=()

# Config file mount (required for repo_config.py)
MOUNTS+=(-v "$CONFIG_FILE:/config/repositories.yaml:ro")

# EGG config directory (contains secrets.env, github-app.pem, launcher-secret)
# Mount at /home/egg/.config/egg since gateway drops to UID 1000 with HOME=/home/egg
# Also mount at /secrets for backward compatibility with gateway code
MOUNTS+=(-v "$CONFIG_DIR:$CONTAINER_HOME/.config/egg:ro")
MOUNTS+=(-v "$CONFIG_DIR:/secrets:ro")

# Repos directory - mount at /home/egg/repos to match container paths
# Needs RW for git worktree add (writes to .git/worktrees/)
# Note: --security-opt label=disable handles SELinux (no :z needed)
if [ -d "$REPOS_DIR" ]; then
    MOUNTS+=(-v "$REPOS_DIR:$CONTAINER_HOME/repos")
fi

# Worktrees directory - mount at /home/egg/.egg-worktrees
# Needs RW for creating per-container worktrees
mkdir -p "$WORKTREES_DIR"
MOUNTS+=(-v "$WORKTREES_DIR:$CONTAINER_HOME/.egg-worktrees")

# State directory - mount at /home/egg/.egg-state
# Persists session data across gateway container restarts
mkdir -p "$STATE_DIR"
MOUNTS+=(-v "$STATE_DIR:$CONTAINER_HOME/.egg-state")

# Git main directory - mount at /home/egg/.git-main
# Needs RW for git fetch (FETCH_HEAD, refs) and object sync after push
if [ -d "$GIT_MAIN_DIR" ]; then
    MOUNTS+=(-v "$GIT_MAIN_DIR:$CONTAINER_HOME/.git-main")
fi

# Local objects directory - mount at /home/egg/.egg-local-objects
# Used to read container-created objects for sync to shared store
if [ -d "$LOCAL_OBJECTS_DIR" ]; then
    MOUNTS+=(-v "$LOCAL_OBJECTS_DIR:$CONTAINER_HOME/.egg-local-objects:ro")
fi

# Shared certs directory - used for SSL bump CA certificate sharing
# Gateway writes CA cert here, egg containers read it for trust store setup
# This enables credential injection via gateway proxy
mkdir -p "$SHARED_CERTS_DIR"
chmod 755 "$SHARED_CERTS_DIR"
MOUNTS+=(-v "$SHARED_CERTS_DIR:/shared/certs")

# Dynamic git mounts from local_repos in repositories.yaml
# Parse local_repos.paths from YAML and generate git directory mounts
# NOTE: We pass CONTAINER_HOME as the destination path base so mounts match
# what egg containers expect (fixed /home/egg user since PR #538)
SCRIPT_DIR="$(dirname "$0")"
if command -v python3 &> /dev/null; then
    GIT_MOUNTS_OUTPUT=$(python3 "$SCRIPT_DIR/parse-git-mounts.py" "$CONFIG_FILE" "$CONTAINER_HOME" 2>&1) || true

    # Parse output line by line (handles paths with spaces)
    while IFS= read -r mount_spec; do
        # Skip warning lines (sent to stderr but captured due to 2>&1)
        if [[ "$mount_spec" == Warning:* ]]; then
            echo "$mount_spec" >&2
            continue
        fi
        # Skip empty lines
        if [ -n "$mount_spec" ]; then
            MOUNTS+=(-v "$mount_spec")
        fi
    done <<< "$GIT_MOUNTS_OUTPUT"
fi

# Build environment variable arguments
ENV_ARGS=(-e EGG_REPO_CONFIG=/config/repositories.yaml)

# Set HOME for the gateway process so Path.home() resolves correctly
# This is needed for token_refresher.py to find ~/.config/egg/
ENV_ARGS+=(-e "HOME=$CONTAINER_HOME")

# Pass host home directory for path translation in API responses
# The gateway runs with CONTAINER_HOME=/home/egg but needs to return
# host paths to the egg launcher for Docker mount sources
ENV_ARGS+=(-e "HOST_HOME=$HOME_DIR")

# Pass host UID/GID for privilege dropping
# Container starts as root (Squid needs this), then drops to host user for Python gateway
ENV_ARGS+=(-e "HOST_UID=$(id -u)")
ENV_ARGS+=(-e "HOST_GID=$(id -g)")

# Gateway always runs with locked-down Squid.
# Only private containers route through the proxy; public containers bypass it.
# This allows private and public containers to run simultaneously.
# Note: PRIVATE_MODE env var is no longer used - mode is per-container via sessions

# Pass user token if configured (for personal GitHub account attribution)
if [ -n "${GITHUB_USER_TOKEN:-}" ]; then
    ENV_ARGS+=(-e "GITHUB_USER_TOKEN=$GITHUB_USER_TOKEN")
fi

# Pass gateway configuration environment variables from secrets.env
# These are required for policy enforcement (bot identity, branch prefixes, trusted users)
if [ -n "${GATEWAY_BOT_NAME:-}" ]; then
    ENV_ARGS+=(-e "GATEWAY_BOT_NAME=$GATEWAY_BOT_NAME")
fi
if [ -n "${GATEWAY_BOT_BRANCH_PREFIX:-}" ]; then
    ENV_ARGS+=(-e "GATEWAY_BOT_BRANCH_PREFIX=$GATEWAY_BOT_BRANCH_PREFIX")
fi
if [ -n "${GATEWAY_TRUSTED_USERS:-}" ]; then
    ENV_ARGS+=(-e "GATEWAY_TRUSTED_USERS=$GATEWAY_TRUSTED_USERS")
fi

# Extract git identity from user_mode config in repositories.yaml
# This is used for git commits in user mode repos
if command -v python3 &> /dev/null; then
    USER_GIT_NAME=$(python3 -c "
import yaml
try:
    with open('$CONFIG_FILE') as f:
        config = yaml.safe_load(f)
    print(config.get('user_mode', {}).get('git_name', ''))
except:
    pass
" 2>/dev/null)
    USER_GIT_EMAIL=$(python3 -c "
import yaml
try:
    with open('$CONFIG_FILE') as f:
        config = yaml.safe_load(f)
    print(config.get('user_mode', {}).get('git_email', ''))
except:
    pass
" 2>/dev/null)

    if [ -n "$USER_GIT_NAME" ]; then
        ENV_ARGS+=(-e "EGG_USER_GIT_NAME=$USER_GIT_NAME")
    fi
    if [ -n "$USER_GIT_EMAIL" ]; then
        ENV_ARGS+=(-e "EGG_USER_GIT_EMAIL=$USER_GIT_EMAIL")
    fi
fi

# =============================================================================
# Main Execution
# =============================================================================

# Gateway always runs in locked mode - per-container mode is set at container start
MODE_DISPLAY="LOCKED (Squid locked to api.anthropic.com, per-container mode via network)"

echo "=== Gateway Sidecar Startup ==="
echo "Configuration:"
echo "  Squid mode: $MODE_DISPLAY"
echo "  Networks: $ISOLATED_NETWORK (internal) + $EXTERNAL_NETWORK (external)"
echo "  Gateway IPs: $GATEWAY_ISOLATED_IP (isolated), $GATEWAY_EXTERNAL_IP (external)"
echo "  egg containers: Dynamic IPs from 172.32.0.0/24 subnet"
echo "  API port: 9848"
echo "  Proxy port: 3129"
echo ""

# Verify networks exist
if ! docker network inspect "$ISOLATED_NETWORK" &>/dev/null; then
    echo "ERROR: $ISOLATED_NETWORK network not found" >&2
    echo "Run create-networks.sh first to set up the required networks" >&2
    exit 1
fi
if ! docker network inspect "$EXTERNAL_NETWORK" &>/dev/null; then
    echo "ERROR: $EXTERNAL_NETWORK network not found" >&2
    echo "Run create-networks.sh first to set up the required networks" >&2
    exit 1
fi

# Remove existing gateway container if present
docker rm -f egg-gateway 2>/dev/null || true

# Start gateway on isolated network first (with fixed IP)
# Note: No --user flag - Squid needs to start as root to read its certificate,
# then drops privileges to proxy user. This is standard Squid operation.
echo "Starting gateway container on $ISOLATED_NETWORK..."
docker run -d \  # noqa: EGG100 - gateway container startup from host launcher
    --name egg-gateway \
    --network "$ISOLATED_NETWORK" \
    --ip "$GATEWAY_ISOLATED_IP" \
    --security-opt label=disable \
    -p 9848:9848 \
    -p 3129:3129 \
    "${ENV_ARGS[@]}" \
    "${MOUNTS[@]}" \
    egg-gateway

# Connect to external network (dual-homed)
echo "Connecting gateway to $EXTERNAL_NETWORK..."
docker network connect --ip "$GATEWAY_EXTERNAL_IP" "$EXTERNAL_NETWORK" egg-gateway

# Wait for gateway to be fully ready on both networks
# This prevents race conditions where Squid cannot resolve DNS
# for allowed domains during the window between container start
# and external network connection
echo "Waiting for gateway readiness..."
max_wait=30
elapsed=0
while [ $elapsed -lt $max_wait ]; do
    health_output=$(curl -s --max-time 2 "http://localhost:9848/api/v1/health" 2>&1) && {
        echo "Gateway health check passed"
        echo "  Response: $health_output"
        break
    }
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $((elapsed % 5)) -eq 0 ]; then
        echo "  Waiting for gateway API... ($elapsed/$max_wait)"
        # Check if container is still running
        if ! docker ps -q -f name=egg-gateway | grep -q .; then
            echo "ERROR: egg-gateway container stopped unexpectedly" >&2
            docker logs --tail 20 egg-gateway 2>/dev/null || true
            exit 1
        fi
    fi
done

if [ $elapsed -ge $max_wait ]; then
    echo "WARNING: Gateway health check timed out after $max_wait seconds"
    echo "Gateway may not be fully ready. Check logs for errors."
    echo ""
    echo "Recent gateway logs:"
    docker logs --tail 30 egg-gateway 2>&1 || true
    echo ""
    echo "Gateway network configuration:"
    docker inspect egg-gateway --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} ({{.NetworkID | printf "%.12s"}}){{"\n"}}{{end}}' 2>/dev/null || true
fi

echo "Gateway started in lockdown mode (dual-homed)"
echo ""

# Show actual network configuration for verification
echo "Gateway network verification:"
gateway_isolated_ip=$(docker inspect egg-gateway --format '{{with index .NetworkSettings.Networks "egg-isolated"}}{{.IPAddress}}{{end}}' 2>/dev/null)
gateway_external_ip=$(docker inspect egg-gateway --format '{{with index .NetworkSettings.Networks "egg-external"}}{{.IPAddress}}{{end}}' 2>/dev/null)
echo "  egg-isolated IP: ${gateway_isolated_ip:-NOT CONNECTED}"
echo "  egg-external IP: ${gateway_external_ip:-NOT CONNECTED}"

if [ "$gateway_isolated_ip" != "$GATEWAY_ISOLATED_IP" ]; then
    echo "  WARNING: Expected isolated IP $GATEWAY_ISOLATED_IP but got $gateway_isolated_ip"
fi
if [ "$gateway_external_ip" != "$GATEWAY_EXTERNAL_IP" ]; then
    echo "  WARNING: Expected external IP $GATEWAY_EXTERNAL_IP but got $gateway_external_ip"
fi

echo ""
echo "Container topology:"
echo "  egg containers (172.32.0.x) -> gateway (${gateway_isolated_ip:-172.32.0.2}:3129) -> Internet (allowlisted)"
echo ""
echo "To test connectivity from host:"
echo "  curl http://localhost:9848/api/v1/health"
echo "  curl -x http://localhost:3129 https://api.anthropic.com/"
echo ""

# Follow logs (similar to exec behavior)
exec docker logs -f egg-gateway
