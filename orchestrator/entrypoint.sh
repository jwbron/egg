#!/bin/bash
# egg-orchestrator entrypoint
#
# Starts the orchestrator using the CLI module.
# Uses gosu to drop privileges to the host user's UID/GID so that
# volume-mounted repos and Docker socket are accessible.

set -e

# Configuration from environment
ORCHESTRATOR_MODE="${ORCHESTRATOR_MODE:-serve}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-9849}"
ORCHESTRATOR_HOST="${ORCHESTRATOR_HOST:-0.0.0.0}"
ORCHESTRATOR_DEBUG="${ORCHESTRATOR_DEBUG:-false}"
HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"

echo "egg-orchestrator starting..."
echo "  Mode: $ORCHESTRATOR_MODE"
echo "  Host: $ORCHESTRATOR_HOST"
echo "  Port: $ORCHESTRATOR_PORT"
echo "  Debug: $ORCHESTRATOR_DEBUG"
echo "  UID/GID: $HOST_UID:$HOST_GID"

# Wait for gateway if configured
if [ -n "$WAIT_FOR_GATEWAY" ] && [ "$WAIT_FOR_GATEWAY" = "true" ]; then
    GATEWAY_HOST="${GATEWAY_HOST:-egg-gateway}"
    GATEWAY_PORT="${GATEWAY_PORT:-9848}"
    echo "Waiting for gateway at $GATEWAY_HOST:$GATEWAY_PORT..."

    max_attempts=30
    attempt=0
    while [ $attempt -lt $max_attempts ]; do
        if curl -sf "http://$GATEWAY_HOST:$GATEWAY_PORT/api/v1/health" > /dev/null 2>&1; then
            echo "Gateway is ready"
            break
        fi
        attempt=$((attempt + 1))
        echo "  Waiting for gateway (attempt $attempt/$max_attempts)..."
        sleep 2
    done

    if [ $attempt -eq $max_attempts ]; then
        echo "Warning: Gateway not available after $max_attempts attempts"
    fi
fi

# Build command arguments
CMD_ARGS="serve --host $ORCHESTRATOR_HOST --port $ORCHESTRATOR_PORT"
if [ "$ORCHESTRATOR_DEBUG" = "true" ]; then
    CMD_ARGS="$CMD_ARGS --debug"
fi

# Drop privileges to match host user so volume mounts are accessible
if [ "$(id -u)" = "0" ]; then
    # Ensure the target UID has a passwd entry pointing to /home/egg
    if ! getent passwd "$HOST_UID" > /dev/null 2>&1; then
        getent group "$HOST_GID" > /dev/null 2>&1 || groupadd -g "$HOST_GID" egghost 2>/dev/null || true
        useradd -u "$HOST_UID" -g "$HOST_GID" -d /home/egg -s /bin/bash -M -N egghost 2>/dev/null || true
    fi
    chown "$HOST_UID:$HOST_GID" /home/egg

    # chown Docker volume mount points that are root-owned by default
    for vol_dir in /home/egg/.egg-state; do
        if [ -d "$vol_dir" ]; then
            chown -R "$HOST_UID:$HOST_GID" "$vol_dir"
        fi
    done
    # Chown repo bind-mount points — Docker bind mounts preserve host
    # ownership, so these directories may be root-owned inside the
    # container. Only chown the top-level directories (not recursive) —
    # repo file contents are managed by git/gateway worktree operations.
    if [ -d /home/egg/repos ]; then
        chown "$HOST_UID:$HOST_GID" /home/egg/repos
        for repo_dir in /home/egg/repos/*/; do
            if [ -d "$repo_dir" ]; then
                chown "$HOST_UID:$HOST_GID" "$repo_dir"
            fi
        done
    fi

    # Ensure .egg-state/pipelines dirs inside repos are writable by the orchestrator.
    # These may have been created by git or the gateway under a different UID.
    for repo_dir in /home/egg/repos /home/egg/repos/*/; do
        egg_state="$repo_dir/.egg-state"
        if [ -d "$egg_state" ]; then
            chown -R "$HOST_UID:$HOST_GID" "$egg_state"
        fi
        # Pre-create the pipelines directory so the orchestrator can write to it
        pipelines_dir="$repo_dir/.egg-state/pipelines"
        mkdir -p "$pipelines_dir" 2>/dev/null || true
        chown -R "$HOST_UID:$HOST_GID" "$repo_dir/.egg-state" 2>/dev/null || true
    done

    # Grant Docker socket access to the dropped-privilege user.
    # Change the socket's group to HOST_GID so gosu'd process can use it.
    if [ -S /var/run/docker.sock ]; then
        chgrp "$HOST_GID" /var/run/docker.sock
        chmod g+rw /var/run/docker.sock
        echo "  Docker socket: granted group access to GID $HOST_GID"
    fi

    # Configure git identity for state commits
    gosu "$HOST_UID:$HOST_GID" git config --global user.name "egg-orchestrator"
    gosu "$HOST_UID:$HOST_GID" git config --global user.email "egg@localhost"

    exec gosu "$HOST_UID:$HOST_GID" python -u cli.py $CMD_ARGS
else
    git config --global user.name "egg-orchestrator"
    git config --global user.email "egg@localhost"

    exec python -u cli.py $CMD_ARGS
fi
