# Orchestrator

Central coordination engine for egg's SDLC pipeline execution, container lifecycle, and multi-agent orchestration.

## Overview

The orchestrator manages the end-to-end SDLC pipeline that turns GitHub issues into reviewed pull requests. It:

- **Manages pipeline state** — persists phase transitions, agent executions, and decisions on a git-backed state branch
- **Spawns and monitors agent pods** — creates Kubernetes Jobs with proper configuration via the gateway sidecar
- **Coordinates multi-agent execution** — runs specialized agents across five categories (execution, analysis, review, utility, interface) in dependency-ordered waves or concurrently with message-based coordination
- **Handles HITL decisions** — queues questions for human reviewers and blocks until resolved
- **Streams real-time status** — provides SSE streams and DAG visualizations for pipeline monitoring

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
│   ┌──────────┐  ┌──────────┐  ┌─────────────┐  ┌───────────┐   │
│   │ Sandbox  │  │ Sandbox  │  │ Sandbox     │  │ Sandbox   │   │
│   │ (Coder)  │  │ (Tester) │  │(Documenter) │  │ (Reviewer │   │
│   └──────────┘  └──────────┘  └─────────────┘  └───────────┘   │
└────────────────────────────────────────────────────────────────┘
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
| **Interface** | Monitoring and health | overseer |

See [Agent Roles Reference](../docs/reference/agent-roles.md) for the complete roster.

### State Persistence

Pipeline state is stored on a dedicated `egg/pipeline-state` orphan branch accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree`. The branch is local-only (never pushed to remote) and persists across orchestrator restarts via a Kubernetes PersistentVolume.

### Concurrent Execution Mode

By default, agents within the refine, plan, and implement phases run simultaneously via BRC consensus (configurable via `concurrent_phases`). When `concurrent_execution: true` is set, this extends to all phases. Agents coordinate through:

- **Message bus** — Agents exchange typed messages (PROGRESS, QUESTION, STATUS, HANDOFF, AGENT_FAILED) via the orchestrator's message API. Messages can target a specific role or broadcast to all agents.
- **BRC action guards** — Each protocol action (propose, ACK, NACK, confirm, withdraw) has formal preconditions defined in `orchestrator/action_guards.py`. Guards are the canonical protocol specification — `PeerConsensusTracker` delegates to them before mutating state. See [Concurrent Execution — Action Guards](../docs/guides/concurrent-execution.md#action-guards).
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

**Lifecycle-control auth (#1769):** Endpoints that mutate agent lifecycle require
`Authorization: Bearer $EGG_LIFECYCLE_SECRET`. This covers HITL
resolve/cancel, pipeline create/update/delete/start, agent and phase restarts,
manual phase overrides (`/phase`, `/phase/start|complete|fail|populate-contract`),
and container spawn/stop/delete. Endpoints marked below with † are gated.
Requests without the header return 401; a server without
`EGG_LIFECYCLE_SECRET` configured fails closed with 503. The MCP server
(in-process) and host-side CLIs (`egg-sdlc`, `egg-orch`) attach the header
automatically from the env var. Agent pods never receive the secret, so
in-cluster agents cannot bypass HITL phase gates. Successful calls log a
`source` field sourced from the advisory `X-Egg-Source` header.

### Pipelines

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | List all pipelines |
| `POST` | `/pipelines` † | Create new pipeline (supports `source_branch` for artifact reuse) |
| `GET` | `/pipelines/{id}` | Get pipeline details |
| `PATCH` | `/pipelines/{id}` † | Update pipeline (async container cleanup) |
| `DELETE` | `/pipelines/{id}` † | Delete pipeline |
| `GET` | `/pipelines/{id}/status` | Get pipeline status summary (includes server-computed timing) |
| `POST` | `/pipelines/{id}/start` † | Start or restart pipeline |
| `GET` | `/pipelines/{id}/visualization` | Get DAG visualization (JSON, text, or ASCII) |
| `GET` | `/pipelines/{id}/stream` | SSE stream for single pipeline |
| `GET` | `/pipelines/stream` | Unified SSE stream for all active pipelines |

**PATCH cancel/fail behavior:** When a pipeline is updated to `cancelled` or `failed` status, the PATCH handler cancels pending HITL decisions and marks agent records as terminated synchronously, then returns the response immediately. Pod and worktree cleanup runs in a background daemon thread so the caller is not blocked by slow k8s/gateway operations. The response includes `cleanup_pending: true` to indicate that pod teardown is still in progress. The DELETE handler re-runs `cleanup_pipeline()` as a safety net, so any pods not yet removed by the background thread will be caught there.

**POST `source_branch` parameter:** The `POST /pipelines` endpoint accepts an optional `source_branch` field. When provided, the orchestrator reads plan and analysis artifacts from the specified branch during pipeline setup via `git show`, avoiding the need to pass large (50-80KB+) content inline. The orchestrator falls back to `git ls-tree` prefix matching when the pipeline ID prefix doesn't match files on the source branch. Inline `analysis`/`plan` values take precedence. See the [SDLC Pipeline guide](../docs/guides/sdlc-pipeline.md#creating-a-pipeline) for usage examples.

**POST branch-exists relaxation:** The branch existence check in `POST /pipelines` now allows reusing branches from prior terminal (cancelled/failed/complete) pipelines. A 409 is only returned when the branch exists AND an active pipeline is running for that ID.

### Signals

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/signal` | Sandbox completion/progress/error callbacks (with branch verification) |
| `POST` | `/pipelines/{id}/signal/batch` | Batch multiple signals |

**Completion signal branch verification:** When an agent signals completion with a `commit` SHA, the orchestrator verifies the commit exists on the pipeline's expected branch. If the commit is not found on the expected branch (e.g., the agent pushed to an improvised branch name), the signal is rejected with HTTP 409. Verification failures (network issues, git errors) are non-blocking — the signal is accepted when verification cannot be performed. Additionally, the orchestrator logs a warning if no new commits have been pushed since the phase started (detected via `phase_start_sha` recorded at phase start).

**Signal types include:** `complete`, `progress`, `error`, `heartbeat`, `readiness`, `consensus_propose`, `consensus_ack`, `consensus_nack`, `consensus_withdraw`, `consensus_confirmed`, `consensus_producer_push`. The `consensus_producer_push` signal triggers automatic re-proposal when a producer pushes new commits after proposing — see [Auto Re-Propose on Push/Commit](../docs/guides/concurrent-execution.md#auto-re-propose-on-pushcommit).

**BRC content validation:** The four BRC signal handlers (`consensus_propose`, `consensus_ack`, `consensus_nack`, `consensus_withdraw`) enforce a minimum content floor — message bodies must be ≥50 characters, non-empty, and not match trivial boilerplate phrases. Non-substantive messages are rejected with HTTP 400 before any tracker or message store state is mutated.

### Restart

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/agents/{role}/restart` † | Restart a single stuck agent (increment count, stop, respawn, then reset consensus — concurrency-safe via per-agent lock) |
| `POST` | `/pipelines/{id}/phases/{phase}/restart` † | Restart an entire phase (stop all containers, reset consensus and review cycles, respawn all agents) |

### Containers

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/spawn` † | Spawn container |
| `GET` | `/pipelines/{id}/containers` | List containers |
| `GET` | `/pipelines/{id}/containers/{cid}` | Get container details |
| `DELETE` | `/pipelines/{id}/containers/{cid}` † | Remove container |
| `POST` | `/pipelines/{id}/containers/{cid}/stop` † | Stop container |
| `GET` | `/pipelines/{id}/containers/{cid}/logs` | Get container logs |
| `GET` | `/pipelines/{id}/containers/{cid}/health` | Container health check |

### HITL Decisions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines/{id}/decisions` | List pending decisions |
| `POST` | `/pipelines/{id}/decisions` | Create decision |
| `GET` | `/pipelines/{id}/decisions/{did}` | Get decision details |
| `POST` | `/pipelines/{id}/decisions/{did}/resolve` † | Resolve decision |
| `POST` | `/pipelines/{id}/decisions/{did}/cancel` † | Cancel decision |
| `GET` | `/pipelines/{id}/decisions/status` | Decision queue summary |

### Phases

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines/{id}/phase` | Get current phase |
| `POST` | `/pipelines/{id}/phase` † | Advance to next phase |
| `POST` | `/pipelines/{id}/phase/start` † | Start current phase |
| `POST` | `/pipelines/{id}/phase/complete` † | Complete current phase |
| `POST` | `/pipelines/{id}/phase/fail` † | Fail current phase |
| `POST` | `/pipelines/{id}/phase/populate-contract` † | Populate contract from plan artifacts |

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

| Method | Path | Purpose | Wired to | Cost on request path |
|--------|------|---------|----------|----------------------|
| `GET` | `/health` | Rich operator/dashboard payload (status, components, transitions) | `mcp__egg__check_health`, manual diagnostics | Single dict read (cache) |
| `GET` | `/ready` | Traffic-routing readiness — flips when state-store cache is stale or unhealthy | kubelet `readinessProbe`, `startupProbe` | Single dict read (cache) |
| `GET` | `/live` | Process liveness | kubelet `livenessProbe` | Pure JSON return |
| `GET` | `/pipelines/{id}/health` | On-demand pipeline health check | manual / phase-advance gating | Two-tier framework run |
| `GET` | `/pipelines/{id}/health/alerts` | Active deterministic health alerts | manual / overseer | In-memory read |

The first three endpoints serve cached values populated by a background thread (`state_store_probe.py`); none of them runs `git`, calls `get_state_store()`, or holds locks on the request path. See [`docs/guides/deployment.md` § Orchestrator health](../docs/guides/deployment.md#orchestrator-health) for the curative-probe cadence and #2191 for the rationale.

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
├── models.py               # Pydantic models (Pipeline, AgentExecution, HITLDecision, etc.) with k8s-native fields (pod_name, namespace, job_name)
├── state_store.py          # Git-backed persistent state storage (includes the commit-authorship sub-store from #1882 — per-pipeline sharded `{sha → role}` mapping on the `egg/pipeline-state` branch, first-wins semantics)
├── commit_authorship_store.py  # Sub-store facade over state_store for the commit-authorship registry (#1882)
├── kubernetes_client.py    # Kubernetes API client wrapper (Job CRUD, pod logs, status)
├── kubernetes_spawner.py   # Agent Job spawning with gateway session integration; agent restart (stop + respawn preserving worktree)
├── kubernetes_monitor.py   # Kubernetes Job state monitoring and lifecycle tracking
├── container_backend.py    # ContainerBackend protocol (structural typing interface for backend abstraction)
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
│   │   ├── container_liveness.py   # Verify RUNNING agent pods exist in Kubernetes
│   │   ├── startup_state.py        # Post-startup reconciliation verification
│   │   ├── phase_output.py         # Detect missing artifacts (commits, plans)
│   │   └── state_consistency.py    # Cross-reference orchestrator state vs k8s pod state vs contract
├── sse.py                  # Server-Sent Events for real-time status
├── unified_sse.py          # Unified SSE stream for multiple pipelines
├── dag_visualizer.py       # Pipeline DAG visualization
├── consensus_wrapper.py    # BRC consensus wrapper script builder (transient crash detection and restart with backoff)
├── concurrent_executor.py  # Concurrent phase execution with BRC consensus
├── action_guards.py        # Formal action guards (preconditions) for BRC protocol actions — canonical protocol specification
├── peer_consensus.py       # Peer consensus tracker for BRC protocol (delegates to action_guards.py)
├── resilience.py           # Retry, circuit breaker, and resilience patterns
├── metrics.py              # Metrics collection and reporting
├── status_reporter.py      # Status reporting utilities
├── webhooks.py             # GitHub webhook handlers
├── Dockerfile              # Container image (Python 3.14-slim)
├── entrypoint.sh           # Container startup script
├── requirements.txt        # Python dependencies
├── overseer/               # Overseer agent server-side logic
│   ├── __init__.py         # Package init
│   ├── monitor.py          # Main poll loop, health checks, CLI wrappers (explicit pipeline routing)
│   ├── classifier.py       # Haiku classification (stall, loop, error, off-track)
│   ├── decision_maker.py   # Sonnet/Opus corrective decision-making (routes restartable infra errors to restart_agent)
│   ├── issue_filer.py      # Autonomous GitHub issue filing with diagnostics
│   ├── self_monitor.py     # Self-monitoring (poll timing, message volume, LLM costs)
│   └── utils.py            # Utility functions
├── routes/
│   ├── anchors.py          # Agent anchor CRUD and team anchor generation endpoints
│   ├── commit_authorship.py # /api/v1/commit-authorship/{register,lookup} — gateway-written registry of {sha → role} for push attribution (#1882)
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

Both checkpoint tools accept an optional `repo` parameter (string, `owner/repo` format) to specify the checkpoint repository when checkpoints are stored separately (e.g., `owner/repo-checkpoints`). The value is forwarded as `source_repo` to the gateway.

### Orchestrator-Backed Tools

`submit_task`, `get_status`, `provide_input`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`, `start_phase`, `complete_phase`, `populate_contract`

The `get_status` tool returns an enriched pipeline status response. In addition to the standard fields (`current_phase`, `status`, `pipeline`, `running_agents`, `completed_agents`, `pending_decisions`, `recent_messages`), the response includes server-computed timing fields:

- **`phase_started_at`** (ISO 8601 string) — Timestamp when the current phase started. Omitted when the phase has no `started_at` value (e.g., pending phases).
- **`phase_elapsed_seconds`** (integer) — Server-computed seconds since the current phase started. Omitted when `phase_started_at` is unavailable.
- **Per-agent `elapsed_seconds`** (integer) — Each entry in `running_agents` includes an `elapsed_seconds` field computed from the agent's `started_at` timestamp. Omitted for agents without a `started_at` value.

Server-computed elapsed times eliminate clock-skew between client and server and are unaffected by client-side blocking (e.g., dialog prompts that pause poll loops). Monitoring clients should prefer these fields over local wall-clock tracking.

The `submit_task` tool accepts an optional `source_branch` parameter (string) to load plan and analysis artifacts from a prior run's branch server-side, instead of passing them inline. This avoids MCP transport size limits for large artifacts. See the [SDLC Pipeline guide](../docs/guides/sdlc-pipeline.md#creating-a-pipeline) for details.

The `restart_agent` tool accepts `task_id`, `agent_role`, and optional `reason` parameters. It proxies to the agent restart API endpoint. The `restart_phase` tool accepts `task_id`, `phase`, and optional `reason`/`context` parameters. It proxies to the phase restart API endpoint. Both are available to HITL operators via the MCP server.

The `advance_phase` tool accepts `task_id`, `target_phase` (required), and `force` (boolean, optional) parameters. When `force=true`, it first stops all running containers from the current phase before advancing to prevent SIGTERM cascading into the new phase. The `start_phase` tool accepts `task_id` to start the current phase and spawn agents. The `complete_phase` tool accepts `task_id` and optional `artifacts` to manually mark a phase as complete. The `populate_contract` tool accepts `task_id` and populates the SDLC contract from plan artifacts by parsing yaml-tasks from the plan draft. These four tools are designed for pipeline recovery and manual intervention — see [#1646](https://github.com/jwbron/egg/issues/1646) for motivation.

## Health Check Framework

The orchestrator includes a two-tier health check framework for proactive pipeline failure detection. See [`health_checks/README.md`](health_checks/README.md) for full details.

### Overview

Health checks run at key lifecycle points to catch infrastructure and semantic failures before they cascade. The framework uses a common `HealthCheck` protocol so new checks can be added without modifying the runner or integration points.

**Tier 1 (Programmatic)** — Fast, deterministic checks that verify structural invariants:

| Check | Purpose | Triggers |
|-------|---------|----------|
| `ContainerLivenessCheck` | Verify RUNNING agent pods exist in Kubernetes | All |
| `StartupStateCheck` | Post-startup reconciliation verification | STARTUP, ON_DEMAND |
| `PhaseOutputPresenceCheck` | Detect missing artifacts (commits, plans) | WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND |
| `StateConsistencyCheck` | Cross-reference orchestrator state vs k8s pod state vs contract | RUNTIME_TICK, WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND |

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

The `health_monitor.py` module subscribes to EventBus events and evaluates seven tripwire rules. Thresholds are **phase-aware**: during the implement phase, the heartbeat and progress stall thresholds use `orchestrator_implement_heartbeat_timeout_seconds` (default 600s) instead of the standard 120s. BRC-aware suppression prevents false positives: reviewer-only agents are suppressed from heartbeat/progress stall checks both while waiting for upstream proposals and during a configurable grace period after a proposal arrives (`post_proposal_grace_seconds`, default 300s). A separate BRC progress check detects producers stuck in heartbeat loops after being fully ACKed (`orchestrator_post_ack_confirmation_timeout_seconds`, default 180s).

| Tripwire | Threshold Config | Action |
|----------|-----------------|--------|
| Heartbeat timeout | `orchestrator_heartbeat_timeout_seconds` (120s) / `orchestrator_implement_heartbeat_timeout_seconds` (600s) | Escalate to overseer/HITL |
| Container exit | — | HITL escalation |
| Repeated errors | `orchestrator_error_repeat_threshold` (3) | Escalate to overseer |
| Message volume spike | `orchestrator_message_rate_limit` (20/min) | Auto-throttle |
| Progress stall | Same as heartbeat (phase-aware) | Escalate to overseer/HITL |
| Infrastructure error | Pattern-matched on blocked progress events | Critical alert → restartable errors route to `restart_agent`; non-restartable to HITL |
| BRC progress stall | `orchestrator_post_ack_confirmation_timeout_seconds` (180s) | Escalate to overseer/HITL |

### Overseer Agent

Phase-scoped: spawned at the start of each pipeline phase and torn down when the phase completes, advances, or fails (when `overseer_enabled` is true). Each phase gets a fresh overseer instance with no accumulated state. The overseer runs with a configurable Agent SDK turn budget (`overseer_max_turns`, default 2000) to prevent premature exits during long-running phases. Uses Haiku for anomaly classification and Sonnet/Opus for corrective decisions via `shared/egg_agent/`. No code access. The health monitor thread auto-respawns the overseer if it exits mid-phase, gated by a `phase_overseer_active` flag to prevent respawn between phases. On respawn, the orchestrator captures the old container's log tail and broadcasts an `OVERSEER_ALERT` message with diagnostic metadata (exit code, container IDs, log tail, respawn attempt count). All CLI operations (alert broadcasting, message sending, alert resolution, HITL decisions) pass the pipeline ID explicitly to prevent cross-pipeline alert leakage. See `orchestrator/overseer/` for server-side logic.

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
| `HOST_HOME` | Host machine home directory (for worktree path translation) | None |
| `ORCHESTRATOR_PORT` | API port | `9849` |
| `EGG_GATEWAY_READY_TIMEOUT_SECONDS` | Max wait for the gateway to become healthy at `POST /api/v1/pipelines`. Set to `0` to disable the gate. See #1851. | `60` |

### Constants

Defined in `shared/egg_config/constants.py`:

| Constant | Value |
|----------|-------|
| Deployment name | `egg-orchestrator` |
| Port | `9849` |
| MCP server port | `9850` |
| Service DNS | `orchestrator.egg-system.svc.cluster.local` |
| Namespace | `egg-system` |

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
