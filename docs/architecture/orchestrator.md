# Orchestrator Architecture

This document describes the orchestrator component and the three deployment modes for egg: local, remote-single, and distributed.

## Overview

The orchestrator manages SDLC pipeline execution, container lifecycle, and agent coordination. It provides:

- Pipeline state management (phases, tasks, decisions)
- Container spawning and monitoring
- Multi-agent coordination (for parallel execution)
- Human-in-the-loop (HITL) decision handling
- Completion signaling and handoff management

## Pipeline State Persistence

The orchestrator persists pipeline state using a dedicated git worktree on an orphan branch.

**Architecture:**
- All pipeline state is stored in `.egg-state/pipelines/{id}.json` files
- Files live on the `egg/pipeline-state` orphan branch (never merged to main)
- Accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree` (single-repo) or `/home/egg/.egg-state/pipeline-worktree-{repo_name}` per repo (multi-repo)
- State branch is synced to remote after every commit (best-effort, async push via daemon thread)
- On startup, restores from remote if local branch is missing (cross-host recovery)

**Key properties:**
- Read/write operations go directly to the worktree directory on disk
- Commits are made in-place and stay on the state branch
- Survives orchestrator restarts by reading from git
- Mirrors the `egg/checkpoints/v2` pattern for cross-host recovery

**Worktree lifecycle:**
- Created lazily on first state access
- Validated on each access (repairs stale/broken worktrees)
- Cleaned up via `git worktree prune` on first access (not container startup)

This differs from agent worktrees (managed by the gateway for agent isolation). The orchestrator manages its own state worktree independently.

**Startup reconciliation:**

On orchestrator restart, orphaned container state is automatically recovered:

1. **RUNNING pipelines**: For each pipeline showing `status=RUNNING`, the reconciliation process recovers orphaned container state:
   - Scans only the **current phase** for stale containers. Containers from prior phases are intentionally terminated and their absence is expected — checking all phases caused false `FAILED` transitions when the orchestrator restarted mid-pipeline.
   - Any agent/container in the current phase whose container ID is absent from the live Docker container set is marked `FAILED`.
   - If at least one stale entry is found, the pipeline itself is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start`.

2. **AWAITING_HUMAN pipelines**: For each pipeline showing `status=AWAITING_HUMAN` with no pending decisions (orphaned after a restart where the decision was already resolved), the pipeline is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start`. The restart endpoint will automatically recover by parsing the latest phase_gate resolution and either advancing to the next phase (approved) or resetting the current phase for re-run (request_changes/change_approach).

3. **Concurrent pipeline consensus trackers**: For each `RUNNING` pipeline (i.e., those not already marked `FAILED` by step 1) in a concurrent phase, the in-memory `PeerConsensusTracker` (which does not persist to disk) is reconstructed by replaying `CONSENSUS_*` messages from the message store in timestamp order. This allows agents that are mid-consensus to resume correctly after a restart rather than looping. Reconstruction is best-effort — if no consensus messages are found, the tracker is left absent and agents will encounter an empty consensus state (handled by the wrapper's message-bus fallback).

This prevents pipelines from being stuck in `RUNNING` or `AWAITING_HUMAN` states indefinitely after a crash. Operators (or CI systems) can detect the `FAILED` status and restart the pipeline using the existing restart endpoint, which preserves worktrees and phase state while re-spawning containers.

See `orchestrator/state_store.py` and `orchestrator/startup_reconciliation.py` for implementation details.

**Runtime container monitoring:**

A background `ContainerMonitor` thread runs continuously after orchestrator startup to detect container failures during execution. The monitor periodically checks container status and invokes registered handlers when state changes occur (container exits, fails, or becomes unhealthy).

A pipeline reconciliation handler detects when agent containers exit or fail during runtime and updates pipeline state accordingly. The handler scans **all phases** within each `RUNNING` pipeline (including completed phases) to find the exited container, as reviewer agents may continue running after their phase has transitioned to `COMPLETE`.

When a container running an agent exits with a non-zero code, the handler marks the container as `FAILED`, marks the owning agent as `FAILED` with an error message, and transitions the entire pipeline to `FAILED` status — unless the agent is already `COMPLETE` (i.e., it completed via BRC consensus), in which case the exit is ignored. Containers that exit with code 0 (graceful exit) emit a `STOPPED` event and do not trigger failure reconciliation. When BRC consensus completes, the concurrent phase runner proactively marks agent containers as `EXITED` with exit code 0 so that subsequent monitor sweeps treat them as clean exits; the agent-COMPLETE check is a secondary defense for the event window before that update is persisted. This complements startup reconciliation by catching failures that occur during execution rather than only on orchestrator restart.

In addition to the event-driven handler, `ContainerMonitor.start_periodic_reconciliation()` runs a second background thread that sweeps every 30 seconds for stale containers that may have exited between Docker events (e.g., missed events during a partial restart). This periodic sweep checks only the **current phase** of each `RUNNING` pipeline — the same scope as startup reconciliation. Like the event-driven handler, the sweep inspects the actual container exit code before reconciling: containers that exited with code 0 (clean exit — e.g., the consensus wrapper completing gracefully) are skipped and do not trigger `FAILED` reconciliation. The first sweep is intentionally delayed by one interval since startup reconciliation already ran immediately before the thread was started.

The monitor uses per-pipeline locking and optimistic version checks to prevent race conditions with concurrent state writers (e.g., agent signal handlers).

See `orchestrator/container_monitor.py` for implementation details.

**Health check framework:**

A two-tier health check framework provides structured, extensible failure detection across the pipeline lifecycle. All checks implement a common `HealthCheck` protocol and produce `HealthResult` values with a status (`HEALTHY`/`DEGRADED`/`FAILED`), reasoning, and a suggested action (`CONTINUE`/`FAIL_PIPELINE`/`ALERT`).

**Tier 1 (Programmatic)** checks are fast and deterministic. They run on every lifecycle trigger and cover structural invariants: container liveness, startup state, phase output presence, state consistency, and consensus stall detection. Container liveness and startup state checks are adapters over existing `ContainerMonitor` and `reconcile_stale_containers` logic. The phase output check detects the issue-835 pattern where agents complete successfully but produce no artifacts (e.g., no commits on the remote branch after an implement phase). The state consistency check cross-references orchestrator state against Docker reality and contract data. The consensus stall check fires on `RUNTIME_TICK` and `ON_DEMAND` for concurrent execution phases: when all agents are confirmed but the phase has not advanced past a 60-second grace period, it reports `DEGRADED` and `ContainerMonitor` drives recovery (tracker reconstruction first, then aggressive agent/phase completion with optimistic locking).

**Tier 2 (Semantic)** checks are LLM-powered and evaluate whether agents made meaningful progress. The `AgentInspectorCheck` sends pipeline context (recent commits, diff stats, agent output files, SDLC contract state) to Claude via the Agent SDK (default model: `sonnet`) and interprets a structured JSON verdict. On API failure, the check gracefully degrades to HEALTHY — Tier 2 failures never block the pipeline. Tier 2 checks run conditionally — at `WAVE_COMPLETE` only when Tier 1 reports `DEGRADED`, and always at `PHASE_COMPLETE` and `ON_DEMAND`.

**Lifecycle integration:**
- `STARTUP`: Runs after startup reconciliation on all RUNNING pipelines (non-blocking)
- `RUNTIME_TICK`: Triggered by container state changes via `ContainerMonitor` (non-blocking)
- `WAVE_COMPLETE`: Runs after each agent wave completes; `FAIL_PIPELINE` breaks wave execution
- `PHASE_COMPLETE`: Runs before phase advance in `routes/phases.py`; `FAIL_PIPELINE` blocks the transition (409 Conflict)
- `ON_DEMAND`: Available via `GET /api/v1/pipelines/{id}/health`

`PipelineHealthContext` provides checks with a read-only snapshot of pipeline state. Constructor parameters are cheap (already-loaded objects); expensive operations like git commands and Docker queries use lazy properties that compute on first access and cache the result.

All check results are emitted to the EventBus as `system.health_check.*` events for observability. Results can also be persisted on `PhaseExecution` records via the `HealthCheckResultModel`.

See `orchestrator/health_checks/README.md` for the full framework reference, including how to add new checks.

**Pipeline health monitoring (two-tier):**

Building on the health check framework, a two-tier pipeline health monitoring system provides continuous, real-time failure detection and corrective action:

**Orchestrator tier (deterministic):** Processes structured agent progress events with configurable tripwire rules. Handles clear-cut failures instantly — heartbeat timeouts trigger escalation to the overseer/HITL, container exits trigger HITL escalation, repeated identical errors escalate to the overseer, and message volume spikes trigger auto-throttling. No LLM involvement. Nudge messages are only sent by the Tier 2 overseer after classifying the alert. See `orchestrator/health_monitor.py`.

Agents emit structured progress via `POST /api/v1/pipelines/{id}/progress` (CLI: `egg-orch progress emit`). Events include step name, state (working/blocked/complete), detail text, and optional blocker description. The orchestrator stores events in-memory with configurable retention and evaluates them against tripwire thresholds from `PipelineConfig`.

**Overseer tier (LLM-powered):** A continuously running agent container (no code access) that handles ambiguous cases the deterministic tier can't resolve. Uses Haiku via `shared/egg_agent/` for lightweight classification (stall vs. legitimate work, loop detection, error triage, off-track detection, cross-phase decision consistency) and Sonnet/Opus for corrective decision-making (composing redirect messages, deciding escalation level, filing diagnostic GitHub issues). Auto-spawned on every pipeline when `overseer_enabled` is true. If the overseer exits before the pipeline reaches a terminal state, the orchestrator's health monitor thread automatically respawns it (up to `overseer_max_respawns` times, default 3).

The overseer follows a corrective action ladder: auto-nudge → redirect message → HITL escalation → GitHub issue filing → Slack notification. It cannot restart agents autonomously — all restart requests go through the HITL decision queue.

See [Pipeline Health Monitoring Guide](../guides/pipeline-health-monitoring.md) for the full reference.

## Pipeline Modes

The orchestrator supports two pipeline modes:

- **`issue`** (default): Standard SDLC pipeline triggered by a GitHub issue. Progresses through refine → plan → implement phases with structured agent teams.
- **`babysit`**: PR review/fix loop triggered by `egg-babysit <PR>`. Runs a continuous polling loop (conflict fix → CI wait → check fix → review → feedback → loop) instead of phase-based progression. Pipeline ID uses `pr-{N}` format. See [Babysit-PR Guide](../guides/babysit-pr.md).

The `babysit` mode registers with the same orchestrator infrastructure (state store, health monitoring, HITL decision queue) but replaces phase-based progression with the review/fix loop from `shared/egg_babysit/loop.py`.

## Network Mode

Pipelines can specify an explicit network mode that controls internet access for spawned containers:

- **`public`**: Full internet access
- **`private`**: Network lockdown - Anthropic API + private GitHub repos only (enforced by gateway proxy)
- **`None`** (auto): Auto-detected from repo visibility (see below)

**Setting network mode:**

- Via `egg-sdlc --private`: Sets `EGG_PRIVATE_MODE=true` environment variable, which `egg-sdlc` detects and passes as `network_mode="private"` when creating the pipeline
- Via `egg-orch pipeline create --network-mode <public|private>`: Explicitly sets the pipeline's network mode
- Via orchestrator API: Include `"network_mode": "public"|"private"` in the pipeline creation request body

**How it works:**

1. Network mode is stored in the pipeline model (`orchestrator/models.py:Pipeline.network_mode`)
2. The resolved mode applies to:
   - Spawned agent containers (network isolation)
   - The orchestrator's own gateway sessions (git push/fetch, branch deletion, PR creation)
3. Resolution logic:
   - If `network_mode` is explicitly set, use it
   - If not set but a repo is associated, query the gateway for repo visibility (`GatewayClient.get_repo_visibility()`)
   - Map visibility: private/internal repos → `"private"` mode, public repos → `"public"` mode
   - If no repo is associated, default to `"public"`
4. The gateway enforces network policy based on the session mode (see `gateway/README.md`)

**Special case: PR phase**

The PR phase no longer spawns an agent. Instead, the orchestrator auto-creates the PR via `GatewayClient.create_pr()`, which:
1. Extracts PR title/description from the contract's `pr` field (populated by the plan agent)
2. Falls back to the issue title or pipeline ID if no PR metadata exists
3. Appends git commit log, diff stats, and a **Pipeline Context** section (pipeline ID + issue number) to the PR body
4. Creates the PR via the gateway using a temporary session with `phase="pr"` permissions and the pipeline's resolved network mode; in `private` mode the PR is created as a draft
5. Applies `egg` and `agent:orchestrator` labels to the newly created PR

The gateway also injects an `<!-- egg-pipeline-context ... -->` HTML comment into the PR body containing machine-parseable pipeline metadata (`pipeline_id`, `agent_role`, `issue`). Labels are applied best-effort — failures are logged but non-fatal.

This eliminates the need for agent interaction during PR creation and ensures consistent PR formatting across all pipelines.

## Per-Pipeline Worktrees

The orchestrator reads pipeline artifacts (verdict files, draft documents, check results) from per-pipeline worktrees created by the gateway. These worktrees isolate work for each pipeline and are separate from both the orchestrator's state worktree and the main repository working directory.

**Architecture:**
- Gateway creates worktrees at `/home/egg/.egg-worktrees/{pipeline-id}/{repo-name}/`
- Agent containers mount these worktrees and write artifacts to them
- Orchestrator mounts `/home/egg/.egg-worktrees` and reads artifacts from pipeline-specific paths
- Worktree paths are resolved dynamically based on pipeline ID and repository

**Key artifact files in worktrees:**
- `.egg-state/contracts/{identifier}.json` — Contract state (issue number for issue-driven pipelines, pipeline ID for prompt-driven pipelines)
- `.egg-state/drafts/{identifier}-analysis.md` — Draft for `refine` phase (special-cased to `analysis`)
- `.egg-state/drafts/{identifier}-{phase}.md` — Draft for other phases (e.g., `plan`). No draft for `implement` phase.
- `.egg-state/reviews/{identifier}-{phase}-{reviewer_type}-review.json` — Review verdict files
- `.egg-state/agent-outputs/{identifier}-{role}-output.json` — Agent handoff data (e.g., `871-coder-output.json`). Falls back to `{role}-output.json` for backward compatibility.
- `.egg-state/checks/{identifier}-implement-results.json` — *(Deprecated)* Previously written by the checker role. The checker has been absorbed into the tester, which reports results via its handoff output instead.

**Volume mounts:**
- Orchestrator: Bind mount from `${HOST_HOME}/.egg-worktrees` to `/home/egg/.egg-worktrees` (read container-written artifacts)
- Integration tests: Named volume `worktrees` (no host filesystem in CI)

**Phase-based readonly mounts:**

During the `implement` phase, certain `.egg-state/` subdirectories are mounted readonly into agent containers to prevent direct filesystem modifications to plan/contract artifacts:

| Directory | Implement phase | Refine/Plan phases |
|-----------|----------------|-------------------|
| `.egg-state/contracts/` | Readonly | Writable |
| `.egg-state/drafts/` | Readonly | Writable |
| `.egg-state/pipelines/` | Readonly | Writable |
| `.egg-state/reviews/` | Readonly (except reviewers) | Writable |

**Reviewer exemption**: Reviewer agents (roles starting with `reviewer`, e.g., `reviewer_code`, `reviewer_contract`) are exempted from the `.egg-state/reviews/` readonly mount because they need to write verdict files to that directory. Other implement phase agents (coder, tester, documenter) still have readonly access.

The orchestrator calls `ensure_egg_state_dirs()` before spawning containers to create the required directories (bind mounts require existing source paths) and place `.egg-readonly` marker files explaining the restriction and current phase. Reviewer agents do not receive the `.egg-readonly` marker in the `reviews/` directory. Then `phase_readonly_mounts()` generates the readonly `MountSpec` entries, which are added alongside the existing `.git` shadow mounts. Only directories that exist on the host are mounted (missing directories are skipped). See `shared/egg_container/__init__.py` and `orchestrator/container_spawner.py`.

**Host path translation:** The gateway returns worktree paths relative to the Docker host (e.g., `/home/jwies/.egg-worktrees/...`), but the orchestrator container only mounts these via `/home/egg/...`. The `_host_to_local_volumes()` helper in `container_spawner.py` uses the `HOST_HOME` env var to translate host paths to orchestrator-accessible local paths for `is_dir()` checks and `ensure_egg_state_dirs()`. Docker mount sources still use the original host paths unchanged.

**Worktree state synchronization:** The orchestrator maintains bidirectional synchronization between local worktree branches and their remote counterparts:

1. **Push to remote** (outbound): The orchestrator pushes worktree contents (including `.egg-state/` files) to the remote branch at key pipeline checkpoints — after contract initialization, after phase completion, and on pipeline failure. This ensures agents always see the latest statefiles without working on unpushed changes. Pushes use `GatewayClient.push_worktree_branch()` with a temporary session token and are logged as warnings on failure (non-blocking).

2. **Fetch from remote** (inbound): Before starting pipeline phases, the orchestrator syncs the local worktree with the remote branch via `_sync_worktree_with_remote()`. This handles orchestrator restarts where the local worktree branch lags behind origin: commits pushed by agents in previous phases (contracts, drafts, statefiles) exist on the remote but not in the local checkout. The function performs a gateway-authenticated fetch (`GatewayClient.fetch_worktree_branch()`) followed by a local `git reset --hard origin/<branch>`. The sync behavior depends on the prior phase's outcome:

   - **Prior phase succeeded, local ahead:** Local commits are pushed to remote first, preserving completed work, then the worktree is reset.
   - **Prior phase failed, local ahead:** Local commits are discarded and the worktree is reset to remote (removes incomplete work from a failed/killed agent).
   - **Diverged (local and remote both have unique commits):** A fast-forward merge is attempted. If the merge fails, the worktree is left unchanged and the error is logged (may require manual intervention).
   - **Local behind or in-sync:** Standard reset to remote tip.

   The operation is best-effort and idempotent — it skips gracefully when the remote branch doesn't yet exist (first pipeline run) or when fetch fails.

3. **Cleanup on deletion**: When a pipeline is deleted, the orchestrator cleans up remote worktree branches (`egg/{container_id}/work`) for all containers across all phases using `GatewayClient.delete_remote_branch()`.
   This prevents accumulation of dangling branches on the remote.
   Branch deletion is best-effort and logged as warnings on failure — deletion failures do not block pipeline removal.

See `orchestrator/routes/pipelines.py` for implementation details.

This architecture ensures the orchestrator reads artifacts from the correct isolated workspace rather than the main repository, preventing cross-contamination between pipelines.

## Multi-Agent Roles

The orchestrator coordinates specialized agent roles across pipeline phases. Each role runs in its own sandbox container with scoped permissions enforced by the gateway.

### Refine Phase Roles

| Role | Responsibility |
|------|----------------|
| **Refiner** | Analyze task, research codebase, evaluate options, recommend approach |

**Execution model**: Refiner runs first, then reviewers validate the analysis before human approval.

**Reviewers:**
- **Refine Reviewer**: Analysis quality and completeness
- **Agent Design Reviewer**: Agent-mode alignment and anti-patterns

### Plan Phase Roles

| Role | Responsibility |
|------|----------------|
| **Architect** | Analyze task, research codebase, recommend approach |
| **Task Planner** | Break work into phases and discrete tasks with acceptance criteria |
| **Risk Analyst** | Identify technical risks, propose mitigation strategies |

**Execution model**: Architect runs first, then task planner and risk analyst run in parallel (both depend on architect's analysis).

**Reviewers:**
- **Plan Reviewer**: Plan quality, task breakdown, dependencies, test strategy, alignment with analysis

### Implement Phase Roles

| Role | Responsibility |
|------|----------------|
| **Coder** | Write code, create commits, push branches |
| **Tester** | Find gaps in implementation, write and run tests, run linters/type checkers, apply auto-fixes |
| **Documenter** | Update docs and READMEs |
| **Reviewer (Code)** | Security, correctness, code quality, testing, documentation |
| **Reviewer (Contract)** | Verify acceptance criteria met, task completion status |

**Execution model**: All implement phase agents run concurrently via the BRC consensus protocol. Agents communicate via the orchestrator message bus and reach phase completion through peer consensus.

### Prompt Context Scoping

Agent prompts are scoped to role-relevant context. Analysis roles (architect, task_planner, risk_analyst) receive the full issue body for problem understanding. Execution roles (tester, documenter) receive a summarized background with pointers to full context on demand. See [SDLC Pipeline Guide: Role-Specific Prompt Context](../guides/sdlc-pipeline.md#role-specific-prompt-context) for details.

## Deployment Modes

egg supports three deployment modes, each suited to different use cases:

### 1. Local Mode (Interactive)

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────────┐    ┌────────────────────────────┐  │
│  │    Gateway     │◄───│      Sandbox               │  │
│  │    Sidecar     │    │  (interactive Claude Code) │  │
│  │                │───►│                            │  │
│  │  - Proxy       │    │  - No credentials          │  │
│  │  - Policy      │    │  - Network isolated        │  │
│  │  - Credentials │    │                            │  │
│  └────────────────┘    └────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Single sandbox container with interactive Claude Code session
- Gateway sidecar provides proxy and policy enforcement
- No orchestrator component needed
- User interacts directly via terminal

**Use case:** Local development, ad-hoc tasks, learning/experimentation

**Environment:**
```bash
# No orchestrator-specific env vars
EGG_ORCHESTRATOR_MODE=local  # (default, can be omitted)
```

### 2. Remote-Single Mode

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │Orchestrator│───►│   Gateway   │───►│   Sandbox   │  │
│  │            │    │   Sidecar   │    │  (Claude)   │  │
│  │ - Pipeline │◄───│             │◄───│             │  │
│  │ - State    │    │ - Proxy     │    │ - Signals   │  │
│  │ - HITL     │    │ - Policy    │    │   back      │  │
│  └────────────┘    └─────────────┘    └─────────────┘  │
│        │                                                │
│        │ Webhooks                                       │
│        ▼                                                │
│  ┌────────────┐                                         │
│  │  GitHub    │                                         │
│  │  (Issues,  │                                         │
│  │   PRs)     │                                         │
│  └────────────┘                                         │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Orchestrator spawns and monitors single sandbox
- Sandbox signals completion/progress back to orchestrator
- Pipeline state persisted locally
- GitHub webhooks drive pipeline transitions

**Use case:** Self-hosted CI/CD integration, single-task automation

**Environment:**
```bash
EGG_ORCHESTRATOR_MODE=remote-single
EGG_ORCHESTRATOR_URL=http://172.32.0.3:9849
EGG_PIPELINE_ID=issue-123
EGG_AGENT_ROLE=coder
```

### 3. Distributed Mode

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────┐    ┌─────────────┐                     │
│  │Orchestrator│───►│   Gateway   │                     │
│  │            │    │   Sidecar   │                     │
│  │ - Pipeline │◄───│             │                     │
│  │ - Dispatch │    │             │                     │
│  │ - Handoffs │    │             │                     │
│  └────────────┘    └──────┬──────┘                     │
│        │                  │                             │
│        │    ┌─────────────┼─────────────┐              │
│        │    │             │             │              │
│        ▼    ▼             ▼             ▼              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Sandbox  │  │ Sandbox  │  │ Sandbox  │             │
│  │ (Coder)  │  │ (Tester) │  │(Docmter) │             │
│  │          │  │          │  │          │             │
│  │ Signals  │  │ Signals  │  │ Signals  │             │
│  │   back   │  │   back   │  │   back   │             │
│  └──────────┘  └──────────┘  └──────────┘             │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Orchestrator spawns multiple sandboxes with different agent roles
- Dependency-based scheduling (coder → tester → documenter)
- Handoff data passed between agents
- Parallel execution of independent agents
**Use case:** Multi-agent workflows, complex implementations. All phases use concurrent BRC execution.

**Environment:**
```bash
EGG_ORCHESTRATOR_MODE=distributed
EGG_ORCHESTRATOR_URL=http://172.32.0.3:9849
EGG_PIPELINE_ID=issue-123
EGG_AGENT_ROLE=coder  # or tester, documenter
```

## Component Interaction

### Network Architecture

All components communicate over Docker networks with controlled access:

| Network | Purpose | Components |
|---------|---------|------------|
| `egg-isolated` | Internal communication | Gateway, Orchestrator, Sandboxes |
| `egg-external` | Internet access | Gateway only (proxies for sandboxes) |

Fixed IPs:
- Gateway: `172.32.0.2` (isolated), `172.33.0.2` (external)
- Orchestrator: `172.32.0.3` (isolated), `172.33.0.3` (external)
- Sandboxes: Dynamic allocation in `172.32.0.128/25` (.128–.254), keeping .2–.127 reserved for static assignments

### API Endpoints

**Gateway (`/api/v1/`)**
- `GET /health` - Health check (includes orchestrator connectivity)
- `POST /git/*` - Policy-enforced git operations
- `POST /gh/*` - Policy-enforced GitHub CLI operations

**Orchestrator (`/api/v1/`)**
- `GET /health` - Health check
- `GET/POST /pipelines` - Pipeline CRUD (list, create). Creating a pipeline whose existing record is in a terminal state (failed, cancelled, or complete) automatically replaces it, enabling resubmission without a prior delete
- `DELETE /pipelines/{id}` - Delete pipeline (stops containers, cleans up remote worktree branches best-effort, removes state)
- `POST /pipelines/{id}/start` - Start or restart a pipeline (restarts failed pipelines by resetting the failed phase; recovers orphaned AWAITING_HUMAN pipelines by parsing the latest phase_gate resolution and either advancing to next phase or resetting for re-run; worktrees are preserved across restarts)
- `GET /pipelines/{id}/visualization` - Pipeline status snapshot (JSON, text, or ASCII)
- `GET /pipelines/{id}/stream` - Real-time SSE stream for single pipeline events and visualization
- `GET /pipelines/stream` - Unified SSE stream for all active pipelines (supports `?ascii=true`, `?active_only=false`, `?full_dag=true`)
- `POST /pipelines/{id}/signal` - Sandbox signals (complete, progress, error, readiness)
- `GET|POST /pipelines/{id}/messages` - Inter-agent message bus (send/poll; concurrent mode)
- `GET /pipelines/{id}/messages/status` - Message bus statistics (concurrent mode)
- `GET /pipelines/{id}/decisions` - HITL decision queue
- `POST /pipelines/{id}/deployment-check/start` - Start devserver for deployment validation
- `GET /pipelines/{id}/deployment-check/status` - Poll devserver status
- `POST /pipelines/{id}/deployment-check/teardown` - Tear down devserver
- `GET /pipelines/{id}/health` - On-demand pipeline health check (all tiers)
- `GET /pipelines/{id}/health/alerts` - List active health alerts
- `GET /pipelines/{id}/progress` - Query structured progress events
- `POST /pipelines/{id}/progress` - Emit a structured progress event (CLI: `egg-orch progress emit`)

**Anchors (`/api/v1/anchors/`)**
- `POST /anchors/{agent_id}` - Create or update an agent anchor (stored in Redis; validated against schema)
- `GET /anchors/{agent_id}` - Get an agent's anchor (cross-agent reads via API)
- `DELETE /anchors/{agent_id}` - Delete an anchor
- `GET /anchors/team/{pipeline_id}` - Get team anchor (orchestrator-generated projection of all agent anchors)
- `POST /anchors/gc/{pipeline_id}` - Garbage-collect anchors for a completed/failed pipeline

**MCP Server (`/mcp`)**
- `GET /health` - MCP server health check
- `POST /mcp` - Streamable HTTP transport endpoint (MCP protocol via JSON-RPC)

Available MCP tools (orchestrator-backed): `submit_task`, `get_status`, `provide_input`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `validate_config`

Available MCP tools (gateway-backed, requires `gateway_url`): `list_checkpoints`, `search_checkpoints`, `get_contract`

**CLI Access:**
The `egg-orch` CLI (`sandbox/bin/egg-orch`) provides command-line access to all orchestrator API endpoints. Available in sandbox containers for agent use, or can be run from the host with appropriate environment variables. See the [README CLI Reference](../../README.md#egg-orch-cli) for command details.

### Signal Flow

1. **Orchestrator → Sandbox**: Container spawn with env vars
2. **Sandbox → Orchestrator**: Signal on completion/error
3. **Gateway → Orchestrator**: Health check (optional)
4. **Orchestrator → GitHub**: Webhook responses, PR updates

## Devserver Management (Deployment Validation)

The orchestrator manages Docker-in-Docker (DinD) devserver stacks during deployment validation checks. This enables testing agent-modified code against locally running services before merge.

### Architecture

**Orchestrator responsibilities:**
- Extract `docker-compose.yml` from committed state (before agent changes)
- Generate override mounts for agent-modified services
- Create isolated Docker network (`egg-check-{pipeline_id}`)
- Start devserver stack with resource limits
- Provide status polling endpoints for sandbox check runner
- Tear down stack after validation completes

**Sandbox check runner responsibilities:**
- Signal orchestrator to start devserver via REST API
- Poll status until healthy or timeout
- Run health checks against service endpoints
- Run validation tests from `.egg/deployment.yml`
- Signal teardown

### Security Properties

**Network isolation:**
- Devserver containers run in dedicated `egg-check-{pipeline_id}` bridge network
- No internet access (internal-only, no gateway, no DNS)
- Services can only communicate within the isolated network
- Sandbox checker makes HTTP requests from outside the devserver network

**Resource limits (per container):**
- CPU: 1.0 core
- Memory: 512 MB
- PIDs: 256 (prevents fork bombs)
- Hard timeout: 5 minutes for entire lifecycle

**Credential safety:**
- No cloud credentials or production secrets injected
- Environment variables scanned for suspicious patterns (AWS_*, GCP_*, AZURE_*, GOOGLE_CLOUD_*, *_SECRET_KEY, *_API_KEY, *_ACCESS_KEY, *_TOKEN, *_PASSWORD, *_CREDENTIALS)
- Only target repo code is mounted (no access to egg internals)

### Configuration

Target repositories opt in by providing `.egg/deployment.yml`:

```yaml
compose_file: "docker-compose.yml"
services:
  - source_dir: "services/api"
    service_name: "api"
    container_mount_path: "/app"
health_endpoints:
  api: "/health"
validation_tests:
  - service: "api"
    path: "/users"
    method: "GET"
    expected_status: 200
    description: "API smoke test"
```

See `shared/egg_contracts/deployment.py` for full schema.

### API Flow

1. **Start**: Sandbox calls `POST /api/v1/pipelines/{id}/deployment-check/start`
   - Orchestrator extracts compose config, generates overrides, starts stack
   - Returns immediately with `{"status": "starting"}`

2. **Poll**: Sandbox polls `GET /api/v1/pipelines/{id}/deployment-check/status`
   - Returns `{"status": "starting" | "healthy" | "unhealthy" | "error"}`
   - Includes service IPs and ports when healthy

3. **Validate**: Sandbox runs health checks and tests against service endpoints

4. **Teardown**: Sandbox calls `POST /api/v1/pipelines/{id}/deployment-check/teardown`
   - Orchestrator stops containers, removes network
   - Returns `{"status": "stopped"}`

See `orchestrator/devserver.py` and `orchestrator/routes/checks.py` for implementation.

## Sandbox Lifecycle

### Orchestrator Mode Detection

The sandbox detects orchestrator mode via environment:

```python
# Detection priority:
# 1. Explicit mode: EGG_ORCHESTRATOR_MODE=remote-single|distributed
# 2. Implicit: EGG_PIPELINE_ID is set
# 3. Implicit: EGG_ORCHESTRATOR_URL is set
```

### Completion Signaling

On exit, orchestrator-managed sandboxes signal completion:

```python
# Success (exit code 0)
POST /api/v1/pipelines/{pipeline_id}/signal
{
    "signal_type": "complete",
    "agent_role": "coder",
    "commit": "abc1234",
    "files_changed": ["src/main.py"]
}

# Failure (non-zero exit)
POST /api/v1/pipelines/{pipeline_id}/signal
{
    "signal_type": "error",
    "agent_role": "coder",
    "error": "Container exited with code 1",
    "recoverable": false
}
```

## Shared Package

The `egg_orchestrator` shared package (`shared/egg_orchestrator/`) provides:

| Module | Purpose |
|--------|---------|
| `client.py` | `OrchestratorClient` for signal API |
| `types.py` | Typed data classes (signals, responses) |
| `detection.py` | Mode detection utilities |
| `constants.py` | Port numbers, network IPs |

Usage:
```python
from egg_orchestrator import (
    OrchestratorClient,
    is_orchestrator_mode,
    DeploymentMode,
)

if is_orchestrator_mode():
    client = OrchestratorClient()
    client.signal_complete(
        pipeline_id="issue-123",
        agent_role="coder",
        commit="abc1234",
    )
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EGG_ORCHESTRATOR_MODE` | Deployment mode (`local`, `remote-single`, `distributed`) | `local` |
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL | None |
| `EGG_PIPELINE_ID` | Current pipeline identifier | None |
| `EGG_AGENT_ROLE` | Agent role for multi-agent mode | None |
| `EGG_BRANCH` | Target branch for the agent's worktree | `egg/{pipeline_id}/work` |
| `EGG_PRIVATE_MODE` | Private network mode (set by host wrapper, detected by `egg-sdlc`) | None |
| `HOST_HOME` | Docker host's home directory (e.g., `/home/jwies`); used to translate host worktree paths to orchestrator-accessible paths | None |

### Constants

Defined in `shared/egg_config/constants.py`:

```python
ORCHESTRATOR_CONTAINER_NAME = "egg-orchestrator"
ORCHESTRATOR_PORT = 9849
ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"
ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"
```

## Related Documentation

- [Gateway README](../../gateway/README.md) - Gateway sidecar details
- [Sandbox README](../../sandbox/README.md) - Sandbox container details
- [Shared README](../../shared/README.md) - Shared packages
- [egg_contracts](../../shared/egg_contracts/) - Contract models and orchestration
