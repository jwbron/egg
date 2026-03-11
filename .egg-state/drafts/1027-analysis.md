# Analysis: Enable cross-agent communication and concurrent phase execution

> Issue: #1027 | Phase: refine

## Problem Statement

Today, agents in the SDLC pipeline operate in a strictly sequential, wave-based model. Within the implement phase, agents execute in dependency-ordered waves (Coder → Tester+Documenter → Integrator), communicating only through file-based handoff data written at completion. There is no mechanism for agents to exchange messages while running, meaning a tester cannot flag a problematic approach until after the coder has fully completed, and a documenter cannot ask the coder for clarification mid-implementation.

The desired outcome is a system where agents within the same phase can exchange messages in real-time, multiple agent types run concurrently rather than in sequential waves, and phase completion requires consensus from all participating agents.

## Current Behavior

### Phase Execution Model

The pipeline progresses through four sequential phases: REFINE → PLAN → IMPLEMENT → PR (`orchestrator/models.py:15-21`). Within the implement phase, agents execute in **dependency-ordered waves** managed by `MultiAgentExecutor` (`orchestrator/multi_agent.py:46-315`):

- **Wave 1**: Coder (no dependencies)
- **Wave 2**: Tester + Documenter (both depend on Coder, run in parallel)
- **Wave 3**: Integrator (depends on Coder + Tester)

Each wave must fully complete before the next begins. Agents in the same wave run in parallel but cannot communicate with each other.

### Inter-Agent Communication

Agents currently communicate exclusively through **handoff data** (`orchestrator/handoffs.py:56-230`):

1. Agent completes and signals via `POST /api/v1/pipelines/{id}/signal` with `signal_type: complete`
2. Output (commit SHA, files changed, handoff data dict) is saved to `.egg-state/agent-outputs/{identifier}-{role}-output.json`
3. Next-wave agents read predecessor outputs via `EGG_HANDOFF_DATA` environment variable injected at spawn time

There is no mechanism for in-flight message exchange between running agents.

### Agent Lifecycle

Each agent runs in an isolated Docker container with its own session, worktree branch, and gateway-enforced restrictions (`gateway/README.md`). The orchestrator spawns containers via `ContainerSpawner` (`orchestrator/container_spawner.py`), monitors them via `ContainerMonitor` (`orchestrator/container_monitor.py`), and collects results via signal handlers (`orchestrator/routes/signals.py:76-400`).

### Existing Infrastructure That Supports This Feature

Several components already exist that this feature could build on:

- **EventBus** (`orchestrator/events.py:35-200`): Pub/sub system with per-pipeline handlers and SSE streaming. Currently used for pipeline/phase/agent lifecycle events, but could be extended for inter-agent messages.
- **SSE streaming** (`orchestrator/sse.py:111-350`): Real-time event delivery to clients via `GET /api/v1/pipelines/{id}/stream`. Could be extended to deliver messages to agent containers.
- **Signal API** (`orchestrator/routes/signals.py`): Already handles `complete`, `progress`, `error`, `heartbeat` signal types. A `message` signal type could be added.
- **OrchestratorClient** (`shared/egg_orchestrator/client.py:1-413`): Sandbox-side client for communicating with the orchestrator. Could be extended with send/receive message methods.
- **Tier 3 parallel execution** (`orchestrator/routes/pipelines.py:3302-3580`): Already supports running independent plan phases in parallel via `ThreadPoolExecutor`. The concurrency infrastructure exists.
- **Per-phase worktrees** (`docs/architecture/orchestrator.md:177-181`): Gateway's `WorktreeManager` can create sub-worktrees for parallel phases. Infrastructure for workspace isolation already exists.

## Constraints

### Technical Constraints
- **Git concurrency**: Multiple agents writing to the same branch simultaneously will create merge conflicts. The current gateway enforces single-branch push ownership per pipeline.
- **Container isolation**: Agents run in separate Docker containers on an isolated network (`172.32.0.0/16`). All communication must route through the gateway or orchestrator — no direct container-to-container networking.
- **Gateway policy enforcement**: All git/gh operations are validated by the gateway. Adding inter-agent messages must maintain the audit trail and policy enforcement guarantees.
- **Checkpoint integrity**: The checkpoint system (`gateway/checkpoint_handler.py`) captures session transcripts. Inter-agent messages must be captured in checkpoints for auditability.
- **Phase restrictions**: The gateway enforces file-level restrictions per phase and role (`gateway/phase_filter.py`). Concurrent agents need compatible restrictions.
- **Claude Code agent model**: Agents are Claude Code sessions. They don't have a built-in event loop or message listener. Any push-based delivery would need to integrate with the agent's existing tool/CLI interface.

### Architectural Constraints
- **Orchestrator is single-process**: The orchestrator runs as a single Flask process. Message routing adds load that must be carefully managed.
- **State is git-backed**: Pipeline state is stored on `egg/pipeline-state` branch with cross-process locking (`orchestrator/state_store.py:98-246`). High-frequency message state would stress this mechanism.
- **Role-based mutation**: The contract system enforces role-based field ownership (`gateway/contract_api.py:61-107`). Concurrent agents cannot violate these boundaries.

### Resource Constraints
- Running 3+ agents concurrently per phase multiplies compute costs (each agent is a Claude Code session with Opus-class model).
- Docker container overhead: memory, CPU, and network resources per container.

### Compatibility Constraints
- Must not break existing Tier 1 (single-agent) and Tier 2 (wave-based) execution models.
- The `egg-orch` and `egg-contract` CLIs are the agent-facing interface — new capabilities must be accessible through CLI extensions, not direct API calls.

## Options Considered

### Option A: Message Bus via Orchestrator (Polling-Based)

**Approach**: Add a message queue to the orchestrator. Agents send messages via `egg-orch message send --to <role> --body "..."` and receive via `egg-orch message poll`. Messages are stored in-memory (or git-backed state) and routed through the orchestrator.

**Pros**:
- Simple to implement — extends existing signal API pattern
- Maintains centralized audit trail (orchestrator logs all messages)
- No changes to container networking
- Compatible with Claude Code's CLI-based tool model (agents poll when ready)
- Orchestrator can enforce communication policies (who can message whom)
- Messages naturally captured in checkpoints

**Cons**:
- Polling introduces latency (agents must periodically check for messages)
- Agents must integrate polling into their workflow (either periodic background checks or explicit poll points)
- High-frequency messaging would stress the orchestrator's single-process architecture
- No guaranteed delivery order without sequence numbers
- Agents may miss time-sensitive messages if polling interval is too long

### Option B: SSE-Based Push Delivery

**Approach**: Extend the existing SSE infrastructure to push messages directly to agent containers. Each agent opens an SSE connection to the orchestrator on startup, and messages are delivered in real-time.

**Pros**:
- Near-real-time delivery (no polling delay)
- Builds on existing SSE infrastructure (`orchestrator/sse.py`)
- Lower orchestrator load than polling (persistent connections vs. repeated requests)
- Natural ordering via SSE event IDs

**Cons**:
- Claude Code agents don't have a background event loop — they'd need a sidecar or background thread to consume SSE events and surface them to the agent
- Requires a new component in the sandbox to bridge SSE events to the agent's CLI interface
- Connection management complexity (reconnection, buffering during disconnection)
- SSE is one-directional (server → client); sending still requires HTTP POST
- Significant sandbox architecture changes

### Option C: Shared Workspace with File-Based Signaling

**Approach**: Instead of a message bus, agents share a workspace directory and communicate via sentinel files. Agents write status files (e.g., `.egg-signals/coder-progress.json`) that other agents can read. A lightweight file watcher notifies agents of changes.

**Pros**:
- No orchestrator changes needed for basic communication
- Files naturally captured in git (audit trail)
- Simple mental model — agents read/write files
- Works with Claude Code's existing file read/write tools

**Cons**:
- No guaranteed delivery or ordering
- Race conditions on concurrent file writes
- Doesn't scale beyond simple status sharing
- Not suitable for conversational back-and-forth
- Requires shared filesystem mount between containers (currently isolated)
- Pollutes the repository with signal files

### Option D: Incremental Enhancement — Extend Wave Model with Feedback Loops

**Approach**: Rather than full concurrent execution, add a feedback mechanism to the existing wave model. After Wave 2 (Tester+Documenter), if issues are found, the system can cycle back to Wave 1 (Coder) with specific feedback. This keeps sequential execution but adds the ability for later agents to influence earlier ones.

**Pros**:
- Minimal architecture changes — extends existing wave model
- Preserves all existing guarantees (isolation, sequential commits, no merge conflicts)
- Already partially implemented via review cycles (`PhaseExecution.review_cycles`)
- No concurrent git access complexity
- No new messaging infrastructure needed
- Lower compute cost than full concurrency

**Cons**:
- Not "real-time collaboration" — still fundamentally sequential
- Feedback loops add latency (full agent restart per cycle)
- Doesn't address the core request for concurrent execution
- Limited to structured feedback, not free-form conversation
- Multiple cycles multiply total compute cost

## Recommended Approach

**Option A (Message Bus via Orchestrator, Polling-Based)** for the communication channel, combined with **incremental concurrent execution** (agents in the same wave can message each other, with future expansion to cross-wave concurrency).

**Rationale**:

1. **Fits the agent model**: Claude Code agents are request-response systems that use CLI tools. Polling via `egg-orch message poll` fits naturally into the agent's workflow without requiring architectural changes to the sandbox.

2. **Builds on existing infrastructure**: The orchestrator already has signal handling, event bus, and per-pipeline state management. Adding a message queue is a natural extension.

3. **Maintains guarantees**: Centralized message routing preserves the audit trail, policy enforcement, and checkpoint capture that are core to egg's security model.

4. **Incremental path**: Start with messaging between agents in the same wave (Tester ↔ Documenter in Wave 2), then expand to cross-wave messaging, then explore true concurrent execution of currently-sequential agents.

5. **Consensus is separable**: The consensus-based completion protocol can be built independently on top of the existing signal API, regardless of which messaging approach is chosen.

**Regarding concurrent execution**: Full concurrent execution (all agents running simultaneously) introduces significant git workspace complexity. The recommended path is:

- **Phase 1**: Add messaging API to orchestrator + agent CLI. Agents in same wave can communicate.
- **Phase 2**: Add consensus-based completion for waves.
- **Phase 3**: Explore relaxing wave boundaries (e.g., start Tester while Coder is still running, using file-level locking or per-agent worktrees).

This incremental approach delivers value at each step while managing risk.

## Open Questions

All decisions and feedback questions below are registered in the contract at `.egg-state/contracts/1027.json` — 6 decisions and 1 feedback item (with 5 open-ended questions) are available for human review during phase approval.

### Decision 1: Communication Model

**Question**: What communication model should inter-agent messaging use?

- [ ] **Asynchronous polling** — Agents poll orchestrator for messages via `egg-orch message poll` (recommended)
- [ ] **Asynchronous push via SSE** — Orchestrator pushes messages to agents via SSE + sandbox sidecar
- [ ] **Request-reply with timeout** — Agent sends message, blocks up to N seconds for response
- [ ] Other (explain in reply)

### Decision 2: Message Format

**Question**: What message format should inter-agent messages use?

- [ ] **Structured JSON** — Typed message schema with `action`/`type` fields, machine-parseable (e.g., `{"type": "test_failure", "file": "foo.py", "line": 42, "message": "..."}`)
- [ ] **Free-form text** — Natural language, interpreted by receiving agent's LLM
- [ ] **Hybrid** — Structured envelope (`from`, `to`, `type`, `timestamp`) with free-form `body` field (recommended)
- [ ] Other (explain in reply)

### Decision 3: Workspace Sharing for Concurrent Agents

**Question**: How should concurrent agents share the git workspace?

- [ ] **Shared worktree** — All agents commit to same branch; merge conflicts resolved at commit time
- [ ] **Per-agent worktrees** — Each agent gets its own branch; integrator merges at end (recommended, leverages existing Tier 3 per-phase worktree infrastructure)
- [ ] **Shared worktree with file-level locking** — Agents claim files via gateway; gateway enforces exclusivity
- [ ] Other (explain in reply)

### Decision 4: Conflict Resolution Between Agents

**Question**: How should disagreements between agents be resolved (e.g., tester says "this approach won't work")?

- [ ] **Automatic HITL escalation** — Human resolves all inter-agent disagreements
- [ ] **Designated lead agent** — Coder has authority in implement phase; other agents can flag but not block
- [ ] **Voting with HITL tiebreaker** — Majority wins; ties escalate to human
- [ ] Other (explain in reply)

### Decision 5: Scope of Initial Implementation

**Question**: Should the initial implementation target full concurrent execution (all agents running simultaneously) or start with messaging within the existing wave model?

- [ ] **Full concurrency** — All agents (coder, tester, documenter) run simultaneously from the start
- [ ] **Incremental** — Add messaging first within existing waves, then expand to cross-wave concurrency (recommended)
- [ ] Other (explain in reply)

### Decision 6: Resource Cost Management

**Question**: Running 3+ concurrent agents per phase significantly increases compute cost. What cost controls should be in place?

- [ ] **No limit** — Let all agents run concurrently; optimize later
- [ ] **Configurable concurrency cap** — `PipelineConfig.max_concurrent_agents` limits simultaneous agents (recommended)
- [ ] **On-demand spawning** — Only spawn additional agents when the lead agent requests collaboration
- [ ] Other (explain in reply)

### Feedback Questions (registered as `feedback-1` in contract)

The following open-ended questions are registered in the contract (`feedback-1`, questions Q1–Q5) for human input:

1. **Message persistence** (Q1): Should inter-agent messages be persisted in the contract/pipeline state (git-backed, survives restarts) or kept in-memory only (lost on orchestrator restart)? What is the expected message volume per phase?

2. **Agent integration pattern** (Q2): Claude Code agents are LLM sessions that use tools. How should incoming messages surface to the agent? Options include: (a) agent periodically calls `egg-orch message poll` as part of its workflow, (b) a wrapper script checks for messages between tool calls and injects them into the conversation, (c) messages appear as tool results in the agent's context. Which integration pattern is preferred?

3. **Backward compatibility** (Q3): Should the concurrent execution model be a new complexity tier (Tier 4) or replace/enhance the existing Tier 2/3 models? The issue describes replacing sequential with concurrent, but existing pipelines rely on sequential guarantees.

4. **Consensus timeout** (Q4): For consensus-based phase completion, what happens if one agent is stuck or crashed? Should there be a timeout after which the remaining agents' consensus is sufficient? What should the timeout be?

5. **Message visibility** (Q5): Should all agents in a phase see all messages (broadcast), or should messaging be point-to-point only? Broadcast is simpler but may create noise for agents that don't need certain messages.

---

*Authored-by: egg*

<!-- METADATA
complexity_tier: high
parallel_phases: true
-->

```yaml
# metadata
complexity_tier: high
parallel_phases: true
```
