#!/bin/bash
# egg-orchestrator entrypoint
#
# Starts the orchestrator using the CLI module.
# Supports multiple startup modes via ORCHESTRATOR_MODE environment variable.

set -e

# Configuration from environment
ORCHESTRATOR_MODE="${ORCHESTRATOR_MODE:-serve}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-9849}"
ORCHESTRATOR_HOST="${ORCHESTRATOR_HOST:-0.0.0.0}"
ORCHESTRATOR_DEBUG="${ORCHESTRATOR_DEBUG:-false}"

echo "egg-orchestrator starting..."
echo "  Mode: $ORCHESTRATOR_MODE"
echo "  Host: $ORCHESTRATOR_HOST"
echo "  Port: $ORCHESTRATOR_PORT"
echo "  Debug: $ORCHESTRATOR_DEBUG"

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

# Run the orchestrator
exec python -u cli.py $CMD_ARGS
