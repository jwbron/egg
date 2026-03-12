# Analysis: Conversational Claude coordinator for dynamic agent orchestration

> Issue: #1028 | Phase: refine

## Problem Statement

The current SDLC pipeline follows a rigid, fixed-phase sequence (refine → plan → implement → pr) driven by CLI commands and pre-configured automation. Every task—regardless of complexity—traverses the same phases, requires explicit CLI orchestration to advance, and cannot adapt mid-execution. The proposal is to introduce a conversational Claude coordinator agent that understands tasks, decides which agents to spawn, delegates work dynamically, and adapts the workflow in real-time.

The desired outcome is a system where a user describes a task in natural language and a Claude coordinator manages the full lifecycle—spawning only the agents needed, skipping unnecessary phases, and creating adaptive feedback loops—while the existing SDLC pipeline remains available as an alternative.

## Current Behavior

### SDLC Pipeline Architecture

The orchestrator (`orchestrator/`) manages the pipeline lifecycle through four fixed phases defined in `orchestrator/models.py:15-21`:

```
REFINE → PLAN → IMPLEMENT → PR
```

Each phase has:
- **Fixed agent roles** dispatched via `orchestrator/dispatch.py` using wave-based execution (`orchestrator/multi_agent.py`)
- **HITL gates** between phases requiring human approval
- **Phase permissions** enforced by the gateway sidecar (`gateway/phase_filter.py`, `.egg/phase-permissions.json`)
- **Structural enforcement** via readonly mounts, branch locks, and commit validation

### Agent Execution Model

Agents are spawned as Docker containers (`orchestrator/container_spawner.py`) with:
- Gateway session registration for credential isolation
- Phase-specific environment variables (`EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `EGG_PHASE`)
- Isolated worktree branches (`egg/issue-{N}/{role}`)
- Instructions injected via `sandbox/.claude/rules/` combined into `CLAUDE.md`
- Claude CLI execution in headless mode (`claude --print --dangerously-skip-permissions`)

### Cross-Agent Communication (#1027, merged)

The recently merged concurrent execution system (`orchestrator/concurrent_executor.py`) enables:
- Simultaneous agent execution within a phase via `ConcurrentPhaseExecutor`
- In-memory message bus (`orchestrator/message_store.py`) with typed messages (`PROGRESS`, `QUESTION`, `STATUS`, `AGENT_FAILED`, `HANDOFF`)
- Consensus-based phase completion (`orchestrator/consensus.py`) with readiness states (`WORKING`, `READY`, `BLOCKED`, `OBJECTING`)
- REST API for messages (`orchestrator/routes/messages.py`) and signals (`orchestrator/routes/signals.py`)
- Per-agent worktree isolation with `EGG_CONCURRENT_MODE=true`

### Agent Role Model

Current roles are defined in `orchestrator/models.py:64-87` as an `AgentRole` enum: `CODER`, `REVIEWER`, `CHECKER`, `TESTER`, `DOCUMENTER`, `INTEGRATOR`, plus phase-specific roles (`ARCHITECT`, `TASK_PLANNER`, `RISK_ANALYST`, `REFINER`, `INSPECTOR`, and several reviewer subtypes). There is no `COORDINATOR` role.

### Key Limitation

The orchestrator has no concept of a persistent "coordinator" session. All intelligence about *what* to do lives in pre-configured dispatch logic (`shared/egg_contracts/orchestrator.py`) and phase defaults. The system cannot reason about whether a phase should be skipped, whether to loop back after a test failure, or whether a task is simple enough for a single agent. No MCP server exists in the codebase.

## Constraints

### Technical Constraints
- **Gateway enforcement must be preserved** — the structural guarantees (branch ownership, credential isolation, merge blocking) are a core security property. A coordinator cannot bypass these.
- **Dependency on #1027** — the cross-agent communication infrastructure (message bus, consensus, concurrent execution) is the foundation the coordinator will use. This is now merged.
- **Claude session persistence** — a coordinator Claude session managing a multi-hour workflow needs to handle context limits and session resumption. Claude's context window is ~200k tokens; typical SDLC pipeline runs span 30-120 minutes. The coordinator must survive context compression and potential session crashes.
- **Docker container lifecycle** — the coordinator must live somewhere (a container, the host, or as a long-running process) and interact with Docker to spawn agent containers.
- **Orchestrator API surface** — current APIs are designed for fixed-phase pipelines. The coordinator needs APIs for flexible agent spawning without rigid phase constraints.
- **Agent instruction protocol** — the coordinator needs a mechanism to pass task-specific context, expectations, and instructions to each agent it spawns. Currently, agent instructions come from `sandbox/.claude/rules/` templates and contract data; a coordinator would need to inject dynamic per-task instructions.

### Cost Constraints
- A long-running coordinator session consuming tokens continuously, plus multiple agent sessions, could be significantly more expensive than the current model.
- Coordinator session length is bounded by Claude's context window and conversation limits.
- Need guardrails to prevent runaway agent spawning (a coordinator that keeps spawning agents in a failure loop).

### Compatibility Constraints
- The existing SDLC CLI must continue working for users who prefer explicit control.
- Existing integrations (GitHub Actions, `egg-sdlc` CLI) should not break.
- Gateway phase enforcement assumes a known phase — a coordinator operating outside fixed phases needs a compatible model.

### Scope Constraints
- This is an architectural change that touches orchestrator, gateway, shared libraries, CLI tools, and agent instructions.
- The coordinator concept introduces a new execution model alongside the existing one.

## Options Considered

### Option A: Coordinator as an Orchestrator Extension (Embedded Coordinator)

**Approach**: Add a "coordinator mode" to the existing orchestrator service. The coordinator runs as a privileged agent container with:
- A new `COORDINATOR` agent role with elevated gateway permissions
- Tools to spawn other agents (via orchestrator APIs, not direct Docker access)
- Access to the message bus for monitoring agent progress
- Ability to advance/skip phases via orchestrator APIs
- MCP server for bridging the coordinator to external Claude Code sessions

The coordinator uses `claude --print` (headless mode) for agent execution, consistent with existing agent patterns (see `concurrent_executor.py:164-176`). Sub-reasoning happens via Claude Code, not direct Anthropic API calls.

**Pros**:
- Reuses existing infrastructure (container spawning, gateway sessions, message bus)
- The orchestrator remains the single source of truth for pipeline state
- Gateway enforcement is naturally preserved — coordinator is just another (privileged) agent
- Incremental: existing SDLC pipeline runs unchanged; coordinator is an opt-in mode
- Simpler deployment — no new services needed
- Coordinator prompt design can follow the "prefer what over how" principle — give the coordinator task objectives and tools, not step-by-step orchestration procedures

**Cons**:
- Coordinator session lifetime is bounded by Claude's context window / conversation limits
- Container overhead for the coordinator itself (always running while agents work)
- The orchestrator API may need significant extensions for flexible agent spawning
- Phase enforcement model needs to accommodate a coordinator that operates across phases

### Option B: Coordinator as a Separate Service (External Coordinator)

**Approach**: Build a new standalone coordinator service that wraps the orchestrator. The coordinator service manages a Claude session (via the Anthropic API directly) and exposes its own API for task submission. It translates high-level task descriptions into orchestrator API calls.

**Pros**:
- Clean separation of concerns — orchestrator manages execution, coordinator manages decisions
- Coordinator can manage multiple pipelines simultaneously
- Can implement custom session persistence (save/resume coordinator state)
- Independent scaling and deployment

**Cons**:
- New service to build, deploy, and maintain
- Adds architectural complexity — another moving part
- **Violates the EGG200 convention** — making direct Anthropic API calls from infrastructure code bypasses the sandbox boundary. The codebase convention (per `docs/guides/agent-mode-design.md`) is that LLM reasoning happens inside Claude Code sessions, not via programmatic API calls from orchestration services
- Must duplicate or proxy some orchestrator functionality
- Network latency between coordinator and orchestrator for every decision
- Requires its own credential management for the Anthropic API
- Higher total cost (coordinator service + Claude API calls)

### Option C: Coordinator as a Claude Code Skill/Session (Host-Level Coordinator)

**Approach**: The coordinator runs as a Claude Code session on the host (or a lightweight container), using existing Claude Code tools plus new coordinator-specific skills/tools. It interacts with the orchestrator via `egg-orch` CLI commands or HTTP calls.

**Pros**:
- Leverages Claude Code's native tool system (file I/O, bash, web search)
- Natural conversation interface — user interacts with Claude Code directly
- Can use Claude Code's built-in context management and session persistence
- Lower barrier to entry — familiar `claude` CLI experience
- Can directly read repository context (no need for container indirection)

**Cons**:
- Runs outside the sandbox security boundary — needs careful permission scoping
- Less isolation than a container-based coordinator
- Depends on Claude Code-specific features that may change
- Host-level execution means less portability
- May conflict with existing Claude Code session if user is also using it

## Recommended Approach

**Option A: Coordinator as an Orchestrator Extension** is recommended.

This approach is the most natural evolution of the current architecture. The coordinator is just another agent — but with elevated permissions and coordinator-specific tools. This preserves the security model (gateway enforcement), reuses all existing infrastructure (container lifecycle, message bus, consensus), and requires no new services.

The key insight is that the coordinator doesn't need to be a fundamentally different kind of entity. It's a Claude session with the right tools: spawn agents, send messages, monitor progress, advance phases, and escalate to humans. The orchestrator already provides most of these capabilities via its API.

The main risk — coordinator session lifetime — can be mitigated with checkpoint/resume support (building on the existing checkpoint infrastructure) and by designing the coordinator to be stateless enough that it can resume from orchestrator state after a crash.

Option B is explicitly rejected because it violates the EGG200 convention of not making direct LLM API calls from infrastructure code. Option C has merit for a future "interactive mode" but conflates the coordinator with the user's development environment. Option A can evolve into Option C later if desired.

### Observability

The coordinator's decision-making must be observable. Since the coordinator runs as an agent container, its session will be captured by the checkpoint system. Key decisions (phase skipping, agent spawning, loopback reasoning) should be emitted as structured messages on the message bus for real-time monitoring via the SSE stream and DAG visualizer.

## Open Questions

The following questions have been registered as contract decisions and feedback items via `egg-contract`. Each question below corresponds to an `egg-contract` registration.

### Decisions (Multiple Choice)

1. **Coordinator session persistence model**: How should the coordinator handle long-running tasks that exceed a single Claude session? (Claude's context window is ~200k tokens; typical pipeline runs are 30-120 min with potentially dozens of tool calls.)

2. **Phase enforcement for coordinator**: How should gateway phase enforcement work when the coordinator can dynamically skip/reorder phases?

3. **Coordinator authority level**: What should the coordinator be able to do without human approval?

4. **Scope of initial implementation**: How much of the coordinator system should be built in the first iteration?

### Feedback (Open-Ended)

5. **Cost guardrails**: What cost limits or guardrails should be applied to coordinator sessions? (e.g., max agents per task, max total token budget, max wall-clock time)

6. **Failure recovery expectations**: What should happen when the coordinator session crashes mid-task with running agents?

7. **Multi-task coordination**: Should the coordinator be able to manage multiple related issues simultaneously, or is one-issue-at-a-time sufficient for v1?

8. **User interaction model**: How should users interact with the coordinator — via Slack, GitHub issue comments, a dedicated CLI, or the existing `egg` command? What is the expected UX for submitting tasks and receiving updates?

---

*Authored-by: egg*

# metadata
complexity_tier: high
parallel_phases: true
