# Issue #1032: Agent Anchor Mechanism for Post-Compaction State Recovery

## Refined Specification

### Problem Statement

In long-running agent sessions — especially in the async team model (#1027, #1028, #1030) — context compaction is inevitable. When Claude's context window is compressed, agents lose:

1. **Task focus** — what sub-task they're working on, what's completed
2. **Inter-agent decisions** — agreements made via BRC consensus or direct messaging
3. **Coordination state** — who they're waiting on, who's waiting on them
4. **Failure history** — approaches that already failed
5. **Consensus commitments** — positions taken in the BRC protocol

The existing recovery mechanisms are insufficient:
- **egg-contract** is pipeline/phase-scoped, not agent-scoped or granular enough for mid-task recovery
- **Checkpoints** are post-session snapshots, not live state — they can't be read during the same session
- **consensus_wrapper.py** handles clean-exit recovery but not mid-session compaction
- **Redis messages** persist but an agent post-compaction doesn't know *which* messages matter or what decisions they represent

### Proposed Solution: Agent Anchor Files

Persistent, structured state files that agents maintain at `.egg-state/agent-anchors/<agent-id>.yaml`. After context compaction, the anchor is injected into the refreshed context (like `CLAUDE.md`), giving the agent enough state to continue coherently.

---

## Design Decisions

### 1. FORMAT: YAML

**Recommendation: YAML only.**

**Rationale:**
- Anchors are primarily **consumed by LLMs** (injected into agent context post-compaction). YAML is more token-efficient and readable for language models — fewer braces, quotes, and structural noise.
- Anchors are **written by agents** (via shell commands or the `egg-orch` CLI). YAML is easier to produce from shell scripts and heredocs than JSON (no escaping issues with embedded strings).
- The codebase already uses YAML for agent-facing configuration: `repositories.yaml`, `.egg/schemas/yaml-tasks.schema.json` (YAML task definitions validated by JSON Schema).
- Human readability matters for debugging — developers will inspect anchors during incident response.
- JSON Schema can validate YAML files (the existing `yaml-tasks.schema.json` pattern proves this works).
- **Counterargument considered**: JSON is more machine-parseable, but anchors are not consumed by high-throughput machine pipelines. The orchestrator reads them infrequently (on compaction events), and `PyYAML`/`ruamel.yaml` are already available in the sandbox.

**Schema validation**: Define `.egg/schemas/agent-anchor.schema.json` following the existing pattern (`contract.schema.json`, `checkpoint.schema.json`). Validate YAML content against JSON Schema using the same approach as `yaml-tasks.schema.json`.

### 2. SIZE BUDGET: 2KB agent / 4KB team — Validated with adjustments

**Recommendation: 2KB soft limit per agent anchor, 4KB soft limit for team anchor. 3KB hard limit per agent, 6KB hard limit for team. Gateway enforces hard limits.**

**Rationale:**
- Claude's context window after compaction retains ~50-70% of capacity. Injecting a 2KB anchor (~500 tokens) consumes <1% of even the smallest usable window. This is comparable to a `CLAUDE.md` section.
- The existing contract files average ~2.2-2.5KB (`901.json` = 2203 bytes, `897.json` = 2510 bytes). Anchors should be similar in size — they serve a similar structural purpose.
- **Soft limit** (2KB/4KB): The CLI warns when exceeded but doesn't block. Agents should self-regulate by summarizing older entries and pruning completed items.
- **Hard limit** (3KB/6KB): The gateway rejects writes that exceed this. Prevents runaway anchors from consuming excessive context budget. The 50% overhead above soft limit allows for temporary spikes during complex coordination.
- **Team anchor at 2x**: The team anchor tracks N agents' statuses plus cross-cutting decisions. 4KB soft / 6KB hard is sufficient for teams of up to ~8 agents (the current max is 10 per pipeline).

**Auto-pruning strategy**: When an anchor approaches the soft limit, the agent should:
1. Move completed items to a `history` section (excluded from injection)
2. Summarize older decisions into a single line
3. Drop `files_modified` entries for files no longer being actively edited

### 3. UPDATE MECHANISM: Self-report with orchestrator enrichment

**Recommendation: Agents self-report their own anchors. The orchestrator enriches with observed state for the team anchor only.**

**Protocol:**

**Agent anchors (self-report):**
- Agents write their own anchor via `egg-orch anchor update` CLI command
- Updates happen at natural checkpoints: sub-task completion, decision made, status change, BRC state transition
- The CLI validates the YAML against the schema before writing
- Agents are prompted (via system instructions) to update anchors at these moments

**Team anchor (orchestrator-maintained):**
- The orchestrator/mediator maintains the team anchor by aggregating:
  - Agent status from heartbeats and signals
  - BRC consensus state from `PeerConsensusTracker`
  - Cross-agent decisions from the message bus
  - Dependency/blocking relationships
- This avoids requiring agents to maintain a shared file (which would cause write conflicts)

**Why not purely observed?**
- Only the agent knows its *intent* — what it's currently trying to do, what approach it chose, what it learned. Observed state (heartbeats, git commits) captures *actions* but not *reasoning*.
- The consensus_wrapper recovery prompt already relies on self-reported BRC state; anchors extend this pattern.

**Why not purely self-report?**
- Agents can't reliably observe other agents' state. The team anchor needs a coordinator perspective.
- Self-reported status can lag (agent forgets to update). The orchestrator enriches with authoritative timing data.

### 4. CONFLICT RESOLUTION: Anchor-wins with freshness check

**Recommendation: After compaction, the anchor is the authoritative starting point. The agent then validates against live state and reconciles.**

**Protocol:**

1. **Injection**: Post-compaction, the anchor is injected into context with a header:
   ```
   ## ANCHOR RECOVERY — Your context was compacted
   The following is your last saved state. Verify against live state before proceeding.
   ```

2. **Freshness check**: The injected prompt instructs the agent to:
   - Run `egg-orch consensus status` to verify BRC state matches anchor
   - Run `egg-orch message poll --since <last_message_id>` to catch messages received after last anchor update
   - Check `git log --oneline -5` to verify file state matches anchor's `files_modified`

3. **Reconciliation rules**:
   - **Anchor says "waiting on X", but X already responded**: Agent processes the response (messages are in Redis, not lost). Agent updates anchor.
   - **Anchor says "working on task A", but task A was completed post-anchor-write**: Git log shows the commit. Agent advances to next task and updates anchor.
   - **Anchor says "decided approach B", but team anchor shows approach A**: Team anchor wins (it reflects consensus). Agent adjusts.
   - **Anchor is stale (>5 minutes old with no updates)**: Warn the agent that significant state may have changed. Encourage thorough verification.

4. **Corruption/missing anchor**: If the anchor file is missing or unparseable, fall back to:
   - Team anchor (for role/task assignment)
   - `egg-contract show` (for task list)
   - `egg-orch consensus status` (for BRC state)
   - Last checkpoint (if available)

### 5. ANCHOR CLEANUP: Lifecycle tied to agent session, retained for audit

**Recommendation: Create on agent spawn, delete (soft) on agent termination, retain in checkpoints.**

**Lifecycle:**

| Event | Action |
|-------|--------|
| Agent spawned | Create empty anchor with `agent_id`, `role`, `team`, `task` from spawn context |
| Agent working | Agent updates anchor at milestones |
| Agent CONFIRMED (BRC) | Mark anchor status as `confirmed` |
| Agent terminated (clean) | Move anchor to `.egg-state/agent-anchors/archive/<agent-id>.yaml` |
| Agent terminated (crash) | Anchor stays in place; consensus_wrapper or replacement agent uses it for recovery |
| Pipeline complete | Archive all anchors to checkpoint branch |
| Pipeline deleted | Delete anchor directory |

**Retention:**
- Active anchors: live in `.egg-state/agent-anchors/` during pipeline execution
- Archived anchors: moved to `archive/` subdirectory on clean termination
- Checkpoint inclusion: gateway's checkpoint handler includes anchor files in the checkpoint commit (alongside transcripts and artifacts)
- On-disk retention: 7 days in archive, then auto-pruned by a cleanup job (or next pipeline run)

**Team anchor lifecycle:**
- Created by orchestrator when the first agent is spawned
- Updated continuously by orchestrator
- Archived when all agents are terminated and pipeline advances/completes

### 6. GATEWAY ENFORCEMENT: Yes, agent-scoped writes enforced

**Recommendation: The gateway enforces that agents can only write their own anchor file. Team anchor is write-restricted to orchestrator/mediator roles.**

**Implementation:**

Add to `gateway/phase_filter.py` a new `FileRestriction` rule:
```python
FileRestriction(
    role="*",  # All roles
    blocked_patterns=[".egg-state/agent-anchors/*.yaml"],
    blocked_reason="Agents may only write their own anchor file"
)
```

The gateway's git push filter already normalizes paths and checks role-based restrictions. For anchors:
- Extract `agent_id` from the filename
- Verify it matches the pushing agent's session ID (available from the gateway session)
- Allow writes only to `agent-anchors/<own-agent-id>.yaml`
- Team anchor (`team-<team-id>.yaml`) writable only by `mediator`, `overseer`, `coordinator`, and `liaison` roles

This follows the existing pattern where `phase_filter.py` enforces role-based file restrictions (e.g., coders can't write to `.egg-state/contracts/`).

**CLI enforcement (defense in depth):**
- `egg-orch anchor update` verifies `EGG_AGENT_ROLE` and session identity before writing
- Rejects writes to other agents' anchor files at the CLI level (before gateway check)

---

## Schema Definition

```yaml
# .egg/schemas/agent-anchor.schema.json (expressed here in YAML for readability)
$schema: "https://json-schema.org/draft/2020-12/schema"
$id: "https://github.com/jwbron/egg/schemas/agent-anchor.schema.json"
title: "Agent Anchor"
description: "Persistent state anchor for post-compaction recovery"
type: object
required: [schema_version, agent_id, role, team, task, status, updated_at]
properties:
  schema_version:
    type: string
    pattern: "^[0-9]+\\.[0-9]+$"
    default: "1.0"
  agent_id:
    type: string
    description: "Unique agent identifier (e.g., coder-abc123)"
  role:
    type: string
    description: "Agent role (coder, tester, documenter, etc.)"
  team:
    type: string
    description: "Team/pipeline identifier (e.g., issue-432)"
  task:
    type: string
    description: "Current task description (human-readable)"
    maxLength: 200
  spawned_by:
    type: string
    description: "Role that spawned this agent (liaison, coordinator)"
  status:
    type: string
    enum: [initializing, in_progress, waiting, blocked, confirmed, completed, failed]
  updated_at:
    type: string
    format: date-time
  last_message_id:
    type: string
    description: "Last processed message ID for post-compaction catch-up"
  progress:
    type: array
    items:
      type: object
      required: [state, description]
      properties:
        state:
          type: string
          enum: [completed, current, pending]
        description:
          type: string
          maxLength: 120
    maxItems: 10
    description: "Ordered task progress (max 10 items to enforce size budget)"
  decisions:
    type: array
    items:
      type: object
      required: [with_agent, decided, timestamp]
      properties:
        with_agent:
          type: string
        decided:
          type: string
          maxLength: 150
        timestamp:
          type: string
          format: date-time
    maxItems: 8
    description: "Key decisions made with other agents (max 8)"
  waiting_on:
    type: array
    items:
      type: string
    description: "Agent roles this agent is waiting on"
  blocked_by:
    type: array
    items:
      type: string
    description: "Agent roles blocking this agent"
  files_modified:
    type: array
    items:
      type: string
    maxItems: 15
    description: "Files currently being modified (max 15)"
  key_context:
    type: array
    items:
      type: string
      maxLength: 150
    maxItems: 5
    description: "Critical context that must survive compaction (max 5 items)"
  brc_state:
    type: object
    description: "BRC consensus protocol state snapshot"
    properties:
      phase:
        type: string
        enum: [WORKING, PROPOSED, REVIEWING, CONFIRMED]
      proposal_version:
        type: integer
      pending_reviews:
        type: array
        items:
          type: string
      received_acks:
        type: array
        items:
          type: string
      received_nacks:
        type: array
        items:
          type: object
          properties:
            reviewer:
              type: string
            reason:
              type: string
additionalProperties: false
```

**Team anchor schema** extends this with:
- `agents`: map of agent_id → {role, status, last_heartbeat}
- `team_decisions`: array of team-level decisions
- `dependency_graph`: current blocking relationships
- `escalation_history`: array of HITL escalations

---

## Integration Points

### With egg-contract system
- Anchors complement contracts, not replace them. Contracts track pipeline-level tasks and acceptance criteria. Anchors track agent-level working state.
- `egg-contract show` output can seed the initial anchor's `task` and `progress` fields.
- When an agent links a commit via `egg-contract add-commit`, the anchor's `progress` should also be updated (the CLI can do both).

### With checkpoint system
- Anchor files are included in checkpoint commits (extend `gateway/checkpoint_handler.py`)
- On agent restart (not just compaction), the checkpoint loader can surface the previous session's anchor as additional context.
- `egg-checkpoint show` should display anchor state alongside transcript/artifacts.

### With BRC consensus protocol
- The anchor's `brc_state` section mirrors `PeerConsensusTracker` state for the specific agent.
- After compaction, the agent uses the anchor's `brc_state` to know whether it has already proposed, received reviews, etc.
- The `consensus_wrapper.py` recovery prompt should reference the anchor file instead of (or in addition to) querying the orchestrator API. This reduces API calls and provides richer context.
- `last_message_id` enables the agent to poll only new messages post-compaction.

### With cross-agent messaging (#1027)
- When an agent receives a message that represents a decision, it should record it in the anchor's `decisions` array.
- The `last_message_id` field enables efficient catch-up after compaction (poll `--since <id>` instead of re-reading all messages).

---

## CLI Design

```bash
# Update own anchor (validates schema, enforces size limit)
egg-orch anchor update --status in_progress \
  --progress '{"state":"current","description":"Fixing token validation"}' \
  --decision '{"with_agent":"tester","decided":"Use parametrized tests"}' \
  --key-context "Token validation skips expiry for admin scope" \
  --last-message-id msg-abc123

# View own anchor
egg-orch anchor show

# View another agent's anchor (read-only)
egg-orch anchor show --agent coder-abc123

# View team anchor
egg-orch anchor show --team issue-432

# Validate anchor against schema
egg-orch anchor validate
```

The CLI writes to `.egg-state/agent-anchors/<agent-id>.yaml` using the agent ID from `EGG_AGENT_ROLE` + session context.

---

## Edge Cases

### Agent crash before anchor update
- The last-written anchor is the recovery point. It may be stale.
- The freshness check protocol (section 4) handles this: the replacement agent verifies against live state.
- Worst case: anchor is from session start. The agent falls back to contract + checkpoint + message history.

### Concurrent anchor writes
- Not possible for agent anchors (one agent per file, gateway-enforced).
- Team anchor: the orchestrator is single-threaded per pipeline. No concurrent writes.
- If the architecture changes to allow multiple writers, use Redis-based locking (already available in the sandbox).

### Anchor corruption
- YAML parse failure: fall back to contract + live state queries (section 4).
- Schema validation failure: log warning, attempt partial recovery of parseable fields.
- Empty file: treat as missing (section 4 fallback chain).

### Anchor-context mismatch after rapid changes
- Between the last anchor update and compaction, significant state may have changed.
- The `updated_at` timestamp lets the post-compaction injection warn: "Anchor is N minutes stale."
- The freshness check protocol catches up via message poll and git log.

### Multiple compaction events in one session
- Each compaction re-injects the anchor. The agent should update the anchor after each recovery to keep it fresh.
- The injection prompt reminds: "Update your anchor after verifying state."

---

## Implementation Phases

### Phase 1: Core infrastructure
1. Define JSON Schema (`.egg/schemas/agent-anchor.schema.json`)
2. Add `egg-orch anchor` CLI commands (update, show, validate)
3. Add anchor directory creation to pipeline startup
4. Add gateway enforcement for agent-scoped writes
5. Add constants to `shared/egg_config/constants.py` (`ANCHOR_DIR`, size limits)

### Phase 2: Agent integration
1. Add anchor update instructions to agent system prompts (CLAUDE.md rules)
2. Modify `consensus_wrapper.py` to reference anchor in recovery prompt
3. Add anchor injection to post-compaction context loading
4. Extend checkpoint handler to include anchors

### Phase 3: Team anchor & enrichment
1. Implement team anchor maintenance in orchestrator
2. Add orchestrator enrichment (heartbeat → team anchor)
3. Integrate BRC state snapshots into anchors
4. Add `last_message_id` tracking for efficient catch-up

### Phase 4: Cleanup & observability
1. Implement anchor archival on agent termination
2. Add anchor state to `egg-checkpoint show` output
3. Add anchor size monitoring and auto-pruning warnings
4. Add anchor freshness alerts to overseer agent

---

## Success Criteria

1. Agents update anchors at natural milestones (sub-task completion, decisions, status changes)
2. After context compaction, agents resume coherently using anchor + freshness check
3. No duplicate work or contradictory decisions after compaction events
4. Team anchor provides mediator/overseer with consistent team state view
5. Anchor files stay within size budget (>95% of updates under soft limit)
6. Gateway enforces agent-scoped writes (no cross-agent anchor tampering)
7. Anchors are included in checkpoints for audit/debugging
8. Anchor system adds <500ms latency to agent milestone operations
