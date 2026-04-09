# Orchestrator

Central coordination engine for egg's SDLC pipeline execution, container lifecycle, and multi-agent orchestration.

## Overview

The orchestrator manages the end-to-end SDLC pipeline that turns GitHub issues into reviewed pull requests. It:

- **Manages pipeline state** — persists phase transitions, agent executions, and decisions on a git-backed state branch
- **Spawns and monitors agent Jobs** — creates Kubernetes Jobs in the `egg-agents` namespace with proper configuration
- **Coordinates multi-agent execution** — runs specialized agents across five categories (execution, analysis, review, utility, interface) in dependency-ordered waves or concurrently with message-based coordination
- **Handles HITL decisions** — queues questions for human reviewers and blocks until resolved
- **Streams real-time status** — provides SSE streams and DAG visualizations for pipeline monitoring

## Architecture

```
Namespace: egg-system                    Namespace: egg-agents
┌─────────────────────────────────────┐  ┌──────────────────────────────┐
│  ┌──────────────┐  ┌─────────────┐  │  │  ┌──────┐ ┌──────┐ ┌─────┐ │
│  │ Orchestrator │  │   Gateway   │  │  │  │Coder │ │Tester│ │Doc. │ │
│  │   :9849      │  │   :9848     │  │  │  │ Job  │ │ Job  │ │ Job │ │
│  │              │  │   :3129     │  │  │  └──┬───┘ └──┬───┘ └──┬──┘ │
│  │ - Pipeline   │  │   :9851    │  │  │     │        │        │    │
│  │ - State      │  │            │  │  │     └────────┴────────┘    │
│  │ - HITL       │  │ - Sessions │  │  │              │             │
│  └──────┬───────┘  │ - Policy   │◄─┼──┼──────────────┘             │
│         │          │ - Proxy    │  │  │  (egress only to gateway)  │
│         │ k8s API  └────────────┘  │  └──────────────────────────────┘
│         └─────────────────────────►│
│           (create/delete Jobs)     │
└─────────────────────────────────────┘
```

## Key Concepts

### Pipeline Phases

Pipelines progress through four SDLC phases:

| Phase | Purpose | Agents |
|-------|---------|--------|
| **refine** | Analyze task, evaluate options | Refiner, reviewers |
| **plan** | Break work into tasks, assess risks | Architect, Task Planner, Risk Analyst, reviewers |
| **implement** | Write code, tests, docs | Coder, Tester, Documenter, reviewers |
| **review** | Cross-phase review | Dynamically assigned reviewers |
| **pr** | Create pull request | Single agent |

Each phase transition requires human approval (except implement → pr when all checks pass). All phases support concurrent agent execution via BRC consensus.

### Agent Categories

Agent roles are organized into five categories defined in `shared/egg_contracts/agent_roles.py`:

| Category | Description | Example Roles |
|----------|-------------|---------------|
| **Execution** | Produce primary artifacts | coder, tester, documenter |
| **Analysis** | Analyze and plan work | refiner, architect, task_planner, risk_analyst |
| **Review** | Validate quality | reviewer_code, reviewer_contract, reviewer_plan |
| **Utility** | Cross-cutting support | autofixer, conflict_resolver |
| **Interface** | Monitoring and health | inspector, overseer |

See [Agent Roles Reference](../docs/reference/agent-roles.md) for the complete roster.

### State Persistence

Pipeline state is stored on a dedicated `egg/pipeline-state` orphan branch accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree`. The branch is local-only (never pushed to remote) and persists across orchestrator restarts via the Docker state volume.

### Concurrent Execution Mode

By default, agents within the refine, plan, and implement phases run simultaneously via BRC consensus (configurable via `concurrent_phases`). When `concurrent_execution: true` is set, this extends to all phases. Agents coordinate through:

- **Message bus** — Agents exchange typed messages (PROGRESS, QUESTION, RESPONSE, STATUS, AGENT_FAILED) via the orchestrator's message API. Messages can target a specific role or broadcast to all agents.
- **Readiness consensus** — Each agent signals its readiness state (WORKING, READY, BLOCKED, OBJECTING). The phase advances only when all agents reach READY. Any OBJECTING agent blocks phase completion.
- **Shared pipeline branch** — All concurrent agents operate on the pipeline's shared branch (e.g., `egg/issue-999`). Agents coordinate commits via the message bus.

The `GET /pipelines/{id}/status` endpoint includes a `concurrent` section when this mode is active, showing message counts, consensus state, and agent lifecycle info. See [SDLC Pipeline Guide — Concurrent Execution](../docs/guides/sdlc-pipeline.md#concurrent-execution-mode) for full details.

### Worktree Sync

Before each pipeline phase starts, the orchestrator syncs the agent worktree with the remote branch so downstream code (contract loading, draft reading) sees the full pipeline state. The sync behavior depends on the prior phase's outcome:

- **Prior phase succeeded + local ahead of remote:** Local commits are pushed to remote before resetting, preserving completed work.
- **Prior phase failed + local ahead of remote:** Local commits are discarded and the worktree is reset to remote, removing incomplete work.
- **Local and remote diverged:** A fast-forward merge is attempted. If the merge fails, the orchestrator logs an error and leaves the worktree unchanged (may require manual intervention).
- **Local behind or in-sync with remote:** Standard reset to remote tip.

### HITL Decisions

The orchestrator queues blocking decisions for human input and pauses pipeline execution until resolved. Decisions are typed via the `decision_type` field to enable context-appropriate rendering:

| Type | Purpose | Example |
|------|---------|---------|
| `phase_gate` | Phase approval with draft review | "Approve this analysis?" |
| `choice` | Select from discrete options | "Which database?" |
| `feedback` | Collect structured multi-question answers | "What is the expected traffic volume?" |

Decisions also support a `questions` field (list of `{id, question, answer}` dicts) for structured feedback collection. In local mode, the terminal handler (`sdlc_hitl.py`) dispatches to type-specific UIs; in issue mode, decisions flow through GitHub comments.

Resolution payloads are JSON objects with an `action` field (`approve`, `select`, `submit_feedback`, `request_changes`, `change_approach`), enabling the pipeline to distinguish between approval, selection, feedback submission, and revision requests. Legacy bare-string resolutions are still supported for backward compatibility.

## API Endpoints

All endpoints are prefixed with `/api/v1`.

### Pipelines

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | List all pipelines |
| `POST` | `/pipelines` | Create new pipeline |
| `GET` | `/pipelines/{id}` | Get pipeline details |
| `PATCH` | `/pipelines/{id}` | Update pipeline (async container cleanup) |
| `DELETE` | `/pipelines/{id}` | Delete pipeline |
| `GET` | `/pipelines/{id}/status` | Get pipeline status summary |
| `POST` | `/pipelines/{id}/start` | Start or restart pipeline |
| `GET` | `/pipelines/{id}/visualization` | Get DAG visualization (JSON, text, or ASCII) |
| `GET` | `/pipelines/{id}/stream` | SSE stream for single pipeline |
| `GET` | `/pipelines/stream` | Unified SSE stream for all active pipelines |

**PATCH cancel/fail behavior:** When a pipeline is updated to `cancelled` or `failed` status, the PATCH handler cancels pending HITL decisions and marks agent records as terminated synchronously, then returns the response immediately. Container and worktree cleanup runs in a background daemon thread so the caller is not blocked by slow Docker/gateway operations. The response includes `cleanup_pending: true` to indicate that container teardown is still in progress. The DELETE handler re-runs `cleanup_pipeline()` as a safety net, so any containers not yet removed by the background thread will be caught there.

### Signals

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/signal` | Sandbox completion/progress/error callbacks (with branch verification) |
| `POST` | `/pipelines/{id}/signal/batch` | Batch multiple signals |

**Completion signal branch verification:** When an agent signals completion with a `commit` SHA, the orchestrator verifies the commit exists on the pipeline's expected branch. If the commit is not found on the expected branch (e.g., the agent pushed to an improvised branch name), the signal is rejected with HTTP 409. Verification failures (network issues, git errors) are non-blocking — the signal is accepted when verification cannot be performed. Additionally, the orchestrator logs a warning if no new commits have been pushed since the phase started (detected via `phase_start_sha` recorded at phase start).

### Restart

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/agents/{role}/restart` | Restart a single stuck agent (stop, reset consensus, respawn with same config) |
| `POST` | `/pipelines/{id}/phases/{phase}/restart` | Restart an entire phase (stop all containers, reset consensus and review cycles, respawn all agents) |

### Containers

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/spawn` | Spawn container |
| `GET` | `/pipelines/{id}/containers` | List containers |
| `GET` | `/pipelines/{id}/containers/{cid}` | Get container details |
| `DELETE` | `/pipelines/{id}/containers/{cid}` | Remove container |
| `POST` | `/pipelines/{id}/containers/{cid}/stop` | Stop container |
| `GET` | `/pipelines/{id}/containers/{cid}/logs` | Get container logs |
| `GET` | `/pipelines/{id}/containers/{cid}/health` | Container health check |

### HITL Decisions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines/{id}/decisions` | List pending decisions |
| `POST` | `/pipelines/{id}/decisions` | Create decision |
| `GET` | `/pipelines/{id}/decisions/{did}` | Get decision details |
| `POST` | `/pipelines/{id}/decisions/{did}/resolve` | Resolve decision |
| `POST` | `/pipelines/{id}/decisions/{did}/cancel` | Cancel decision |
| `GET` | `/pipelines/{id}/decisions/status` | Decision queue summary |

### Phases

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines/{id}/phase` | Get current phase |
| `POST` | `/pipelines/{id}/phase` | Advance to next phase |
| `POST` | `/pipelines/{id}/phase/start` | Start current phase |
| `POST` | `/pipelines/{id}/phase/complete` | Complete current phase |
| `POST` | `/pipelines/{id}/phase/fail` | Fail current phase |

### Structured Progress

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/progress` | Emit structured progress event |
| `GET` | `/pipelines/{id}/progress` | Query progress events (filterable by agent, time, limit) |

### Anchors

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/anchors/{agent_id}` | Create or update agent anchor |
| `GET` | `/anchors/{agent_id}` | Get agent anchor |
| `DELETE` | `/anchors/{agent_id}` | Delete agent anchor |
| `GET` | `/anchors/team/{pipeline_id}` | Get team anchor (orchestrator-generated projection) |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health |
| `GET` | `/ready` | Readiness check |
| `GET` | `/live` | Liveness check |
| `GET` | `/pipelines/{id}/health` | On-demand pipeline health check |
| `GET` | `/pipelines/{id}/health/alerts` | Active deterministic health alerts |

### Metrics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/metrics` | Orchestrator metrics (JSON) |
| `GET` | `/metrics/prometheus` | Metrics in Prometheus format |

## Deployment Modes

| Mode | Orchestrator? | Agents | Use Case |
|------|---------------|--------|----------|
| **local** | No | Single interactive sandbox | Local development, ad-hoc tasks |
| **remote-single** | Yes | Single sandbox, signals back | CI/CD integration, single-task automation |
| **distributed** | Yes | Multiple sandboxes in waves | Multi-agent workflows, complex implementations |

See [Architecture: Deployment Modes](../docs/architecture/orchestrator.md#deployment-modes) for diagrams and details.

## Files

```
orchestrator/
├── api.py                  # Flask REST API server with blueprint registration
├── cli.py                  # CLI interface (serve, health, pipelines commands)
├── models.py               # Pydantic models (Pipeline, AgentExecution, HITLDecision, ReviewVerdict, etc.)
├── state_store.py          # Git-backed persistent state storage
├── container_backend.py    # ContainerBackend Protocol (abstract interface for container runtimes)
├── kubernetes_client.py    # Kubernetes client wrapper (implements ContainerBackend)
├── kubernetes_spawner.py   # Kubernetes Job spawning with gateway session integration
├── kubernetes_monitor.py   # Kubernetes Job/Pod state monitoring and lifecycle tracking
├── decision_queue.py       # HITL decision queue management (supports typed decisions)
├── handoffs.py             # Agent-to-agent data handoff mechanism
├── gateway_client.py       # Gateway API client for session management
├── sandbox_template.py     # Sandbox container configuration templates
├── mcp_server.py           # SSE-based MCP server for pipeline management tools (port 9850)
├── mcp_tools.py            # MCP tool definitions and handlers (submit_task, get_status, checkpoints, contracts, etc.)
├── events.py               # Event emission and tracking
├── health_monitor.py       # Deterministic tripwire processor (heartbeat, error repeat, stall detection)
├── progress_store.py       # In-memory structured progress event storage
├── health_checks/          # Two-tier health check framework (see health_checks/README.md)
│   ├── types.py            # HealthCheck protocol, HealthResult, enums
│   ├── context.py          # PipelineHealthContext with lazy properties
│   ├── runner.py           # HealthCheckRunner — trigger dispatch and tier escalation
│   ├── tier1/              # Programmatic checks (fast, deterministic)
│   │   ├── container_liveness.py   # Verify RUNNING Jobs/Pods exist in Kubernetes
│   │   ├── startup_state.py        # Post-startup reconciliation verification
│   │   ├── phase_output.py         # Detect missing artifacts (commits, plans)
│   │   └── state_consistency.py    # Cross-reference orchestrator state vs k8s vs contract
│   └── tier2/              # Semantic checks (LLM-powered)
│       └── agent_inspector.py   # Claude-powered agent progress analysis
├── sse.py                  # Server-Sent Events for real-time status
├── unified_sse.py          # Unified SSE stream for multiple pipelines
├── dag_visualizer.py       # Pipeline DAG visualization
├── consensus_wrapper.py    # BRC consensus wrapper script builder (transient crash detection and restart with backoff)
├── concurrent_executor.py  # Concurrent phase execution with BRC consensus
├── peer_consensus.py       # Peer consensus tracker for BRC protocol
├── resilience.py           # Retry, circuit breaker, and resilience patterns
├── metrics.py              # Metrics collection and reporting
├── status_reporter.py      # Status reporting utilities
├── webhooks.py             # GitHub webhook handlers
├── Dockerfile              # Container image (Python 3.11-slim)
├── entrypoint.sh           # Container startup script
├── requirements.txt        # Python dependencies
├── overseer/               # Overseer agent server-side logic
│   ├── __init__.py         # Package init
│   ├── monitor.py          # Main poll loop, health checks, CLI wrappers (explicit pipeline routing)
│   ├── classifier.py       # Haiku classification (stall, loop, error, off-track)
│   ├── decision_maker.py   # Sonnet/Opus corrective decision-making
│   ├── issue_filer.py      # Autonomous GitHub issue filing with diagnostics
│   ├── self_monitor.py     # Self-monitoring (poll timing, message volume, LLM costs)
│   └── utils.py            # Utility functions
├── routes/
│   ├── anchors.py          # Agent anchor CRUD and team anchor generation endpoints
│   ├── containers.py       # Container lifecycle endpoints
│   ├── decisions.py        # HITL decision endpoints
│   ├── health.py           # Health check endpoints (includes /health/alerts)
│   ├── metrics.py          # Metrics endpoints
│   ├── phases.py           # Phase management endpoints
│   ├── pipelines.py        # Pipeline CRUD endpoints
│   ├── progress.py         # Structured progress ingestion and query endpoints
│   └── signals.py          # Sandbox signal callback endpoints
└── tests/                  # Unit and integration tests (30+ files, including health check and concurrent execution tests)
```

## MCP Server

The orchestrator includes an MCP server (port 9850) that exposes pipeline management and checkpoint tools to Claude Code and other MCP clients via Streamable HTTP transport.

### Gateway-Backed Tools

These tools require a `gateway_url` and authenticate via a gateway session. The session is registered with `pipeline_id="mcp-server"`, which uses the gateway's exemption for orchestrator-internal sessions (no repos list required).

| Tool | Description |
|------|-------------|
| `list_checkpoints` | List agent checkpoints with filters (issue, pipeline, agent_type, phase, status, repo, limit) |
| `search_checkpoints` | Search checkpoint metadata by text with filters (issue, pipeline, agent_type, repo, limit) |
| `get_contract` | Get SDLC contract state by issue number or task ID |

Both checkpoint tools accept an optional `repo` parameter (string, `owner/repo` format) to specify the checkpoint repository when checkpoints are stored separately (e.g., `jwbron/egg-checkpoints`). The value is forwarded as `source_repo` to the gateway.

### Orchestrator-Backed Tools

`submit_task`, `get_status`, `provide_input`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `validate_config`, `restart_agent`, `restart_phase`

The `restart_agent` tool accepts `task_id`, `agent_role`, and optional `reason` parameters. It proxies to the agent restart API endpoint. The `restart_phase` tool accepts `task_id`, `phase`, and optional `reason`/`context` parameters. It proxies to the phase restart API endpoint. Both are available to HITL operators via the MCP server.

## Health Check Framework

The orchestrator includes a two-tier health check framework for proactive pipeline failure detection. See [`health_checks/README.md`](health_checks/README.md) for full details.

### Overview

Health checks run at key lifecycle points to catch infrastructure and semantic failures before they cascade. The framework uses a common `HealthCheck` protocol so new checks can be added without modifying the runner or integration points.

**Tier 1 (Programmatic)** — Fast, deterministic checks that verify structural invariants:

| Check | Purpose | Triggers |
|-------|---------|----------|
| `ContainerLivenessCheck` | Verify RUNNING containers exist in Docker | All |
| `StartupStateCheck` | Post-startup reconciliation verification | STARTUP, ON_DEMAND |
| `PhaseOutputPresenceCheck` | Detect missing artifacts (commits, plans) | WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND |
| `StateConsistencyCheck` | Cross-reference orchestrator state vs Docker vs contract | RUNTIME_TICK, WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND |

**Tier 2 (Semantic)** — LLM-based checks that evaluate whether agents made meaningful progress:

| Check | Purpose | Triggers |
|-------|---------|----------|
| `AgentInspectorCheck` | Claude-powered analysis of agent git history, outputs, and contract state | WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND |

### Lifecycle Triggers

| Trigger | When | Tier 1 | Tier 2 |
|---------|------|--------|--------|
| `STARTUP` | Orchestrator boot | Always | No |
| `RUNTIME_TICK` | Container state change | Always | No |
| `WAVE_COMPLETE` | After each agent wave | Always | If Tier 1 DEGRADED |
| `PHASE_COMPLETE` | Before phase advance | Always | Always |
| `ON_DEMAND` | `GET /pipelines/{id}/health` | Always | Always |

### Event Types

Health check results are emitted via the EventBus:

| Event | Description |
|-------|-------------|
| `system.health_check.started` | Runner begins check execution |
| `system.health_check.completed` | Individual check or aggregate completion |
| `system.health_check.degraded` | Check returned DEGRADED status |
| `system.health_check.failed` | Check returned FAILED status |

### Phase-Advance Gating

When health checks run at `PHASE_COMPLETE`, a `FAIL_PIPELINE` action blocks the phase transition (returns 409 Conflict). This prevents advancing past a broken state.

## Pipeline Health Monitoring

Beyond the lifecycle-triggered health checks above, the orchestrator provides continuous real-time monitoring via two tiers:

### Structured Progress API

Agents emit structured progress events via `POST /api/v1/pipelines/{id}/progress`. Events include step name, state (`working`/`blocked`/`complete`), detail text, and optional blocker description. Progress is stored in-memory with configurable retention (`progress_store.py`).

CLI: `egg-orch progress emit --step "..." --state working` / `egg-orch progress query`

### Deterministic Tripwires

The `health_monitor.py` module subscribes to EventBus events and evaluates six tripwire rules. Thresholds are **phase-aware**: during the implement phase, the heartbeat and progress stall thresholds use `orchestrator_implement_heartbeat_timeout_seconds` (default 600s) instead of the standard 120s. Agents idle in BRC protocol (waiting for an upstream producer's proposal) are suppressed from heartbeat and progress stall checks.

| Tripwire | Threshold Config | Action |
|----------|-----------------|--------|
| Heartbeat timeout | `orchestrator_heartbeat_timeout_seconds` (120s) / `orchestrator_implement_heartbeat_timeout_seconds` (600s) | Escalate to overseer/HITL |
| Container exit | — | HITL escalation |
| Repeated errors | `orchestrator_error_repeat_threshold` (3) | Escalate to overseer |
| Message volume spike | `orchestrator_message_rate_limit` (20/min) | Auto-throttle |
| Progress stall | Same as heartbeat (phase-aware) | Escalate to overseer/HITL |
| Infrastructure error | Pattern-matched on blocked progress events | Critical alert → HITL fast-path |

### Overseer Agent

Phase-scoped: spawned at the start of each pipeline phase and torn down when the phase completes, advances, or fails (when `overseer_enabled` is true). Each phase gets a fresh overseer instance with no accumulated state. Uses Haiku for anomaly classification and Sonnet/Opus for corrective decisions via `shared/egg_agent/`. No code access. The health monitor thread auto-respawns the overseer if it exits mid-phase, gated by a `phase_overseer_active` flag to prevent respawn between phases. All CLI operations (alert broadcasting, message sending, alert resolution, HITL decisions) pass the pipeline ID explicitly to prevent cross-pipeline alert leakage. See `orchestrator/overseer/` for server-side logic.

See the [Pipeline Health Monitoring Guide](../docs/guides/pipeline-health-monitoring.md) for the full reference.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EGG_ORCHESTRATOR_MODE` | Deployment mode (`local`, `remote-single`, `distributed`) | `local` |
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL | `http://egg-orchestrator:9849` |
| `EGG_PIPELINE_ID` | Current pipeline identifier | None |
| `EGG_AGENT_ROLE` | Agent role for multi-agent mode | None |
| `EGG_BRANCH` | Target branch for the agent's worktree | `egg/{pipeline_id}/work` |
| `EGG_PRIVATE_MODE` | Private network mode | None |
| `HOST_HOME` | Docker host home directory (for worktree path translation) | None |
| `ORCHESTRATOR_PORT` | API port | `9849` |

### Constants

Defined in `shared/egg_config/constants.py`:

| Constant | Value |
|----------|-------|
| Container name | `egg-orchestrator` |
| Port | `9849` |
| MCP server port | `9850` |
| Isolated network IP | `172.32.0.3` |
| External network IP | `172.33.0.3` |

## Testing

```bash
# Run all orchestrator tests
.venv/bin/pytest orchestrator/tests/ -v

# Run specific test
.venv/bin/pytest orchestrator/tests/test_concurrent_integration.py -v
```

## Related Documentation

- [Orchestrator Architecture](../docs/architecture/orchestrator.md) — Deployment modes, state persistence, multi-agent roles
- [Gateway README](../gateway/README.md) — Policy enforcement gateway
- [Sandbox README](../sandbox/README.md) — Agent execution environment
- [Shared README](../shared/README.md) — Shared packages (egg_contracts, egg_container, egg_config)
- [SDLC Pipeline Guide](../docs/guides/sdlc-pipeline.md) — End-to-end pipeline usage
- [Architecture Overview](../docs/architecture/README.md) — System design
