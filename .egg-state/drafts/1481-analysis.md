# Analysis: Per-agent worktree isolation and role-aware file enforcement

> Issue: #1481 | Phase: refine

## Problem Statement

The current multi-agent enforcement model has a fundamental gap: agents share a single worktree per pipeline and can write any file freely, but the gateway only enforces role boundaries at push time. This creates cascading failures:

1. **Agents commit disallowed files** (e.g., coder commits test files) → push blocked → agent enters recovery loops burning tokens (#1470)
2. **Downstream agents inherit disallowed files** in the diff from main → their pushes are also blocked (#1470)
3. **Agents write disallowed files** → can't push them → `auto_commit_worktree()` sweeps them into unreviewed WIP commits → CI fails (#1480)
4. **Agents waste tokens** writing files they can't commit, when that work belongs to another agent
5. **Shared worktree** means agents can overwrite each other's uncommitted changes

The desired outcome is a layered enforcement model where agents are isolated from each other's working directories, receive early feedback when attempting out-of-scope writes, and the safety nets at commit/push time handle anything that slips through.

## Current Behavior

### Shared Worktree (Orchestrator)

All agents in a pipeline currently share a single worktree. In `orchestrator/routes/pipelines.py`, the worktree ID is set to the pipeline ID, forcing all containers to share:

```python
worktree_id = pipeline_id
```

The infrastructure already supports per-container worktrees — `WorktreeManager.create_worktree()` creates worktrees keyed by container ID. The current code overrides this by using `pipeline_id` as the key.

However, the cleanup code in the same file (around line 7176-7198) already anticipates per-agent session worktrees, sweeping `egg-{pipeline_id}-{role}` worktrees as a safety net. This suggests per-agent worktrees were anticipated in the design.

### Push-time Enforcement (Gateway)

The gateway enforces file restrictions in two layers:

1. **Phase restrictions** (`gateway/phase_filter.py`): `check_phase_file_restrictions()` validates files against phase-specific patterns (e.g., refine phase can only modify `.egg-state/` files).

2. **Agent role restrictions** (`gateway/agent_restrictions.py`): `validate_agent_push()` validates files against role-specific `AgentFilePattern` objects. Each role has explicit `allowed_patterns` and `blocked_patterns`. The patterns are comprehensive and well-defined for all 16 agent roles.

The file detection function `get_changed_files_in_push()` (`gateway/git_client.py`) computes `origin/<branch>..HEAD` diff, which returns ALL files changed across ALL commits being pushed — not just the current agent's commits.

### Auto-commit Safety Net (Gateway)

`auto_commit_worktree()` (`gateway/post_agent_commit.py`) runs on container exit. It currently filters by phase restrictions but **not by agent role restrictions**. This means disallowed files written by an agent get committed as WIP and pushed, causing CI failures.

### Agent SDK (Shared)

The Agent SDK (`shared/egg_agent/`) currently has no file write interception. Agents can freely use Write/Edit tools on any file. The `EGG_AGENT_ROLE` environment variable is already set in agent containers via `concurrent_executor.py:get_agent_env()`, providing the role identity needed for enforcement.

### Git Author Identity

All agents commit as `egg <egg@localhost>` (`sandbox/entrypoint.py`). There is no way to distinguish which agent authored which commit from the git log.

## Constraints

- **Backward compatibility**: Existing single-agent pipelines (non-concurrent) must continue to work. The worktree model change should be transparent to them.
- **Disk usage**: Per-agent worktrees duplicate working tree files (but share the git object store). With up to 5 agents per phase, this multiplies working tree disk usage by ~5x. For most repos this is negligible; for large monorepos it could matter.
- **No merge conflicts guarantee**: The design relies on agents having mutually exclusive file write permissions. If role patterns accidentally overlap, the pull-before-push strategy breaks. This is a config correctness requirement, not an architecture flaw.
- **Gateway as the security boundary**: The Agent SDK interception is a soft optimization (saves tokens), not a security boundary. The gateway remains the hard enforcement point — Bash file writes can bypass SDK interception.
- **BRC protocol compatibility**: BRC proposals reference committed+pushed artifacts (commit SHAs). Reviewers read from the branch, not the worktree. Per-agent worktrees don't affect this — all agents push to the same branch.
- **Auto-commit ordering**: With per-agent worktrees, multiple containers may exit and try to auto-commit/push simultaneously. The pull-before-push retry logic must handle concurrent pushes.

## Options Considered

### Option A: Full Implementation (All 6 Components)

**Approach**: Implement all six layers from the issue design:
1. Per-agent worktree isolation
2. Shared branch with pull-before-push
3. Scoped push file detection
4. Per-agent git author
5. Agent SDK tool interception
6. `auto_commit_worktree()` role filtering

**Pros**:
- Complete solution addressing all observed failure modes
- Defense in depth — each layer catches different failure patterns
- SDK interception saves tokens by providing early feedback
- Per-agent git author improves auditability
- Subsumes #1470, #1480, and #1484 in one coherent design

**Cons**:
- Larger scope increases implementation risk
- SDK tool interception adds complexity to the agent execution path
- More components to test and maintain
- Scoped push file detection becomes simpler with per-agent worktrees (structural isolation handles it), potentially making component 3 unnecessary

### Option B: Core Infrastructure Only (Components 1, 2, 4, 6)

**Approach**: Implement per-agent worktrees, pull-before-push, per-agent git author, and auto-commit role filtering. Defer SDK interception and scoped push file detection.

**Pros**:
- Addresses the root causes (shared worktree stomping, auto-commit sweeping disallowed files)
- Per-agent worktrees structurally eliminate the scoped file detection problem (component 3 becomes unnecessary)
- Smaller scope, lower risk
- SDK interception can be added later as a pure optimization

**Cons**:
- Agents still waste tokens writing files they can't push (no early feedback)
- Slightly less defense in depth at the SDK layer

### Option C: Minimal Fix (Components 3, 6 only)

**Approach**: Fix `get_changed_files_in_push()` to scope to current agent's commits (the #1484 stopgap), and add role filtering to `auto_commit_worktree()`. Keep shared worktrees.

**Pros**:
- Smallest change — fixes the immediate symptoms (#1470, #1480)
- No architectural changes
- Low risk

**Cons**:
- Agents can still stomp each other's uncommitted work
- Commit attribution requires either per-agent git author or commit message metadata
- Doesn't prevent agents from wasting tokens on out-of-scope files
- Doesn't eliminate the need for agents to understand role boundaries themselves
- Band-aid fix that leaves the fundamental shared-worktree problem in place

## Recommended Approach

**Option B: Core Infrastructure Only** is recommended.

The per-agent worktree change (component 1) is the keystone — it structurally eliminates agent stomping, makes scoped file detection trivial (component 3 becomes free), and simplifies auto-commit filtering. Combined with pull-before-push (component 2), per-agent git author (component 4), and auto-commit role filtering (component 6), this addresses all the root causes identified in the issue.

Component 3 (scoped push file detection) becomes unnecessary with per-agent worktrees because each worktree only contains that agent's commits. The `origin/branch..HEAD` diff naturally returns only the agent's own files.

Component 5 (SDK tool interception) is a pure optimization for token savings. It doesn't affect correctness — the gateway push validation and auto-commit filtering catch everything. It should be deferred to a follow-up issue to keep scope manageable.

**Key argument for Option B over A**: The issue design itself notes that SDK interception "is not a security boundary" and "Bash file redirects can bypass this." Since it's purely an optimization, including it in the first implementation adds scope without adding correctness guarantees. The hard enforcement layers provide complete coverage.

**Key argument for Option B over C**: The shared worktree is the root cause of multiple failure modes. Fixing only symptoms (C) leaves the fundamental problem in place and requires ongoing workarounds.

## Complexity Assessment

**High.** This is an architectural change touching multiple components (orchestrator, gateway, sandbox, shared) across different services. The per-agent worktree change modifies container lifecycle management, the merge strategy affects push reliability, and the auto-commit filtering adds a new enforcement layer. Each component is individually moderate, but they are interdependent and require coordinated implementation and testing.

## Open Questions

> **Note**: `egg-contract add-decision` and `egg-contract add-feedback` commands failed with "Contract for issue #1481 not found". The gateway's contract API reports `exists: false` for issue 1481 despite the pipeline running. The contract was likely not initialized by the orchestrator for this pipeline. Verified by calling `curl -s -H "Authorization: Bearer $EGG_SESSION_TOKEN" "http://egg-gateway:9848/api/v1/contract/exists/1481"` which returned `{"data":{"exists":false}}`. All HITL questions below require manual registration once the contract is available, or direct human review via the analysis document.

### Decisions Needed

**Decision 1: Related issue disposition**

Should #1470 (auto-filter on push) and #1480 (write-time prevention) be formally closed as subsumed by this issue, or kept open as independent concerns that could be addressed separately?

- [ ] **Close both as subsumed by #1481** — This design addresses all their concerns
- [ ] **Keep both open as fallback tracks** — Implement independently if #1481 is descoped
- [ ] **Close #1484 only** — Keep #1470 and #1480 as separate work items
- [ ] Other (explain in reply)

**Decision 2: Migration strategy for existing pipelines**

For per-agent worktrees: should existing in-flight pipelines be migrated to the new model, or should only new pipelines use per-agent worktrees?

- [ ] **New pipelines only** — Existing pipelines finish with shared worktrees
- [ ] **Migrate all pipelines on next phase transition** — Switchover at natural boundary
- [ ] **Feature flag** — Opt-in per pipeline via config field
- [ ] Other (explain in reply)

**Decision 3: SDK tool interception scope**

Should the Agent SDK tool interception (soft enforcement) be included in the initial implementation, or deferred to a follow-up issue?

- [ ] **Include in initial implementation** — Saves tokens from day one
- [ ] **Defer** — The hard enforcement layers (worktree isolation + gateway + auto-commit filtering) are sufficient for correctness
- [ ] Other (explain in reply)

### Open-ended Questions

**Q1**: Are there any known workflows or debugging scenarios where agents intentionally need to see each other's uncommitted changes in a shared worktree? The design assumes no such workflows exist. The issue states this explicitly ("No workflows depend on agents seeing each other's uncommitted work"), but confirmation is appreciated.

**Q2**: What is the maximum number of agents expected per pipeline phase? This affects disk usage calculations for per-agent worktrees (each duplicates the working tree files). The current `max_concurrent_agents` default is 6, and the implement phase spawns up to 5 agents (coder, tester, documenter, reviewer_code, reviewer_contract). Are there plans to increase this?

**Q3**: For the per-agent git author change (e.g., `egg (coder)` / `coder@egg.local`), are there any downstream consumers of the git author field that might break with a changed format? Specifically: checkpoint indexing, CI scripts, GitHub PR author display, or commit signing.

**Q4**: The pull-before-push strategy assumes non-overlapping file sets guarantee no merge conflicts during `git pull --rebase`. What is the desired behavior if a rebase conflict does occur (e.g., due to a role pattern misconfiguration)? Options include: fail the push and alert, force-push the agent's version, or escalate to HITL.

**Q5**: Should the auto-commit role filtering use `git checkout -- <file>` to restore disallowed modified files (reverting them), or `git rm` to remove them? The issue specifies both (`checkout` for modified, `rm` for untracked), but confirmation is needed for the case where an agent creates a new file outside its role.

---

*Authored-by: egg*
