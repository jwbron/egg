# Sandbox Container

The untrusted container where the LLM agent runs with no credentials and restricted access.

## Overview

The sandbox container provides a secure, isolated environment for the Claude agent. It includes:
- Container entrypoint and lifecycle management
- Git/gh wrappers that route all operations through the gateway sidecar
- Claude Code configuration (rules, commands)
- LLM integration (Claude Code runner)
- Interactive tools for agent use
- Shared directories for communication with the host

## Directory Structure

```
sandbox/
├── entrypoint.py           # Container entry point and orchestration
├── statusbar.py            # Status bar display
├── egg                     # Main egg CLI script
├── Dockerfile              # Sandbox container image
├── docker-setup.py         # Build-time tool installation and per-repo dependency setup
├── pyproject.toml          # Package configuration
│
├── egg_lib/                # Container utility libraries
│   ├── cli.py              # CLI command handling
│   ├── config.py           # Configuration management
│   ├── auth.py             # Authentication handling
│   ├── gateway.py          # Gateway communication
│   ├── docker.py           # Docker image build, Dockerfile generation, dependency caching
│   ├── context.py          # Context management
│   ├── runtime.py          # Runtime utilities
│   ├── setup_flow.py       # Setup workflow
│   ├── network_mode.py     # Network mode handling
│   ├── container_logging.py # Container logging
│   ├── timing.py           # Timing utilities
│   ├── output.py           # Output formatting
│   ├── compose.py          # Docker Compose operations
│   ├── checkpoint_cli.py   # Checkpoint CLI implementation
│   ├── contract_cli.py     # SDLC contract CLI (egg-contract)
│   ├── orchestration.py    # Multi-agent orchestration support
│   ├── orch_cli.py         # Orchestrator CLI (egg-orch)
│   ├── orch_client.py      # Orchestrator API client (decision creation with type support)
│   ├── sdlc_cli.py         # SDLC pipeline CLI (egg-sdlc)
│   ├── sdlc_hitl.py        # SDLC human-in-the-loop support (type-aware rendering)
│   └── self_improvement/   # Self-improvement data collection
│       ├── collect.py      # Data collection orchestrator
│       ├── config.py       # Collection configuration
│       └── collectors/     # Data source collectors
│           ├── base.py     # Base collector class
│           ├── gha.py      # GitHub Actions collector
│           └── local.py    # Local metrics collector
│
├── llm/                    # LLM integration
│   ├── runner.py           # LLM runner abstraction
│   ├── config.py           # LLM configuration
│   ├── result.py           # Result handling
│   └── claude/             # Claude Code implementation
│       ├── runner.py       # Claude Code runner
│       └── config.py       # Claude-specific config
│
├── bin/                    # CLI tools and symlinks (added to PATH)
│   ├── git -> ../scripts/git
│   ├── gh -> ../scripts/gh
│   ├── git-credential-github-token -> ../scripts/git-credential-github-token
│   ├── egg-checkpoint -> ../egg_lib/checkpoint_cli.py
│   ├── egg-contract -> ../egg_lib/contract_cli.py
│   ├── egg-orch -> ../egg_lib/orch_cli.py
│   ├── egg-onboarding-docs      # Onboarding doc generator (bash)
│   ├── egg-pipeline-watch       # Pipeline progress watcher
│   └── egg-sdlc                 # Interactive SDLC pipeline CLI
│
├── scripts/                # Wrapper script implementations
│   ├── git                 # Git wrapper (routes to gateway)
│   ├── gh                  # GitHub CLI wrapper (routes to gateway)
│   └── git-credential-github-token
│
├── agent-config/           # Agent rules and commands (deployed into containers)
│   ├── commands/           # Custom slash commands
│   │   ├── README.md       # Commands documentation
│   │   ├── sdlc.md         # /sdlc — SDLC pipeline initialization
│   │   └── show-metrics.md # /show-metrics — Activity monitoring report
│   └── rules/              # Agent behavior rules
│       ├── README.md       # Rules documentation
│       ├── mission.md      # Agent mission and workflow
│       ├── environment.md  # Sandbox constraints
│       ├── code-standards.md # Code standards
│       ├── test-workflow.md  # Testing workflow
│       ├── pr-descriptions.md # PR guidelines
│       ├── orchestrator.md # Orchestrator CLI commands
│       ├── contract.md     # SDLC contract CLI commands
│       └── checkpoint.md   # Checkpoint browser CLI commands
│
├── tools/                  # Interactive tools
│   ├── discover-tests.py   # Test framework discovery
│   └── github-app-token.py # Token generation utility
│
└── scripts/                # Container helper scripts
```

## Container Filesystem

```
~/repos/                      # Code workspace (RW) - contains repos, NOT itself a git repo
~/sharing/                    # Shared with host (mounted from ~/.egg-sharing/)
├── notifications/           # Agent -> Human (notifications)
├── incoming/                # Human -> Agent (tasks)
├── responses/               # Human -> Agent (responses)
└── context/                 # Persistent knowledge across rebuilds

~/context-sync/              # Read-only context (mounted from host)
├── confluence/             # Confluence documentation
└── jira/                   # JIRA tickets
```

## Security

**Isolation**:
- No SSH keys or cloud credentials
- Network: All traffic routes through gateway sidecar
- No inbound ports (can't accept connections)
- No direct GitHub token access

**What the agent CAN do**:
- Read/write code in `~/repos/` (isolated git worktree)
- Git commits locally
- Create/manage PRs via `gh` CLI (routed through gateway)
- Push changes via `git push` (routed through gateway)
- Run tests and builds
- Read context docs (Confluence, JIRA)
- Write notifications for human review

**What the agent CANNOT do**:
- Merge PRs (gateway blocks merge operations)
- Access credentials (held by gateway sidecar)
- Deploy to cloud (no credentials)
- Push to protected branches (gateway enforces branch ownership)
- Access git metadata (`.git/` is shadowed)

## GitHub CLI

All GitHub operations route through the gateway sidecar:

```bash
# Pull Requests
gh pr create --title "..." --body "..." --base main
gh pr view [<number>]
gh pr list

# Issues
gh issue list
gh issue view <number>

# Comments
gh pr comment <number> --body "..."
gh pr review <number> --comment --body-file /tmp/review-body.md
```

## Command Timeout

A system-level per-command timeout wrapper prevents runaway shell commands (e.g., `grep -rn 'pattern' /`) from consuming unbounded CPU and memory. The wrapper interposes on `/bin/bash` and wraps `-c` invocations (the pattern used by Claude Code's Bash tool) with the `timeout` utility.

**How it works:**
- `entrypoint.py` moves `/bin/bash` to `/bin/bash.real` and installs a wrapper script at `/bin/bash`
- The wrapper only wraps `bash -c "..."` invocations; interactive shells and script sourcing pass through unchanged
- The top-level exec command (e.g. the consensus wrapper script launched by `run_exec()`) bypasses the wrapper by invoking `bash.real` directly — only Claude's internal Bash tool commands are subject to the per-command timeout
- On timeout, `SIGTERM` is sent first, followed by `SIGKILL` after a grace period

**Configuration (environment variables):**

| Variable | Description | Default |
|----------|-------------|---------|
| `BASH_COMMAND_TIMEOUT` | Timeout in seconds (0 to disable) | `300` |
| `BASH_COMMAND_TIMEOUT_GRACE` | SIGKILL grace period in seconds | `10` |

**Files:** `sandbox/entrypoint.py` (`setup_command_timeout()`, `run_exec()`), `sandbox/tests/test_command_timeout.py`

## Configuration

Container setup is automated via `docker-setup.py`, which runs during the Docker image **build** to install dependencies, configure the environment, and set up Claude Code. No manual setup is required inside the container.

### Build-Time Dependency Installation

Per-repo `build_commands` in `repositories.yaml` allow project-specific dependencies (npm packages, Python venvs, Go modules, etc.) to be installed during the image build and baked into the image. This is critical for private mode, where containers have no runtime network access beyond the Anthropic API.

**Build flow:**
1. `create_dockerfile()` (in `egg_lib/docker.py`) copies each repo's `watch_files` from local paths into the build context at `repo-deps/<repo-name>/`
2. The Dockerfile `COPY repo-deps/` layer picks up these files — changes to watch files (e.g., `package-lock.json`) invalidate the Docker cache for this layer
3. `docker-setup.py` reads `build_commands` and `extra_packages` from `manifest.json` in `repo-deps/` (since `repositories.yaml` is unavailable in the build context) and executes each repo's commands in its watch files directory
4. `compute_build_hash()` includes watch file contents, so `egg` automatically detects when a rebuild is needed
5. If `persist_dirs` is configured, `persist_build_dirs()` copies those directories (e.g., `node_modules`) from the build context to `/opt/prebuilt-deps/<repo>/` in the image
6. At container startup, `restore_prebuilt_deps()` in `entrypoint.py` restores persisted directories into the mounted repo, making them available without network access

> **Note:** `persist_dirs` is not included in `compute_build_hash()`. Changing only `persist_dirs` won't trigger an automatic rebuild — add or modify a `watch_files` entry or use `egg --rebuild`.

**Config propagation:** During Docker builds, `repositories.yaml` is not available in the build context. Both `build_commands` and `extra_packages` are propagated via a `manifest.json` file that `create_dockerfile()` writes into `repo-deps/`. The `docker-setup.py` script reads this manifest as a fallback when `repositories.yaml` is not found.

See [Configuration README](../config/README.md#per-repo-build-commands-dependency-caching) for configuration details.

## Related Documentation

- [Claude Code Configuration](agent-config/README.md) - Agent rules and commands
- [Gateway Sidecar](../gateway/README.md) - Policy enforcement gateway
- [Architecture Overview](../docs/architecture/README.md) - System design
