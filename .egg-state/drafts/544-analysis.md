# Analysis: Implement remaining orchestrator integration items

> Issue: #544 | Phase: refine

## Problem Statement

Five acceptance criteria from the orchestrator contract (#524) remain unimplemented. These items form the integration layer needed for remote/distributed deployment modes where sandboxes and the orchestrator run on separate hosts or in different container environments:

1. **AC-24**: Gateway health endpoint doesn't report orchestrator connectivity
2. **AC-27**: No typed Python client exists for sandbox-to-orchestrator communication
3. **AC-28**: Sandboxes don't detect orchestrator mode or signal completion on exit
4. **AC-29**: No shared package makes orchestrator types available across containers
5. **AC-33**: Orchestrator deployment modes lack architecture documentation

The core orchestrator functionality (pipeline engine, phase execution, checkpoint system, HITL gates, gateway session management) is implemented. These items complete the picture for production-grade distributed deployments.

## Current Behavior

### AC-24: Gateway Health Endpoint

**File**: `gateway/gateway.py:397-427`

The current `/api/v1/health` endpoint reports:
```json
{
  "status": "healthy|degraded",
  "github_token_valid": true,
  "auth_configured": true,
  "active_sessions": 3,
  "service": "gateway",
  "client_ip": "172.32.0.10"
}
```

It checks GitHub token validity and launcher secret configuration, but has no awareness of the orchestrator. The orchestrator already has a health endpoint at `GET /api/v1/health` (file: `orchestrator/routes/health.py:12-41`) that returns component status.

### AC-27: Sandbox-to-Orchestrator Client

**Current state**: No `sandbox/egg_lib/orchestrator_client.py` exists.

The **orchestrator→gateway** direction has a typed client (`orchestrator/gateway_client.py:77-479`) with:
- `GatewayClient` class with session management, health checks, proxy configuration
- `GatewayHealth` and `SessionInfo` dataclasses
- `GatewayError` exception type
- Singleton pattern via `get_gateway_client()`

The orchestrator already exposes signal endpoints for sandbox callbacks (`orchestrator/routes/signals.py:70-434`):
- `POST /api/v1/pipelines/<id>/signal` — receive complete/progress/error/heartbeat signals
- `POST /api/v1/pipelines/<id>/signal/batch` — batch signal handling

However, sandboxes currently have no typed client to call these endpoints.

### AC-28: Orchestrator Mode Detection

**File**: `sandbox/entrypoint.py`

The entrypoint currently supports two modes:
1. **Interactive mode** (default): When no args provided, launches Claude Code CLI
2. **Exec mode**: When args provided, executes the given command

Both modes use `os.execvpe()` which replaces the process, so the entrypoint doesn't "return" after launching. Signal handlers (`SIGTERM`, `SIGINT`) call `cleanup_on_exit()` which performs minimal cleanup.

**Missing orchestrator awareness**:
- No detection of `EGG_PIPELINE_ID` or `EGG_ORCHESTRATOR_URL` environment variables
- No completion signaling back to orchestrator on exit
- No exit code propagation to orchestrator

The orchestrator sets these environment variables when spawning containers (`orchestrator/gateway_client.py:443-478`), but the sandbox doesn't act on them.

### AC-29: Shared Orchestrator Package

**Current shared packages** (`shared/`):
- `egg_config/` — Configuration framework with constants, validators, service configs
- `egg_logging/` — Structured JSON logging
- `egg_git/` — Git utilities (default branch detection)
- `egg_container/` — Container launch command builder
- `egg_contracts/` — SDLC contract models, validation, HITL (76 exports)

**Missing**: `shared/egg_orchestrator/` — No shared package for orchestrator types.

The orchestrator defines its own models in `orchestrator/models.py`:
- `PipelinePhase`, `PipelineStatus`, `AgentRole`, `ContainerStatus` enums
- `Pipeline`, `PhaseExecution`, `ContainerInfo`, `HITLDecision` dataclasses

These types would be useful in both gateway (for health reporting) and sandbox (for completion signaling), but currently live only in the orchestrator package.

### AC-33: Architecture Documentation

**Existing docs**:
- `docs/architecture/README.md` — System overview (two-container model, components)
- `docs/guides/deployment.md` — Three deployment methods (CLI, Docker Compose, GitHub Action)
- `docs/guides/sdlc-pipeline.md` — Pipeline operational guide

**Missing**: `docs/architecture/orchestrator.md` describing:
- The three deployment modes (local, remote-single, distributed)
- How components interact in each mode
- Network topology per mode
- When to use each mode

The deployment guide (`docs/guides/deployment.md:96-118`) shows a network diagram but doesn't explain the orchestrator's role in different deployment scenarios.

## Constraints

### Technical Constraints

1. **Network isolation**: Sandboxes on the `egg-isolated` network can only reach the gateway (172.32.0.2) and orchestrator (172.32.0.3). The orchestrator client must use these internal IPs.

2. **No direct credentials**: Sandboxes don't have GitHub tokens or launcher secrets. Any orchestrator client must work without privileged credentials.

3. **Process replacement**: The entrypoint uses `os.execvpe()`, so completion detection must happen before the exec or via signal handlers.

4. **Import compatibility**: Shared packages must work in both Docker containers (`/app/shared/`) and host development (`../../shared/`). Existing packages use `sys.path` manipulation.

5. **Minimal dependencies**: Shared packages should only depend on Python stdlib + pydantic (for models). The orchestrator client in sandbox cannot use `requests` since private mode sandboxes have limited network access.

### Scope Constraints

1. **AC-24** should add orchestrator connectivity check as optional (orchestrator may not be running in CLI-only deployments).

2. **AC-27** should mirror the patterns in `orchestrator/gateway_client.py` for consistency.

3. **AC-28** should be opt-in (detect orchestrator mode via env vars, don't assume orchestrator is always present).

4. **AC-29** should extract types that both gateway and sandbox need, not duplicate the full orchestrator models.

5. **AC-33** should document what exists today (three modes are already supported), not design new modes.

## Options Considered

### Option A: Minimal Integration Layer

**Approach**: Implement each AC as a standalone, minimal addition with no new patterns.

- **AC-24**: Add `orchestrator_reachable: bool` field to health response (simple HTTP check)
- **AC-27**: Single-file client in `sandbox/egg_lib/orchestrator_client.py` using `urllib`
- **AC-28**: Wrapper script that runs entrypoint and signals completion on exit
- **AC-29**: Create `shared/egg_orchestrator/` with only the enums/types needed by gateway+sandbox
- **AC-33**: Write documentation describing existing deployment modes

**Pros**:
- Minimal code changes
- Low risk of breaking existing functionality
- Each AC can be implemented independently

**Cons**:
- Wrapper script approach for AC-28 adds complexity
- May miss edge cases (signal handling, exit codes)

### Option B: Integrated Orchestrator Awareness

**Approach**: Build orchestrator awareness directly into existing components.

- **AC-24**: Gateway health endpoint calls orchestrator health if `ORCHESTRATOR_URL` is set
- **AC-27**: Typed client in `sandbox/egg_lib/orchestrator_client.py` with dataclasses matching `orchestrator/routes/signals.py`
- **AC-28**: Add orchestrator mode detection to `entrypoint.py` with `atexit` handler for completion signaling
- **AC-29**: Extract shared types to `shared/egg_orchestrator/` and update orchestrator to import from there
- **AC-33**: Comprehensive architecture doc with diagrams for each mode

**Pros**:
- Cleaner integration with existing code
- Exit handling happens in the entrypoint itself
- Shared types prevent drift between components

**Cons**:
- More invasive changes
- AC-29 requires updating orchestrator imports (migration step)

### Option C: Hybrid Approach (Recommended)

**Approach**: Use Option B for most items, but keep AC-28 simpler by using `atexit` + signal handlers instead of a wrapper.

**AC-24**: Gateway health endpoint adds optional `orchestrator` field:
```python
# In gateway/gateway.py health_check()
if orchestrator_url := os.environ.get("ORCHESTRATOR_URL"):
    try:
        resp = urlopen(f"{orchestrator_url}/api/v1/health", timeout=5)
        orchestrator_healthy = resp.status == 200
    except:
        orchestrator_healthy = False
    response["orchestrator"] = {
        "reachable": orchestrator_healthy,
        "url": orchestrator_url
    }
```

**AC-27**: Create `sandbox/egg_lib/orchestrator_client.py` following `orchestrator/gateway_client.py` patterns:
- `OrchestratorClient` class with signal methods
- `SignalType` enum (complete, progress, error, heartbeat)
- `SignalResponse` dataclass
- Uses `urllib` (no external dependencies)

**AC-28**: Modify `sandbox/entrypoint.py`:
```python
# Detect orchestrator mode
ORCHESTRATOR_URL = os.environ.get("EGG_ORCHESTRATOR_URL")
PIPELINE_ID = os.environ.get("EGG_PIPELINE_ID")
AGENT_ROLE = os.environ.get("EGG_AGENT_ROLE")

def signal_completion(exit_code: int):
    if ORCHESTRATOR_URL and PIPELINE_ID:
        client = OrchestratorClient(ORCHESTRATOR_URL)
        if exit_code == 0:
            client.signal_complete(PIPELINE_ID, AGENT_ROLE)
        else:
            client.signal_error(PIPELINE_ID, AGENT_ROLE, f"Exit code: {exit_code}")

# Register atexit handler (runs before os.execvpe)
atexit.register(lambda: signal_completion(0))

# Update signal handlers to signal completion
def signal_handler(signum: int, frame: Any) -> None:
    signal_completion(0)
    cleanup_on_exit(config, logger)
    sys.exit(0)
```

**Note**: `atexit` handlers don't run when `os.execvpe()` is called because the process is replaced. The completion signaling must happen either:
1. Before the `execvpe()` call (not useful—we want to signal when the agent finishes)
2. Via signal handlers (SIGTERM when container stops)
3. By the orchestrator detecting container exit (current behavior)

This is a key design decision—see Open Questions below.

**AC-29**: Create `shared/egg_orchestrator/`:
```
shared/egg_orchestrator/
├── __init__.py        # Exports: SignalType, AgentRole, PipelinePhase, etc.
├── types.py           # Shared enums and dataclasses
└── py.typed           # PEP 561 marker
```

Only include types that both gateway and sandbox need. Keep full pipeline models in orchestrator.

**AC-33**: Create `docs/architecture/orchestrator.md`:
- Deployment modes: local (CLI), remote-single (compose), distributed (future)
- Component interaction diagrams per mode
- When to use each mode
- Configuration requirements per mode

**Pros**:
- Consistent patterns with existing code
- Clear separation of shared vs orchestrator-specific types
- Documentation reflects actual current state

**Cons**:
- AC-28 completion signaling has edge cases (see Open Questions)

## Recommended Approach

**Option C (Hybrid Approach)** is recommended.

**Rationale**:
1. Follows existing patterns (gateway_client.py for AC-27)
2. Shared types prevent drift (AC-29)
3. Gateway health enhancement is backward-compatible (AC-24)
4. Documentation captures current reality (AC-33)
5. AC-28 needs clarification on completion signaling approach

**Implementation order**:
1. **AC-29** first — shared types are needed by AC-27
2. **AC-27** second — orchestrator client needed by AC-28
3. **AC-28** third — depends on AC-27
4. **AC-24** fourth — can use shared types
5. **AC-33** last — documents the complete picture

## Open Questions

### 1. How should sandbox completion be signaled to the orchestrator?

The current entrypoint uses `os.execvpe()` which replaces the Python process with Claude Code. This means:
- `atexit` handlers don't run
- The entrypoint doesn't "return" after the agent finishes
- Only signal handlers (SIGTERM) could send completion signals

**Options**:

```markdown
egg-contract add-decision --question "How should sandbox completion be signaled to the orchestrator?" \
  --options \
  "Container exit detection" \
  "Wrapper process approach" \
  "Agent-initiated signaling" \
  --format markdown
```

<!-- HITL-DECISION: completion-signaling -->
**Question**: How should sandbox completion be signaled to the orchestrator?

- [ ] **Container exit detection** — Orchestrator monitors container exit (current behavior). No sandbox changes needed. Orchestrator already does this via Docker events.
- [ ] **Wrapper process approach** — Replace `os.execvpe()` with `subprocess.run()` so entrypoint can signal after agent exits. More control but changes execution model.
- [ ] **Agent-initiated signaling** — Claude Code (the agent) calls the orchestrator API before exiting. Requires changes to agent prompts/commands. Most accurate signal timing.
- [ ] Other (explain in reply)
<!-- /HITL-DECISION -->

### 2. What types should be shared in egg_orchestrator?

```markdown
egg-contract add-feedback \
  --question "Should shared/egg_orchestrator/ include only signal-related types, or also pipeline state types (PipelinePhase, PipelineStatus)?" \
  --format markdown
```

<!-- HITL-FEEDBACK: shared-types-scope -->
**Question**: Should `shared/egg_orchestrator/` include only signal-related types (SignalType, AgentRole), or also pipeline state types (PipelinePhase, PipelineStatus, ContainerStatus)?

Including more types enables richer gateway health reporting (e.g., current pipeline phase) but increases coupling. Including fewer types minimizes coupling but limits what the gateway can report.

Your answer: ___

- [ ] Submit feedback
<!-- /HITL-FEEDBACK -->

### 3. Should AC-24 health check be synchronous or async?

The gateway health endpoint is synchronous. Adding an orchestrator connectivity check adds latency.

**Recommendation**: Use a short timeout (2-5 seconds) and make the check optional (only when `ORCHESTRATOR_URL` is set). Health endpoints should be fast.

---

*Authored-by: egg*
