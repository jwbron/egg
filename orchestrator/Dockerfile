FROM python:3.14-slim

# Install git for state management, curl for health checks, gosu for dropping privileges
RUN apt-get update && apt-get install -y \
    git curl gosu \
    && rm -rf /var/lib/apt/lists/*

# Install kustomize so `validate_deployment_manifests` (POST
# /api/v1/deployment/validate-manifests) can render overlays. Without
# it `_run_kustomize` falls through both kustomize and `kubectl
# kustomize` invocations and the route returns 500
# `kustomize_unavailable` for every call (#2647). Pinned to a known-
# good release; bump deliberately. The SHA256s are the published
# checksums for the per-arch tarballs (see checksums.txt alongside the
# release); bumping KUSTOMIZE_VERSION requires updating both in
# lockstep (#2681). Arch is resolved at build time via
# `dpkg --print-architecture` so the image is correct on both amd64
# and arm64 hosts — matching the arch-aware pattern in the gateway and
# sandbox Dockerfiles (previously hardcoded amd64, which crashed under
# emulation on arm64 dev machines; #2641/#2658).
ARG KUSTOMIZE_VERSION=5.6.0
ARG KUSTOMIZE_SHA256_AMD64=54e4031ddc4e7fc59e408da29e7c646e8e57b8088c51b84b3df0864f47b5148f
ARG KUSTOMIZE_SHA256_ARM64=ad8ab62d4f6d59a8afda0eec4ba2e5cd2f86bf1afeea4b78d06daac945eb0660
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) sha256="${KUSTOMIZE_SHA256_AMD64}" ;; \
         arm64) sha256="${KUSTOMIZE_SHA256_ARM64}" ;; \
         *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_${arch}.tar.gz" \
        -o /tmp/kustomize.tar.gz \
    && echo "${sha256}  /tmp/kustomize.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/kustomize.tar.gz -C /usr/local/bin kustomize \
    && rm /tmp/kustomize.tar.gz \
    && chmod +x /usr/local/bin/kustomize

# Create egg user with UID/GID 1000 as default
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg

WORKDIR /app

# Copy orchestrator code
COPY orchestrator/*.py ./
COPY orchestrator/routes/ ./routes/
COPY orchestrator/health_checks/ ./health_checks/

# Copy shared modules (egg_logging, egg_config, egg_contracts, egg_restrictions, egg_container, egg_agent, egg_git, egg_anchor, egg_health)
COPY shared/egg_logging/ ./egg_logging/
COPY shared/egg_config/ ./egg_config/
COPY shared/egg_contracts/ ./egg_contracts/
COPY shared/egg_restrictions/ ./egg_restrictions/
COPY shared/egg_container/ ./egg_container/
COPY shared/egg_agent/ ./egg_agent/
COPY shared/egg_orchestrator/ ./egg_orchestrator/
COPY shared/egg_git/ ./egg_git/
COPY shared/egg_anchor/ ./egg_anchor/
COPY shared/egg_health/ ./egg_health/

# Copy config module (#2528: orchestrator reads per-repo role_patterns
# from repositories.yaml so plan-time validation and the planner prompt
# match what the gateway will enforce at push time). The two-step
# import in shared/egg_restrictions/patterns.py falls back from
# ``config.repo_config`` to top-level ``repo_config``, mirroring the
# gateway's existing pattern.
COPY config/repo_config.py ./repo_config.py

# Copy anchor schema files (required by egg_anchor.validator)
COPY .egg/schemas/ ./.egg/schemas/

# Copy shared prompt criteria files
COPY shared/prompts/ ./prompts/

# Install dependencies
COPY orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH="/app"

# Orchestrator API port (configured in shared/egg_config/constants.py)
EXPOSE 9849
# MCP server port (configured via EGG_MCP_SERVER_PORT)
EXPOSE 9850
# Standalone kubelet-probe listener (#2414, configured via
# EGG_ORCH_PROBE_LISTENER_PORT). Isolated from the API port so probe
# latency is decoupled from waitress thread-pool pressure.
EXPOSE 9851

COPY orchestrator/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
