# Agent Design Review: Issue #1032 — Agent Anchor Mechanism (Refined Spec)

> Reviewer: reviewer_agent_design | Phase: refine | Pipeline: issue-1032
> Artifact reviewed: `.egg-state/drafts/1032-analysis.md` (commit `1dc8aa2e0`)
> Prior review: `.egg-state/reviews/1032-refine-reviewer_agent_design-review.md` (commit `7d615ac32`) — Conditional NACK

## Overall Assessment

The refined spec substantially improves on the original issue proposal and addresses the majority of concerns raised in the prior agent design review. The design decisions are well-reasoned, the schema is concrete, and the integration points are clearly mapped. The spec is ready for the plan phase with the caveats below.

**Verdict: ACK with conditions** — ready for planning. The conditions below are resolvable during plan/implement and should not block phase advancement.

---

## Review by Criteria

### 1. Agent Lifecycle Integration — IMPROVED, one gap remains

**Prior concern**: Atomic writes, signal coordination, post-agent auto-commit interaction.

**What the refined spec addresses**:
- CLI-based updates (`egg-orch anchor update`) with schema validation before write (1032-analysis.md §3, CLI Design section)
- Lifecycle table mapping spawn/work/confirmed/terminate to anchor actions (§5)
- Orchestrator enrichment model for team anchors eliminates shared-file write conflicts (§3)

**Remaining gap: Atomic writes not explicitly specified.** The spec says the CLI "validates the YAML against the schema before writing" but doesn't specify atomic write semantics (write-to-temp + rename). This matters because agent crashes mid-write can corrupt the file. The CLI Design section should specify that `egg-orch anchor update` performs an atomic file write. This is a one-line implementation detail — not a design blocker.

**Assessment: Satisfactory.** The lifecycle integration is well-defined. The atomic write concern is a plan-phase detail.

### 2. Context Window Impact — WELL-DESIGNED

**Proposal**: 2KB soft / 3KB hard per agent, 4KB soft / 6KB hard for team.

**My analysis**:
- 2KB ≈ 500 tokens. Post-compaction, Claude retains ~100-140K usable tokens. 500 tokens = 0.4%. Acceptable.
- Worst case: mediator reads own anchor (2KB) + team anchor (4KB) = 6KB ≈ 1500 tokens = 1.1%. Still fine.
- Schema enforces limits via `maxItems` (10 progress items, 8 decisions, 5 key_context, 15 files). These are tight enough to stay within 2KB if `maxLength` constraints on strings are also respected.
- Auto-pruning strategy (move completed to history, summarize old decisions) is described but the `history` section is not in the schema (`additionalProperties: false` blocks it). **Minor schema gap** — see §5 below.

**Assessment: Good.** The budget is realistic and well-bounded. The hard limit enforcement via gateway is a strong guardrail.

### 3. Cross-Agent Coordination — SUBSTANTIALLY IMPROVED, one nuance

**Prior concern**: Team anchor as single point of failure; coordinator dependency.

**What the refined spec addresses**:
- Team anchor is orchestrator-maintained, not coordinator-authored (§3). This directly resolves the prior review's concern about coordinator self-recovery bootstrap.
- Team anchor tracks agents' statuses from heartbeats + BRC state from `PeerConsensusTracker` + message bus decisions (§3). This is the right data source.
- The `brc_state` section in the agent schema now captures `phase`, `proposal_version`, `pending_reviews`, `received_acks`, `received_nacks` (Schema Definition section). This is more structured than the original flat `decisions` list.

**Nuance: BRC state section is still incomplete relative to `PeerConsensusTracker`.** Missing:
- `flip_flop_count` — needed to prevent infinite propose/nack cycles
- Cooldown timers — the tracker uses these to prevent rapid re-proposals
- `changed_artifacts` — critical for re-proposals (reviewers need to know what changed)

The current schema captures the 80% case. The missing fields are relevant for edge cases (repeated NACKs, rapid re-proposals). The plan phase should decide whether to add these or whether the `egg-orch consensus status` catch-up query (§4 freshness check) is sufficient to recover them.

**On liaison/mediator patterns (#1028)**: The #1028 analysis describes a coordinator agent (Option A — orchestrator extension) that manages team-level state. The refined spec's decision to have the orchestrator server (not the coordinator agent) maintain the team anchor is architecturally correct — it avoids the circular dependency where the coordinator needs the team anchor to recover, but is also responsible for maintaining it. The team anchor becomes a projection of orchestrator state rather than a separately-authored document. This aligns well with #1028's model where the coordinator queries orchestrator APIs rather than maintaining its own parallel state.

**Assessment: Good.** The team anchor design is sound. BRC state gaps are addressable in planning.

### 4. Self-Update Reliability — THE KEY RISK

This is the aspect I'm least confident about in the design.

**The fundamental tension**: Anchor updates are self-reported by agents, but compaction is triggered by Claude Code without notice. If an agent makes 3 decisions, updates the anchor after the 1st, then compaction happens before the agent updates again, decisions 2 and 3 are lost.

**What the refined spec provides**:
- Agents are "prompted via system instructions" to update at natural checkpoints (§3)
- `updated_at` timestamp + staleness warning (§4)
- Message bus catch-up via `last_message_id` (§4)
- Freshness check protocol post-compaction (§4)

**My concern**: The catch-up mechanism is good for **inter-agent** decisions (they're on the message bus). But **intra-agent** state (e.g., "I tried approach A and it failed, now I'm trying approach B") is only in the agent's context. If the agent doesn't write this to the anchor before compaction, it's lost. The message bus won't have it. The git log won't have it (no commit yet).

**Mitigation options** (for the plan phase to evaluate):
1. **More frequent auto-updates**: The `consensus_wrapper.py` shell script could periodically call `egg-orch anchor update --auto` which snapshots the agent's last few tool calls. But the wrapper runs outside Claude — it can't read Claude's internal state.
2. **System prompt reinforcement**: Include "Update your anchor after every significant decision or failed approach" in the agent's CLAUDE.md rules. This is probabilistic — agents may forget, especially under context pressure.
3. **Accept the risk**: Document that intra-agent reasoning may be lost on compaction. The anchor preserves structural state (what task, what files, what decisions with others) but not reasoning history. This is an acceptable tradeoff if the spec is honest about it.

**Recommendation**: Option 3 is pragmatic. The spec should explicitly state: "Anchors preserve structural state (tasks, decisions, coordination), not reasoning history. After compaction, agents should expect to re-derive their approach from code state + anchor, not reconstruct their reasoning chain." This sets the right expectations and avoids over-engineering.

**Assessment: Acceptable with documented limitation.** The catch-up protocol for inter-agent state is well-designed. The intra-agent reasoning loss should be acknowledged as a known limitation rather than "solved."

### 5. Schema Extensibility — NEEDS ADJUSTMENT

**Current schema**: `additionalProperties: false` with fixed fields for all roles.

**Problem**: Different agent roles need different anchor content:
- **Coder**: `files_modified`, `key_context` (currently in schema) — fits well
- **Tester**: needs `tests_written`, `coverage_delta`, `edge_cases_covered` — not in schema
- **Documenter**: needs `sections_updated`, `links_verified` — not in schema
- **Reviewer**: needs `files_reviewed`, `issues_found` — not in schema

The current one-size-fits-all schema means:
- Testers will stuff test info into generic `key_context` strings (lossy, unstructured)
- Or the schema grows to include every role's fields (bloated — most fields unused per agent)

**Recommendation**: Use a role-specific `extras` object:
```yaml
extras:
  type: object
  description: "Role-specific state (schema varies by role)"
  additionalProperties: true
  maxProperties: 5
```

This preserves the core schema's strictness while allowing roles to carry structured role-specific state. The plan phase should define sub-schemas per role that validate the `extras` content.

**Also**: The `additionalProperties: false` blocks the auto-pruning `history` section described in §2. Either add `history` to the schema or change the auto-pruning strategy to use truncation rather than archival within the same file.

**Assessment: Needs schema adjustment.** The `additionalProperties: false` + no role-specific extension point is too rigid. This is a schema design issue, not a conceptual one — fixable in planning.

---

## Injection Mechanism — Critical Implementation Detail

The prior review identified this as the hardest problem: Claude Code's compaction is opaque to egg. The refined spec (§4) describes a "freshness check" protocol with a recovery header, but doesn't fully resolve HOW the anchor gets read post-compaction.

The refined spec says (§4): "Post-compaction, the anchor file is injected into context with a header." But there's no compaction hook in Claude Code. The injection can't be triggered externally.

The pragmatic approach (identified in the prior review as "Approach B") is:
1. Add to the agent's system prompt (CLAUDE.md rules): "If you notice your conversation history seems shorter than expected, or if you're uncertain about your current task state, read your anchor file at `.egg-state/agent-anchors/{agent-id}.yaml`"
2. Claude Code's `system-reminder` tags (which survive compaction) could include an anchor-read instruction

The refined spec implicitly assumes this but should make it explicit. The plan phase should specify exactly what goes into `sandbox/.claude/rules/anchor.md`.

---

## Summary: Prior Review Concerns — Resolution Status

| Prior Concern | Status | Notes |
|---|---|---|
| 1. Atomic writes / signal coordination | **Mostly resolved** | CLI-based writes with validation. Atomic write semantics should be specified. |
| 2. BRC consensus state capture | **Substantially resolved** | New `brc_state` section. Missing flip_flop/cooldown/changed_artifacts. |
| 3. Post-compaction injection | **Partially resolved** | Freshness check protocol is good. Trigger mechanism still needs explicit specification. |
| 4. Anchor staleness | **Resolved** | `updated_at` + `last_message_id` + message bus catch-up. |
| 5. Team anchor SPOF / coordinator coupling | **Resolved** | Orchestrator-maintained projection. |
| 6. Checkpoint interaction | **Resolved** | Included in checkpoint commits, retained in archive. |
| 7. Gateway enforcement | **Resolved** | Agent-scoped writes via session ID matching. |

---

## Final Verdict

**ACK** — The refined spec is ready for the plan phase.

**Conditions for planning** (not blockers):
1. Schema: Add `extras` object for role-specific state; resolve `history` section for auto-pruning
2. Injection: Explicitly specify what goes into `sandbox/.claude/rules/anchor.md` for post-compaction recovery
3. Self-update limitation: Document that intra-agent reasoning is not preserved — only structural state
4. BRC completeness: Plan phase should decide whether to add `flip_flop_count`/`changed_artifacts` to schema or rely on API catch-up

**Files reviewed**: `.egg-state/drafts/1032-analysis.md` (commit `1dc8aa2e0`), issue #1032 body, `orchestrator/consensus_wrapper.py`, `orchestrator/container_spawner.py`, `orchestrator/peer_consensus.py`, `.egg-state/drafts/1028-analysis.md`
