#!/bin/bash
set -e

# =============================================================================
# Gateway Sidecar Entrypoint
#
# Starts the gateway API server and Squid proxy for network filtering.
#
# The gateway always runs with locked-down Squid (allows only api.anthropic.com).
# Per-container mode is enforced at the container level:
# - Private containers: Use isolated network + route through this proxy
# - Public containers: Use external network + bypass proxy (direct internet)
#
# This allows private and public containers to run simultaneously without
# gateway restarts.
# =============================================================================

echo "=== Egg Gateway Sidecar Starting (Per-Container Mode Architecture) ==="
echo "  Squid: Locked (api.anthropic.com only)"
echo "  Private containers: Use proxy on isolated network"
echo "  Public containers: Bypass proxy on external network"
echo ""

# Always use locked-down Squid (only private containers route through it)
# Note: PRIVATE_MODE env var is no longer used - mode is per-container via sessions
SQUID_CONF="/etc/squid/squid.conf"

# Note: GitHub tokens are now managed in-memory by token_refresher.py
# We only need to verify the launcher secret is mounted
if [ ! -f "/secrets/launcher-secret" ]; then
    echo "ERROR: /secrets/launcher-secret not mounted"
    exit 1
fi

# Export launcher secret for authentication
export EGG_LAUNCHER_SECRET=$(cat /secrets/launcher-secret)

# =============================================================================
# Start Squid Proxy for Network Filtering
# =============================================================================

echo "Starting Squid proxy for network filtering..."
echo "Using config: $SQUID_CONF"

# Ensure log and spool directories exist and are writable
# Note: We may not have permission to chown (running as non-root), so we
# check writability directly and configure Squid to run with current user.
mkdir -p /var/log/squid /var/spool/squid

# Try to set ownership for squid's preferred user, but don't fail if we can't
if chown -R proxy:proxy /var/log/squid /var/spool/squid 2>/dev/null; then
    echo "  Log directories owned by proxy:proxy"
else
    # Running as non-root - verify directories are writable
    if [ -w /var/log/squid ] && [ -w /var/spool/squid ]; then
        echo "  Log directories writable by current user ($(id -un))"
    else
        echo "WARNING: Log directories may not be writable - Squid logging may fail"
    fi
fi

# Initialize cache directories if needed
if [ ! -d "/var/spool/squid/00" ]; then
    /usr/sbin/squid -z -N 2>/dev/null || true
fi

# Verify Squid configuration exists
if [ ! -f "$SQUID_CONF" ]; then
    echo "ERROR: Squid configuration not found: $SQUID_CONF"
    exit 1
fi
# Only check allowed_domains.txt in lockdown mode (not used in allow-all mode)
if [ "$SQUID_CONF" = "/etc/squid/squid.conf" ] && [ ! -f "/etc/squid/allowed_domains.txt" ]; then
    echo "ERROR: Allowed domains file not found: /etc/squid/allowed_domains.txt"
    exit 1
fi

# Start Squid in daemon mode
/usr/sbin/squid -f "$SQUID_CONF"

# Wait for Squid to start
elapsed=0
max_wait=30
while [ $elapsed -lt $max_wait ]; do
    if /usr/sbin/squid -k check 2>/dev/null; then
        echo "Squid proxy started successfully on port 3128"
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
    echo "Waiting for Squid to start... ($elapsed/$max_wait)"
done

if [ $elapsed -ge $max_wait ]; then
    echo "ERROR: Squid failed to start within $max_wait seconds"
    cat /var/log/squid/cache.log 2>/dev/null || true
    exit 1
fi

# =============================================================================
# Start Gateway API Server
# =============================================================================

echo "Starting gateway API server on port 9847..."

# Run gateway on all interfaces (for container networking)
# Use exec to replace shell process with Python for proper signal handling
#
# If HOST_UID/HOST_GID are set, drop privileges using gosu before starting
# the Python gateway. This is required because:
# - Container starts as root so Squid can read its certificate
# - Gateway Python code must run as host user to avoid root-owned git files
if [ -n "${HOST_UID:-}" ] && [ -n "${HOST_GID:-}" ] && [ "$(id -u)" = "0" ]; then
    echo "Dropping privileges to UID=$HOST_UID GID=$HOST_GID"
    # Explicitly set HOME before gosu (consistent with sandbox container entrypoint)
    # This ensures Path.home() resolves correctly in token_refresher.py
    export HOME=/home/sandbox
    exec gosu "$HOST_UID:$HOST_GID" python3 -m gateway.gateway --host 0.0.0.0 --port 9847
else
    exec python3 -m gateway.gateway --host 0.0.0.0 --port 9847
fi
