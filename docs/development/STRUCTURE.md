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
│   ├── egg-contract        # Symlink to contract_cli.py
│   └── git-credential-github-token
├── egg_lib/                # Container utility libraries
│   └── contract_cli.py     # SDLC contract CLI implementation
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
├── egg_contracts/          # SDLC contract models, plan parser, role-based validation, circuit breaker, HITL
├── egg_git/                # Git utilities
└── egg_logging/            # Structured logging
```

## Integration Tests Structure

```
integration_tests/
├── conftest.py                    # Shared fixtures for all integration tests
├── docker-compose.yml             # Test environment setup
├── agent_findings.py              # Security findings for agent security fuzz tests
├── test_agent_security_fuzz.py    # Agent security fuzzing tests
├── test_credential_security.py    # Credential isolation verification
├── test_e2e_workflow.py           # End-to-end workflow tests
├── test_error_recovery.py         # Error handling and recovery tests
├── test_fail_closed.py            # Fail-closed security property tests
├── test_gateway_auth.py           # Gateway authentication tests
├── test_gateway_operations.py     # Gateway API endpoint tests
├── test_network_isolation.py      # Network security tests
├── test_network_security.py       # Network policy enforcement tests
├── test_performance.py            # Performance and scaling tests
├── test_policy_enforcement.py     # Policy enforcement tests
├── test_rate_limiting.py          # Rate limiting tests
├── test_stack_lifecycle.py        # Container lifecycle tests
└── sdlc/                          # SDLC pipeline integration tests
    ├── conftest.py                # SDLC test fixtures
    ├── test_happy_path.py         # Full pipeline success flow
    ├── test_review_rejection.py   # Reviewer rejection and fix cycles
    ├── test_circuit_breaker.py    # Circuit breaker escalation
    ├── test_hitl_flow.py          # Human-in-the-loop decision flow
    └── test_role_enforcement.py   # Role-based mutation enforcement
```

## Action Directory

```
action/
├── action.yml                              # GitHub Action metadata
├── entrypoint.sh                           # Action entry point
├── generate-config.sh                      # Runtime config generator
├── build-mention-prompt.sh                 # @mention workflow prompt builder
├── build-review-prompt.sh                  # PR review workflow prompt builder
├── build-autofixer-prompt.sh               # Autofixer workflow prompt builder
├── build-agent-mode-design-review-prompt.sh # Agent-mode design review prompt
├── build-doc-updater-prompt.sh             # Doc updater workflow prompt builder
├── build-sdlc-prompt.sh                    # SDLC pipeline prompt builder
├── contract-state.sh                       # Contract state management utility
├── escalate.sh                             # SDLC pipeline escalation handler
├── autofixer-conventions.md                # Guidelines for autofixer behavior
├── review-conventions.md                   # Guidelines for review communication
└── README.md
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
├── templates/              # SDLC phase document templates (analysis, plan)
└── troubleshooting/        # Common issues and solutions
```

**Rule**: Documentation should live close to code. Only cross-cutting docs belong in the central `docs/` directory.
