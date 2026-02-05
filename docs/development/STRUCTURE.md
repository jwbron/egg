# Project Structure Guidelines

This document describes the directory structure conventions for egg.

## Top-Level Structure

```
egg/
├── bin/                    # CLI entry points
├── config/                 # Central configuration (repos, secrets template)
├── docs/                   # Cross-cutting documentation
├── gateway/                # Gateway sidecar (trusted container)
├── integration_tests/      # Integration tests (require Docker)
├── sandbox/                # Sandbox container (untrusted, runs the LLM agent)
├── scripts/                # Validation and lint scripts
├── shared/                 # Shared Python libraries (used by gateway + sandbox)
├── tests/                  # Unit tests
├── dev                     # Development CLI (setup, lint, test, ci)
└── README.md
```

## Directory Details

| Directory | Purpose | Runs In |
|-----------|---------|---------|
| `bin/` | CLI entry points (`egg`, `setup-gateway`) | Host |
| `config/` | Repository config, secrets template | Host |
| `gateway/` | Gateway sidecar: policy enforcement, credential injection, proxying | Gateway container |
| `integration_tests/` | Integration tests requiring Docker and real containers | CI / local |
| `sandbox/` | Agent environment: Claude Code, tools, entrypoint | Sandbox container |
| `shared/` | Shared libraries: logging, config, git utilities | Both containers |
| `scripts/` | CI/lint scripts (config validation, import checks) | CI / local |
| `tests/` | Test suite | CI / local |

## Gateway Structure

The gateway sidecar holds credentials and enforces policies:

```
gateway/
├── gateway.py              # Main HTTP server
├── git_client.py           # Git operation handler
├── github_client.py        # GitHub API handler
├── policy.py               # Branch ownership, push policies
├── fork_policy.py          # Fork access policies
├── private_repo_policy.py  # Private/public repo access
├── token_refresher.py      # GitHub App token management
├── anthropic_credentials.py # API key injection for Claude
├── worktree_manager.py     # Git worktree lifecycle
├── session_manager.py      # Agent session management
├── proxy_monitor.py        # Squid proxy monitoring
├── rate_limiter.py         # Rate limiting
├── Dockerfile              # Gateway container image
├── squid.conf              # Proxy config (private mode)
├── scripts/                # Gateway helper scripts
└── tests/                  # Gateway tests
```

## Sandbox Structure

The sandbox container is where the LLM agent runs:

```
sandbox/
├── entrypoint.py           # Container entry point
├── statusbar.py            # Status bar display
├── egg                     # Main egg script
├── Dockerfile              # Sandbox container image
├── docker-setup.py         # In-container tool installation
├── bin/                    # Git/gh wrapper scripts (route to gateway)
│   ├── git
│   ├── gh
│   └── git-credential-github-token
├── egg_lib/                # Container utility libraries
├── llm/                    # Claude Code / Agent SDK integration
├── tools/                  # Interactive tools
│   ├── discover-tests.py   # Test framework discovery
│   └── github-app-token.py # Token generation utility
├── claude-commands/        # Custom slash commands
├── claude-rules/           # Agent behavior rules
└── scripts/                # Container helper scripts
```

## Shared Libraries

```
shared/
├── egg_config/             # Configuration utilities
├── egg_git/                # Git utilities
└── egg_logging/            # Structured logging
```

## Config Directory

```
config/
├── repositories.yaml.example  # Repository access configuration template
├── secrets.template.env        # Secrets template
├── repo_config.py              # Python API for repo access
└── README.md
```

## File Naming Conventions

| Type | Convention | Example |
|------|------------|---------|
| Python scripts | kebab-case | `discover-tests.py` |
| Python packages | snake_case | `egg_config/`, `egg_lib/` |
| Shell scripts | kebab-case | `setup.sh`, `start-gateway.sh` |
| Config files | `.yaml` (not `.yml`) | `repositories.yaml` |
| Documentation | UPPERCASE.md for guides, lowercase.md for READMEs | `STRUCTURE.md`, `README.md` |

## Documentation Organization

```
docs/
├── index.md                # Documentation navigation hub
├── README.md               # Documentation overview
├── adr/                    # Architecture Decision Records
├── architecture/           # System design docs
├── development/            # Developer guides (this file)
├── reference/              # Quick reference guides
├── setup/                  # Setup instructions
└── troubleshooting/        # Common issues and solutions
```

**Rule**: Documentation should live close to code. Only cross-cutting docs belong in the central `docs/` directory.
