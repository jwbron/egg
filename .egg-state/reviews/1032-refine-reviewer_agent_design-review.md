# Agent Design Review: Issue #1032 — Agent Anchor Mechanism

> Reviewer: reviewer_agent_design | Phase: refine | Pipeline: issue-1032

## Overall Assessment

The anchor mechanism addresses a real gap — long-running agents in the team model (#1027, #1028) will inevitably face context compaction, and without recovery state, they'll repeat work, forget decisions, and break consensus. The proposal is directionally correct. However, several architectural concerns need resolution before implementation.

**Verdict: Conditional NACK** — the concept is sound but the design has gaps in five areas detailed below. These should be resolved in the refined spec.

---

## 1. Agent Lifecycle Integration — CONCERN: Update Timing & Atomicity

**Current lifecycle**: spawn → session registration → work → signal progress/complete → checkpoint capture → container cleanup

The proposal inserts anchor writes throughout the "work" phase, but doesn't define how anchor updates relate to the existing signal/checkpoint lifecycle:

- **Anchor vs signal**: When an agent calls `egg-orch signal progress --percent 50`, does the orchestrator also update the anchor? Or is the agent responsible for both? If both, they can diverge. If the orchestrator does it, the orchestrator doesn't have the agent's internal reasoning context.
  
- **Atomicity concern**: The anchor is a YAML file on disk. If an agent crashes mid-write (e.g., between writing `progress` and `decisions` sections), the anchor is corrupt. **Recommendation**: Write to a temp file and atomic-rename, or use the orchestrator's state store (which is already git-backed and transactional).

- **Post-agent auto-commit**: When a container exits, the gateway auto-commits unsaved work. This will commit whatever anchor state exists — potentially stale or partial. The anchor cleanup policy needs to account for this. Should auto-commit include anchors, or should they be excluded (like readonly phase files)?

**Recommendation**: Anchors should be written through a library call (`egg-anchor update`) rather than raw file writes. This library handles atomic writes, schema validation, and coordinates with the signal API.

---

## 2. BRC Consensus Protocol Interaction — CONCERN: Consensus State Capture

This is the most critical gap. The BRC protocol (`orchestrator/peer_consensus.py`) tracks:
- Producer state: WORKING → PROPOSED → CONFIRMED
- Reviewer state: WORKING → REVIEWING → CONFIRMED
- Approval matrix: ACK/NACK per (reviewer, producer) edge
- Proposal version numbers for invalidation on re-proposal
- Flip-flop counters and cooldown timers

**Problem**: The proposed anchor schema has a flat `decisions` list, but BRC consensus state is a directed graph with versioned edges. A flat list cannot represent:
- "I proposed v2, reviewer_code ACKed, reviewer_contract NACKed, I'm revising"
- "I'm waiting for re-review after addressing NACK from reviewer_code on files X,Y"
- "My proposal was invalidated because the tester re-proposed and my code depends on their test harness"

**Recommendation**: The anchor schema needs a dedicated `consensus` section that mirrors the `PeerConsensusTracker` state structure:

```yaml
consensus:
  my_state: PROPOSED  # WORKING|PROPOSED|CONFIRMED
  proposal_version: 2
  reviews_received:
    - from: reviewer_code
      verdict: ACK
      proposal_version: 2
    - from: reviewer_contract
      verdict: NACK
      reason: "Missing error handling for expired tokens"
      proposal_version: 2
  pending_reviews_from: [reviewer_contract]
  flip_flop_count: 1
```

Without this, a compacted agent cannot resume consensus correctly — it might re-propose (resetting reviewer state), or miss that it needs to address a NACK.

**Also**: The orchestrator already tracks consensus state in its git-backed state store. The anchor's consensus section should be populated FROM the orchestrator state (source of truth), not self-reported by the agent. This avoids the agent's in-memory view diverging from the orchestrator's view.

---

## 3. Post-Compaction Injection — CONCERN: Injection Ordering & Context Budget

The proposal says anchors are "injected into the agent's context similar to how CLAUDE.md is always loaded." But the current injection pipeline is:

1. `sandbox/entrypoint.py` assembles CLAUDE.md from `sandbox/.claude/rules/*.md`
2. Claude Code loads CLAUDE.md into system prompt on startup
3. Claude Code handles compaction internally (not controlled by egg)

**Problem 1**: Egg doesn't control Claude Code's compaction. When Claude Code compresses context, it keeps the system prompt (CLAUDE.md) intact but summarizes conversation history. Egg has no hook to inject the anchor at compaction time. The anchor would need to be part of the system prompt to survive compaction — but the system prompt is assembled once at startup.

**Problem 2**: If the anchor IS part of the system prompt, it's static — it won't reflect updates made during the session. If it's NOT part of the system prompt, it gets compacted away with the conversation.

**Possible approaches**:
- **A) Periodic self-read**: The agent periodically reads its own anchor file (like a cron reminder). This requires Claude Code's context to still have the instruction to read the anchor. Fragile.
- **B) System prompt placeholder**: Include a `<!-- ANCHOR: read .egg-state/agent-anchors/{id}.yaml on every tool call -->` instruction in CLAUDE.md. The instruction survives compaction; the agent re-reads the file. More robust but adds overhead.
- **C) Claude Code hook**: If Claude Code exposes a post-compaction hook (it currently doesn't), use it to inject the anchor. This is the cleanest but requires Claude Code changes.

**Recommendation**: Approach B is the most pragmatic. Add an instruction to the agent's system prompt: "After any context compaction event (when you notice your conversation history is shorter than expected), read your anchor file at `.egg-state/agent-anchors/{agent-id}.yaml` to recover state." This is an imperfect heuristic but workable.

**Context budget**: 2KB per agent anchor is ~500 tokens. 4KB team anchor is ~1000 tokens. With a 200K context window post-compaction, this is <1% — acceptable. But if the team has 6 agents, the mediator's team anchor + 6 agent anchors = 16KB (~4000 tokens, still ~2%). This scales linearly. **Set a hard cap**: total anchor budget per agent read ≤ 8KB (anchor + team anchor).

---

## 4. Anchor Staleness — CONCERN: Stale Anchors Are Worse Than No Anchors

**Scenario**: Agent A compacts at T=10. Anchor was last updated at T=8. Between T=8 and T=10, agent A received a message from agent B changing the agreed approach. Post-compaction, agent A reads the T=8 anchor and proceeds with the old approach, contradicting the T=10 decision.

This is the "stale anchor" problem, and it's the most dangerous failure mode — the agent confidently acts on outdated state.

**Mitigation strategies**:
1. **Timestamp + staleness warning**: Include `last_updated` in the anchor. If it's older than N minutes, warn the agent to re-query the message bus before proceeding.
2. **Message bus catch-up**: Post-compaction, the agent should read its anchor AND poll the message bus for messages since `last_updated`. The combination provides recovery.
3. **Orchestrator-side invalidation**: When the orchestrator detects a compaction event (e.g., agent's tool calls become shorter/simpler), it pushes a "refresh" message containing the delta since the anchor's timestamp.

**Recommendation**: (1) + (2) are essential. The anchor should include a `last_updated` timestamp and the injection instruction should say: "After reading the anchor, poll the message bus for messages since `{last_updated}` to catch up on any decisions made after this anchor was written."

---

## 5. Team-Level Anchor — CONCERN: Single Point of Failure & Coordinator Coupling

The proposal has the mediator/orchestrator maintaining `.egg-state/agent-anchors/team-{team-id}.yaml`. This creates two concerns:

**Concern A: Coordinator dependency.** The team anchor is most useful for the coordinator (#1028), but if the coordinator itself compacts or crashes, the team anchor IS its recovery mechanism. This creates a bootstrap problem — who updates the team anchor when the coordinator is recovering? 

**Recommendation**: The team anchor should be updated by the orchestrator (the server process, not the coordinator agent). The orchestrator already has the full state in its git-backed store. The team anchor should be a projection of orchestrator state, not coordinator-authored content.

**Concern B: Redundancy with orchestrator state.** The orchestrator's `state_store.py` already maintains per-pipeline state including agent statuses, decisions, and consensus. The team anchor duplicates this. 

**Recommendation**: The team anchor should be a lightweight summary GENERATED from orchestrator state on demand, not a separately maintained document. This eliminates divergence risk. The orchestrator could expose an endpoint: `GET /api/v1/pipelines/{id}/team-anchor` that returns a compaction-friendly summary.

---

## 6. Checkpoint Interaction — Complementary, Not Duplicative (POSITIVE)

Checkpoints (`egg-checkpoint`) capture full session transcripts at session end. Anchors capture live working state during a session. These are complementary:

| | Checkpoints | Anchors |
|---|---|---|
| **When** | Session end / push | Continuously during session |
| **Purpose** | Audit / cross-session context | Intra-session recovery |
| **Size** | Large (full transcript) | Small (2-4KB summary) |
| **Consumer** | Next-session agents | Same-session agent post-compaction |
| **Persistence** | Git branch (permanent) | Ephemeral (session lifetime) |

**One concern**: Anchors should be INCLUDED in checkpoints for debugging. If an agent misbehaves post-compaction, the checkpoint should contain the anchor state it recovered from. The `checkpoint_handler.py` should be extended to capture the anchor file alongside the transcript.

---

## 7. Gateway Enforcement — Feasible But Needs Scope Definition

The existing `agent_restrictions.py` can enforce agent-scoped anchor writes. The pattern is straightforward:

```python
# In agent file patterns:
allowed_patterns = [f".egg-state/agent-anchors/{agent_id}.yaml"]
blocked_patterns = [".egg-state/agent-anchors/*.yaml"]  # block all, then allow own
```

**However**: The current restriction system uses role-based patterns, not agent-ID-based patterns. Adding per-agent-ID enforcement requires extending the pattern system. The session manager already has `agent_role` but would need a new `agent_id` field.

**Recommendation**: This is feasible and should be done. The session already carries `container_id` which can serve as the agent identity. Pattern becomes: `.egg-state/agent-anchors/{container_id}.yaml`.

---

## Summary of Required Changes to Proposal

| Area | Issue | Required Change |
|------|-------|----------------|
| Lifecycle | Atomic writes, signal coordination | Define `egg-anchor` library; specify relationship to signal API |
| BRC consensus | Flat schema can't represent consensus graph | Add structured `consensus` section mirroring PeerConsensusTracker |
| Injection | No compaction hook in Claude Code | Use system prompt instruction + re-read pattern (Approach B) |
| Staleness | Stale anchors cause confident wrong behavior | Require timestamp + message bus catch-up post-compaction |
| Team anchor | Single point of failure, redundancy | Generate from orchestrator state, not coordinator-authored |
| Checkpoints | Integration | Include anchor in checkpoint capture |
| Gateway | Per-agent-ID enforcement is new | Extend session model with agent_id, add per-ID pattern support |

---

## Questions for the Refiner

1. Has the refiner considered that Claude Code's compaction is opaque to egg? There's no hook for injection. How does the refined spec handle this?
2. Does the refined spec define a schema version for anchors? When the schema evolves, what happens to in-flight sessions with old-format anchors?
3. The issue mentions "the cross-agent message bus when summarizing conversations" can update anchors. This implies the message bus writes to disk — is that a new capability or does it route through the orchestrator?
4. What happens when two agents from the same team simultaneously update the team anchor? (Race condition on the file.)

