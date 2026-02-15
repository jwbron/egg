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
├── docker-setup.py         # In-container tool installation
├── pyproject.toml          # Package configuration
│
├── egg_lib/                # Container utility libraries
│   ├── cli.py              # CLI command handling
│   ├── config.py           # Configuration management
│   ├── auth.py             # Authentication handling
│   ├── gateway.py          # Gateway communication
│   ├── docker.py           # Docker operations
│   ├── context.py          # Context management
│   ├── runtime.py          # Runtime utilities
│   ├── setup_flow.py       # Setup workflow
│   ├── network_mode.py     # Network mode handling
│   ├── container_logging.py # Container logging
│   ├── timing.py           # Timing utilities
│   ├── output.py           # Output formatting
│   ├── compose.py          # Docker Compose operations
│   ├── checkpoint_cli.py   # Checkpoint CLI for saving/restoring state
│   ├── contract_cli.py     # SDLC contract CLI (egg-contract)
│   ├── orchestration.py    # Multi-agent orchestration support
│   ├── orch_cli.py         # Orchestrator CLI (egg-orch)
│   ├── orch_client.py      # Orchestrator API client
│   ├── sdlc_cli.py         # SDLC pipeline CLI (egg-contract)
│   ├── sdlc_hitl.py        # SDLC human-in-the-loop support
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
├── .claude/                # Claude Code customization
│   ├── commands/           # Custom slash commands
│   │   ├── README.md       # Commands documentation
│   │   ├── sdlc.md         # /sdlc — SDLC pipeline initialization
│   │   ├── show-metrics.md # /show-metrics — Activity monitoring report
│   │   ├── coder-mode.md   # /coder-mode — Coder agent mode
│   │   ├── documenter-mode.md # /documenter-mode — Documenter agent mode
│   │   ├── integrator-mode.md # /integrator-mode — Integrator agent mode
│   │   └── tester-mode.md  # /tester-mode — Tester agent mode
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
~/repos/                      # Code workspace (RW) - mounted repositories
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

## Configuration

Container setup is automated via `docker-setup.py`, which runs on container start to install dependencies, configure the environment, and set up Claude Code. No manual setup is required inside the container.

## Related Documentation

- [Claude Code Configuration](.claude/README.md) - Agent rules and commands
- [Gateway Sidecar](../gateway/README.md) - Policy enforcement gateway
- [Architecture Overview](../docs/architecture/README.md) - System design
