# Analysis: Agent Anchor Mechanism for Post-Compaction State Recovery

> Issue: #1032 | Phase: refine

## Problem Statement

Long-running agents in the async team model (#1027, #1028, #1030) inevitably hit context window limits. When this happens, the agent loses working memory: current sub-task, cross-agent decisions, BRC consensus state, coordination status, and prior failed approaches. Without a structured recovery mechanism, compacted agents produce duplicated work, contradictory decisions, and broken consensus.

The issue proposes replacing lossy context compaction with a **clear + reload** strategy: disable compaction entirely, and when the context window fills, fully clear it and reload from a persistent "anchor" file that captures the agent's essential working state. This provides a deterministic, predictable recovery path.

**Current state**: No anchor mechanism exists. Recovery is limited to the consensus wrapper's restart-based recovery (handles clean exits, not mid-session compaction) and pipeline-scoped contracts (too coarse for agent-level working state).

**Desired outcome**: Agents maintain coherent working state across context clears, with sub-2% context window overhead and <500ms latency on anchor operations.

## Current Behavior

### Existing Recovery Mechanisms

1. **Consensus wrapper** (`orchestrator/consensus_wrapper.py`): Handles agent clean exits during BRC by injecting a recovery system prompt with BRC state and restarting the agent (max 2 restarts). This only covers post-exit recovery, not mid-session compaction.

2. **egg-contract** (`shared/egg_contracts/`): Pipeline/phase-scoped task tracking. Tracks tasks, decisions, and commits at the pipeline level. Not granular enough for agent-level working state (e.g., "I'm on sub-task 3 of 7" or "coder and I agreed to use approach B").

3. **Checkpoints** (`shared/egg_contracts/checkpoints.py`): Post-session snapshots captured at commit or session-end triggers. Cannot be read during the same session — they're audit artifacts, not live state.

4. **Redis message bus** (`orchestrator/redis_message_store.py`): Persists messages via Redis Streams with `pipeline:{pipeline_id}:messages` key pattern. Messages are durable but an agent post-compaction doesn't know which messages are relevant or what decisions they encode.

### Relevant Codebase Components

| Component | Location | Relevance |
|-----------|----------|-----------|
| Orchestrator CLI | `orchestrator/cli.py` (580 lines) | New `anchor` subcommand group needed |
| Orchestrator API | `orchestrator/routes/` (Flask Blueprints) | New anchor CRUD endpoints |
| Redis store | `orchestrator/redis_message_store.py` | Pattern for Redis key namespacing |
| Peer consensus | `orchestrator/peer_consensus.py` (950 lines) | `brc_state` mirrors `PeerConsensusTracker` |
| Consensus wrapper | `orchestrator/consensus_wrapper.py` (324 lines) | Must load anchor in recovery prompt |
| Container spawner | `orchestrator/container_spawner.py` (765 lines) | Must set `AGENT_ANCHOR_ID` env var + compaction flag |
| Phase filter | `gateway/phase_filter.py` | Must allow `.egg-state/agent-anchors/*` writes |
| Constants | `shared/egg_config/constants.py` | Anchor size limits, Redis key prefixes |
| JSON schemas | `.egg/schemas/` | New `agent-anchor.schema.json` |
| Agent rules | `sandbox/.claude/rules/` | New `anchor-recovery.md` rule |
| Phase permissions | `.egg/phase-permissions.json` | Must allow anchor file writes in all phases |
| `.egg-state/` directory | `.egg-state/` | New `agent-anchors/` subdirectory |

## Constraints

### Technical Constraints

- **Context window overhead**: Own anchor (~2KB / ~500 tokens) + team anchor (~4KB / ~1000 tokens) must stay under 2% of post-clear window. The 2KB soft / 3KB hard limit per agent is well-justified.
- **Latency budget**: <500ms for anchor read/write operations. Atomic file writes (temp-then-rename) and Redis SET/GET are well within this.
- **Concurrent file access**: In BRC concurrent phases, multiple agents share a worktree. Agent-scoped filenames (`<agent-id>.json`) prevent write conflicts, but concurrent reads of each other's anchors must handle partial writes (atomic rename solves this).
- **Gateway enforcement**: The gateway's session-scoped validation must map `agent_id` from the anchor filename to the current session. This requires the gateway to know the agent ID for each session — the container spawner already sets `EGG_AGENT_ROLE`, but the full `{role}-{container_id}` format is new.
- **Redis persistence**: Anchors in Redis should survive orchestrator restarts. Redis Streams are already used for messages; simple key-value (SET/GET with JSON) is more appropriate for anchors.
- **Schema validation**: JSON Schema with `maxItems`/`maxLength` constraints structurally enforces the size budget. This is consistent with existing schemas in `.egg/schemas/`.

### Compatibility Constraints

- **Compaction disabled**: All agent containers must launch with compaction disabled. This is a behavioral change to the container spawner and affects all agents using the anchor system.
- **Soft dependency on #1027**: `last_message_id` for message catch-up requires the cross-agent communication system. Anchors work standalone without it, but post-compaction message replay is degraded.
- **Backward compatibility**: Existing pipelines without anchors should continue to work. Anchor init should be opt-in or gracefully degraded.

### Scope Constraints

- The issue is comprehensive and prescriptive — most design decisions are already resolved in the issue itself. The analysis should validate these decisions against codebase patterns rather than re-derive them.
- Four implementation phases are defined in the issue. The plan phase will break these into concrete tasks.

## Options Considered

### Option A: Implement as Specified in Issue

**Approach**: Follow the issue's design closely — JSON anchors at `.egg-state/agent-anchors/<agent-id>.json`, synced to Redis via orchestrator API, with clear+reload recovery strategy, `egg-orch anchor` CLI subcommands, and `shared/egg_anchor/` Python library.

**Pros**:
- Design is thorough and well-reasoned, with all major decisions already resolved
- Consistent with existing codebase patterns (JSON schemas, Flask Blueprints, Redis storage, CLI subcommands)
- Clear integration points identified for all existing components
- Size budget is well-justified with concrete token calculations
- Single-writer design eliminates conflict resolution complexity

**Cons**:
- Large scope — touches orchestrator, gateway, sandbox, shared libs, schemas, and agent rules
- Compaction-disabled requirement is a significant behavioral change
- Team anchor generation adds complexity to the orchestrator API

### Option B: Minimal Anchor (File-Only, No Orchestrator API)

**Approach**: Implement only the local file-based anchor with CLI commands. Skip the orchestrator API sync, team anchor generation, and Redis persistence. Agents read/write anchor files locally. Cross-agent reads happen via filesystem in shared worktrees.

**Pros**:
- Significantly reduced scope — no API routes, no Redis storage, no team anchor
- Faster to implement and test
- Core recovery functionality works for the primary use case (single-agent compaction recovery)

**Cons**:
- No cross-agent anchor reads outside BRC concurrent phases (agents on separate worktrees)
- No team anchor for mediator/overseer visibility
- No durable backup — anchor only exists in worktree files
- Would need to be extended later, potentially requiring rework

### Option C: Extend Existing Consensus Wrapper Only

**Approach**: Instead of a new anchor system, enhance the consensus wrapper's recovery prompt to include more state — pull from contract, recent commits, and message history to reconstruct context on restart.

**Pros**:
- Minimal new infrastructure
- Works within existing recovery mechanism

**Cons**:
- Only handles clean-exit restarts, not mid-session context clears
- Cannot capture agent-specific working memory (sub-task progress, failed approaches)
- Doesn't address the core problem of compaction losing coordination state
- Fundamentally different mechanism — restart recovery vs. in-session recovery

## Recommended Approach

**Option A: Implement as specified in the issue**, with phased delivery as outlined.

The issue's design is well-aligned with existing codebase patterns:
- CLI subcommands follow the `orchestrator/cli.py` argparse pattern
- API endpoints follow the Flask Blueprint pattern in `orchestrator/routes/`
- Redis storage follows the key-prefix pattern from `redis_message_store.py`
- JSON Schema follows the existing `.egg/schemas/` convention
- Shared library follows the `shared/egg_*` pattern
- Gateway integration follows `phase_filter.py` patterns

The phased approach (core infrastructure -> API -> gateway integration -> checkpoint lifecycle) provides natural checkpoints and allows early value delivery. Option B would deliver faster but creates technical debt; Option C doesn't solve the actual problem.

**Key implementation considerations**:
1. The `egg-orch anchor` CLI should be added as a subcommand group in `orchestrator/cli.py`, following the same argparse pattern as existing commands (`pipelines`, `gateway`).
2. The anchor API routes should be a new Blueprint in `orchestrator/routes/anchors.py`.
3. Redis storage should use `anchor:{pipeline_id}:{agent_id}` keys with JSON values (simpler than Streams since anchors are single-document state, not append-only logs).
4. The `shared/egg_anchor/` library should provide Pydantic models (consistent with `shared/egg_contracts/models.py`), atomic file I/O, and schema validation.
5. Gateway session validation for agent-scoped writes requires the container spawner to pass the full `{role}-{container_id}` agent ID, which should be set as `AGENT_ANCHOR_ID` env var.

## Open Questions

The issue resolves most design decisions explicitly, but several questions remain that need human input. Each is registered below via `egg-contract`.

All questions below were registered via `egg-orch decision create` (the `egg-contract add-decision` gateway route was unavailable for this pipeline).

### Decision 1: Mandatory vs Opt-In Adoption

**Question**: Should the anchor system be mandatory for all agents or opt-in? The issue implies mandatory (all containers launch with compaction disabled), but existing non-concurrent single-agent pipelines may not need anchors.

**Options**: (a) Mandatory for all agents, (b) Mandatory only for concurrent/BRC agents, (c) Opt-in via pipeline config

**Registered as**: `decision-1`

### Decision 2: Redis Namespace

**Question**: Should anchors use `anchor:{pipeline_id}:{agent_id}` prefix in the same Redis database, or a separate Redis database?

**Options**: (a) Same database with `anchor:` prefix (recommended by issue), (b) Separate Redis database

**Registered as**: `decision-2`

### Decision 3: Auto-Pruning Behavior

**Question**: When an anchor approaches the 2KB soft limit, should the CLI auto-prune old completed progress items, or just warn and let the agent decide?

**Options**: (a) Warn only, agent decides (recommended by issue), (b) Auto-prune oldest completed items

**Registered as**: `decision-3`

### Decision 4: Non-Pipeline Mode Support

**Question**: Should anchors work outside pipeline mode (ad-hoc interactive sessions with `pipeline_id=null`)? This adds complexity but enables recovery for all agent sessions.

**Options**: (a) Yes, support non-pipeline mode, (b) No, pipeline-only for initial implementation

**Registered as**: `decision-4`

### Decision 5: Agent ID Format

**Question**: The issue specifies `{role}-{short_container_id}` (e.g., `coder-abc12345`). Should the `short_container_id` be the Docker container ID prefix, or a randomly generated ID? Docker IDs are available at spawn time but may change on restart.

**Options**: (a) Docker container ID prefix (12 chars), (b) Random UUID prefix (8 chars), (c) Role + pipeline-scoped counter (e.g., `coder-1`)

**Registered as**: `decision-5`

### Decision 6: Compaction Disable Mechanism

**Question**: The issue requires all agent containers to launch with compaction disabled. What is the specific mechanism for disabling compaction in the agent runtime (Claude Code)? Is `--no-compact` a real flag, or does it need to be implemented/configured differently?

**Options**: (a) Use existing `--no-compact` flag, (b) Configure via CLAUDE.md instruction, (c) Implement custom compaction hook

**Registered as**: `decision-6`

### Decision 7: Issue Splitting

**Question**: This feature touches ~8 components across 4 implementation phases. Should this be split into separate issues per phase, or kept as one large issue?

**Options**: (a) Keep as single issue with phased implementation, (b) Split into 4 issues (one per implementation phase)

**Registered as**: `decision-7`

## Complexity Assessment

**High**

This is an architectural change introducing a new subsystem (`shared/egg_anchor/`) with cross-cutting integration across orchestrator CLI, orchestrator API (new Flask Blueprint), Redis storage, gateway phase filter, container spawner, JSON schemas, agent sandbox rules, consensus wrapper, and checkpoint system. The 4 implementation phases can be parallelized to some degree (Phase 1 core infra is a prerequisite, but Phases 2-4 have some independence).

---

*Authored-by: egg*
