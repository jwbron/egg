# Anchor Recovery Guide

Post-compaction state recovery for autonomous agents using the anchor mechanism.

## Overview

When an agent's context window fills, the context is **fully cleared** (not compacted) and the agent reloads working state from its anchor file. This provides a clean, predictable recovery path instead of relying on lossy compaction that may unpredictably preserve or discard information.

**Related**: [Agent Recovery Reference](../reference/agent-recovery.md) | [Concurrent Execution](concurrent-execution.md) | [egg_anchor Library](../../shared/egg_anchor/README.md)

## Compaction Policy

All agents using the anchor mechanism must have context compaction **disabled**. The container spawner sets the compaction-disabled flag (e.g., `--no-compact`) automatically when `AGENT_ANCHOR_ID` is configured.

| Instead of... | Agents do... |
|---------------|-------------|
| Lossy compaction summarization | Full context clear |
| Hoping compaction preserves the right details | Structured reload from anchor file |
| Unpredictable context state | Clean, deterministic recovery path |

## Recovery Protocol

When context is cleared, the agent follows this recovery sequence:

### Step 1: Read Own Anchor

```bash
egg-orch anchor show
```

Returns the agent's last saved state: current task, progress, decisions, BRC state, key context, and error history.

### Step 2: Catch Up on Messages

```bash
egg-orch message poll --since <last_message_id>
```

The anchor's `_meta.last_message_id` field records the last processed message. Polling since that ID retrieves any messages the agent missed during the context gap.

### Step 3: Verify File State

```bash
git log --oneline -5
git status
```

Compare the working tree state against `files_modified` in the anchor to detect any changes made by other agents (in shared worktree scenarios) or unexpected state drift.

### Step 4: Resume from Current Progress Item

Find the `progress` item with `state: "working"` in the anchor. This is where the agent left off. Any items with `state: "complete"` should not be repeated. Items with `state: "pending"` are upcoming work.

### Step 5: Reconcile with Team Anchor

```bash
egg-orch anchor show --team
```

If the team anchor contradicts the agent anchor (e.g., another agent completed a task the agent thought was pending), the **team anchor wins** because it reflects consensus state.

### Step 6: Update Anchor After Recovery

```bash
egg-orch anchor update --status working \
  --key-context "Recovered from context clear, resuming from step N"
```

Record the recovery event so future clears know this has happened before.

## What the Anchor Preserves

| Section | Purpose | Why It Matters Post-Clear |
|---------|---------|---------------------------|
| `task` | Current task (id, description, phase) | Agent knows what it's working on |
| `progress` | Sub-step states (pending/working/complete/blocked) | Prevents repeating completed work |
| `decisions` | HITL decisions and resolutions | Prevents re-opening resolved questions |
| `brc_state` | Consensus phase, ACKs, NACKs | Agent re-enters BRC at correct point |
| `key_context` | Critical labeled context items | Non-derivable information that would otherwise be lost |
| `errors_encountered` | Failed approaches with resolutions | Prevents re-attempting known failures |
| `files_modified` | Active file list | Agent knows which files it was editing |
| `brc_state.last_message_id` | Message bus position | Enables efficient message catch-up |

## When to Update Anchors

Agents should update their anchor at **natural checkpoints** — not continuously:

| Event | Anchor Update |
|-------|---------------|
| Sub-task completed | Mark progress item as `complete`, add next `working` |
| Decision made with another agent | Add to `decisions` array |
| Status change (blocked, waiting, etc.) | Update `status` field |
| BRC state transition | Update `brc_state` |
| Important context discovered | Add to `key_context` |
| Approach failed | Add to `errors_encountered` |
| New file started | Add to `files_modified` |

## BRC Consensus Recovery

The `brc_state` section mirrors `PeerConsensusTracker` state and enables agents to re-enter the BRC protocol at the correct point after context clear:

```json
{
  "brc_state": {
    "phase": "proposed",
    "proposed_at": "2026-03-17T06:00:00Z",
    "acks": ["reviewer_contract"],
    "nacks": [],
    "last_message_id": "msg-42"
  }
}
```

The consensus wrapper (`consensus_wrapper.py`) automatically loads anchor data into its recovery prompt when `AGENT_ANCHOR_ID` is set, so agents resume BRC from the correct state.

## Conflict Resolution

The anchor uses a **single-writer design** — each agent writes only its own anchor. This eliminates write conflicts entirely. The only risk is **temporal staleness** (anchor was written before the most recent events).

The recovery protocol addresses staleness through:
1. **`_meta.sequence`** — monotonic counter detects out-of-order reads
2. **`_meta.updated_at`** — timestamp shows age of anchor data
3. **Message catch-up** — `--since last_message_id` fills in the gap
4. **Team anchor authority** — team anchor (orchestrator-generated) wins on contradictions

## Size Budget

Anchors are size-constrained to keep recovery lightweight:

| Scope | Soft Limit | Hard Limit |
|-------|-----------|------------|
| Per agent | 2 KB | 3 KB |
| Team | 4 KB | 6 KB |

2 KB ≈ 500-600 tokens. Total recovery context (own + team) ≈ 1,500 tokens — under 2% of the post-clear window.

The `egg-orch anchor validate` command checks both schema compliance and size limits.

## Gateway Enforcement

The gateway enforces anchor file access:
- `.egg-state/agent-anchors/*` writes are allowed in **all phases** (refine, plan, implement)
- Agents can **only write their own** anchor file (session-scoped validation via `AGENT_ANCHOR_ID`)
- Cross-agent anchor reads use the orchestrator API (`egg-orch anchor show --agent <id>`)

## Lifecycle

| Pipeline Event | Anchor Behavior |
|----------------|-----------------|
| Agent spawned | `egg-orch anchor init` creates initial anchor |
| Agent running | Agent updates via `egg-orch anchor update` |
| Agent CONFIRMED (BRC) | `status` updated to `confirmed` |
| Agent terminated (clean) | Retained in Redis + local file |
| Agent terminated (crash) | Retained for recovery/debugging |
| Pipeline complete | Archived to checkpoint, Redis keys cleared |
| Pipeline failed | Retained for 7-day TTL |

## Troubleshooting

**Anchor not found after clear**: Check `AGENT_ANCHOR_ID` env var is set. Verify the anchor file exists at `.egg-state/agent-anchors/<agent-id>.json`.

**Stale anchor data**: Run `egg-orch message poll --since <last_message_id>` to catch up on messages since the anchor was last written. Check `_meta.updated_at` to see when the anchor was last updated.

**Size limit exceeded**: Use `egg-orch anchor validate` to check. Prune old `completed` progress items or outdated `key_context` entries. The CLI warns at soft limit and rejects at hard limit.

**Cross-agent anchor read fails**: Ensure the orchestrator API is reachable. Use `egg-orch health` to verify. Cross-agent reads go through the API, not the filesystem.

## Related Documentation

- [egg_anchor Library README](../../shared/egg_anchor/README.md) — Python library reference
- [Orchestrator CLI Reference](../reference/orchestrator-cli.md) — `egg-orch anchor` commands
- [Agent Recovery Reference](../reference/agent-recovery.md) — Retry, circuit breaker, conflict detection
- [Concurrent Execution Guide](concurrent-execution.md) — BRC consensus protocol
