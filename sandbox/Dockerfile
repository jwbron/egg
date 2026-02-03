# Docker Layer Caching Strategy:
# - Layers are cached by checksum - unchanged layers reuse cache
# - Order: least-changing (base packages) → most-changing (scripts, configs)
# - When a layer changes, all subsequent layers rebuild
# - Copying files with COPY checks file contents, not timestamps

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

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

# Install Python 3.11 as default (Ubuntu 22.04 ships with 3.10)
# Required for datetime.UTC and other 3.11+ features used by egg scripts
RUN add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    update-alternatives --set python3 /usr/bin/python3.11 && \
    # Ensure 'python' command maps to python3 (many tools expect this)
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# Install pyyaml early - required for reading config
RUN pip3 install --no-cache-dir pyyaml

# Install Python dependencies for egg components
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
    pytest pytest-cov pytest-asyncio pytest-xdist hypothesis \
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

# Install beads llm issue tracker
# When running as root, the install script puts bd directly in /usr/local/bin
RUN curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash && \
    which bd

# Copy egg runtime scripts and tools to /opt/egg-runtime
# This provides container-resident executables available in PATH
RUN mkdir -p /opt/egg-runtime/sandbox/bin \
    /opt/egg-runtime/sandbox/scripts \
    /opt/egg-runtime/shared

# Copy sandbox scripts (git/gh wrappers will be added later)
COPY sandbox/scripts/ /opt/egg-runtime/sandbox/scripts/
RUN chmod +x /opt/egg-runtime/sandbox/scripts/* 2>/dev/null || true

# Make egg shared modules importable via PYTHONPATH
ENV PYTHONPATH="/opt/egg-runtime/sandbox:/opt/egg-runtime/shared"

# Create tmp directory in image (not mounted - container-only scratch space)
RUN mkdir -p /tmp/agent-tmp && chmod 1777 /tmp/agent-tmp

# Create fixed container user 'egg' with default UID/GID 1000
# The entrypoint will adjust UID/GID at runtime to match host user for proper file permissions
RUN groupadd -g 1000 egg && \
    useradd -m -u 1000 -g 1000 -s /bin/bash egg && \
    echo "egg ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/010-egg-nopasswd && \
    chmod 0440 /etc/sudoers.d/010-egg-nopasswd && \
    # Create .config directory with proper ownership for uv and other tools
    mkdir -p /home/egg/.config && chown egg:egg /home/egg/.config

# Install Claude Code CLI (native installer) for the egg user
# ARG is used (not ENV) so that changing the version busts Docker's cache
# The egg script checks for new versions and passes the latest version here
ARG CLAUDE_CODE_VERSION=stable
RUN su - egg -c "curl -fsSL https://claude.ai/install.sh | bash -s -- $CLAUDE_CODE_VERSION" && \
    # Save installed version for update checks
    /home/egg/.local/bin/claude --version 2>/dev/null | head -1 > /home/egg/.local/VERSION || true && \
    # Replace bundled ripgrep with system ripgrep to avoid ARM64 crashes on Asahi Linux
    CLAUDE_RG=$(find /home/egg/.local -name "rg" -type f 2>/dev/null | grep -E "ripgrep.*linux" | head -1) && \
    if [ -n "$CLAUDE_RG" ]; then \
        ln -sf /usr/bin/rg "$CLAUDE_RG" && \
        echo "Patched ripgrep: $CLAUDE_RG -> /usr/bin/rg"; \
    else \
        echo "Note: No bundled ripgrep found to patch (may not be needed)"; \
    fi

# Copy entrypoint script (Python for maintainability)
# Note: entrypoint.py needs to be ported from jib-container with jib→egg renaming
# For now, use a simple bash entrypoint
COPY sandbox/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /home/egg

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# =============================================================================
# END OF DOCKERFILE
# =============================================================================
# The entrypoint script handles:
# - Adjusting egg user's UID/GID to match host user (for file permissions)
# - Git configuration and worktree setup
# - Claude Code configuration
# - Agent rules installation (CLAUDE.md)
# - Beads persistent memory initialization
# - Interactive/exec mode handling
# =============================================================================
