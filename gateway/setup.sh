#!/bin/bash
# Setup script for Gateway Sidecar
#
# ============================================================================
# DEPRECATION NOTICE
# ============================================================================
# This script is DEPRECATED. Gateway lifecycle is now managed automatically
# by the `egg` binary. When you run `egg`, the gateway will:
#   - Start automatically if not running
#   - Rebuild if source files have changed (hash-based detection)
#   - Create required Docker networks automatically
#
# This script is kept for manual debugging and backward compatibility only.
# To migrate from systemd-managed gateway, simply run `egg` - it will
# automatically stop the systemd service and take over management.
#
# For manual gateway operations, use:
#   ./gateway/start-gateway.sh    # Start gateway manually
#   docker logs egg-gateway       # View logs
#   docker rm -f egg-gateway      # Stop gateway
# ============================================================================
#
# Builds the gateway Docker image and installs a systemd service to manage it.
set -e

echo ""
echo "============================================================================"
echo "DEPRECATION NOTICE"
echo "============================================================================"
echo "This script is deprecated. Gateway lifecycle is now managed automatically"
echo "by the 'egg' binary. Simply run 'egg' and the gateway will start."
echo ""
echo "If you have an existing systemd setup, it will be automatically migrated."
echo "============================================================================"
echo ""
read -p "Continue with legacy systemd setup anyway? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled. Run 'egg' to use the new automatic gateway management."
    exit 0
fi
echo ""

COMPONENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${COMPONENT_DIR}/.." && pwd)"
SERVICE_NAME="egg-gateway.service"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
CONFIG_DIR="${HOME}/.config/egg"
GATEWAY_IMAGE_NAME="egg-gateway"
# Network lockdown requires dual networks created by create-networks.sh
ISOLATED_NETWORK="egg-isolated"
EXTERNAL_NETWORK="egg-external"
MOUNTS_ENV_FILE="${CONFIG_DIR}/gateway-mounts.env"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            echo "Usage: $0"
            echo ""
            echo "Builds the gateway Docker image and installs a systemd service to manage it."
            echo ""
            echo "Prerequisites:"
            echo "  - Docker must be installed and running"
            echo "  - GitHub App credentials configured in ~/.config/egg/"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run '$0 --help' for usage"
            exit 1
            ;;
    esac
done

echo "Setting up Gateway Sidecar..."
echo ""

# Common setup: directories and secrets
ensure_directories() {
    mkdir -p "$CONFIG_DIR"
    echo "Config directory exists: $CONFIG_DIR"

    # Create shared certs directory for SSL bump CA certificate
    mkdir -p "${HOME}/.egg-shared-certs"
    echo "Shared certs directory exists: ${HOME}/.egg-shared-certs"

    # Create worktrees directory for gateway-managed git worktrees
    # This directory must be owned by the current user for the gateway to create
    # per-container worktree subdirectories
    WORKTREES_DIR="${HOME}/.egg-worktrees"
    if [[ ! -d "$WORKTREES_DIR" ]]; then
        mkdir -p "$WORKTREES_DIR"
        echo "Worktrees directory created: $WORKTREES_DIR"
    else
        # Fix ownership if directory exists but is owned by root (common issue)
        if [[ "$(stat -c '%U' "$WORKTREES_DIR" 2>/dev/null)" == "root" ]]; then
            echo "Fixing worktrees directory ownership (currently owned by root)..."
            if sudo -n chown -R "$(id -u):$(id -g)" "$WORKTREES_DIR" 2>/dev/null; then
                echo "Worktrees directory ownership fixed: $WORKTREES_DIR"
            else
                echo "WARNING: Could not fix worktrees directory ownership."
                echo "  Run: sudo chown -R \$(id -u):\$(id -g) $WORKTREES_DIR"
            fi
        else
            echo "Worktrees directory exists: $WORKTREES_DIR"
        fi
    fi
}

generate_launcher_secret() {
    # Generate launcher secret for session management
    # This authenticates the egg launcher when registering sessions
    LAUNCHER_SECRET_FILE="${CONFIG_DIR}/launcher-secret"
    if [[ ! -f "$LAUNCHER_SECRET_FILE" ]]; then
        echo "Generating launcher secret..."
        python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$LAUNCHER_SECRET_FILE"
        chmod 600 "$LAUNCHER_SECRET_FILE"
        echo "Launcher secret generated: $LAUNCHER_SECRET_FILE"
    else
        echo "Launcher secret exists: $LAUNCHER_SECRET_FILE"
    fi
    # Note: Launcher secret is NOT shared with egg containers - containers use
    # session tokens (EGG_SESSION_TOKEN), not the launcher secret.
    # Only the launcher process on the host needs access to register sessions.
}

# Check prerequisites
check_prerequisites() {
    # Check GitHub App credentials
    # - App ID and Installation ID should be in secrets.env
    # - Private key should be in github-app.pem
    SECRETS_FILE="${CONFIG_DIR}/secrets.env"
    PRIVATE_KEY_FILE="${CONFIG_DIR}/github-app.pem"

    # Check if secrets.env exists and has required values
    HAS_APP_ID=false
    HAS_INSTALL_ID=false
    if [[ -f "$SECRETS_FILE" ]]; then
        if grep -q "^GITHUB_APP_ID=" "$SECRETS_FILE"; then
            HAS_APP_ID=true
        fi
        if grep -q "^GITHUB_APP_INSTALLATION_ID=" "$SECRETS_FILE"; then
            HAS_INSTALL_ID=true
        fi
    fi

    if [[ "$HAS_APP_ID" != "true" ]] || [[ "$HAS_INSTALL_ID" != "true" ]] || [[ ! -f "$PRIVATE_KEY_FILE" ]]; then
        echo "WARNING: GitHub App credentials not fully configured."
        echo ""
        echo "Expected in $CONFIG_DIR/:"
        echo "  - secrets.env with GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID"
        echo "  - github-app.pem (private key)"
        echo ""
        echo "Run 'egg --setup' to configure credentials"
        echo ""
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Setup cancelled."
            exit 1
        fi
    else
        echo "GitHub App credentials found in: $CONFIG_DIR"
    fi

    # Check Docker is available
    if ! command -v docker &> /dev/null; then
        echo "ERROR: docker is required but not installed."
        exit 1
    fi

    # Check Dockerfile exists
    DOCKERFILE="${COMPONENT_DIR}/Dockerfile"
    if [[ ! -f "$DOCKERFILE" ]]; then
        echo "ERROR: Dockerfile not found at $DOCKERFILE"
        exit 1
    fi
}

# Build Docker image
build_image() {
    DOCKERFILE="${COMPONENT_DIR}/Dockerfile"

    echo ""
    echo "Building gateway container image..."
    echo "  Image: $GATEWAY_IMAGE_NAME"
    echo "  Dockerfile: $DOCKERFILE"
    echo "  Context: $REPO_ROOT"
    echo ""
    docker build -t "$GATEWAY_IMAGE_NAME" -f "$DOCKERFILE" "$REPO_ROOT"

    echo ""
    echo "Gateway image built successfully!"
}

# Create Docker networks for network lockdown mode
create_networks() {
    # Run the network creation script which sets up the dual-network architecture:
    # - egg-isolated: Internal network (no external route) for egg containers
    # - egg-external: External network for gateway internet access
    echo "Setting up network lockdown architecture..."
    "$COMPONENT_DIR/create-networks.sh"
}

# Generate environment file with dynamic mounts
generate_mounts_env() {
    echo "Generating dynamic mount configuration..."

    # Build .git-main mounts for each repo
    # Worktree .git files point to ~/.git-main/<repo>/worktrees/<name>
    # so gateway needs the same mounts that egg containers use
    GIT_MOUNTS=""
    SHARED_DIR="${REPO_ROOT}/shared"

    while IFS= read -r repo_path; do
        if [ -n "$repo_path" ] && [ -d "$repo_path" ]; then
            repo_name=$(basename "$repo_path")
            git_dir="${repo_path}/.git"

            # Mount .git directory at ~/.git-main/<repo> for worktree resolution
            if [ -d "$git_dir" ]; then
                GIT_MOUNTS="${GIT_MOUNTS} -v ${git_dir}:${HOME}/.git-main/${repo_name}:ro,z"
                echo "  Will mount .git for: $repo_name"
            fi
        fi
    done < <(PYTHONPATH="${SHARED_DIR}:${PYTHONPATH}" python3 -m egg_config.config 2>/dev/null)

    # Write environment file for systemd
    echo "GIT_MOUNTS=${GIT_MOUNTS}" > "$MOUNTS_ENV_FILE"
    chmod 600 "$MOUNTS_ENV_FILE"
    echo "Mount configuration written to: $MOUNTS_ENV_FILE"
}

# Install and start systemd service
install_service() {
    # Verify mounts env file exists (required by systemd service)
    if [[ ! -f "$MOUNTS_ENV_FILE" ]]; then
        echo "ERROR: Mounts environment file not found at $MOUNTS_ENV_FILE"
        echo "This file is required by the systemd service for git worktree resolution."
        echo "Run generate_mounts_env or re-run this setup script."
        exit 1
    fi

    # Stop existing service if running
    if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
        echo "Stopping existing gateway service..."
        systemctl --user stop "$SERVICE_NAME"
    fi

    # Remove any existing container
    if docker container inspect egg-gateway &>/dev/null; then
        echo "Removing existing gateway container..."
        docker rm -f egg-gateway >/dev/null
    fi

    # Generate service file with correct paths
    mkdir -p "$SYSTEMD_DIR"
    cat > "$SYSTEMD_DIR/$SERVICE_NAME" << EOF
[Unit]
Description=Egg Gateway Sidecar - Git/GitHub policy enforcement for egg containers
Documentation=file://${REPO_ROOT}/gateway/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
Environment=CONTAINER_NAME=egg-gateway
Environment=IMAGE_NAME=egg-gateway

# Remove any stopped container before starting
ExecStartPre=-/usr/bin/docker rm -f egg-gateway

# Run container via startup script
ExecStart=${COMPONENT_DIR}/start-gateway.sh

ExecStop=/usr/bin/docker stop egg-gateway

# Restart on failure with exponential backoff
Restart=on-failure
RestartSec=10s

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=egg-gateway

[Install]
WantedBy=default.target
EOF
    echo "Service file generated at $SYSTEMD_DIR/$SERVICE_NAME"

    # Reload systemd
    systemctl --user daemon-reload
    echo "Systemd daemon reloaded"

    # Enable service
    systemctl --user enable "$SERVICE_NAME"
    echo "Service enabled"

    # Start service
    systemctl --user start "$SERVICE_NAME"
    echo "Service started"

    # Wait for service to be ready
    echo ""
    echo "Waiting for gateway to be ready..."
    sleep 3

    # Health check with retry
    HEALTH_URL="http://localhost:9848/api/v1/health"
    MAX_RETRIES=5
    RETRY_DELAY=2

    for i in $(seq 1 $MAX_RETRIES); do
        if curl -s "$HEALTH_URL" | grep -q '"status"'; then
            echo ""
            echo "Gateway is running!"
            echo ""
            curl -s "$HEALTH_URL" | python3 -m json.tool
            echo ""
            echo "Service status:"
            systemctl --user status "$SERVICE_NAME" --no-pager || true
            return 0
        fi

        # Check if service is still running or has failed
        if ! systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
            echo ""
            echo "ERROR: Gateway service failed to start."
            echo ""
            echo "Service status:"
            systemctl --user status "$SERVICE_NAME" --no-pager || true
            echo ""
            echo "Recent logs:"
            journalctl --user -u "$SERVICE_NAME" -n 20 --no-pager
            echo ""
            echo "Fix the issue and run: systemctl --user restart $SERVICE_NAME"
            return 1
        fi

        echo "Waiting for gateway... (attempt $i/$MAX_RETRIES)"
        sleep $RETRY_DELAY
    done

    echo ""
    echo "ERROR: Gateway health check failed after $MAX_RETRIES attempts."
    echo ""
    echo "Service status:"
    systemctl --user status "$SERVICE_NAME" --no-pager || true
    echo ""
    echo "Check logs: journalctl --user -u $SERVICE_NAME -f"
    return 1
}

print_summary() {
    echo ""
    echo "Setup complete!"
    echo ""
    echo "The gateway sidecar:"
    echo "  - Runs as Docker container managed by systemd"
    echo "  - Listens on http://localhost:9848"
    echo "  - Network lockdown mode: dual-homed on $ISOLATED_NETWORK + $EXTERNAL_NETWORK"
    echo "  - Enforces branch/PR ownership policies"
    echo "  - Blocks merge operations (human must merge via GitHub UI)"
    echo ""
    echo "The 'egg' command will use this gateway automatically."
    echo ""
    echo "Private Mode (optional):"
    echo "  Controls BOTH network access AND repository visibility:"
    echo "  - PRIVATE_MODE=true:  Private repos + locked network (Anthropic API only)"
    echo "  - PRIVATE_MODE=false: Public repos + full internet (default)"
    echo "  To enable private mode, add to ~/.config/egg/network.env:"
    echo "     PRIVATE_MODE=true"
    echo ""
    echo "  Additional cache configuration (optional):"
    echo "     VISIBILITY_CACHE_TTL_READ=60    # Cache TTL for read operations (seconds)"
    echo "     VISIBILITY_CACHE_TTL_WRITE=0    # Cache TTL for write operations (0 = always verify)"
    echo ""
    echo "  Restart the gateway after changes: systemctl --user restart $SERVICE_NAME"
    echo ""
    echo "User mode (optional):"
    echo "  To use a personal GitHub account instead of the bot:"
    echo "  1. Create ~/.config/egg/secrets.env with:"
    echo "     GITHUB_USER_TOKEN=ghp_your_personal_access_token"
    echo "  2. Configure user_mode.github_user in ~/.config/egg/repositories.yaml"
    echo "  3. Restart the gateway: systemctl --user restart $SERVICE_NAME"
    echo ""
    echo "Useful commands:"
    echo "  systemctl --user status $SERVICE_NAME    # Check status"
    echo "  systemctl --user restart $SERVICE_NAME   # Restart service"
    echo "  systemctl --user stop $SERVICE_NAME      # Stop service"
    echo "  journalctl --user -u $SERVICE_NAME -f    # View logs"
    echo "  docker logs egg-gateway                  # View container logs"
    echo "  curl http://localhost:9848/api/v1/health # Health check"
}

# Main execution
ensure_directories
generate_launcher_secret
check_prerequisites
build_image
create_networks
generate_mounts_env
install_service
print_summary
