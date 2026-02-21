# Project Structure Guidelines

This document describes the directory structure conventions for egg.

## Top-Level Structure

```
egg/
├── bin/                    # CLI entry points (egg, egg-deploy, egg-status)
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
| `bin/` | CLI entry points (`egg`, `egg-sdlc`) | Host |
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
├── checkpoint_handler.py   # Checkpoint capture (commit and session-end triggers)
├── transcript_buffer.py    # API proxy transcript capture buffer
├── worktree_manager.py     # Git worktree lifecycle
├── session_manager.py      # Agent session management (branch lock, auto-commit trigger)
├── post_agent_commit.py    # Post-agent auto-commit for uncommitted worktree changes
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
├── devserver.py            # Devserver lifecycle manager for deployment validation (DinD)
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
├── sse.py                  # Server-Sent Events streaming for pipeline visualization
├── startup_reconciliation.py # Startup reconciliation for orphaned containers
├── state_store.py          # Git-backed pipeline state
├── status_reporter.py      # Real-time status reporter for collaborators
├── unified_sse.py          # Unified SSE stream for all pipelines
├── webhooks.py             # GitHub webhook handlers
├── routes/                 # API route handlers
│   ├── checks.py           # Deployment validation check endpoints
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
│   ├── egg-onboarding-docs # Generate repository documentation via egg-sdlc
│   ├── egg-pipeline-watch  # Real-time pipeline progress viewer via SSE
│   ├── egg-orch            # Symlink to orch_cli.py
│   └── git-credential-github-token
├── egg_lib/                # Container utility libraries
│   ├── contract_cli.py     # SDLC contract CLI implementation
│   ├── checkpoint_cli.py   # Checkpoint browsing CLI wrapper
│   └── orch_cli.py         # Orchestrator API CLI implementation
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
│   ├── constants.py        # Centralized constants (ports, networks, container names, devserver resource limits)
│   └── validators.py       # Validation functions (URLs, emails, tokens, check commands)
├── egg_container/          # Shared container-launch config builder
│   └── __init__.py         # build_sandbox_config(), build_sandbox_docker_cmd(), git_shadow_mounts(), phase_readonly_mounts(), ensure_egg_state_dirs(), to_dockerpy_kwargs()
├── egg_contracts/          # SDLC contract models, plan parser, role-based validation, HITL, feedback, phase checks, multi-agent orchestration, checkpoints
│   ├── models.py           # Pydantic models including CheckDefinition, CheckResult, PhaseConfig, AgentExecutionModel
│   ├── phase_defaults.py   # Default check configurations per SDLC phase
│   ├── deployment.py       # Deployment validation configuration models (.egg/deployment.yml)
│   ├── agent_roles.py      # Multi-agent role definitions (all agent and reviewer roles)
│   ├── orchestrator.py     # Multi-agent orchestration dispatch logic
│   ├── orchestration.py    # Agent execution state management (Tier 2 role-key + Tier 3 composite key)
│   ├── dependency_graph.py # Agent and phase dependency resolution for parallel execution (Tier 2 DependencyGraph + Tier 3 PhaseDependencyGraph)
│   ├── plan_parser.py      # Plan document parsing with task extraction and phase dependency normalization
│   ├── agent_recovery.py   # Failed agent recovery logic
│   ├── checkpoints.py      # Checkpoint data models
│   ├── checkpoint_loader.py # Checkpoint storage and retrieval
│   ├── checkpoint_cli.py   # Checkpoint browsing CLI (list, show, browse)
│   ├── transcript_extractor.py # API transcript extraction
│   └── redactor.py         # Sensitive data redaction for checkpoints
├── prompts/                # Shared prompt criteria (used by GHA scripts AND orchestrator)
│   ├── agent-design-criteria.md  # Agent-mode design review criteria
│   ├── autofixer-rules.md        # Autofixer auto-fix vs report-only rules
│   ├── code-review-criteria.md   # Code review security/correctness criteria
│   ├── contract-review-criteria.md # Contract verification rules
│   └── onboarding-docs-prompt.md # Documentation onboarding standard (egg-onboarding-docs)
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
├── deployment_validation/         # Deployment validation integration tests
│   ├── __init__.py
│   └── test_deployment_check_e2e.py  # End-to-end devserver lifecycle tests
├── local_pipeline/                # Local orchestrator integration tests
│   ├── conftest.py                # Local pipeline test fixtures
│   ├── docker-compose.yml         # Orchestrator test environment
│   ├── helpers.py                 # Shared API helper functions for tests
│   ├── mock-sandbox/              # Mock sandbox for testing
│   ├── test_api_validation.py     # API input validation tests
│   ├── test_concurrent_pipelines.py  # Concurrent pipeline execution tests
│   ├── test_error_recovery.py     # Error recovery scenario tests
│   ├── test_hitl_edge_cases.py    # HITL decision edge case tests
│   ├── test_local_pipeline.py     # Orchestrator pipeline tests
│   ├── test_signals.py            # Signal handling tests
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
├── action/                        # GitHub Action prompt builder tests
│   ├── test_build_agent_mode_design_review_prompt.py
│   └── test_build_review_prompt.py
├── sandbox/                       # Sandbox component tests
│   ├── test_contract_cli.py       # Contract CLI tests
│   └── ...
├── scripts/
│   ├── test_checks.py             # Check script framework tests
│   └── test_deployment_check.py   # Deployment check unit tests
├── shared/
│   └── egg_contracts/
│       ├── test_models.py         # Contract model tests including check models
│       ├── test_phase_defaults.py # Phase default configuration tests
│       ├── test_deployment_config.py # Deployment configuration tests
│       ├── test_agent_recovery.py # Agent recovery and circuit breaker tests
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
├── build-review-prompt.sh                  # PR review workflow prompt builder
├── build-feedback-prompt.sh                # Review feedback addressing workflow prompt builder
├── build-autofixer-prompt.sh               # Autofixer workflow prompt builder
├── build-agent-mode-design-review-prompt.sh # Agent-mode design review prompt
├── build-doc-updater-prompt.sh             # Doc updater workflow prompt builder
├── build-conflict-prompt.sh                # Conflict resolution workflow prompt builder
├── build-contract-verification-prompt.sh  # Contract verification review prompt builder
├── autofixer-conventions.md                # Guidelines for autofixer behavior
├── conflict-conventions.md                 # Guidelines for conflict resolution via merge commits
├── review-conventions.md                   # Guidelines for review communication
└── README.md
```

## GitHub Workflows (PR Operations)

Key workflows for PR automation (see `.github/workflows/` for complete list):

```
.github/workflows/
├── on-pull-request.yml                     # AI code review on PR open/sync
├── on-check-failure.yml                    # Auto-fix failing checks
├── on-merge-conflict.yml                   # Auto-resolve merge conflicts
├── on-review-feedback.yml                  # Address review feedback on bot/authorized-user PRs
├── on-push-doc-updater.yml                 # Auto-update docs after merge
├── on-pull-request-agent-mode-design.yml   # Agent-mode design review
├── on-pull-request-contract-verify.yml    # Contract verification on PRs
├── reusable-review.yml                     # PR-based code review workflow
├── reusable-autofix.yml                    # Reusable auto-fix logic
├── reusable-conflict-resolve.yml           # Reusable conflict resolution
├── lint.yml                                # Lint workflow
├── test.yml                                # Test workflow
└── test-integration.yml                    # Integration test workflow
```

## GitHub Workflow Scripts

```
.github/
├── scripts/
│   ├── checks/                            # Python-based check framework
│   │   ├── __init__.py
│   │   ├── base.py                        # CheckRunner base class
│   │   ├── check_fixer.py                 # Auto-fix check (runs make fix)
│   │   ├── deployment_check.py            # Deployment validation (DinD devserver)
│   │   ├── draft_validation_check.py      # Draft document validation
│   │   ├── lint_check.py                  # Lint check (runs make lint)
│   │   ├── merge_conflict_check.py        # Merge conflict marker detection
│   │   ├── plan_yaml_check.py             # Plan YAML structure validation
│   │   ├── run_check.py                   # Check runner entry point
│   │   └── test_check.py                  # Test check (runs make test)
│   └── create-release.sh                  # Semantic versioning release script
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
| Shell scripts | kebab-case | `entrypoint.sh`, `create-networks.sh` |
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
