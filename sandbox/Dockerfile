# Docker Multi-Stage Build Strategy:
# - Stage 1 (repo-deps): Builds repo dependencies independently of the base image.
#   Changes to base system packages do NOT invalidate the repo-deps cache.
# - Stage 2 (base): System packages, Python, pip deps, Claude tools.
#   Claude agent SDK and CLI are installed last (they change most often).
# - Stage 3 (final): Combines base + repo-deps, adds runtime scripts and entrypoint.

# =============================================================================
# Stage 1: repo-deps — independent cache for repository dependency installation
# =============================================================================
# This stage has its own FROM so its cache is completely independent of the
# base stage. Changes to base system packages (Stage 2) do NOT invalidate
# the repo dependency cache, avoiding expensive npm install / go mod download.
FROM ubuntu:22.04 AS repo-deps

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ARG PIP_IGNORE_INSTALLED=1
# Point GOMODCACHE at the module cache populated during `docker build`
# (via build_commands in repositories.yaml). Without this, Go defaults to
# $HOME/go/pkg/mod at runtime and can't find modules cached under
# /usr/local/pkg/mod/, causing proxy.golang.org lookups that fail in private mode.
# Using GOMODCACHE instead of GOPATH avoids side effects on GOBIN/GOPATH/src.
ENV GOMODCACHE=/usr/local/pkg/mod

# Install minimal tooling needed to run docker-setup.py and build commands
# Python 3.14 is required because repos may set requires-python >= 3.14 in
# pyproject.toml. Without it, uv downloads a managed Python into /root/ which
# creates broken symlinks when the .venv is persisted and restored for the
# non-root runtime user.
RUN set -eux \
    && apt-get update && apt-get install -y \
        python3 python3-pip \
        git curl wget make build-essential pkg-config \
        jq unzip software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    # deadsnakes PPA occasionally returns 5xx during apt-get update, leaving
    # python3.14* packages unlocatable on install (see PR #2694 CI failure).
    # Retry the post-PPA update+install with backoff; Acquire::Retries handles
    # short blips inside a single apt invocation, the outer loop handles
    # longer outages. APT::Update::Error-Mode=any promotes per-source index
    # errors to a non-zero exit, so a deadsnakes 5xx trips the retry directly
    # instead of leaking through as an unlocatable-package install failure.
    && for i in 1 2 3 4 5; do \
        if apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any \
            && apt-get install -y python3.14 python3.14-venv python3.14-dev; then \
            break; \
        fi; \
        if [ "$i" = 5 ]; then \
            echo "deadsnakes install failed after 5 attempts" >&2; exit 1; \
        fi; \
        echo "deadsnakes install attempt $i failed, sleeping $((i*15))s..." >&2; \
        sleep $((i*15)); \
    done \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.14 1 \
    && update-alternatives --set python3 /usr/bin/python3.14 \
    && python3.14 -m ensurepip --upgrade \
    && pip3 install --no-cache-dir pyyaml \
    && rm -rf /var/lib/apt/lists/*

# Copy watch files and manifest for build_commands
COPY repo-deps/ /tmp/repo-deps/
COPY sandbox/docker-setup.py /tmp/docker-setup.py

# Run docker-setup.py: installs extra packages, runs build commands,
# persists output dirs to /opt/prebuilt-deps/
RUN chmod +x /tmp/docker-setup.py && \
    apt-get update && \
    python3 /tmp/docker-setup.py && \
    rm /tmp/docker-setup.py && \
    rm -rf /tmp/repo-deps && \
    mkdir -p /opt/prebuilt-deps

# =============================================================================
# Stage 2: base — system packages, Python 3.14, pip deps, Claude tools
# =============================================================================
FROM ubuntu:22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
# Allow pip to install over distutils-managed system packages (safe in Docker).
# PIP_BREAK_SYSTEM_PACKAGES bypasses PEP 668 — kept as ENV because agents may
# pip-install at runtime in public mode.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
# PIP_IGNORE_INSTALLED bypasses the distutils uninstall error (e.g. blinker 1.4
# installed by apt can't be removed by pip, but we can install a newer version
# into Python 3.14's site-packages). ARG so it only applies during build — at
# runtime it would cause pip to wastefully reinstall already-present packages.
ARG PIP_IGNORE_INSTALLED=1
# Point GOMODCACHE at the module cache populated during `docker build`
# (via build_commands in repositories.yaml). Without this, Go defaults to
# $HOME/go/pkg/mod at runtime and can't find modules cached under
# /usr/local/pkg/mod/, causing proxy.golang.org lookups that fail in private mode.
# Using GOMODCACHE instead of GOPATH avoids side effects on GOBIN/GOPATH/src.
ENV GOMODCACHE=/usr/local/pkg/mod

# Install comprehensive development tools
RUN apt-get update && apt-get install -y \
    # Core utilities (includes grep, cut, sort, uniq, tr, etc.)
    coreutils findutils util-linux \
    wget curl ca-certificates software-properties-common \
    gnupg lsb-release sudo gosu git \
    # Text processing
    sed gawk less vim nano \
    # Build tools
    make cmake gcc g++ build-essential pkg-config autoconf automake libtool \
    # Network tools
    netcat-openbsd telnet iputils-ping dnsutils net-tools iproute2 \
    # File operations
    rsync tar gzip bzip2 zip unzip p7zip-full \
    # Process management
    procps htop lsof psmisc \
    # Development
    strace ltrace gdb \
    # Other useful tools
    jq tree watch tmux screen inotify-tools ripgrep shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install glow (charmbracelet terminal markdown renderer)
RUN curl -fsSL https://repo.charm.sh/apt/gpg.key | gpg --dearmor -o /usr/share/keyrings/charm.gpg \
    && chmod go+r /usr/share/keyrings/charm.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/charm.gpg] https://repo.charm.sh/apt/ * *" > /etc/apt/sources.list.d/charm.list \
    && apt-get update \
    && apt-get install -y glow \
    && rm -rf /var/lib/apt/lists/*

# Install Python 3.14 as default (Ubuntu 22.04 ships with 3.10)
# Note: We run apt-get update twice - once before adding the PPA to refresh
# base Ubuntu package lists (avoiding stale libexpat1 etc), and once after
# to pick up deadsnakes packages.
# python3-pip from apt targets the system Python (3.10), not 3.14 — so we
# omit it and bootstrap pip via ensurepip after setting update-alternatives.
RUN set -eux \
    && apt-get update \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    # deadsnakes PPA occasionally returns 5xx during apt-get update, leaving
    # python3.14* packages unlocatable on install (see PR #2694 CI failure).
    # Retry the post-PPA update+install with backoff; Acquire::Retries handles
    # short blips inside a single apt invocation, the outer loop handles
    # longer outages. APT::Update::Error-Mode=any promotes per-source index
    # errors to a non-zero exit, so a deadsnakes 5xx trips the retry directly
    # instead of leaking through as an unlocatable-package install failure.
    && for i in 1 2 3 4 5; do \
        if apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any \
            && apt-get install -y python3.14 python3.14-venv python3.14-dev; then \
            break; \
        fi; \
        if [ "$i" = 5 ]; then \
            echo "deadsnakes install failed after 5 attempts" >&2; exit 1; \
        fi; \
        echo "deadsnakes install attempt $i failed, sleeping $((i*15))s..." >&2; \
        sleep $((i*15)); \
    done \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.14 1 \
    && update-alternatives --set python3 /usr/bin/python3.14 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.14 1 \
    && python3.14 -m ensurepip --upgrade \
    && python3.14 -m pip install --no-cache-dir --upgrade pip

# Install pyyaml early - required by docker-setup.py for reading config
RUN pip3 install --no-cache-dir pyyaml

# Install Python dependencies for egg components
# These are required by scripts in /opt/egg-runtime/ (copied from sandbox/)
RUN pip3 install --no-cache-dir \
    pyyaml \
    requests \
    cryptography \
    PyJWT \
    # Dev dependencies for linting and testing
    ruff \
    pytest \
    # Required by gateway (for running tests)
    flask \
    waitress

# Phase 2 Network Lockdown: Pre-install all Python dependencies
# In lockdown mode, the container cannot access PyPI at runtime.
# All commonly-needed packages must be pre-installed here.
RUN pip3 install --no-cache-dir \
    # HTTP/API clients
    requests httpx aiohttp urllib3 \
    # Data formats and parsing
    pyyaml toml python-dateutil orjson \
    # Testing frameworks
    pytest pytest-cov pytest-asyncio pytest-xdist hypothesis fakeredis \
    # Code quality tools
    black ruff mypy isort \
    # Type stubs
    types-requests types-PyYAML types-toml \
    # CLI and terminal
    click rich typer \
    # Cryptography and security
    cryptography PyJWT \
    # Utilities
    python-dotenv tenacity pydantic \
    # Database clients
    psycopg2-binary redis \
    # Web frameworks
    flask waitress \
    # Documentation
    markdown

# Note: Claude authentication is mounted at runtime from ~/.egg/claude/ on host
# This directory is shared across all containers for consistent authentication
# See entrypoint script for setup and credential detection

# Copy Claude command documentation
RUN mkdir -p /usr/local/share/claude-commands
COPY sandbox/claude-commands/*.md /usr/local/share/claude-commands/
RUN chmod 644 /usr/local/share/claude-commands/*.md

# Copy Claude skills
COPY skills/ /usr/local/share/claude-skills/
RUN find /usr/local/share/claude-skills -type f -exec chmod 644 {} \;

# Copy Claude agent rules directory
RUN mkdir -p /opt/claude-rules
COPY sandbox/claude-rules/*.md /opt/claude-rules/
RUN chmod 644 /opt/claude-rules/*.md

# Create tmp directory in image (not mounted - container-only scratch space)
RUN mkdir -p /tmp/agent-tmp && chmod 1777 /tmp/agent-tmp

# Create fixed container user 'egg' with default UID/GID 1000
# The entrypoint will adjust UID/GID at runtime to match host user for proper file permissions
# Placed BEFORE the frequently-changing layers so user creation is cached
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg && \
    echo "egg ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/010-egg-nopasswd && \
    chmod 0440 /etc/sudoers.d/010-egg-nopasswd && \
    # Create .config directory with proper ownership for uv and other tools
    mkdir -p /home/egg/.config && chown egg:egg /home/egg/.config

# Install claude-agent-sdk (changes more often than base deps, less often than CLI)
# The egg script always passes the specific PyPI version to bust Docker's layer
# cache when a new version is released. The "latest" fallback only applies if the
# build-arg is omitted (e.g. manual docker build without the egg script).
ARG CLAUDE_AGENT_SDK_VERSION=0.1.65
RUN if [ "$CLAUDE_AGENT_SDK_VERSION" = "latest" ]; then \
        pip3 install --no-cache-dir --upgrade 'claude-agent-sdk>=0.1.65,<0.2'; \
    else \
        pip3 install --no-cache-dir claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}; \
    fi && \
    # Save installed version for update checks (non-fatal if this fails)
    (pip3 show claude-agent-sdk 2>/dev/null | grep '^Version:' | cut -d' ' -f2 > /opt/claude-agent-sdk-version.txt || true)

# Install Claude Code CLI (native installer) for the egg user
# The egg script always passes the specific npm version to bust Docker's layer
# cache when a new version is released. The "stable" fallback only applies if the
# build-arg is omitted (e.g. manual docker build without the egg script).
ARG CLAUDE_CODE_VERSION=stable
RUN su - egg -c "curl -fsSL https://claude.ai/install.sh | bash -s -- $CLAUDE_CODE_VERSION" && \
    # Fail the build if the CLI binary is missing (e.g. install script 403'd)
    test -x /home/egg/.local/bin/claude && \
    # Save installed version for update checks (non-fatal if this fails)
    (/home/egg/.local/bin/claude --version 2>/dev/null | head -1 > /home/egg/.local/VERSION || true) && \
    # Replace bundled ripgrep with system ripgrep to avoid ARM64 crashes on Asahi Linux
    CLAUDE_RG=$(find /home/egg/.local -name "rg" -type f 2>/dev/null | grep -E "ripgrep.*linux" | head -1) && \
    if [ -n "$CLAUDE_RG" ]; then \
        ln -sf /usr/bin/rg "$CLAUDE_RG" && \
        echo "Patched ripgrep: $CLAUDE_RG -> /usr/bin/rg"; \
    else \
        echo "Note: No bundled ripgrep found to patch (may not be needed)"; \
    fi

# =============================================================================
# Stage 3: final — combine base + repo-deps, add runtime scripts
# =============================================================================
FROM base AS final

# Pull in cached repo dependencies from Stage 1
# This is a simple COPY that doesn't re-run any build commands
COPY --from=repo-deps /opt/prebuilt-deps/ /opt/prebuilt-deps/

# Install extra packages configured via repositories.yaml.
# docker-setup.py writes the package list to /opt/prebuilt-deps/ in Stage 1,
# but apt-installed packages don't carry across stages — reinstall them here.
# Use || true to preserve the old lenient behavior (install_extra_packages used
# check=False) so that typos or unavailable packages don't fail the build.
RUN if [ -f /opt/prebuilt-deps/extra-packages-apt.txt ] && [ -s /opt/prebuilt-deps/extra-packages-apt.txt ]; then \
        apt-get update && \
        (xargs apt-get install -y < /opt/prebuilt-deps/extra-packages-apt.txt || true) && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Restore system-level directories installed by build_commands in Stage 1.
# These are absolute-path installations (e.g., /usr/local/go, /usr/local/node)
# that were persisted to /opt/prebuilt-deps/__egg_system_dirs__/<abs_path> by docker-setup.py.
# Copy the entire tree back to / so paths are restored to their original locations.
RUN if [ -d /opt/prebuilt-deps/__egg_system_dirs__ ]; then \
        cp -a /opt/prebuilt-deps/__egg_system_dirs__/. /; \
    fi

# Copy sandbox runtime scripts and tools to /opt/egg-runtime
# This provides container-resident executables available in PATH
# The bin/ directory contains symlinks to all executables
# Structure:
#   /opt/egg-runtime/sandbox/{bin,scripts,tools}
#   /opt/egg-runtime/shared/
# This layer changes frequently with source edits — keep it AFTER the slow
# Claude Code install so source changes don't bust that cache
COPY . /opt/egg-runtime/
RUN chmod +x /opt/egg-runtime/sandbox/bin/* 2>/dev/null || true && \
    chmod +x /opt/egg-runtime/sandbox/scripts/* 2>/dev/null || true && \
    chmod +x /opt/egg-runtime/sandbox/tools/* 2>/dev/null || true

# Relocate git/gh binaries to hidden path and symlink /usr/bin to wrappers
# This ensures ALL git/gh invocations route through the gateway sidecar,
# whether called as 'git' (PATH) or '/usr/bin/git' (absolute path)
RUN mkdir -p /opt/.egg-internal && \
    mv /usr/bin/git /opt/.egg-internal/git && \
    mv /usr/bin/gh /opt/.egg-internal/gh && \
    ln -s /opt/egg-runtime/sandbox/scripts/git /usr/bin/git && \
    ln -s /opt/egg-runtime/sandbox/scripts/gh /usr/bin/gh && \
    ln -s /opt/egg-runtime/sandbox/scripts/jira /usr/bin/jira

# Make sandbox and shared modules importable via PYTHONPATH
# This is simpler than pip install and doesn't require pyproject.toml
ENV PYTHONPATH="/opt/egg-runtime/sandbox:/opt/egg-runtime/shared"

# Put egg CLIs on PATH at the image level so every shell (agent Bash tool
# calls, kubectl exec, interactive debug) finds egg-contract/egg-orch/etc.
# without needing the entrypoint's env to propagate. See issue #1799.
ENV PATH="/opt/egg-runtime/sandbox/bin:${PATH}"

# Copy entrypoint script (Python for maintainability)
COPY sandbox/entrypoint.py /usr/local/bin/entrypoint.py
RUN chmod +x /usr/local/bin/entrypoint.py

WORKDIR /home/egg

ENTRYPOINT ["python3", "/usr/local/bin/entrypoint.py"]

# =============================================================================
# END OF DOCKERFILE
# =============================================================================
# The entrypoint script (entrypoint.py) handles:
# - Adjusting egg user's UID/GID to match host user (for file permissions)
# - Git configuration and worktree setup
# - Claude Code configuration
# - Agent rules installation (CLAUDE.md)
# - Interactive/exec mode handling
# =============================================================================
