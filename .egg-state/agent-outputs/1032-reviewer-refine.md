# Review: Issue #1032 — Agent Anchor Mechanism for Post-Compaction State Recovery

**Reviewer**: reviewer_refine
**Issue**: #1032
**Status**: Independent assessment (refiner draft not yet available at time of review)

---

## Overall Assessment

The proposal addresses a real gap: long-running agents in the concurrent model (#1027/#1028) will inevitably undergo context compaction, and without a recovery mechanism, agents lose coherence. The design is conceptually sound but has several critical gaps and unstated assumptions that must be resolved.

**Verdict**: The direction is correct, but the spec needs significant refinement before implementation.

---

## Critical Issues

### 1. No Mid-Session Injection Mechanism Exists (BLOCKER)

The proposal states: "After context compaction, the anchor file is injected into the agent's context (similar to how CLAUDE.md is always loaded)."

**This mechanism does not exist in the egg codebase.** After thorough code review:

- `CLAUDE.md` is assembled once at container startup by `sandbox/entrypoint.py:setup_agent_rules()` and symlinked into the working directory.
- There is NO dynamic mid-session injection from the orchestrator or gateway.
- Context compaction is handled internally by Claude Code's SDK, not by egg's orchestration layer.
- The `<system-reminder>` tags are a Claude Code SDK feature, not an egg-controlled injection point.

**What this means**: The anchor mechanism depends on Claude Code's internal behavior of re-reading CLAUDE.md and memory files after compaction. This is an **external dependency on undocumented Claude Code behavior**, not something egg controls.

**Recommendation**: The spec MUST explicitly address the injection mechanism. Options:
- **(A)** Write anchors as Claude Code memory files in `~/.claude/projects/.../memory/` — Claude Code auto-loads these after compaction.
- **(B)** Append anchor content to `CLAUDE.md` dynamically — Claude Code re-reads this.
- **(C)** Use a Claude Code custom skill or system-reminder hook if such an API exists.
- **(D)** Accept that anchors are "pull-based" — agents read their own anchor file on-demand (e.g., via a tool call or prompt instruction telling them to check their anchor).

Option (D) is the most robust because it doesn't depend on undocumented SDK behavior. The agent prompt should include standing instructions like: "After context compaction, read your anchor file at `.egg-state/agent-anchors/<your-id>.yaml` to recover state."

### 2. Phase Permission Gap for Anchor Files

The current `phase-permissions.json` has no rules for `.egg-state/agent-anchors/`. During the implement phase, agents can push code but `.egg-state/drafts/*` is blocked. Anchors need explicit permissions:

- **All phases**: Agents must be able to write their own anchor files.
- **Gateway enforcement**: The proposal mentions "agents can only write their own anchor file" — this requires a new gateway filter rule keyed on agent ID, which doesn't exist today. Current file restrictions are role-based, not agent-ID-based.

**Recommendation**: Add `.egg-state/agent-anchors/*` to `allowed_patterns` for all phases. Design the gateway enforcement as agent-ID-scoped write permissions (a new capability).

### 3. 2KB Size Budget Is Insufficient for Complex Tasks

The proposed 2KB per-agent anchor is too small for realistic scenarios:

- The example YAML in the issue is ~700 bytes with minimal content.
- A coder working on a multi-file refactor with 5+ decisions, 10+ files modified, and key context notes will easily exceed 2KB.
- `files_modified` alone could consume 500+ bytes for 15-20 file paths.
- `decisions` with timestamps and agent references grow ~150 bytes each.
- `key_context` entries need enough detail to be useful — cryptic one-liners save bytes but defeat the purpose.

**Analysis**: With realistic content:
- `progress` (5 items): ~400 bytes
- `decisions` (4 items): ~600 bytes
- `files_modified` (10 items): ~400 bytes
- `key_context` (5 items): ~500 bytes
- Header/metadata: ~200 bytes
- **Total**: ~2100 bytes — already over budget with modest content

**Recommendation**: 4KB per agent anchor, 8KB for team anchor. Alternatively, implement a priority-based truncation strategy: if over budget, truncate `progress.completed` items (keep only last 3), summarize older `decisions`, keep `current` and `key_context` intact.

---

## Design Concerns

### 4. Conflict Resolution Strategy Is Underspecified

The issue asks "If an agent's in-memory state diverges from its anchor" but proposes no resolution. This is the hardest problem and the spec must address it.

**Concrete scenario**: Agent receives a message from the tester (via #1027 message bus) that changes the agreed approach. Agent updates in-memory state but hasn't written the anchor yet. Compaction occurs. Agent reads stale anchor and reverts to the old approach — directly contradicting the team agreement.

**Recommendation**: Adopt a "last-write-wins + message replay" strategy:
- Anchor is always the **floor**, not the ceiling. After compaction, the agent reads its anchor AND replays any messages received since the anchor's `last_updated` timestamp.
- Add a `last_updated` timestamp to the anchor schema.
- The message bus (#1027) must support "messages since timestamp T" queries to enable replay.
- This is analogous to event sourcing: anchor = snapshot, messages = event log.

### 5. Self-Report vs Observed State Creates Trust Issues

The proposal suggests "both" self-report and orchestrator-observed updates. This creates a consistency problem:

- If the agent self-reports "status: completed" but the orchestrator observes the agent is still running, which wins?
- If the message bus updates the anchor with "decision made with tester" but the agent hasn't processed that message yet, the anchor is ahead of the agent's actual state.

**Recommendation**: Make the agent the **sole writer** of its own anchor. External systems (message bus, orchestrator) should NOT write to agent anchors directly. Instead, agents pull external state (messages, orchestrator status) and incorporate it into their own anchor. This maintains a single source of truth per anchor.

The team-level anchor (maintained by mediator/orchestrator) is the correct place for observed state.

### 6. YAML vs JSON Decision

YAML is proposed for human readability. However:
- YAML parsing is notoriously error-prone (implicit type coercion, indentation sensitivity).
- The rest of `.egg-state/` uses JSON (contracts, pipeline state).
- Agents will read/write these programmatically, not humans.

**Recommendation**: Use JSON for consistency with the existing `.egg-state/` ecosystem. Add a `egg-anchor show` CLI command for human-readable display (like `egg-contract show`).

### 7. Anchor Schema Completeness by Role

The example schema is coder-centric. Other roles need different fields:

| Role | Missing Fields |
|------|----------------|
| **Tester** | `tests_written`, `tests_passing`, `coverage_delta`, `test_plan_status` |
| **Documenter** | `sections_updated`, `links_verified`, `docs_reviewed` |
| **Reviewer** | `files_reviewed`, `issues_found`, `ack_nack_status`, `producers_reviewed` |
| **Mediator/Liaison** | `team_members`, `escalations_pending`, `consensus_state`, `handoffs_tracked` |
| **Integrator** | `branches_merged`, `conflicts_resolved`, `pr_status` |

**Recommendation**: Define a base anchor schema (common fields) + role-specific extension schemas. This maps naturally to Pydantic model inheritance (consistent with `shared/egg_contracts/models.py` patterns).

---

## Dependency Analysis

### 8. #1027 (Cross-Agent Communication) — Correctly Identified, Needs Tighter Coupling

The anchor mechanism needs the message bus for conflict resolution (see point 4). Specifically:
- Message bus must support temporal queries ("messages since T") — verify this is in #1027's design.
- The #1027 plan defines message types (`PROGRESS`, `QUESTION`, `STATUS`, `AGENT_FAILED`, `HANDOFF`). Anchor updates should be a new message type or piggyback on `PROGRESS`.

### 9. #1028 (Conversational Coordinator) — Correctly Identified

The coordinator's `CoordinatorState` (Pydantic model on Pipeline) already tracks agent status, decisions, and escalations. The team-level anchor may be **redundant** with `CoordinatorState`. The spec should clarify the boundary:
- `CoordinatorState`: orchestrator-side view (API-accessible, coordinator-managed)
- Team anchor: agent-side view (file-accessible, mediator-managed)

If these overlap significantly, consider having the team anchor be a **projection** of CoordinatorState written to disk, rather than an independent data structure.

### 10. #1030 (Agent Roster) — No Drafts Available, Risk

There are no analysis or plan files for #1030 in `.egg-state/drafts/`. The anchor schema references `agent_id`, `role`, `team`, and `spawned_by` — all of which presumably come from the roster. If #1030's design changes, the anchor schema must adapt.

**Recommendation**: Decouple anchor identity fields from the roster by using a minimal ID scheme (e.g., `agent_id` as opaque string). Don't embed roster-specific structures in the anchor schema.

---

## Missing Requirements

### 11. Anchor Lifecycle in BRC Protocol

The current BRC (Broadcast-Review-Converge) protocol (#1122, already merged) has no integration point for anchors. When an agent proposes consensus, should the anchor state be included as an attestation artifact? When a reviewer ACKs/NACKs, should the anchor be updated?

**Recommendation**: Add anchor hash to BRC proposals as an attestation field. This creates an audit trail linking consensus events to agent state snapshots.

### 12. Concurrent Write Safety

Multiple processes in the same container (e.g., consensus wrapper bash script + Claude Code) could write to the anchor simultaneously. The contract system uses atomic write-to-temp-then-rename (`shared/egg_contracts/loader.py`). Anchors need the same pattern.

### 13. Anchor Versioning

No schema version field is proposed. When the anchor format evolves (and it will), agents reading old-format anchors will break.

**Recommendation**: Add `schema_version: "1.0"` to the anchor schema. Implement forward-compatible reading (ignore unknown fields, provide defaults for missing fields).

### 14. Cleanup Policy

The issue asks about cleanup but proposes nothing concrete. Suggested policy:
- **On agent termination**: Archive anchor to checkpoint (already proposed in success criteria), then delete from `.egg-state/agent-anchors/`.
- **On team completion**: Delete team anchor after archiving.
- **Retention**: Anchors exist only during active execution. Post-execution state lives in checkpoints. This prevents disk/branch pollution.
- **Orphan detection**: If an agent crashes without cleanup, the orchestrator should garbage-collect anchors for terminated agents (keyed off container status).

---

## Summary of Recommendations

| # | Priority | Recommendation |
|---|----------|---------------|
| 1 | **BLOCKER** | Define the injection mechanism — likely pull-based (agent reads own anchor via standing prompt instructions) |
| 2 | **HIGH** | Add `.egg-state/agent-anchors/*` to phase permissions; design agent-ID-scoped gateway enforcement |
| 3 | **HIGH** | Increase budget to 4KB/agent, 8KB/team; add priority-based truncation |
| 4 | **HIGH** | Define conflict resolution: anchor + message replay since `last_updated` |
| 5 | **MEDIUM** | Agent is sole writer of its own anchor; external state pulled, not pushed |
| 6 | **MEDIUM** | Use JSON, not YAML, for consistency |
| 7 | **MEDIUM** | Define role-specific schema extensions |
| 8 | **MEDIUM** | Verify #1027 supports temporal message queries |
| 9 | **LOW** | Clarify team anchor vs CoordinatorState boundary |
| 10 | **LOW** | Decouple from #1030 roster with opaque IDs |
| 11 | **LOW** | Integrate anchor hash into BRC attestations |
| 12 | **MEDIUM** | Use atomic write pattern for concurrent safety |
| 13 | **LOW** | Add schema versioning |
| 14 | **MEDIUM** | Define explicit cleanup/GC policy |

