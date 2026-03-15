# Plan Phase Review: Agent Anchor Mechanism (#1032)

> Reviewer: reviewer_plan | Date: 2026-03-15

## Documents Reviewed

1. `architecture-plan.md` (architect)
2. `task-plan.md` (task_planner)
3. `risk-analysis.md` (risk_analyst)
4. `1032-analysis.md` (refine phase output, for context)

## Overall Assessment

**Verdict: CONDITIONAL ACK — sound architecture with specific issues to resolve before implementation.**

The architecture is well-reasoned and the risk analysis is thorough. However, there are **file path inaccuracies**, **schema divergences between architect and task planner**, and **gaps in the gateway enforcement design** that need resolution. The size budget question deserves more scrutiny.

---

## 1. Architecture Plan Review

### 1.1 Strengths

- **AD-1 (CLAUDE.md passive + respawn injection)** is the right call for v1. Avoiding auto-memory coupling is wise — Claude Code's memory internals are undocumented and could change.
- **AD-2 (message-mediated coordination updates)** correctly avoids cross-worktree filesystem access. This aligns with the existing message bus architecture.
- **AD-3 (event-driven with 5-min floor)** is pragmatic. The five trigger points are well-chosen.
- **AD-4 (agent-created, not roster-created anchors)** is correct — the agent knows its own initial state best.
- **Schema design** is clean with clear separation between agent-writable and orchestrator-writable fields.

### 1.2 Issues

#### ISSUE-A1: Incorrect file paths in implementation plan (MUST FIX)

The architecture plan references files that don't exist:
- `gateway/push_validator.py` — **Does not exist**. Push validation happens in `gateway/gateway.py` (the `handle_push()` endpoint at ~line 720) with filtering logic in `gateway/phase_filter.py`.
- `orchestrator/executor.py` — **Does not exist**. The actual executor classes are:
  - `orchestrator/multi_agent.py` → `MultiAgentExecutor`
  - `orchestrator/concurrent_executor.py` → `ConcurrentPhaseExecutor`
  - `orchestrator/coordinator_executor.py` → `CoordinatorExecutor`

**Impact**: Implementers will waste time looking for nonexistent files. The file changes summary table (lines 324-340) needs correction.

#### ISSUE-A2: Size budget discrepancy between refine and architecture (CLARIFY)

The refine analysis recommends **1.5KB agent / 3KB team**. The architecture plan adopts this. But the risk analysis and task plan reference **2KB agent / 4KB team** (the original issue proposal). These need to be reconciled.

**My recommendation**: Adopt the architect's 1.5KB/3KB with an escape valve. Define the limits as configurable constants (not hardcoded) so they can be tuned without code changes. The key question is: **can the schema actually fit in 1.5KB?**

#### ISSUE-A3: Size budget feasibility analysis is missing (SHOULD FIX)

Neither document provides a concrete byte count for a realistic anchor. Let me estimate:

```yaml
# Minimal populated agent anchor (all required fields)
agent_id: coder-a1b2c3d4           # ~25 bytes
role: coder                         # ~12 bytes
pipeline_id: issue-1032             # ~25 bytes
team_id: issue-1032                 # ~22 bytes
spawned_at: "2026-03-15T10:00:00Z"  # ~35 bytes
spawned_by: coordinator-e5f6g7h8    # ~35 bytes
status: working                     # ~18 bytes
last_updated: "2026-03-15T10:30:00Z" # ~40 bytes
task_summary: "Implement anchor..."  # ~60 bytes (120 char max = ~130 bytes with key)
progress:                           # ~5 bytes
  completed: [5 items × 40 chars]   # ~250 bytes
  current: "..."                    # ~210 bytes
  pending: [3 items × 40 chars]     # ~150 bytes
files_modified: [10 paths × 40 chars] # ~450 bytes
key_context: [5 × 200 chars]        # ~1100 bytes
failed_approaches: [3 × 150 chars]  # ~500 bytes
waiting_on: []                      # ~15 bytes
blocked_by: []                      # ~15 bytes
decisions: [5 entries]              # ~600 bytes
consensus: {...}                    # ~200 bytes
```

**Total estimate for a fully populated anchor: ~3,700 bytes.** This significantly exceeds the 1.5KB budget. Even with structured pruning, a coder with 5 completed tasks, 10 modified files, 5 key context items, and 5 decisions will blow the budget.

**Recommendation**: Either:
1. Increase the agent anchor budget to **2.5KB** and team to **4KB**, OR
2. Reduce field limits: `files_modified` max 5 (not 10), `key_context` max 3 (not 5), `decisions` max 3 (not 5), OR
3. Accept that most anchors will be pruned and design the pruning strategy explicitly (which entries survive?)

#### ISSUE-A4: `$EGG_AGENT_ID` fallback to `$EGG_AGENT_ROLE` is underspecified (SHOULD FIX)

The plan says to fall back to `$EGG_AGENT_ROLE` if `$EGG_AGENT_ID` (from #1030) isn't available. But this creates a **non-unique filename** when multiple agents of the same role run. The architect acknowledges this is "acceptable for single-agent-per-role pipelines" but doesn't address what happens when we eventually have multiple coders. This should have a clearer migration path:

- v1: Use `{role}-{pipeline_id}` as the agent ID if `$EGG_AGENT_ID` is not set. This is unique within a pipeline even with multiple roles.
- Wait, actually multiple agents of the SAME role is the problem. Use `{role}-{container_id_first8chars}` as fallback.

#### ISSUE-A5: Dynamic pattern mechanism for gateway is underspecified (SHOULD FIX)

Task 2-1 introduces "dynamic patterns" parameterized by `$EGG_AGENT_ID`. The current `AgentFilePattern` dataclass uses static glob patterns. Adding `{agent_id}` template substitution to patterns is a reasonable approach, but the plan doesn't address:
- Where does the gateway get the agent ID from? (Answer: from the session, which has `agent_role` but currently no `agent_id` field)
- Does `session_manager.py` need a new `agent_id` field? (Yes)
- How do sessions get the agent ID? (Container spawn sets it as an env var, but it needs to be passed to session creation)

This is a prerequisite chain that should be explicit in the task plan.

---

## 2. Task Plan Review

### 2.1 Strengths

- Dependency graph is clear and acyclic — no circular dependencies.
- Complexity ratings seem accurate. TASK-1-3 (AnchorManager) and TASK-2-1 (lifecycle hooks) are correctly rated HIGH.
- Implementation order is reasonable — foundation first, then integration, then tests.
- CLI tool (`egg-anchor`) is a nice addition not in the architecture plan.

### 2.2 Issues

#### ISSUE-T1: Schema divergence from architecture plan (MUST FIX)

The task plan defines a **different schema** from the architecture plan:

| Field | Architecture Plan | Task Plan |
|-------|------------------|-----------|
| Current task | `task_summary` (string) + `progress.current` | `current_task` (object with id, description, status, progress_percent) |
| Completed items | `progress.completed` (list[string]) | `completed_tasks` (list[string] of task IDs) |
| Key context | `key_context` (list[string]) | `key_context` (list of {category, content} objects) |
| Files | `files_modified` (list[string]) | `files_in_progress` (list[string]) |
| Decisions | `decisions` (list of {with, decided, timestamp}) | `decisions_made` (list of {question, resolution, timestamp}) |
| Last commit | Not present | `last_commit` (string) |
| Schema version | Not present | `schema_version: "1.0"` |

**Impact**: These are fundamentally different schemas. Implementers will not know which to follow.

**Recommendation**: Adopt a unified schema. The architecture plan's schema is more closely aligned with the refine phase output and has better field names for the coordination context. But the task plan correctly adds `schema_version` and `last_commit` which should be incorporated. The task plan's `current_task` object with `progress_percent` is more useful than the architecture's flat `task_summary` string — but also larger.

#### ISSUE-T2: Package location disagreement (MUST FIX)

- Architecture plan creates a new package: `shared/egg_anchors/`
- Task plan places models in: `shared/egg_contracts/anchor_models.py`

**Recommendation**: Use `shared/egg_anchors/` as a new package (architecture plan's approach). The anchor system is a distinct concern from contracts — coupling them in `egg_contracts` would make the already-large contracts package even bigger (27 files currently). A clean new package with `__init__.py`, `models.py`, `io.py`, `manager.py` is better separation of concerns.

#### ISSUE-T3: Task plan references correct orchestrator files (GOOD) but task plan is vague on hook integration (SHOULD FIX)

TASK-2-1 says to "hook into existing `OrchestratorClient` signal methods" but doesn't specify which file. The `OrchestratorClient` is in `shared/egg_orchestrator/client.py`. This hook-based approach is reasonable, but the task should specify:
- Is this a decorator/wrapper pattern, or direct modification of `signal_complete()`/`signal_progress()`?
- Who instantiates the AnchorManager — the client, the agent startup script, or CLAUDE.md instructions to the agent?

#### ISSUE-T4: Missing task — session_manager.py update for agent_id (MUST FIX)

Neither plan includes a task for adding `agent_id` to the gateway session model (`gateway/session_manager.py`). This is a prerequisite for gateway enforcement (TASK-4-1). Without it, the gateway can't know which agent ID is making the push, and can't enforce agent-scoped anchor writes.

Required changes:
1. Add `agent_id` parameter to session creation in `session_manager.py`
2. Update container spawn to pass `agent_id` when creating the session
3. Update `check_agent_restrictions()` in `gateway/gateway.py` to use `session.agent_id`

#### ISSUE-T5: Complexity count is wrong (MINOR)

The summary table says 8 medium-complexity tasks but lists 9 in the medium row (TASK-6-4 and TASK-6-5 are listed but there are 9 items total). This is cosmetic.

---

## 3. Risk Analysis Review

### 3.1 Strengths

- Comprehensive — 10 risks covering correctness, security, performance, compatibility, and operations.
- Severity/likelihood ratings are well-calibrated. R-1 (size growth) as HIGH/HIGH is correct.
- R-10 (context budget pressure) raises an excellent point about **tiered injection** that the architecture plan should incorporate.
- CC-4 (multi-pipeline isolation) is an important catch — the architecture plan uses `.egg-state/agent-anchors/<agent-id>.yaml` without pipeline namespacing. If two pipelines run on the same repo, anchors will collide.

### 3.2 Issues

#### ISSUE-R1: R-8 dependency analysis is partially outdated (CLARIFY)

R-8 says all three dependencies (#1027, #1028, #1030) are "in analysis/plan phase — none are merged." But the coordinator state output shows #1027 and #1028 as merged/live (the architect's dependency table confirms this). Only #1030 (agent roster) is unstarted. This changes the risk profile significantly — R-8 severity should drop from HIGH to MEDIUM.

#### ISSUE-R2: R-3 recommends against team anchors, contradicting architecture (CONFLICT)

R-3 mitigation says "No shared/team anchor files. Cross-agent state lives in the message bus, not anchor files." But the architecture plan explicitly includes team anchors as a core component, and the refine analysis identified them as important for coordinator recovery. This is a fundamental disagreement.

**My assessment**: Team anchors are valuable for coordinator recovery post-compaction. The race condition risk is mitigated by the architecture plan's decision that only the coordinator writes the team anchor (AD-5). Single-writer eliminates the race. The risk analysis should revise R-3 to acknowledge single-writer ownership rather than recommending removal of team anchors.

#### ISSUE-R3: R-4 hash verification is overkill for v1 (MINOR)

The risk analysis recommends including "a hash of the anchor content in the orchestrator's state store" for integrity verification. This adds complexity with minimal benefit given that:
1. Gateway enforcement already prevents cross-agent writes
2. The threat model (prompt injection via repo content) is already mitigated by gateway scoping
3. Anchors are advisory, not authoritative (per the refine analysis)

Skip hash verification for v1. Revisit if there's evidence of anchor tampering.

#### ISSUE-R4: Missing risk — anchor injection in non-compaction scenarios (ADD)

Neither the risk analysis nor architecture plan addresses what happens when an agent reads its anchor file **outside of compaction** — for example, if CLAUDE.md instructions tell the agent to read its anchor proactively. If the agent reads its anchor while actively working, it might confuse its in-memory state with the on-disk anchor state. The anchor should include clear framing: "This is your recovery state. Only use this if you've lost context."

---

## 4. Cross-Document Consistency Issues

### 4.1 Pipeline path namespacing (MUST RESOLVE)

- Architecture plan: `.egg-state/agent-anchors/<agent-id>.yaml`
- Risk analysis (CC-4): `.egg-state/agent-anchors/{pipeline-id}/{agent-id}.yaml`

The risk analysis is correct that pipeline namespacing is needed. Without it, two pipelines on the same repo would have anchor collisions. Adopt the nested path.

### 4.2 Team anchor filename

- Architecture plan: `.egg-state/agent-anchors/team-<pipeline-id>.yaml`
- Task plan: `.egg-state/agent-anchors/_team.yaml`

These are different. With pipeline namespacing, it should be `.egg-state/agent-anchors/{pipeline-id}/_team.yaml` or `.egg-state/agent-anchors/{pipeline-id}/team.yaml`.

### 4.3 Size budgets

- Refine analysis: 1.5KB agent, 3KB team
- Architecture plan: 1.5KB agent (1,536 bytes), 3KB team (3,072 bytes)
- Task plan: references "2KB budget" from original issue
- Risk analysis: references "2KB budget"

All documents should agree. See ISSUE-A3 for the feasibility analysis showing 1.5KB is likely too tight.

---

## 5. Gateway Enforcement Feasibility

Based on reviewing the actual gateway code (`gateway/gateway.py`, `gateway/agent_restrictions.py`, `gateway/phase_filter.py`):

### 5.1 Agent-scoped writes: FEASIBLE with modifications

Current `agent_restrictions.py` uses static glob patterns per role (e.g., `CODER_PATTERNS`). Adding dynamic patterns parameterized by agent ID requires:
1. A new `agent_id` field on sessions (currently only `agent_role` exists)
2. Pattern interpolation in `check_agent_restrictions()`
3. A way to get agent_id from the session in the push handler

This is doable but is a **non-trivial change** to the gateway. It should be its own task with explicit prereqs.

### 5.2 Size cap enforcement: FEASIBLE

The push handler in `gateway/gateway.py` already has access to changed files via `get_changed_files_in_push()`. Adding a size check for `.egg-state/agent-anchors/*.yaml` files is straightforward — read the file, check `len(content)`, reject if over limit.

Note: No per-file size limits currently exist in the gateway. This would be the first such check.

### 5.3 Phase permissions: FEASIBLE and simple

Adding `.egg-state/agent-anchors/` to the allowed paths for all phases in `phase_filter.py` is a one-line change per phase in the `PHASE_PATTERNS` dict.

---

## 6. Post-Compaction Injection Mechanism

The architecture plan's approach (CLAUDE.md passive instruction + respawn injection) is sound. The key concern is: **will agents actually follow the CLAUDE.md instructions to read their anchor?**

Based on current agent behavior, agents follow CLAUDE.md instructions reliably when they're specific and actionable. The recovery instructions proposed in the refine analysis are good. One improvement: the CLAUDE.md instruction should tell agents to read their anchor **at the start of every response**, not just when they "suspect context loss." Agents can't reliably detect compaction — they just notice they're confused. A proactive read (check anchor timestamp vs current time; if stale, reconcile) is more robust than a reactive one.

However, this increases token consumption. The tiered injection from R-10 is the right solution here — a compact 50-token summary is cheap enough to read every turn.

---

## 7. Test Strategy Assessment

### Architecture plan tests (Phase 7): Adequate coverage

- Unit tests for models/IO, gateway enforcement, integration — standard layering.
- Missing: **compaction simulation test**. The risk analysis (CC-1) correctly calls this out. How do you test that anchor recovery works after compaction? You need a test harness that:
  1. Creates an agent with an anchor
  2. Simulates compaction (clears conversation context)
  3. Triggers anchor read + reconciliation
  4. Verifies agent resumes correctly

### Task plan tests (Phase 6): Good granularity

5 test tasks covering models, manager, lifecycle, gateway, and multi-agent. The task plan's tests are more granular than the architecture plan's.

### Missing test scenarios

Neither plan includes tests for:
- **Concurrent writes from lifecycle hooks**: If `signal_complete()` triggers an anchor update, and the agent is also manually updating its anchor, what happens?
- **Anchor file locked by another process**: What if the gateway is reading the anchor for size validation while the agent is writing?
- **Very large pipelines**: What happens with 8+ agents all maintaining anchors? Is the `.egg-state/agent-anchors/` directory performance acceptable?

---

## 8. Summary of Required Changes

### MUST FIX (block implementation)

1. **Unify the schema** — Architect and task planner have different schemas. Pick one and document it.
2. **Correct file paths** — Architecture plan references nonexistent `push_validator.py` and `executor.py`. Update to actual file names.
3. **Add session_manager task** — Neither plan includes gateway session changes needed for agent_id.
4. **Resolve pipeline path namespacing** — Adopt `.egg-state/agent-anchors/{pipeline-id}/` from risk analysis CC-4.
5. **Resolve package location** — `shared/egg_anchors/` (new) vs `shared/egg_contracts/` (existing). Recommend new package.

### SHOULD FIX (improve quality)

6. **Size budget feasibility** — Provide a concrete byte estimate showing the schema fits. Likely need 2-2.5KB, not 1.5KB.
7. **Resolve R-3 conflict** — Risk analysis says no team anchors; architecture says yes. Team anchors with single-writer are fine.
8. **Specify agent_id fallback** — Use `{role}-{container_id_short}` not `{role}` alone.
9. **Add gateway prereq chain** — Document that session_manager needs agent_id before enforcement can work.
10. **Tiered injection** — Incorporate R-10's tiered injection into architecture plan for v1.

### NICE TO HAVE

11. Update R-8 dependency status (1027/1028 are merged).
12. Drop hash verification from R-4 for v1.
13. Add compaction simulation test harness to test plan.
14. Specify the pruning strategy explicitly (which fields get truncated first when over budget).

---

## Recommendation

**Proceed to implementation after resolving MUST FIX items 1-5.** The architecture is fundamentally sound — the issues are primarily about consistency between the three documents and accuracy of codebase references. The risk analysis is thorough and the mitigations are realistic. With the fixes above, this is ready for a coder to implement.
