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
# PIP_IGNORE_INSTALLED is for this stage's own system-Python `pip3 install`
# below. It's an ARG so it doesn't persist into the final image, but Docker
# still exposes ARGs as env vars to RUN subprocesses in this stage — including
# `python3 /tmp/docker-setup.py` below, which then leaks it into each repo's
# build commands. docker-setup.py's run_build_commands strips it from the env
# it passes to those subprocesses; keep both halves of the contract in sync.
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
RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    git curl wget make build-essential pkg-config \
    # Common build deps needed by repo build_commands
    jq unzip software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y python3.14 python3.14-venv python3.14-dev \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.14 1 \
    && update-alternatives --set python3 /usr/bin/python3.14 \
    && python3.14 -m ensurepip --upgrade \
    && pip3 install --no-cache-dir pyyaml \
    && rm -rf /var/lib/apt/lists/*

# Copy watch files and manifest for build_commands
COPY repo-deps/ /tmp/repo-deps/
COPY sandbox/docker-setup.py /tmp/docker-setup.py

# Run docker-setup.py: installs extra packages, runs build commands,
# persists repo output dirs to /opt/prebuilt-deps/ and system-level installs
# (e.g. /usr/local/go) to /opt/egg-system-dirs/. The trailing mkdirs make
# both paths exist even when nothing was persisted, so the final stage's
# COPY --from of each cannot fail on a missing source.
RUN chmod +x /tmp/docker-setup.py && \
    apt-get update && \
    python3 /tmp/docker-setup.py && \
    rm /tmp/docker-setup.py && \
    rm -rf /tmp/repo-deps && \
    mkdir -p /opt/prebuilt-deps /opt/egg-system-dirs

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
# into Python 3.14's site-packages). ARG so it doesn't persist into the final
# image — at runtime it would cause pip to wastefully reinstall already-present
# packages, and (more critically) leak into agent-launched venv pip installs
# where it reinstalls pip itself, breaking version-pinned tooling. The Stage 1
# `repo-deps` ARG is stripped by docker-setup.py for the same reason.
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
RUN apt-get update && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.14 python3.14-venv python3.14-dev && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.14 1 && \
    update-alternatives --set python3 /usr/bin/python3.14 && \
    # Ensure 'python' command maps to python3 (many tools expect this)
    update-alternatives --install /usr/bin/python python /usr/bin/python3.14 1 && \
    # Bootstrap pip for Python 3.14 (ensurepip installs pip3 to /usr/local/bin)
    python3.14 -m ensurepip --upgrade && \
    python3.14 -m pip install --no-cache-dir --upgrade pip

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
    markdown \
    # MCP search server used as a WebSearch/WebFetch fallback when routing
    # through LiteLLM to non-Anthropic models (see #2856). Installed here
    # rather than fetched via uvx at runtime because the sandbox runs under
    # Phase 2 Network Lockdown. Pinned: the hook's redirect targets are the
    # exact tool names this server exposes (mcp__ddg__search /
    # mcp__ddg__fetch_content), so a future upstream rename would silently
    # break the fallback with no test failure. 0.4.0 exposes search /
    # fetch_content.
    duckduckgo-mcp-server==0.4.0

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
# Nothing passes this build-arg anymore (the legacy egg CLI that resolved a
# concrete PyPI version was removed in #1762), so the default below is the
# operative pin — bumping it is also what busts Docker's layer cache so the
# new release actually installs. Keep it in sync with the bounded range in
# sandbox/pyproject.toml.
ARG CLAUDE_AGENT_SDK_VERSION=0.2.97
RUN if [ "$CLAUDE_AGENT_SDK_VERSION" = "latest" ]; then \
        pip3 install --no-cache-dir --upgrade 'claude-agent-sdk>=0.2.97,<0.3'; \
    else \
        pip3 install --no-cache-dir claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}; \
    fi && \
    # Save installed version for update checks (non-fatal if this fails)
    (pip3 show claude-agent-sdk 2>/dev/null | grep '^Version:' | cut -d' ' -f2 > /opt/claude-agent-sdk-version.txt || true)

# Install Claude Code CLI (native installer) for the egg user
#
# Channel choice: 'latest', not 'stable'. orchestrator/agent_model_resolution.py
# spawns agents on bare model-family aliases ('opus' is the built-in default;
# 'fable' remains opt-in selectable per #3197), and the stable channel lags
# new family launches — at the time of #3136 stable (2.1.153) predated the
# fable alias entirely, so a stable-channel image crash-loops any agent
# spawned on an alias it doesn't know.
ARG CLAUDE_CODE_VERSION=latest
# Cache-bust (#3136): BuildKit re-checks this URL's content on every build, so
# the install layer below re-runs exactly when the release channel moves to a
# new version. Without it the layer is keyed on the literal channel name
# ('latest'/'stable') and the image's Claude Code freezes at whatever the
# channel pointed to the first time the layer built.
#
# Caveats worth knowing if this ever silently regresses (#3137 review):
#   - The URL is hardcoded to the 'latest' manifest regardless of the
#     CLAUDE_CODE_VERSION arg. If the operator overrides the arg to
#     'stable' or to a concrete version (e.g. '2.1.173'), the layer still
#     rebuilds whenever 'latest' moves. For 'stable' this is wasteful but
#     functionally fine — the install reruns and re-fetches the stable
#     pointer. For a concrete version the reinstall is deterministic and
#     idempotent. The wasteful-rebuild edge case is intentional: keeping
#     the URL hardcoded keeps the cache-bust behaviour the same across
#     channel-name args, which is the dominant path.
#   - The cache-bust depends on the CDN preserving HTTP cache headers
#     (verified: 'cache-control: public,no-cache,max-age=0' + 'ETag' +
#     'Last-Modified', which BuildKit's 'ADD <URL>' uses as its cache
#     key). If the CDN ever drops those headers (config change, hosting
#     migration), the bust silently degrades to 'cached forever per URL
#     string'. The alias gate further down catches that regression at
#     build time as long as the 'fable' / 'opus' families haven't both
#     also stagnated, but defense-in-depth: rebuild with --no-cache if
#     you're debugging a stuck channel pointer.
ADD https://downloads.claude.ai/claude-code-releases/latest /tmp/claude-code-latest-release
RUN su - egg -c "curl -fsSL https://claude.ai/install.sh | bash -s -- $CLAUDE_CODE_VERSION" && \
    rm -f /tmp/claude-code-latest-release && \
    # Fail the build if the CLI binary is missing (e.g. install script 403'd)
    test -x /home/egg/.local/bin/claude && \
    # Save installed version for update checks (non-fatal if this fails)
    (/home/egg/.local/bin/claude --version 2>/dev/null | head -1 > /home/egg/.local/VERSION || true) && \
    # Fail the build if the installed build predates a model family that
    # orchestrator/agent_model_resolution.py can spawn agents on ('opus', the
    # built-in default; 'fable', opt-in selectable per #3197): a Claude
    # Code build whose alias table lacks the family rejects the bare alias at
    # session init and the agent crash-loops at spawn (#3136). No auth is
    # mounted at build time, so this is a strings-level heuristic — a build
    # that knows a family alias embeds the versioned 'claude-<family>-*' IDs
    # the alias maps to; a build that predates the family contains none.
    for family in fable opus; do \
        if ! grep -aq "claude-${family}-" /home/egg/.local/bin/claude; then \
            echo "ERROR: installed Claude Code ($(cat /home/egg/.local/VERSION 2>/dev/null)) does not know the '${family}' model family."; \
            echo "agent_model_resolution.py can spawn agents on '${family}' — agents would crash-loop at spawn (#3136)."; \
            echo "Pass a newer build: docker build --build-arg CLAUDE_CODE_VERSION=<version|latest> ..."; \
            echo "If pinning to an older CC build is intentional (and every"; \
            echo "agent_models entry has been pinned away from '${family}'),"; \
            echo "drop '${family}' from the 'for family in ...' list above to"; \
            echo "skip the gate for that family (matches the bypass guidance"; \
            echo "in docs/guides/per-agent-models.md)."; \
            exit 1; \
        fi; \
    done && \
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
# that were persisted to /opt/egg-system-dirs/<abs_path> by docker-setup.py.
# COPY straight from Stage 1 to / so paths land at their original locations.
# This must be a COPY --from, not a `cp` of an already-COPYed tree: copying
# a directory the image already contains stores its multi-GB content TWICE
# (once in the source layer, once in the cp layer) — the pre-#2999 layout
# did exactly that via /opt/prebuilt-deps/__egg_system_dirs__ and cost ~2 GB
# of image size.
COPY --from=repo-deps /opt/egg-system-dirs/ /

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
