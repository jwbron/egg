# Orchestrator

Central coordination engine for egg's SDLC pipeline execution, container lifecycle, and multi-agent orchestration.

## Overview

The orchestrator manages the end-to-end SDLC pipeline that turns GitHub issues into reviewed pull requests. It:

- **Manages pipeline state** — persists phase transitions, agent executions, and decisions on a git-backed state branch
- **Spawns and monitors containers** — creates sandbox containers with proper configuration via the gateway sidecar
- **Coordinates multi-agent execution** — runs specialized agents (coder, tester, documenter, etc.) in dependency-ordered waves
- **Handles HITL decisions** — queues questions for human reviewers and blocks until resolved
- **Streams real-time status** — provides SSE streams and DAG visualizations for pipeline monitoring
- **Validates deployments** — manages Docker-in-Docker devserver stacks for pre-merge testing

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         Host Machine                           │
│                                                                │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │ Orchestrator │───►│   Gateway    │                          │
│  │   :9849      │    │   Sidecar    │                          │
│  │              │◄───│   :9848      │                          │
│  │ - Pipeline   │    │              │                          │
│  │ - Dispatch   │    │ - Sessions   │                          │
│  │ - State      │    │ - Policy     │                          │
│  │ - HITL       │    │ - Creds      │                          │
│  └──────┬───────┘    └──────┬───────┘                          │
│         │                   │                                  │
│         │    ┌──────────────┼──────────────┐                   │
│         ▼    ▼              ▼              ▼                   │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│   │ Sandbox  │  │ Sandbox  │  │ Sandbox  │  │ Sandbox  │       │
│   │ (Coder)  │  │ (Tester) │  │(Docmter) │  │(Integr.) │       │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
└────────────────────────────────────────────────────────────────┘
```

## Key Concepts

### Pipeline Phases

Pipelines progress through four SDLC phases:

| Phase | Purpose | Agents |
|-------|---------|--------|
| **refine** | Analyze task, evaluate options | Refiner, reviewers |
| **plan** | Break work into tasks, assess risks | Architect, Task Planner, Risk Analyst, reviewers |
| **implement** | Write code, tests, docs | Coder, Tester, Documenter, Checker, Integrator, reviewers |
| **pr** | Create pull request | Single agent |

Each phase transition requires human approval (except implement → pr when all checks pass).

### State Persistence

Pipeline state is stored on a dedicated `egg/pipeline-state` orphan branch accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree`. The branch is local-only (never pushed to remote) and persists across orchestrator restarts via the Docker state volume.

### Multi-Agent Execution

Agents execute in dependency-ordered waves:

- **Tier 2** (standard): Coder → Tester + Documenter (parallel) → Integrator
- **Tier 3** (high complexity): Each plan phase gets its own implement cycle (Coder → Tester → Documenter → Checker → Code Reviewer), with independent phases running in parallel. An Integrator with expanded write access runs after all phase cycles complete.

Reviewers always run as a separate step after all workers complete, spawning in parallel with a configurable concurrency limit.

### HITL Decisions

The orchestrator queues blocking decisions for human input (architecture choices, go/no-go gates) and pauses pipeline execution until resolved. Decisions are tracked with timeout management and handler notifications.

## API Endpoints

All endpoints are prefixed with `/api/v1`.

### Pipelines

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | List all pipelines |
| `POST` | `/pipelines` | Create new pipeline |
| `GET` | `/pipelines/{id}` | Get pipeline details |
| `PATCH` | `/pipelines/{id}` | Update pipeline |
| `DELETE` | `/pipelines/{id}` | Delete pipeline |
| `GET` | `/pipelines/{id}/status` | Get pipeline status summary |
| `POST` | `/pipelines/{id}/start` | Start or restart pipeline |
| `GET` | `/pipelines/{id}/visualization` | Get DAG visualization (JSON, text, or ASCII) |
| `GET` | `/pipelines/{id}/stream` | SSE stream for single pipeline |
| `GET` | `/pipelines/stream` | Unified SSE stream for all active pipelines |

### Signals

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/signal` | Sandbox completion/progress/error callbacks |
| `POST` | `/pipelines/{id}/signal/batch` | Batch multiple signals |

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

### Deployment Checks

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/deployment-check/start` | Start devserver stack |
| `GET` | `/pipelines/{id}/deployment-check/status` | Poll devserver status |
| `POST` | `/pipelines/{id}/deployment-check/teardown` | Tear down devserver |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health |
| `GET` | `/ready` | Readiness check |
| `GET` | `/live` | Liveness check |

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
├── models.py               # Pydantic models (Pipeline, AgentExecution, ContainerInfo, etc.)
├── state_store.py          # Git-backed persistent state storage
├── container_spawner.py    # Container spawning with gateway session integration
├── container_monitor.py    # Container state monitoring and lifecycle tracking
├── multi_agent.py          # Wave-based parallel agent execution
├── dispatch.py             # Dispatch logic bridging orchestrator and egg_contracts
├── decision_queue.py       # HITL decision queue management
├── handoffs.py             # Agent-to-agent data handoff mechanism
├── devserver.py            # Docker-in-Docker devserver management
├── gateway_client.py       # Gateway API client for session management
├── docker_client.py        # Docker client wrapper
├── sandbox_template.py     # Sandbox container configuration templates
├── events.py               # Event emission and tracking
├── sse.py                  # Server-Sent Events for real-time status
├── unified_sse.py          # Unified SSE stream for multiple pipelines
├── dag_visualizer.py       # Pipeline DAG visualization
├── resilience.py           # Retry, circuit breaker, and resilience patterns
├── metrics.py              # Metrics collection and reporting
├── status_reporter.py      # Status reporting utilities
├── webhooks.py             # GitHub webhook handlers
├── Dockerfile              # Container image (Python 3.11-slim)
├── entrypoint.sh           # Container startup script
├── requirements.txt        # Python dependencies
├── routes/
│   ├── checks.py           # Deployment check endpoints
│   ├── containers.py       # Container lifecycle endpoints
│   ├── decisions.py        # HITL decision endpoints
│   ├── health.py           # Health check endpoints
│   ├── metrics.py          # Metrics endpoints
│   ├── phases.py           # Phase management endpoints
│   ├── pipelines.py        # Pipeline CRUD endpoints
│   └── signals.py          # Sandbox signal callback endpoints
└── tests/                  # Unit and integration tests (25+ files)
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EGG_ORCHESTRATOR_MODE` | Deployment mode (`local`, `remote-single`, `distributed`) | `local` |
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL | `http://egg-orchestrator:9849` |
| `EGG_PIPELINE_ID` | Current pipeline identifier | None |
| `EGG_AGENT_ROLE` | Agent role for multi-agent mode | None |
| `EGG_PRIVATE_MODE` | Private network mode | None |
| `HOST_HOME` | Docker host home directory (for worktree path translation) | None |
| `ORCHESTRATOR_PORT` | API port | `9849` |

### Constants

Defined in `shared/egg_config/constants.py`:

| Constant | Value |
|----------|-------|
| Container name | `egg-orchestrator` |
| Port | `9849` |
| Isolated network IP | `172.32.0.3` |
| External network IP | `172.33.0.3` |

## Testing

```bash
# Run all orchestrator tests
.venv/bin/pytest orchestrator/tests/ -v

# Run specific test
.venv/bin/pytest orchestrator/tests/test_multi_agent.py -v
```

## Related Documentation

- [Orchestrator Architecture](../docs/architecture/orchestrator.md) — Deployment modes, state persistence, multi-agent roles, devserver management
- [Gateway README](../gateway/README.md) — Policy enforcement gateway
- [Sandbox README](../sandbox/README.md) — Agent execution environment
- [Shared README](../shared/README.md) — Shared packages (egg_contracts, egg_container, egg_config)
- [SDLC Pipeline Guide](../docs/guides/sdlc-pipeline.md) — End-to-end pipeline usage
- [Architecture Overview](../docs/architecture/README.md) — System design
