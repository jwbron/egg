# Docker Layer Caching Strategy:
# - Layers are cached by checksum - unchanged layers reuse cache
# - Order: least-changing (base packages) -> most-changing (scripts, configs)
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

# Network Lockdown: Pre-install all Python dependencies
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

# Copy egg runtime scripts and tools to /opt/egg-runtime
# This provides container-resident executables available in PATH
COPY container/sandbox/ /opt/egg-runtime/sandbox/
COPY shared/ /opt/egg-runtime/shared/
RUN chmod +x /opt/egg-runtime/sandbox/bin/* 2>/dev/null || true && \
    chmod +x /opt/egg-runtime/sandbox/scripts/* 2>/dev/null || true

# Relocate git/gh binaries and symlink /usr/bin to wrappers
# This ensures ALL git/gh invocations route through the gateway sidecar,
# whether called as 'git' (PATH) or '/usr/bin/git' (absolute path)
RUN mkdir -p /opt/.egg-internal && \
    mv /usr/bin/git /opt/.egg-internal/git && \
    mv /usr/bin/gh /opt/.egg-internal/gh && \
    ln -s /opt/egg-runtime/sandbox/scripts/git /usr/bin/git && \
    ln -s /opt/egg-runtime/sandbox/scripts/gh /usr/bin/gh

# Make egg modules importable via PYTHONPATH
ENV PYTHONPATH="/opt/egg-runtime/sandbox:/opt/egg-runtime/shared"

# Create tmp directory in image (not mounted - container-only scratch space)
RUN mkdir -p /tmp/agent-tmp && chmod 1777 /tmp/agent-tmp

# Create fixed container user 'sandbox' with default UID/GID 1000
# The entrypoint will adjust UID/GID at runtime to match host user for proper file permissions
RUN groupadd -g 1000 sandbox && \
    useradd -m -u 1000 -g 1000 -s /bin/bash sandbox && \
    echo "sandbox ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/010-sandbox-nopasswd && \
    chmod 0440 /etc/sudoers.d/010-sandbox-nopasswd

# Copy entrypoint script (Python for maintainability)
COPY container/sandbox/entrypoint.py /usr/local/bin/entrypoint.py
RUN chmod +x /usr/local/bin/entrypoint.py

WORKDIR /home/sandbox

ENTRYPOINT ["python3", "/usr/local/bin/entrypoint.py"]

# =============================================================================
# END OF DOCKERFILE
# =============================================================================
# The entrypoint script (entrypoint.py) handles:
# - Adjusting sandbox user's UID/GID to match host user (for file permissions)
# - Git configuration and worktree setup
# - Environment configuration
# - Interactive/exec mode handling
# =============================================================================
