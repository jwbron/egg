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

### MCP Integration Status

MCP integration is partially implemented per `docs/adr/implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md`:
- GitHub MCP Server is active (configured via `api.githubcopilot.com`)
- No custom MCP server exists in the codebase for egg-specific operations
- No MCP server exposes orchestrator APIs or coordinator capabilities

### SSE Infrastructure

The orchestrator already has mature SSE streaming (`orchestrator/sse.py` and `orchestrator/unified_sse.py`):
- Per-pipeline event streaming with heartbeats
- Unified multi-pipeline stream (added in #620)
- Event bus with pub/sub pattern (`orchestrator/events.py`)
- DAG visualization rendering per client preference

### Key Limitation

The orchestrator has no concept of a persistent "coordinator" session. All intelligence about *what* to do lives in pre-configured dispatch logic (`shared/egg_contracts/orchestrator.py`) and phase defaults. The system cannot reason about whether a phase should be skipped, whether to loop back after a test failure, or whether a task is simple enough for a single agent. No coordinator-facing MCP server exists.

## Constraints

### Technical Constraints
- **Gateway enforcement must be preserved** — the structural guarantees (branch ownership, credential isolation, merge blocking) are a core security property. A coordinator cannot bypass these.
- **Dependency on #1027** — the cross-agent communication infrastructure (message bus, consensus, concurrent execution) is the foundation the coordinator will use. This is now merged.
- **Claude session persistence** — a coordinator Claude session managing a multi-hour workflow needs to handle context limits and session resumption. Claude's context window is ~200k tokens; typical SDLC pipeline runs span 30-120 minutes. The coordinator must survive context compression and potential session crashes.
- **Docker container lifecycle** — the coordinator must live somewhere (a container, the host, or as a long-running process) and interact with Docker to spawn agent containers.
- **Orchestrator API surface** — current APIs are designed for fixed-phase pipelines. The coordinator needs APIs for flexible agent spawning without rigid phase constraints.
- **Agent instruction protocol** — the coordinator needs a mechanism to pass task-specific context, expectations, and instructions to each agent it spawns. Currently, agent instructions come from `sandbox/.claude/rules/` templates and contract data; a coordinator would need to inject dynamic per-task instructions. This should follow the "orientation not pre-fetching" principle — passing task objectives and lightweight metadata rather than pre-fetched diffs or file contents.

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
- MCP server for bridging the coordinator to external Claude Code sessions (see MCP Server Design section below)

The coordinator uses `claude --print` (headless mode) for agent execution, consistent with existing agent patterns (see `concurrent_executor.py`). Sub-reasoning happens via Claude Code, not direct Anthropic API calls.

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

## MCP Server Design

The issue identifies the MCP server as "a thin bridge between the coordinator and the outside world." This is a critical component that exposes coordinator capabilities to external Claude Code sessions.

### Purpose

The MCP server enables the human interaction pattern: a user in a Claude Code session describes a task conversationally, Claude calls MCP tools to submit the task to the coordinator, and receives status updates and escalations back through the same tools. The MCP server is not the coordinator itself — it's the communication layer between the coordinator agent (running inside a sandbox container) and external consumers (Claude Code sessions, async triggers).

### Tool Definitions

The MCP server would expose tools such as:
- **`submit_task`** — Accept a natural language task description and optional metadata (issue number, repo, urgency). Creates a coordinator pipeline and returns a task ID.
- **`get_status`** — Query the current state of a coordinator-managed task: which agents are running, what phase the work is in, any pending decisions.
- **`provide_input`** — Supply human input in response to a coordinator escalation (e.g., answering a question, approving a direction, redirecting the approach).
- **`list_tasks`** — List active and recent coordinator tasks.
- **`cancel_task`** — Request cancellation of a running task.

### Protocol Choices

Two MCP transport protocols are available:

- **stdio** — the coordinator launches the MCP server as a subprocess and communicates via stdin/stdout. This is the standard Claude Code MCP pattern. Simple to implement but requires the MCP server process to coexist with the Claude Code session.
- **SSE (Server-Sent Events)** — the MCP server runs as an HTTP endpoint. The Claude Code session connects to it over the network. This allows the MCP server to run independently (e.g., as a sidecar alongside the orchestrator) and serve multiple clients.

For the coordinator use case, **SSE is more appropriate** because:
1. The MCP server needs to be reachable from any Claude Code session (not just one process)
2. It should persist independently of any single client session
3. It naturally maps to the orchestrator's existing HTTP API architecture
4. It supports the async/unattended mode where no Claude Code session is active

### State Management

The MCP server itself is stateless — it proxies requests to the orchestrator, which owns all pipeline state. This means:
- The MCP server can crash and restart without losing task state
- Multiple MCP server instances can run concurrently (for availability)
- State recovery is handled by the orchestrator's existing persistence model

### Deployment

The MCP server would run as a sidecar process alongside the orchestrator (or within the orchestrator container), sharing the same network. It translates MCP protocol messages into orchestrator REST API calls.

## Claude Code Integration

The issue describes the Claude Code session as the primary human interface. The user describes tasks conversationally; Claude calls MCP tools to submit tasks to the coordinator, check status, and provide input when escalated.

### Interactive Mode

In interactive mode, the user has an active Claude Code session. The interaction pattern is:

1. User describes a task: "Fix the auth bug in #432"
2. Claude Code calls `submit_task` via MCP → coordinator receives task
3. Coordinator analyzes the issue, decides on agents, instructs orchestrator
4. User can query status: "How's the auth fix going?" → `get_status` via MCP
5. Coordinator escalates when input needed: "Tester found an edge case, should I have the coder fix it?" → `provide_input` via MCP
6. User can redirect: "Actually include a documenter too" → `provide_input` via MCP

The Claude Code session does not make workflow decisions — it's a conversational interface. The coordinator (running as an agent container) makes all orchestration decisions.

### Relationship to Existing SDLC CLI

The SDLC pipeline is one workflow the coordinator can choose to run. For simple tasks, the coordinator skips phases. The existing pipeline remains available directly via `egg-sdlc` for users who prefer explicit control. The coordinator mode and CLI mode coexist — they are alternative entry points into the same orchestrator.

## Async / Unattended Mode

The same coordinator agent can be triggered without a live Claude Code session. Entry points include:

- **Slack** — user posts a task description; a Slack integration calls the MCP server's `submit_task` tool
- **Webhook** — CI or external systems trigger tasks via HTTP
- **GitHub issue events** — new issue created with a label triggers the coordinator (similar to existing `egg-sdlc` but with coordinator-driven workflow selection)

In unattended mode, the coordinator runs to completion and notifies the human when done or when input is needed (via Slack, GitHub comment, or email). The MCP server and coordinator logic are shared between interactive and async modes — the only difference is the trigger mechanism and the notification channel.

This is designated as a follow-on concern in the issue, but it influences the coordinator's design: the coordinator must not assume a live human session. Escalation must support both synchronous (MCP tool response) and asynchronous (notification + wait for response) patterns.

## Recommended Approach

**Option A: Coordinator as an Orchestrator Extension** is recommended.

This approach is the most natural evolution of the current architecture. The coordinator is just another agent — but with elevated permissions and coordinator-specific tools. This preserves the security model (gateway enforcement), reuses all existing infrastructure (container lifecycle, message bus, consensus), and requires no new services.

The key insight is that the coordinator doesn't need to be a fundamentally different kind of entity. It's a Claude session with the right tools: spawn agents, send messages, monitor progress, advance phases, and escalate to humans. The orchestrator already provides most of these capabilities via its API.

The main risk — coordinator session lifetime — can be mitigated with checkpoint/resume support (building on the existing checkpoint infrastructure) and by designing the coordinator to be stateless enough that it can resume from orchestrator state after a crash. Per agent-design reviewer guidance: a resumed coordinator should re-assess from current orchestrator state (objectives + current state), not replay prior reasoning.

Option B is explicitly rejected because it violates the EGG200 convention of not making direct LLM API calls from infrastructure code. Option C has merit for a future "interactive mode" but conflates the coordinator with the user's development environment. Option A can evolve into Option C later if desired.

### Observability

The coordinator's decision-making must be observable. Since the coordinator runs as an agent container, its session will be captured by the checkpoint system. Key decisions should be emitted as lightweight event signals on the message bus for real-time monitoring via the SSE stream and DAG visualizer. Proposed message types:

- `WORKFLOW_DECISION` — coordinator chose to skip/reorder phases (e.g., "skipping refine for simple bug fix")
- `AGENT_SPAWN` — coordinator requested a new agent (e.g., "spawning coder for issue #432")
- `ESCALATION` — coordinator surfaced a question to the human
- `LOOPBACK` — coordinator decided to re-run an agent based on results (e.g., "tester found issue, re-spawning coder")

These should be simple event emissions (subject + brief body), not detailed structured summaries of coordinator reasoning — per agent-design reviewer guidance, avoid building structured-output requirements around coordinator thinking.

## Open Questions

The following questions have been registered as contract decisions and feedback items via `egg-contract`. Each question below corresponds to an `egg-contract` registration.

### Decisions (Multiple Choice)

<!-- egg-hitl-decision id=decision-1 -->
**1. Coordinator session persistence model**: How should the coordinator handle long-running tasks that exceed a single Claude session? (Claude's context window is ~200k tokens; typical pipeline runs are 30-120 min with potentially dozens of tool calls.)
- **Checkpoint/resume** — Save coordinator state to orchestrator at key points; resume from state after crash (builds on existing checkpoint infra)
- **Stateless polling** — Coordinator is ephemeral; re-spawned periodically to poll orchestrator state and take next action
- **Long-running session** — Accept context limits; design coordinator prompts to be compact enough for a single session

<!-- egg-hitl-decision id=decision-2 -->
**2. Phase model for coordinator-driven pipelines**: Should the coordinator operate within the existing phase system, outside it, or in a hybrid mode?
- **Within existing phases** — Coordinator uses dynamic phase selection within the REFINE → PLAN → IMPLEMENT → PR model (can skip phases but phases still exist)
- **Outside phases** — New freeform execution model with its own permission model
- **Hybrid** — Phases exist but are advisory; coordinator can skip/reorder freely with a fallback permission model

<!-- egg-hitl-decision id=decision-3 -->
**3. Coordinator authority level**: What should the coordinator be able to do without human approval?
- **Full autonomy** — Spawn agents, skip phases, loop back, create PRs without approval (human reviews PR at the end)
- **Checkpoint approval** — Autonomous within a phase but requires approval at phase boundaries
- **Supervised** — Every significant decision (agent spawn, phase skip, loopback) requires human approval

<!-- egg-hitl-decision id=decision-4 -->
**4. Scope of initial implementation**: How much of the coordinator system should be built in the first iteration?
- **Full system** — Coordinator agent + MCP server + Claude Code integration + async triggers
- **Core only** — Coordinator agent + orchestrator API extensions (MCP server in follow-up)
- **Prototype** — Coordinator agent with manual CLI interaction only (validate the concept before building MCP)

### Feedback (Open-Ended)

<!-- egg-hitl-feedback id=feedback-1 -->
**5. Cost guardrails**: What cost limits or guardrails should be applied to coordinator sessions? (e.g., max agents per task, max total token budget, max wall-clock time)

<!-- egg-hitl-feedback id=feedback-2 -->
**6. Failure recovery expectations**: What should happen when the coordinator session crashes mid-task with running agents? (e.g., running agents continue and auto-commit, new coordinator is spawned to assess state, human is notified)

<!-- egg-hitl-feedback id=feedback-3 -->
**7. Multi-task coordination**: Should the coordinator be able to manage multiple related issues simultaneously, or is one-issue-at-a-time sufficient for v1? Are there specific multi-task scenarios you want supported?

---

*Authored-by: egg*

# metadata
complexity_tier: high
parallel_phases: true
