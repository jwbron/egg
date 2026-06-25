# Project Structure Guidelines

This document describes the directory structure conventions for egg.

Per-directory file listings are intended to be exhaustive: every Python module
in a listed directory should have a one-line entry, unless the listing is
explicitly truncated with `...`. A module missing from a non-truncated listing
is drift, not an intentional omission — backfill it (the doc-updater bot adds
entries for new files going forward).

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
├── plugins/                # Claude Code plugins (distributable via egg-tools marketplace)
├── sandbox/                # Sandbox container (untrusted, runs the LLM agent)
├── scripts/                # Validation, lint, and operational telemetry scripts
├── shared/                 # Shared Python libraries (used by gateway + sandbox)
├── skills/                 # Claude Code skills (installed into sandbox at startup)
├── tests/                  # Unit tests
├── dev                     # Development CLI (setup, lint, test, ci)
├── .claude-plugin/         # Marketplace registry (egg-tools plugin index)
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
| `plugins/` | Claude Code plugins distributed via the egg-tools marketplace (each subdirectory is a plugin with `.claude-plugin/plugin.json` and a `skills/` subtree) | External (installed by users via Claude Code) |
| `sandbox/` | Agent environment: Claude Code, tools, entrypoint | Sandbox container |
| `scripts/` | CI/lint and operational telemetry scripts (config validation, import checks, hardcoded port detection, reviewer job name enforcement, LLM API boundary enforcement, model alias enforcement); `prepare-sandbox-build-context.py` populates `repo-deps/` from `repositories.yaml` for `make build` | CI / local |
| `shared/` | Shared libraries: logging, config, git utilities, centralized constants | All containers |
| `skills/` | Claude Code skills (each subdirectory is a skill with `SKILL.md`) | Sandbox container |
| `tests/` | Test suite | CI / local |
| `.claude-plugin/` | Plugin marketplace registry (`marketplace.json`) — indexes plugins under `plugins/` for the `egg-tools` Claude Code plugin collection | External |

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
├── commit_registry_client.py # HTTP client for the orchestrator's commit-authorship registry (register + lookup_bulk + lookup_patch_ids for SHA-rewrite recovery)
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
├── consensus_wrapper.py    # Shell wrapper template: one-shot-only per-event arm (#3164 retired the in-pod wait-loop + 30s heartbeat; legacy capped-restart template and EGG_BRC_EVENT_PUMP flag deleted in #2908 slice-4). The orchestrator (event_loop.py) owns the BRC loop and spawns a one-shot pod per actionable event (EGG_EVENT_ACTION)
├── dag_visualizer.py       # ASCII DAG visualization for pipeline status
├── decision_queue.py       # HITL decision queue
├── events.py               # Event bus for pipeline events
├── gateway_client.py       # Gateway API client (sessions, worktrees, config)
├── handoffs.py             # Agent handoff data management
├── health_monitor.py       # Deterministic tripwire health monitor (progress events → auto-nudge/escalate)
├── message_store.py        # Inter-agent message types + store singleton accessor (Redis Streams only, #3159)
├── progress_store.py       # In-memory structured progress event store with configurable retention
├── peer_consensus.py       # BRC (Broadcast-Review-Converge) peer consensus tracker
├── pr_obligations.py       # Shared Pre-merge Obligations PR-body renderer (open + resolved sections from DeferredAction; shared by single-PR and slice-DAG context-PR paths — the legacy terminal-slice umbrella treatment was removed in #2777)
├── mcp_server.py           # MCP server providing comprehensive egg platform interface to Claude Code (port 9850)
├── mcp_tools.py            # MCP tool definitions and handlers: pipeline state, containers, messages, contracts, health, deployment
├── redaction.py            # Secret redaction helpers for operator-facing diagnostic output (env vars, Bearer JWTs, API key shapes)
├── metrics.py              # Pipeline metrics and telemetry
├── models.py               # Pydantic models for pipelines
├── redis_message_store.py  # Redis Streams-backed message store implementation
├── resilience.py           # Retry and error recovery
├── agent_salvage.py        # Salvage unpushed local commits to egg/recovered/* refs before worktree deletion (#2429)
├── agent_salvage_cleanup.py # Periodic TTL-based pruning of stale egg/recovered/* refs (#2446); driven by RecoveryRefCleaner background thread
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
│   ├── network-policies.yaml          # NetworkPolicies for agent isolation (Cilium-enforced)
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
│   ├── egg-artifact        # Symlink to scripts/egg-artifact; served artifact reads by spec name via gateway (#3216)
│   ├── egg-contract        # Symlink to contract_cli.py
│   ├── egg-onboarding-docs # Generate repository documentation via egg-sdlc
│   ├── egg-pipeline-watch  # Real-time pipeline progress viewer via SSE
│   ├── egg-orch            # Symlink to orch_cli.py
│   └── git-credential-github-token
├── egg_agent_tools/        # In-process SDK MCP server: 45 tools across 7 namespaces (sdlc, brc, phase, progress, task, confluence, jira)
│   ├── server.py           # build_sandbox_mcp_server(): one SDK server per namespace; SYSTEM_PROMPT_NUDGE generated at import
│   ├── schemas.py          # Tool JSON schemas + derive_schema_from_argparse (argparse → JSON Schema)
│   ├── push.py             # Gateway push helper (mcp__brc__propose pre-step)
│   ├── handlers/           # Handler functions called by @tool wrappers and CLIs (MCP↔CLI drift gate)
│   │   ├── _gateway.py     # gateway_request + gateway_data_request helpers
│   │   ├── confluence.py   # Confluence gateway-route handlers (page/space/search/execute; #2994)
│   │   ├── jira.py         # Jira gateway-route handlers (ticket CRUD/search/links/execute; #2994)
│   │   └── ...             # brc.py, sdlc.py, task.py, phase.py, progress.py, message.py, restrictions.py, brc_memory.py, errors.py
│   └── tools/              # @tool wrappers — one module per namespace, each exports REGISTRATIONS
│       ├── confluence.py   # mcp__confluence__* wrappers: 8 read-only gateway mirrors (#2994)
│       ├── jira.py         # mcp__jira__* wrappers: 9 gateway mirrors (5 reads + 4 writes) (#2994)
│       └── ...             # brc.py, sdlc.py, task.py, phase.py, progress.py, message.py, _common.py, _registry.py, _tool_compat.py
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
├── egg_agent/              # Claude Agent SDK wrapper
│   ├── __init__.py         # Public API: AgentResult, build_agent_command
│   ├── __main__.py         # CLI entry point (python3 -m egg_agent)
│   ├── client.py           # run_agent(), run_agent_async()
│   ├── command.py          # build_agent_command() for orchestrator-spawned containers
│   ├── result.py           # AgentResult dataclass
│   ├── tool_interceptor.py # Pre-execution file write checks (Write/Edit/NotebookEdit) against role restrictions
│   ├── tool_output_cap.py  # Predictive PreToolUse cap for built-in CC tools (Read/Grep): denies calls whose model-bound result is likely to be excessive (cost/context discipline, NOT the buffer-crash fix — that's the raised reader buffer in client.py, #2884); tunable via EGG_TOOL_OUTPUT_CAP / EGG_READ_CAP_BYTES (#2876); agents can raise their own session's Read cap by writing the byte size to /tmp/egg-read-cap-bytes (#3175)
│   ├── midturn_messages.py # Throttled PostToolUse hook that polls the message bus mid-turn and injects new operator-authored messages as additionalContext; backed by EGG_MIDTURN_MESSAGES_INTERVAL_SECS (default 60 s) and EGG_MIDTURN_MESSAGES=false escape hatch (#3123)
│   └── route_guidance.py   # Advisory system-prompt addendum appended only on LiteLLM (non-Claude) routes: steers toward batched tool calls, filtered output, and subagent-isolated bulk reads to cut turns × context cost; gated on ANTHROPIC_CUSTOM_MODEL_OPTION, kill switch EGG_ROUTE_PROMPT_GUIDANCE=false (#3175)
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
│   ├── __init__.py         # Public API: AgentFilePattern, check_agent_file_access, validate_agent_push, match_pattern, BLOCKED_HINTS, derive_hint
│   ├── matchers.py         # Canonical glob-pattern matcher (match_pattern) shared by all four enforcement layers
│   ├── patterns.py         # Role-based file access patterns (AgentRole, AgentFilePattern, AGENT_PATTERNS)
│   ├── phase_patterns.py   # Phase-scoped file-write patterns (PHASE_FILE_PATTERNS, PhaseFilePattern, phase_file_verdict) — sandbox mirror of gateway/phase_filter.py
│   ├── checker.py          # File access validation (check_agent_file_access, validate_agent_push)
│   └── hints.py            # Actionable push-denial hints keyed by blocked path category (BLOCKED_HINTS, derive_hint)
├── egg_session_placeholder/ # Session-token placeholder codec for the gateway's /v1/messages proxy
│   └── __init__.py         # Public API: PLACEHOLDER_PREFIX, to_placeholder, from_placeholder — wraps session tokens in sk-ant-oat01- envelope for token-keyed session lookup
├── egg_container/          # Shared container-launch config builder
│   └── __init__.py         # build_sandbox_config(), build_sandbox_docker_cmd(), git_shadow_mounts(), phase_readonly_mounts(), ensure_egg_state_dirs(), to_dockerpy_kwargs()
├── egg_contracts/          # SDLC contract models, plan parser, role-based validation, HITL, feedback, phase checks, multi-agent orchestration
│   ├── models.py           # Pydantic models including CheckDefinition, CheckResult, PhaseConfig, AgentExecutionModel
│   ├── phase_defaults.py   # Default check configurations per SDLC phase
│   ├── agent_roles.py      # Multi-agent role definitions (all agent and reviewer roles)
│   ├── orchestrator.py     # Multi-agent orchestration dispatch logic
│   ├── orchestration.py    # Agent execution state management
│   ├── dependency_graph.py # Generic dependency graph (PEP-695 typed): used for agent-role DAGs and for the implement-phase slice DAG (#2137 generification)
│   ├── plan_parser.py      # Plan document parsing with task extraction and phase dependency normalization
│   ├── agent_recovery.py   # Failed agent recovery logic
│   ├── artifact_spec.py    # Declarative registry of per-phase coordination artifacts producers commit and consumers read (#3077)
│   ├── audit.py            # Audit log entries for contract modifications
│   ├── decisions.py        # Decision.id allocator helpers for the shared decision-N / cq-N namespace
│   ├── feedback.py         # Feedback comment handling for the SDLC pipeline
│   ├── hitl.py             # HITL (human-in-the-loop) checkbox handling for the SDLC pipeline
│   ├── impasse.py          # Typed Impasse primitive — runtime escape hatch for structurally impossible tasks (#2529)
│   ├── loader.py           # Contract loading, saving, and initialization (persistence layer)
│   ├── markdown.py         # Markdown soft-break unwrapper for pipeline-generated PR bodies (unwrap_soft_breaks, #3122)
│   ├── redactor.py         # Sensitive data redaction (env vars, secrets, sensitive file paths)
│   ├── resilience.py       # Resilience utilities for external failure handling (retries, backoff)
│   ├── roles.py            # Contract-mutation role definitions and field ownership mapping
│   ├── validator.py        # Contract mutation validator — enforces role permissions on field writes
│   └── tests/              # In-package test suite (complements tests/shared/egg_contracts/)
│       ├── test_agent_roles.py                  # reviewer_security / reviewer_concurrency role tests (#1965)
│       ├── test_artifact_spec.py                # Artifact-spec consistency suite (#3077)
│       ├── test_composite_execution.py          # Composite (phase_id, role) execution tracking tests
│       ├── test_orchestrator.py                 # load_agent_output / save_agent_output identifier-prefixed path tests
│       ├── test_orchestrator_phase_id.py        # Orchestrator phase_id parameter tests
│       ├── test_plan_parser_dependencies.py     # Plan-parser dependencies field propagation tests
│       ├── test_slice_migration.py              # Phase → Slice schema-rename migration shim tests (#2137)
│       ├── test_validate_forest.py              # Slice-DAG forest validation tests (#2137)
│       ├── test_validate_slice_file_overlap.py  # Slice file-overlap validation tests (#3046)
│       ├── test_validate_task_role_alignment.py # Plan task/producer-role alignment validation tests (#2527)
│       └── test_validator_demote_only.py        # Reviewer demote-only task-status write tests (#3114)
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
├── egg_tool_output.py      # Tool-output size caps for egg-owned MCP tools — truncation + spill-to-file helpers shared by orchestrator MCP server and sandbox @tool wrappers (EGG_TOOL_OUTPUT_CAP_BYTES, #2805)
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
├── test_credential_security.py    # Credential isolation verification
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
├── test_k8s_deployment_tools.py   # Auth-rejection regression suite for the #1759 deployment MCP routes
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
│       ├── test_agent_recovery.py # Agent recovery and circuit breaker tests
│       ├── test_agent_roles.py    # Multi-agent role definition tests
│       ├── test_audit.py          # Contract-modification audit log tests
│       ├── test_decisions.py      # cq-N Decision.id allocator tests
│       ├── test_feedback.py       # Feedback comment handling tests
│       ├── test_hitl.py           # HITL checkbox handling tests
│       ├── test_loader.py         # Contract loader / persistence tests
│       ├── test_markdown.py       # Markdown soft-break unwrapper tests (unwrap_soft_breaks, #3122)
│       ├── test_models.py         # Contract model tests including check models
│       ├── test_models_gaps.py    # Task.gaps regression tests (#1917)
│       ├── test_models_task_description.py # Contract.task_description regression tests (#3033)
│       ├── test_phase_defaults.py # Phase default configuration tests
│       ├── test_plan_parser.py    # Plan document parsing tests
│       ├── test_pr_metadata.py    # PRMetadata legacy context-field removal tests (#2777-replan)
│       ├── test_redactor.py       # Redactor tests for sensitive data masking
│       ├── test_resilience.py     # External-failure resilience utility tests
│       ├── test_roles.py          # Role / field-ownership mapping tests
│       └── test_validator.py      # Contract mutation validator tests
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
├── config.yaml.example          # Configuration template (copy to ~/.config/egg/config.yaml)
├── repositories.yaml.example    # Repository access configuration template
├── secrets.template.env         # Secrets template (includes Jira credential placeholders)
├── context-filters.yaml         # Operator allowlists for external integrations (jira.projects)
├── litellm-models.template.yaml # Operator template for registering non-Claude backends (copy to ~/.config/egg/litellm-models.yaml)
├── litellm/                     # egg-litellm image sources
│   ├── Dockerfile               # Builds egg-litellm: stock LiteLLM + prompt-cache patches
│   ├── patch_litellm_cache.py   # Build-time patches for cache_control passthrough on Qwen/DeepSeek routes
│   └── cost_callback.py         # LiteLLM custom logger: upstream + estimated cost, per-role attribution (x-egg-* headers), cache hit rate -> pod stdout
├── repo_config.py               # Python API for repo access
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
| Agent navigation | `CLAUDE.md` at component root (with `AGENTS.md` symlink alias) | `gateway/CLAUDE.md`, `orchestrator/CLAUDE.md`, `sandbox/CLAUDE.md` |

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
