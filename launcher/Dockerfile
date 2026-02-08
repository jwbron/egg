# egg-launcher: Single-container deployment for egg
#
# This container orchestrates the full egg stack by creating gateway and sandbox
# containers on the host Docker daemon. It provides a simplified deployment
# experience while maintaining the security properties of the two-container model.
#
# Usage:
#   docker run -it \
#     -v ~/.config/egg:/config:ro \
#     -v ~/repos:/repos \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     ghcr.io/jwbron/egg-launcher:latest
#
# The launcher:
# 1. Creates egg-isolated and egg-external networks on the host
# 2. Starts the gateway container (dual-homed)
# 3. Waits for gateway health check
# 4. Starts the sandbox container
# 5. Forwards stdin/stdout for interactive use
# 6. Cleans up on exit (SIGTERM handling)

FROM python:3.11-slim

# Install Docker CLI for managing containers on the host
RUN apt-get update && apt-get install -y \
    curl \
    procps \
    && curl -fsSL https://get.docker.com -o get-docker.sh \
    && sh get-docker.sh \
    && rm get-docker.sh \
    && rm -rf /var/lib/apt/lists/*

# Create egg user for running the launcher
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg

WORKDIR /app

# Copy launcher code
COPY launcher/*.py ./
COPY launcher/requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared modules needed for config parsing
COPY shared/egg_logging/ ./egg_logging/
COPY shared/egg_config/ ./egg_config/

ENV PYTHONPATH="/app"

# Default ports for status API
EXPOSE 8080

# Run as root to access Docker socket (container management requires this)
# The launcher only manages containers - it doesn't run untrusted code itself
USER root

ENTRYPOINT ["python", "/app/entrypoint.py"]
