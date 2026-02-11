FROM python:3.11-slim

# Install git for state management and curl for health checks
RUN apt-get update && apt-get install -y \
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Create egg user with UID/GID 1000 as default
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg

WORKDIR /app

# Copy orchestrator code
COPY orchestrator/*.py ./
COPY orchestrator/routes/ ./routes/

# Copy shared modules (egg_logging, egg_config, egg_contracts)
COPY shared/egg_logging/ ./egg_logging/
COPY shared/egg_config/ ./egg_config/
COPY shared/egg_contracts/ ./egg_contracts/

# Install dependencies
COPY orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH="/app"

# Orchestrator API port (configured in shared/egg_config/constants.py)
EXPOSE 9849

COPY orchestrator/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
