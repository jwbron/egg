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
├── orchestrator/           # SDLC pipeline orchestrator (local execution)
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
| `orchestrator/` | SDLC pipeline orchestrator: state management, container lifecycle, HITL queue | Orchestrator container |
| `sandbox/` | Agent environment: Claude Code, tools, entrypoint | Sandbox container |
| `shared/` | Shared libraries: logging, config, git utilities, centralized constants | All containers |
| `scripts/` | CI/lint scripts (config validation, import checks, hardcoded port detection, reviewer job name enforcement) | CI / local |
| `tests/` | Test suite | CI / local |

## Gateway Structure

The gateway sidecar holds credentials and enforces policies:

```
gateway/
├── gateway.py              # Main HTTP server
├── git_client.py           # Git operation handler
├── github_client.py        # GitHub API handler (supports bot/user/reviewer modes)
├── policy.py               # Branch ownership, push policies, reviewer identity management
├── fork_policy.py          # Fork access policies
├── private_repo_policy.py  # Private/public repo access
├── phase_filter.py         # Phase-based operation filtering, file restrictions
├── agent_restrictions.py   # Agent role-based file access enforcement
├── phase_transition.py     # Phase transition validation
├── phase_api.py            # Phase API endpoints
├── contract_api.py         # Contract API endpoints
├── auth.py                 # Session authentication
├── token_refresher.py      # GitHub App token management (bot and optional reviewer)
├── anthropic_credentials.py # API key injection for Claude
├── checkpoint_handler.py   # Per-commit checkpoint capture
├── transcript_buffer.py    # API proxy transcript capture buffer
├── worktree_manager.py     # Git worktree lifecycle
├── session_manager.py      # Agent session management
├── repo_parser.py          # Repository config parsing
├── repo_visibility.py      # Repository visibility logic
├── proxy_monitor.py        # Squid proxy monitoring
├── rate_limiter.py         # Rate limiting
├── config_validator.py     # Configuration validation
├── error_messages.py       # Error message formatting
├── Dockerfile              # Gateway container image
├── squid.conf              # Proxy config (private mode)
├── scripts/                # Gateway helper scripts
└── tests/                  # Gateway tests
```

## Orchestrator Structure

The orchestrator manages local SDLC pipeline execution. It creates isolated git worktrees for each pipeline via the gateway's worktree API and mounts them into sandbox containers:

```
orchestrator/
├── api.py                  # REST API server (Flask)
├── cli.py                  # CLI for pipeline management
├── container_spawner.py    # Sandbox container lifecycle
├── container_monitor.py    # Container health monitoring
├── dag_visualizer.py       # ASCII DAG visualization for pipeline status
├── decision_queue.py       # HITL decision queue
├── decision_timeout.py     # Decision timeout handling
├── dispatch.py             # Agent dispatch logic
├── docker_client.py        # Docker API client
├── events.py               # Event bus for pipeline events
├── gateway_client.py       # Gateway API client (sessions, worktrees, config)
├── handoffs.py             # Agent handoff data management
├── metrics.py              # Pipeline metrics and telemetry
├── models.py               # Pydantic models for pipelines
├── multi_agent.py          # Multi-agent orchestration
├── resilience.py           # Retry and error recovery
├── sandbox_template.py     # Sandbox container template
├── state_store.py          # Git-backed pipeline state
├── status_reporter.py      # Real-time status reporter for collaborators
├── webhooks.py             # GitHub webhook handlers
├── routes/                 # API route handlers
│   ├── containers.py       # Container management endpoints
│   ├── decisions.py        # HITL decision endpoints
│   ├── health.py           # Health check endpoints
│   ├── metrics.py          # Metrics endpoints
│   ├── phases.py           # Phase management endpoints
│   ├── pipelines.py        # Pipeline CRUD and visualization endpoints
│   └── signals.py          # Signal handling endpoints
├── Dockerfile              # Orchestrator container image
├── entrypoint.sh           # Container entry point
├── requirements.txt        # Python dependencies
└── tests/                  # Orchestrator tests
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
│   ├── egg-checkpoint      # Symlink to checkpoint_cli.py
│   └── git-credential-github-token
├── egg_lib/                # Container utility libraries
│   ├── contract_cli.py     # SDLC contract CLI implementation
│   └── checkpoint_cli.py   # Checkpoint browsing CLI wrapper
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
│   └── constants.py        # Centralized constants (ports, networks, container names)
├── egg_contracts/          # SDLC contract models, plan parser, role-based validation, HITL, feedback, phase checks, multi-agent orchestration, checkpoints
│   ├── models.py           # Pydantic models including CheckDefinition, CheckResult, PhaseConfig, AgentExecutionModel
│   ├── phase_defaults.py   # Default check configurations per SDLC phase
│   ├── agent_roles.py      # Multi-agent role definitions (Coder, Tester, Documenter, Integrator)
│   ├── orchestrator.py     # Multi-agent orchestration dispatch logic
│   ├── orchestration.py    # Agent execution state management
│   ├── dependency_graph.py # Agent dependency resolution for parallel execution
│   ├── agent_recovery.py   # Failed agent recovery logic
│   ├── checkpoints.py      # Checkpoint data models
│   ├── checkpoint_loader.py # Checkpoint storage and retrieval
│   ├── checkpoint_cli.py   # Checkpoint browsing CLI
│   ├── transcript_extractor.py # API transcript extraction
│   └── redactor.py         # Sensitive data redaction for checkpoints
├── egg_git/                # Git utilities
├── egg_logging/            # Structured logging
└── egg_orchestrator/       # Orchestrator integration layer
    ├── __init__.py         # Public API exports
    ├── client.py           # OrchestratorClient for API communication
    ├── constants.py        # Orchestrator configuration constants (ports, IPs, endpoints, environment variable names)
    ├── detection.py        # Orchestrator mode detection utilities
    ├── py.typed            # PEP 561 type marker
    └── types.py            # Typed data classes and enums for signals and responses
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
├── local_pipeline/                # Local orchestrator integration tests
│   ├── conftest.py                # Local pipeline test fixtures
│   ├── docker-compose.yml         # Orchestrator test environment
│   ├── mock-sandbox/              # Mock sandbox for testing
│   ├── test_local_pipeline.py     # Orchestrator pipeline tests
│   ├── test_unified_pipeline_behavior.py  # Unified local/issue mode behavior tests
│   └── test_worktree_integration.py  # Worktree lifecycle and pipeline isolation tests
└── sdlc/                          # SDLC pipeline integration tests
    ├── conftest.py                # SDLC test fixtures
    ├── test_happy_path.py         # Full pipeline success flow
    ├── test_review_rejection.py   # Reviewer rejection and fix cycles
    ├── test_hitl_flow.py          # Human-in-the-loop decision flow
    ├── test_role_enforcement.py   # Role-based mutation enforcement
    └── test_multi_agent_orchestration.py  # Multi-agent workflow tests
```

## Unit Tests Structure

```
tests/
├── sandbox/                       # Sandbox component tests
│   ├── test_contract_cli.py       # Contract CLI tests
│   └── ...
├── scripts/
│   └── test_checks.py             # Check script framework tests
├── shared/
│   └── egg_contracts/
│       ├── test_models.py         # Contract model tests including check models
│       ├── test_phase_defaults.py # Phase default configuration tests
│       ├── test_agent_recovery.py # Agent recovery and circuit breaker tests
│       ├── test_checkpoints.py    # Checkpoint model tests
│       ├── test_redactor.py       # Redactor tests for sensitive data masking
│       └── test_transcript_extractor.py # Transcript extraction tests
└── workflows/                     # Workflow integration tests
    ├── __init__.py
    └── test_hitl_integration.py   # HITL decision format verification
```

## Action Directory

```
action/
├── action.yml                              # GitHub Action metadata
├── entrypoint.sh                           # Action entry point
├── generate-config.sh                      # Runtime config generator
├── build-mention-prompt.sh                 # @mention workflow prompt builder
├── build-review-prompt.sh                  # PR review workflow prompt builder
├── build-feedback-prompt.sh                # Review feedback addressing workflow prompt builder
├── build-autofixer-prompt.sh               # Autofixer workflow prompt builder
├── build-agent-mode-design-review-prompt.sh # Agent-mode design review prompt
├── build-doc-updater-prompt.sh             # Doc updater workflow prompt builder
├── build-conflict-prompt.sh                # Conflict resolution workflow prompt builder
├── build-sdlc-prompt.sh                    # SDLC pipeline prompt builder
├── build-unified-review-prompt.sh          # Unified review prompt builder for all SDLC phases
├── build-coder-prompt.sh                   # Coder agent prompt builder (multi-agent)
├── build-tester-prompt.sh                  # Tester agent prompt builder (multi-agent)
├── build-documenter-prompt.sh              # Documenter agent prompt builder (multi-agent)
├── build-integrator-prompt.sh              # Integrator agent prompt builder (multi-agent)
├── contract-state.sh                       # Contract state management utility
├── populate-contract-tasks.py              # Populates contract tasks from plan document
├── autofixer-conventions.md                # Guidelines for autofixer behavior
├── conflict-conventions.md                 # Guidelines for conflict resolution via merge commits
├── review-conventions.md                   # Guidelines for review communication
└── README.md
```

## GitHub Workflows (SDLC-Related)

Key workflows for the SDLC pipeline (see `.github/workflows/` for complete list):

```
.github/workflows/
├── lint.yml                                # Reusable lint workflow (called by sdlc-work-loop.yml)
├── test.yml                                # Reusable test workflow (called by sdlc-work-loop.yml)
├── test-integration.yml                    # Reusable integration test workflow (called by sdlc-work-loop.yml)
├── sdlc-pipeline.yml                       # SDLC pipeline orchestration
├── sdlc-work-loop.yml                      # Unified work/review cycle for SDLC phases
├── sdlc-multi-agent.yml                    # Multi-agent orchestration for implement phase
├── sdlc-hitl.yml                           # Human-in-the-loop decision handling
└── reusable-review.yml                     # PR-based code review workflow
```

## GitHub Workflows Scripts

```
.github/
└── scripts/
    ├── checks/                             # SDLC phase check scripts
    │   ├── __init__.py
    │   ├── base.py                         # CheckRunner base class
    │   ├── run_check.py                    # Check execution entry point
    │   ├── check_fixer.py                  # Auto-fix check runner
    │   ├── draft_validation_check.py       # Draft document validation
    │   ├── lint_check.py                   # Lint check runner
    │   ├── merge_conflict_check.py         # Merge conflict detection
    │   ├── plan_yaml_check.py              # Plan YAML validation
    │   └── test_check.py                   # Test execution check
    ├── create-release.sh                   # Semantic versioning release script
    ├── push-contract-update.sh             # Conflict-resistant contract push utility
    ├── setup-sdlc-labels.sh                # SDLC label setup (idempotent)
    └── transition-sdlc-label.sh            # Atomic SDLC label transitions
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
