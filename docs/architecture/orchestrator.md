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
- Accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree`
- State branch is local-only (not pushed to remote)
- Persistence relies on the Docker state volume (`/home/egg/.egg-state`)

**Key properties:**
- Read/write operations go directly to the worktree directory on disk
- Commits are made in-place and stay on the state branch
- Survives orchestrator restarts by reading from git
- Distinct from checkpoints, which are pushed to remote for cross-container access

**Worktree lifecycle:**
- Created lazily on first state access
- Validated on each access (repairs stale/broken worktrees)
- Cleaned up via `git worktree prune` on first access (not container startup)

This differs from agent worktrees (managed by the gateway for agent isolation). The orchestrator manages its own state worktree independently.

See `orchestrator/state_store.py` for implementation details.

## Per-Pipeline Worktrees

The orchestrator reads pipeline artifacts (verdict files, draft documents, check results) from per-pipeline worktrees created by the gateway. These worktrees isolate work for each pipeline and are separate from both the orchestrator's state worktree and the main repository working directory.

**Architecture:**
- Gateway creates worktrees at `/home/egg/.egg-worktrees/{pipeline-id}/{repo-name}/`
- Agent containers mount these worktrees and write artifacts to them
- Orchestrator mounts `/home/egg/.egg-worktrees` and reads artifacts from pipeline-specific paths
- Worktree paths are resolved dynamically based on pipeline ID and repository

**Key artifact files in worktrees:**
- `.egg-state/contracts/{issue}.json` — Contract state
- `.egg-state/drafts/{issue}-analysis.md` — Draft for `refine` phase (special-cased to `analysis`)
- `.egg-state/drafts/{issue}-{phase}.md` — Draft for other phases (e.g., `plan`). No draft for `implement` phase.
- `.egg-state/reviews/{issue}-{phase}-{reviewer_type}-review.json` — Review verdict files
- `.egg-state/checks/implement-results.json` — Check results from the `implement` phase

**Volume mounts:**
- Orchestrator: Bind mount from `${HOST_HOME}/.egg-worktrees` to `/home/egg/.egg-worktrees` (read container-written artifacts)
- Integration tests: Named volume `worktrees` (no host filesystem in CI)

This architecture ensures the orchestrator reads artifacts from the correct isolated workspace rather than the main repository, preventing cross-contamination between pipelines.

See `orchestrator/routes/pipelines.py:WORKTREE_BASE_DIR` and `gateway/worktree_manager.py` for implementation details.

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

**Use case:** Multi-agent workflows, complex implementations

**Environment:**
```bash
EGG_ORCHESTRATOR_MODE=distributed
EGG_ORCHESTRATOR_URL=http://172.32.0.3:9849
EGG_PIPELINE_ID=issue-123
EGG_AGENT_ROLE=coder  # or tester, documenter, integrator
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
- Sandboxes: Dynamic allocation in `172.32.0.0/24`

### API Endpoints

**Gateway (`/api/v1/`)**
- `GET /health` - Health check (includes orchestrator connectivity)
- `POST /git/*` - Policy-enforced git operations
- `POST /gh/*` - Policy-enforced GitHub CLI operations

**Orchestrator (`/api/v1/`)**
- `GET /health` - Health check
- `GET/POST /pipelines` - Pipeline CRUD
- `GET /pipelines/{id}/visualization` - Pipeline status snapshot (JSON, text, or ASCII)
- `GET /pipelines/{id}/stream` - Real-time SSE stream for pipeline events and visualization
- `POST /pipelines/{id}/signal` - Sandbox signals (complete, progress, error)
- `GET /pipelines/{id}/decisions` - HITL decision queue
- `POST /sdlc-tokens/generate` - Generate approval tokens for SDLC pipeline (requires launcher secret auth)
- `POST /sdlc-tokens/approve` - Validate token and approve SDLC phase
- `POST /sdlc-tokens/reset` - Clear token-gated state for recovery (requires launcher secret auth)

### Signal Flow

1. **Orchestrator → Sandbox**: Container spawn with env vars
2. **Sandbox → Orchestrator**: Signal on completion/error
3. **Gateway → Orchestrator**: Health check (optional)
4. **Orchestrator → GitHub**: Webhook responses, PR updates

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
