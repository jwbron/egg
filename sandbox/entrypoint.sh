#!/bin/bash
# Egg Sandbox Container Entrypoint
#
# Sets up the sandboxed container environment for the autonomous AI agent.
# This is a simplified version - the full Python entrypoint should be ported
# from james-in-a-box/jib-container/entrypoint.py for production use.

set -e

# Configuration
CONTAINER_USER="${CONTAINER_USER:-egg}"
RUNTIME_UID="${RUNTIME_UID:-1000}"
RUNTIME_GID="${RUNTIME_GID:-1000}"
USER_HOME="/home/egg"

echo "=== Egg Sandbox Container Starting ==="
echo "  User: $CONTAINER_USER (uid=$RUNTIME_UID, gid=$RUNTIME_GID)"

# =============================================================================
# User Setup
# =============================================================================

# Adjust user UID/GID to match host
current_uid=$(id -u egg 2>/dev/null || echo "1000")
current_gid=$(id -g egg 2>/dev/null || echo "1000")

if [ "$current_gid" != "$RUNTIME_GID" ]; then
    echo "Adjusting egg group GID: $current_gid -> $RUNTIME_GID"
    groupmod -g "$RUNTIME_GID" egg
fi

if [ "$current_uid" != "$RUNTIME_UID" ]; then
    echo "Adjusting egg user UID: $current_uid -> $RUNTIME_UID"
    usermod -u "$RUNTIME_UID" egg
    # Fix home directory ownership
    chown -R "$RUNTIME_UID:$RUNTIME_GID" "$USER_HOME"
fi

# =============================================================================
# Environment Setup
# =============================================================================

export HOME="$USER_HOME"
export USER="$CONTAINER_USER"
export PATH="$USER_HOME/.local/bin:/opt/egg-runtime/sandbox/bin:/usr/local/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export DISABLE_AUTOUPDATER=1

# Beads directory
export BEADS_DIR="$USER_HOME/sharing/beads/.beads"

# Git editor - use 'true' (no-op) for non-interactive environment
export GIT_EDITOR=true

# =============================================================================
# Git Configuration
# =============================================================================

gosu "$RUNTIME_UID:$RUNTIME_GID" git config --global user.name "egg"
gosu "$RUNTIME_UID:$RUNTIME_GID" git config --global user.email "egg@localhost"
echo "Git configured to commit as egg <egg@localhost>"

# =============================================================================
# Gateway CA Certificate (if available)
# =============================================================================

GATEWAY_CA_SRC="/shared/certs/gateway-ca.crt"
GATEWAY_CA_DST="/usr/local/share/ca-certificates/gateway-ca.crt"

if [ -f "$GATEWAY_CA_SRC" ]; then
    cp "$GATEWAY_CA_SRC" "$GATEWAY_CA_DST"
    chmod 644 "$GATEWAY_CA_DST"
    update-ca-certificates 2>/dev/null || true
    echo "Gateway CA certificate added to trust store"

    # Configure Python and Node.js to use system CA bundle
    export REQUESTS_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
    export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"
    export NODE_EXTRA_CA_CERTS="$GATEWAY_CA_DST"
fi

# =============================================================================
# Anthropic API Configuration (route through gateway)
# =============================================================================

GATEWAY_URL="${GATEWAY_URL:-http://egg-gateway:9847}"
export ANTHROPIC_BASE_URL="$GATEWAY_URL"

# Placeholder OAuth token for Claude Code's startup validation
# Gateway strips this and injects real credentials
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-PROXY-INJECTED-gateway-handles-real-credential-00000000000000000000000000000000000000000000000000000000000000-000000AAAA"

# Remove any Anthropic API key from container environment
# Credentials are held by gateway only
unset ANTHROPIC_API_KEY

echo "Anthropic API routed through gateway: $GATEWAY_URL"
echo "  Credentials injected by gateway (not in container)"

# =============================================================================
# Claude Code Settings
# =============================================================================

CLAUDE_DIR="$USER_HOME/.claude"
mkdir -p "$CLAUDE_DIR/commands"
mkdir -p "$USER_HOME/.config/claude-code"

# Create settings.json
cat > "$CLAUDE_DIR/settings.json" << 'EOF'
{
  "alwaysThinkingEnabled": true,
  "defaultPermissionMode": "bypassPermissions",
  "autoApproveEdits": true,
  "editorMode": "normal",
  "autoUpdate": false,
  "outputStyle": "default",
  "defaultModel": "opus"
}
EOF

# Ensure user state file exists with onboarding complete
USER_STATE_FILE="$USER_HOME/.claude.json"
if [ ! -f "$USER_STATE_FILE" ]; then
    cat > "$USER_STATE_FILE" << 'EOF'
{
  "hasCompletedOnboarding": true,
  "autoUpdates": false,
  "bypassPermissionsModeAccepted": true,
  "lastOnboardingVersion": "2.0.69",
  "numStartups": 1,
  "installMethod": "api_key"
}
EOF
fi

chown -R "$RUNTIME_UID:$RUNTIME_GID" "$CLAUDE_DIR"
chown "$RUNTIME_UID:$RUNTIME_GID" "$USER_STATE_FILE"
chmod 700 "$CLAUDE_DIR"

echo "Claude settings created: $CLAUDE_DIR/settings.json"

# =============================================================================
# Bash Aliases
# =============================================================================

cat >> "$USER_HOME/.bashrc" << 'EOF'

# Added by egg entrypoint
alias claude='claude --dangerously-skip-permissions'
export PS1='\[\033[01;32m\]\u@sandboxed\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
EOF

chown "$RUNTIME_UID:$RUNTIME_GID" "$USER_HOME/.bashrc"

# =============================================================================
# Sharing Directory Setup
# =============================================================================

SHARING_DIR="$USER_HOME/sharing"
if [ -d "$SHARING_DIR" ]; then
    # Create subdirectories
    for subdir in tmp notifications context tracking traces logs; do
        mkdir -p "$SHARING_DIR/$subdir"
    done
    chown -R "$RUNTIME_UID:$RUNTIME_GID" "$SHARING_DIR"

    # Create convenience symlink
    ln -sf "$SHARING_DIR/tmp" "$USER_HOME/tmp" 2>/dev/null || true

    echo "Shared directories configured"
fi

# =============================================================================
# Beads Setup
# =============================================================================

BEADS_DIR="$SHARING_DIR/beads"
if [ -d "$BEADS_DIR" ]; then
    chown -R "$RUNTIME_UID:$RUNTIME_GID" "$BEADS_DIR"
    ln -sf "$BEADS_DIR" "$USER_HOME/beads" 2>/dev/null || true
    echo "Beads memory system ready"
fi

# =============================================================================
# Gateway Health Check
# =============================================================================

echo "Checking gateway connectivity..."
max_wait=30
elapsed=0
while [ $elapsed -lt $max_wait ]; do
    if curl -s --max-time 2 "$GATEWAY_URL/api/v1/health" >/dev/null 2>&1; then
        echo "Gateway ready!"
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $((elapsed % 5)) -eq 0 ]; then
        echo "  Waiting for gateway... ($elapsed/$max_wait)"
    fi
done

if [ $elapsed -ge $max_wait ]; then
    echo "WARNING: Gateway health check timed out"
    echo "  Container will start but may not be able to reach Claude API"
fi

# =============================================================================
# Launch
# =============================================================================

echo ""
echo "=== Egg Sandbox Ready ==="

# Change to repos directory if it exists
if [ -d "$USER_HOME/repos" ]; then
    cd "$USER_HOME/repos"
else
    cd "$USER_HOME"
fi

# Execute command or start interactive shell
if [ $# -eq 0 ]; then
    echo "Starting interactive shell..."
    exec gosu "$RUNTIME_UID:$RUNTIME_GID" /bin/bash
else
    exec gosu "$RUNTIME_UID:$RUNTIME_GID" "$@"
fi
