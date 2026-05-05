# Project Structure Guidelines

This document describes the directory structure conventions for egg.

## Top-Level Structure

```
egg/
├── bin/                    # CLI entry points (egg, egg-deploy, egg-status)
├── config/                 # Central configuration (repos, secrets template)
├── docs/                   # Cross-cutting documentation
├── gateway/                # Gateway sidecar (trusted container)
├── integration_tests/      # Integration tests (require k3s)
├── k8s/                    # Kubernetes manifests (Kustomize base + overlays)
├── orchestrator/           # SDLC pipeline orchestrator (local execution)
├── sandbox/                # Sandbox container (untrusted, runs the LLM agent)
├── scripts/                # Validation, lint, and operational telemetry scripts
├── shared/                 # Shared Python libraries (used by gateway + sandbox)
├── skills/                 # Claude Code skills (installed into sandbox at startup)
├── tests/                  # Unit tests
├── dev                     # Development CLI (setup, lint, test, ci)
├── CLAUDE.md               # Agent navigation guide (Claude Code entry point)
└── README.md
```

## Directory Details

| Directory | Purpose | Runs In |
|-----------|---------|---------|
| `bin/` | CLI entry points (`egg`, `egg-sdlc`) | Host |
| `config/` | Repository config, secrets template | Host |
| `gateway/` | Gateway sidecar: policy enforcement, credential injection, proxying | Gateway container |
| `integration_tests/` | Integration tests requiring k3s cluster and real pods | CI / local |
| `k8s/` | Kubernetes manifests: Kustomize base + overlays (local/k3s). Namespaces, Deployments, Services, NetworkPolicies, agent Job template, RBAC | k3s cluster |
| `orchestrator/` | SDLC pipeline orchestrator: state management, container lifecycle, HITL queue | Orchestrator container |
| `sandbox/` | Agent environment: Claude Code, tools, entrypoint | Sandbox container |
| `scripts/` | CI/lint and operational telemetry scripts (config validation, import checks, hardcoded port detection, reviewer job name enforcement, LLM API boundary enforcement, model alias enforcement, harness parity validation, scaffold-first BRC compliance telemetry via `scaffold_first_telemetry.py`) | CI / local |
| `shared/` | Shared libraries: logging, config, git utilities, centralized constants | All containers |
| `skills/` | Claude Code skills (each subdirectory is a skill with `SKILL.md`) | Sandbox container |
| `tests/` | Test suite | CI / local |

## Gateway Structure

The gateway sidecar holds credentials and enforces policies:

```
gateway/
├── gateway.py              # Main HTTP server
├── _module_loader.py       # Sibling-module bootstrap loader (isolated to keep gateway.py out of the dynamic-import seed set; do not add gateway imports here)
├── git_client.py           # Git operation handler
├── github_client.py        # GitHub API handler (supports bot/user/reviewer modes)
├── policy.py               # Branch ownership, push policies, reviewer identity management
├── fork_policy.py          # Fork access policies
├── private_repo_policy.py  # Private/public repo access
├── phase_filter.py         # Phase-based operation filtering, file restrictions
├── agent_restrictions.py   # Agent role-based file access enforcement
├── commit_observer.py      # Gateway-inline commit observer: registers new SHAs with the authorship registry after each git-execute call
├── commit_registry_client.py # HTTP client for the orchestrator's commit-authorship registry (register + lookup_bulk)
├── phase_transition.py     # Phase transition validation
├── phase_api.py            # Phase API endpoints
├── contract_api.py         # Contract API endpoints
├── auth.py                 # Session authentication
├── token_refresher.py      # GitHub App token management (bot and optional reviewer)
├── anthropic_credentials.py # API key injection for Claude
├── jira_credentials.py     # Atlassian credential loading from secrets.env (mtime refresh, basic-auth header helper)
├── jira_client.py          # Jira REST client + validate_jira_api_path regex allowlist + 429 retry + 404 envelope
├── jira_policy.py          # Project allowlist loader for config/context-filters.yaml (jira.projects)
├── jira_search.py          # Conservative static JQL project-scope extractor (deny-on-ambiguity)
├── mode_gate.py            # @require_private_mode decorator (fails closed in public mode)
├── checkpoint_handler.py   # Checkpoint capture (commit and session-end triggers)
├── transcript_buffer.py    # API proxy transcript capture buffer
├── worktree_manager.py     # Git worktree lifecycle
├── session_manager.py      # Agent session management (branch lock, worktree cleanup)
├── post_agent_commit.py    # Post-agent exit handling (HITL recovery for uncommitted work)
├── repo_parser.py          # Repository config parsing
├── repo_visibility.py      # Repository visibility logic
├── proxy_monitor.py        # Squid proxy monitoring
├── mem_trace.py            # Opt-in tracemalloc memory sampler (GATEWAY_MEM_TRACE=1)
├── rate_limiter.py         # Rate limiting
├── config_validator.py     # Configuration validation
├── error_messages.py       # Error message formatting
├── Dockerfile              # Gateway container image
├── squid.conf              # Proxy config (private mode)
├── scripts/                # Gateway helper scripts
├── tests/                  # Gateway tests (push-target enforcement, branch lock, 40+ files)
└── CLAUDE.md               # Agent navigation guide
```

## Orchestrator Structure

The orchestrator manages local SDLC pipeline execution. It creates isolated git worktrees for each pipeline via the gateway's worktree API and mounts them into agent pods:

```
orchestrator/
├── api.py                  # REST API server (Flask)
├── cli.py                  # CLI for pipeline management
├── container_backend.py    # ContainerBackend protocol (structural typing interface)
├── kubernetes_client.py    # Kubernetes API client (Job CRUD, pod logs, status)
├── kubernetes_spawner.py   # Agent Job lifecycle (replaces ContainerSpawner)
├── kubernetes_monitor.py   # k8s Job state monitoring (replaces ContainerMonitor)
├── concurrent_executor.py  # Concurrent phase executor (spawns all agents simultaneously)
├── slice_scheduler.py      # Wave-based scheduler for the implement-phase slice DAG: computes execution waves, caps concurrency, two-tier max_cycles accounting, failure-cascade detection (#2137)
├── stacked_pr_reconciler.py # Stacked-PR rebase reconciler: detects child slice PRs whose base branch was deleted after a parent merge and retargets them via gateway rebase_onto (#2137)
├── action_guards.py        # Formal BRC state machine action guards (preconditions for propose/ack/nack/confirm/withdraw)
├── approval_matrix.py      # Per-reviewer ACK/NACK matrix for BRC consensus
├── attestation_schemas.py  # Attestation payload validation for BRC proposals
├── consensus.py            # Legacy READY-tallying consensus (deprecated, kept for transition)
├── consensus_wrapper.py    # Shell wrapper that keeps containers alive polling for consensus after Claude exits
├── dag_visualizer.py       # ASCII DAG visualization for pipeline status
├── decision_queue.py       # HITL decision queue
├── events.py               # Event bus for pipeline events
├── gateway_client.py       # Gateway API client (sessions, worktrees, config)
├── handoffs.py             # Agent handoff data management
├── health_monitor.py       # Deterministic tripwire health monitor (progress events → auto-nudge/escalate)
├── message_store.py        # Inter-agent message store (Redis Streams when available, in-memory fallback)
├── progress_store.py       # In-memory structured progress event store with configurable retention
├── peer_consensus.py       # BRC (Broadcast-Review-Converge) peer consensus tracker
├── mcp_server.py           # MCP server providing comprehensive egg platform interface to Claude Code (port 9850)
├── mcp_tools.py            # MCP tool definitions and handlers: pipeline state, containers, messages, checkpoints, contracts, health, deployment
├── redaction.py            # Secret redaction helpers for operator-facing diagnostic output (env vars, Bearer JWTs, API key shapes)
├── metrics.py              # Pipeline metrics and telemetry
├── models.py               # Pydantic models for pipelines
├── redis_message_store.py  # Redis Streams-backed message store implementation
├── resilience.py           # Retry and error recovery
├── review_graph.py         # Asymmetric review graph topology for BRC consensus
├── sandbox_template.py     # Sandbox container template
├── sse.py                  # Server-Sent Events streaming for pipeline visualization
├── startup_reconciliation.py # Startup reconciliation for orphaned containers
├── commit_authorship_store.py # Durable {sha → role} registry sharded by pipeline on the pipeline-state branch; backing store for the commit-authorship registry
├── state_store.py          # Git-backed pipeline state
├── state_store_probe.py    # Background state-store self-heal probe; decouples curative git ops from kubelet probe traffic (#2191)
├── status_reporter.py      # Real-time status reporter for collaborators
├── unified_sse.py          # Unified SSE stream for all pipelines
├── webhooks.py             # GitHub webhook handlers
├── overseer/               # Overseer agent package (LLM-powered tier of pipeline health monitoring)
│   ├── classifier.py       # Haiku-tier classifiers (stall, loop, error triage, off-track detection)
│   ├── decision_maker.py   # Sonnet/Opus-tier decision-maker (corrective actions, redirect messages)
│   ├── issue_filer.py      # Autonomous GitHub diagnostic issue filing
│   ├── monitor.py          # Main OverseerMonitor polling loop (poll-classify-decide-act cycle)
│   ├── self_monitor.py     # OverseerSelfMonitor (poll timing, message volume, LLM cost tracking)
│   └── utils.py            # Shared utilities for overseer modules
├── health_checks/          # Two-tier health check framework (see health_checks/README.md)
│   ├── types.py            # HealthCheck protocol, HealthResult, enums
│   ├── context.py          # PipelineHealthContext with lazy properties
│   ├── runner.py           # HealthCheckRunner — trigger dispatch and tier escalation
│   ├── tier1/              # Programmatic checks (fast, deterministic)
│   │   ├── container_liveness.py   # Verify RUNNING containers exist in Docker
│   │   ├── startup_state.py        # Post-startup reconciliation verification
│   │   ├── phase_output.py         # Detect missing artifacts (commits, plans)
│   │   ├── consensus_stall.py      # Detect BRC consensus-complete-but-phase-stuck
│   │   └── state_consistency.py    # Cross-reference orchestrator state vs Docker vs contract
│   └── tier2/              # Semantic checks (LLM-powered)
│       └── agent_inspector.py   # Claude-powered agent progress analysis
├── routes/                 # API route handlers
│   ├── anchors.py          # Agent anchor CRUD and team anchor generation endpoints
│   ├── commit_authorship.py # Commit-authorship registry endpoints (register + lookup); called by gateway commit observer and push handler
│   ├── containers.py       # Container management endpoints
│   ├── decisions.py        # HITL decision endpoints
│   ├── deployment.py       # Deployment introspection and action endpoints (k8s diagnostics, prune, rebuild)
│   ├── health.py           # Health check endpoints
│   ├── messages.py         # Inter-agent message bus endpoints (concurrent mode)
│   ├── metrics.py          # Metrics endpoints
│   ├── phases.py           # Phase management endpoints
│   ├── pipelines.py        # Pipeline CRUD and visualization endpoints
│   ├── progress.py         # Structured progress event endpoints (emit, query)
│   └── signals.py          # Signal handling endpoints (incl. readiness for concurrent mode)
├── Dockerfile              # Orchestrator container image
├── entrypoint.sh           # Container entry point
├── requirements.txt        # Python dependencies
├── tests/                  # Orchestrator tests (signal verification, worktree sync, health checks, 30+ files)
└── CLAUDE.md               # Agent navigation guide
```

## Kubernetes Manifests

The `k8s/` directory contains Kustomize manifests for deploying egg to Kubernetes:

```
k8s/
├── base/                              # Environment-agnostic base manifests
│   ├── kustomization.yaml             # Kustomize resource listing
│   ├── namespaces.yaml                # egg-system and egg-agents namespaces
│   ├── orchestrator-deployment.yaml   # Orchestrator Deployment + environment config
│   ├── orchestrator-service.yaml      # Service exposing port 9849
│   ├── gateway-deployment.yaml        # Gateway Deployment + environment config
│   ├── gateway-service.yaml           # Service exposing ports 9848, 3129, 9851
│   ├── network-policies.yaml          # Calico NetworkPolicies for agent isolation
│   └── rbac.yaml                      # ServiceAccount + Role + RoleBinding for orchestrator
(Agent Job specs are built programmatically by ``KubernetesClient.create_container`` — no standalone YAML template.)
│
└── overlays/
    └── local/                         # k3s-specific patches
        ├── kustomization.yaml         # Overlay config referencing base
        └── patches/                   # hostPath storage, local-path provisioner config
```

See [Kubernetes Migration](../architecture/kubernetes-migration.md) for architecture details.

## Sandbox Structure

The sandbox container is where the LLM agent runs:

```
sandbox/
├── entrypoint.py           # Container entry point
├── statusbar.py            # Status bar display
├── overseer_monitor.py     # Pre-built overseer monitoring script (pipeline health; --once for single-cycle, default for continuous loop)
├── egg                     # Main egg script
├── Dockerfile              # Sandbox container image
├── docker-setup.py         # Build-time tool installation and per-repo dependency setup
├── bin/                    # Git/gh wrapper scripts (route to gateway)
│   ├── git
│   ├── gh
│   ├── egg-contract        # Symlink to contract_cli.py
│   ├── egg-checkpoint      # Symlink to checkpoint_cli.py
│   ├── egg-health-inspect  # Pipeline health inspector (delegates LLM calls for tier 2 checks)
│   ├── egg-onboarding-docs # Generate repository documentation via egg-sdlc
│   ├── egg-pipeline-watch  # Real-time pipeline progress viewer via SSE
│   ├── egg-orch            # Symlink to orch_cli.py
│   └── git-credential-github-token
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
│   ├── contract_cli.py     # SDLC contract CLI implementation
│   ├── orchestration.py    # Multi-agent orchestration support
│   ├── orch_cli.py         # Orchestrator CLI implementation
│   ├── orch_client.py      # Orchestrator API client
│   ├── sdlc_cli.py         # SDLC pipeline CLI
│   ├── sdlc_hitl.py        # SDLC human-in-the-loop support
│   ├── data/               # Static data files (HITL editing rules)
│   └── self_improvement/   # Self-improvement data collection
├── llm/                    # Claude Code / Agent SDK integration
├── tools/                  # Interactive tools
│   ├── discover-tests.py   # Test framework discovery
│   └── github-app-token.py # Token generation utility
├── agent-config/           # Agent rules and commands (deployed into containers)
│   ├── commands/           # Custom slash commands
│   └── rules/              # Agent behavior rules
├── claude-commands/        # Symlink → agent-config/commands
├── claude-rules/           # Symlink → agent-config/rules
├── scripts/                # Container helper scripts
├── tests/                  # Sandbox tests (command timeout, test infrastructure)
└── CLAUDE.md               # Agent navigation guide
```

## Shared Libraries

```
shared/
├── egg_harness/            # Custom coding harness — provider-abstracted agent runtime (extractable, no egg imports)
│   ├── __init__.py         # Public API exports
│   ├── __main__.py         # CLI entry point (python3 -m egg_harness)
│   ├── client.py           # run_agent(), run_agent_async() high-level API
│   ├── loop.py             # Core agent loop with compaction support
│   ├── session.py          # Session persistence (JSONL serialize/resume)
│   ├── compaction.py       # Context management / compaction strategy
│   ├── events.py           # Event bus / callback system
│   ├── config.py           # Provider config, model aliases, context window lookup
│   ├── prompt.py           # System prompt assembly (generic)
│   ├── cost.py             # Token cost tracking with hardcoded rates
│   ├── result.py           # AgentResult dataclass (backward-compatible + compaction_count)
│   ├── interactive.py      # Interactive terminal REPL mode
│   ├── providers/          # LLM provider abstractions
│   │   ├── base.py         # Provider ABC, StreamEvent union type (8 event types)
│   │   ├── anthropic.py    # Anthropic Messages API (via SDK, gateway-routed)
│   │   └── openai_compat.py # OpenAI-compatible endpoints (via httpx SSE)
│   ├── tools/              # Standard tool implementations
│   │   ├── registry.py     # Tool registration, dispatch, permission callbacks
│   │   ├── bash.py         # Shell execution (process group timeout, no shell=true)
│   │   ├── read.py         # File reading (line numbers, offset/limit, binary detection)
│   │   ├── write.py        # File creation/overwrite
│   │   ├── edit.py         # Exact string replacement editing
│   │   ├── glob_tool.py    # File pattern matching (pathlib/fd)
│   │   ├── grep.py         # Content search (ripgrep)
│   │   ├── web_fetch.py    # URL fetch + HTML-to-markdown
│   │   └── web_search.py   # Web search
│   └── pyproject.toml      # Package metadata and dependencies
├── egg_harness_integration/ # Egg-specific harness wiring (tools, permissions, prompt, compaction)
│   ├── __init__.py
│   ├── egg_tools.py        # Egg-native tool registration (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli)
│   ├── egg_prompt.py       # CLAUDE.md rule-merging replicating setup_agent_rules()
│   ├── egg_permissions.py  # Role-based file access via egg_restrictions
│   ├── egg_compaction.py   # Anchor-based compaction (#1032 integration)
│   └── harness_factory.py  # Factory wiring all egg integrations into AgentLoop
├── egg_agent/              # Agent SDK wrapper with harness selection (routes via EGG_HARNESS)
│   ├── __init__.py         # Public API: AgentResult, build_agent_command
│   ├── __main__.py         # CLI entry point (python3 -m egg_agent)
│   ├── client.py           # run_agent(), run_agent_async() with harness selection routing
│   ├── command.py          # build_agent_command() for orchestrator-spawned containers
│   ├── result.py           # AgentResult dataclass
│   └── tool_interceptor.py # Pre-execution file write checks (Write/Edit/NotebookEdit) against role restrictions
# (No egg_babysit package — replaced by the /babysit-pr MCP skill in issue #1748.
#  Babysit cycles now run through the orchestrator's implement-phase route with
#  mode=babysit and has_contract=false. See docs/guides/babysit-pr.md.)
├── egg_anchor/             # Agent anchor mechanism for post-compaction state recovery
│   ├── __init__.py         # Public API exports
│   ├── models.py           # Pydantic models (AgentAnchor, AnchorMeta, ProgressItem, Decision, BRCState)
│   ├── loader.py           # Atomic file read/write (temp-then-rename), API sync helper
│   ├── validator.py        # Schema validation, size budget enforcement (soft/hard limits)
│   ├── constants.py        # Re-exports anchor constants from egg_config
│   └── tests/              # Unit tests for models, loader, validator
├── egg_config/             # Configuration utilities
│   ├── constants.py        # Centralized constants (ports, networks, container names, infrastructure branch names, anchor size limits)
│   ├── compose_config.py   # Bridges config.yaml settings to docker-compose environment variables
│   └── validators.py       # Validation functions (URLs, emails, tokens, check commands)
├── egg_restrictions/        # Shared agent file restriction patterns and checking logic
│   ├── __init__.py         # Public API: AgentFilePattern, check_agent_file_access, validate_agent_push, match_pattern
│   ├── matchers.py         # Canonical glob-pattern matcher (match_pattern) shared by all four enforcement layers
│   ├── patterns.py         # Role-based file access patterns (AgentRole, AgentFilePattern, AGENT_PATTERNS)
│   └── checker.py          # File access validation (check_agent_file_access, validate_agent_push)
├── egg_container/          # Shared container-launch config builder
│   └── __init__.py         # build_sandbox_config(), build_sandbox_docker_cmd(), git_shadow_mounts(), phase_readonly_mounts(), ensure_egg_state_dirs(), to_dockerpy_kwargs()
├── egg_contracts/          # SDLC contract models, plan parser, role-based validation, HITL, feedback, phase checks, multi-agent orchestration, checkpoints
│   ├── models.py           # Pydantic models including CheckDefinition, CheckResult, PhaseConfig, AgentExecutionModel
│   ├── phase_defaults.py   # Default check configurations per SDLC phase
│   ├── agent_roles.py      # Multi-agent role definitions (all agent and reviewer roles)
│   ├── orchestrator.py     # Multi-agent orchestration dispatch logic
│   ├── orchestration.py    # Agent execution state management
│   ├── dependency_graph.py # Generic dependency graph (PEP-695 typed): used for agent-role DAGs and for the implement-phase slice DAG (#2137 generification)
│   ├── plan_parser.py      # Plan document parsing with task extraction and phase dependency normalization
│   ├── agent_recovery.py   # Failed agent recovery logic
│   ├── checkpoints.py      # Checkpoint data models
│   ├── checkpoint_loader.py # Checkpoint storage and retrieval
│   ├── checkpoint_cli.py   # Checkpoint browsing CLI (list, show, browse, context, cost, search)
│   ├── transcript_extractor.py # API transcript extraction
│   └── redactor.py         # Sensitive data redaction for checkpoints
├── check-fixers.yml         # Per-check fixer config (non-LLM fixes, retries, model)
├── prompts/                # Shared prompt criteria (used by GHA scripts AND orchestrator)
│   ├── agent-design-criteria.md  # Agent-mode design review criteria
│   ├── autofixer-rules.md        # Autofixer auto-fix vs report-only rules
│   ├── code-review-criteria.md   # Code review security/correctness criteria
│   ├── contract-review-criteria.md # Contract verification rules
│   └── onboarding-docs-prompt.md # Documentation onboarding standard (egg-onboarding-docs)
├── egg_git/                # Git utilities
│   ├── cross_process_lock.py # Cross-process flock serialization for shared bare-repo git ops (bare_repo_lock, lock_path_for_repo)
│   └── default_branch.py   # get_default_branch() helper
├── egg_health/             # Runtime health-transition tracking (readiness history for /api/v1/health)
│   ├── __init__.py         # Public API: HealthTracker
│   └── tracker.py          # Thread-safe healthy/unhealthy transition recorder with snapshot()
├── egg_logging/            # Structured logging
├── egg_overseer/           # Shared overseer library (advisor, issue filing, priority, scrubbing, state)
│   ├── __init__.py         # Package docstring only — import from submodules (e.g. `from egg_overseer.advisor import consult_advisor`)
│   ├── advisor.py          # Opus advisor wrapper — consult_advisor(), AdvisorVerdict (decision: alert|file_issue|watch)
│   ├── infra_error.py      # Infrastructure error detection helpers
│   ├── issue_template.py   # Canonical issue body template (TEMPLATE_LITERAL)
│   ├── priority.py         # Priority label helpers (label_to_alert, alert_to_label)
│   ├── scrubbing.py        # scrub_secrets() — defense-in-depth secret scrubbing for issue bodies
│   └── state.py            # FiledIssueRecord + load_filed_issues()/append_filed_issue(), AgentTimingState + load_agent_timing()/save_agent_timing(), compute_anomaly_signature()
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
├── test_babysit_pr/               # Babysit-PR BRC cycle integration tests
│   ├── __init__.py
│   ├── conftest.py                # Fixtures for babysit-pr tests
│   ├── test_skill.py              # /babysit-pr MCP skill tests (argument validation, POST, 409 duplicate)
│   ├── test_pipeline.py           # End-to-end implement-phase BRC cycle against a fixture PR
│   ├── test_gateway.py            # Staging-branch push validation via the gateway
│   └── test_escalation.py         # Early-exit paths (fork, merged, empty diff) and final-push head-move escalation
├── local_pipeline/                # Orchestrator pipeline integration tests
│   ├── conftest.py                # Pipeline test fixtures
│   ├── docker-compose.yml         # Orchestrator test environment
│   ├── helpers.py                 # Shared API helper functions for tests
│   ├── mock-sandbox/              # Mock sandbox for testing
│   ├── test_api_validation.py     # API input validation tests
│   ├── test_concurrent_pipelines.py  # Concurrent pipeline execution tests
│   ├── test_error_recovery.py     # Error recovery scenario tests
│   ├── test_hitl_edge_cases.py    # HITL decision edge case tests
│   ├── test_k8s_deployment_tools.py  # End-to-end tests for MCP deployment diagnostic tools (k8s runtime)
│   ├── test_local_pipeline.py     # Orchestrator pipeline tests
│   ├── test_signals.py            # Signal handling tests
│   ├── test_unified_pipeline_behavior.py  # Unified pipeline behavior tests
│   └── test_worktree_integration.py  # Worktree lifecycle and pipeline isolation tests
└── sdlc/                          # SDLC pipeline integration tests
    ├── conftest.py                # SDLC test fixtures
    ├── test_happy_path.py         # Full pipeline success flow
    ├── test_review_rejection.py   # Reviewer rejection and fix cycles
    ├── test_hitl_flow.py          # Human-in-the-loop decision flow
    ├── test_role_enforcement.py   # Role-based mutation enforcement
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
├── shared/
│   └── egg_contracts/
│       ├── test_models.py         # Contract model tests including check models
│       ├── test_phase_defaults.py # Phase default configuration tests
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
├── build-autofixer-prompt.sh               # Autofixer workflow prompt builder (deprecated, use build-check-fixer-prompt.sh)
├── build-check-fixer-prompt.sh             # Per-check fixer workflow prompt builder
├── build-agent-mode-design-review-prompt.sh # Agent-mode design review prompt
├── build-doc-updater-prompt.sh             # Doc updater workflow prompt builder
├── build-conflict-prompt.sh                # Conflict resolution workflow prompt builder
├── build-contract-verification-prompt.sh  # Contract verification review prompt builder
├── verify-feedback-contract.sh             # Validates feedback-addressing agent response comments against the contract
├── tests/                                  # Bash tests for action scripts
│   └── verify-feedback-contract.test.sh   # Fixture-driven tests for verify-feedback-contract.sh
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
├── reusable-autofix.yml                    # Reusable auto-fix logic (deprecated, use reusable-check-fixer.yml)
├── reusable-check-fixer.yml                # Per-check fixer with non-LLM fixes and retry tracking
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
├── config.yaml.example        # Configuration template (copy to ~/.config/egg/config.yaml)
├── repositories.yaml.example  # Repository access configuration template
├── secrets.template.env        # Secrets template (includes Jira credential placeholders)
├── context-filters.yaml        # Operator allowlists for external integrations (jira.projects)
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
| Agent navigation | `CLAUDE.md` at component root | `gateway/CLAUDE.md`, `orchestrator/CLAUDE.md`, `sandbox/CLAUDE.md` |

## Documentation Organization

```
docs/
├── index.md                # Documentation navigation hub
├── architecture/           # System design and architecture docs
├── development/            # Developer guides (this file)
├── reference/              # Quick reference guides
├── setup/                  # Setup instructions
├── templates/              # SDLC phase document templates (analysis, plan)
└── troubleshooting/        # Common issues and solutions
```

**Rule**: Documentation should live close to code. Only cross-cutting docs belong in the central `docs/` directory.
