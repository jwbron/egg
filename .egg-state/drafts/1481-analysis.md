# Analysis: Per-Agent Worktree Isolation and Role-Aware File Enforcement

> Issue: #1481 | Phase: refine

## Problem Statement

The current multi-agent pipeline shares a single git worktree across all agents in a pipeline. This creates cascading failures:

1. **Agents commit disallowed files** that the gateway blocks at push time, burning tokens on recovery loops (#1470)
2. **Downstream agents inherit disallowed files** in their diff from main, causing their pushes to also be blocked
3. **`auto_commit_worktree()` sweeps disallowed files** into WIP commits that bypass role restrictions, causing CI failures (#1480)
4. **Agents waste tokens** writing files outside their role scope, when that work belongs to another agent
5. **Shared worktree** means agents can silently overwrite each other's uncommitted changes

The desired outcome is a layered enforcement model where: (a) agents are structurally isolated via per-agent worktrees, (b) the Agent SDK provides early feedback on disallowed writes to save tokens, (c) `auto_commit_worktree()` filters by role restrictions, and (d) push-time validation remains the hard security boundary.

## Current Behavior

### Shared Worktree (orchestrator/routes/pipelines.py:5770-5772)

All containers in a pipeline share a single worktree keyed by `pipeline_id`:

```python
# We use the pipeline_id as the worktree container_id so all
# containers in the pipeline share the same working trees.
worktree_id = pipeline_id
```

The infrastructure already supports per-container worktrees — `WorktreeManager.create_worktree()` creates unique worktrees per container ID. The current code intentionally forces sharing.

### Push File Detection (gateway/git_client.py:1301-1440)

`get_changed_files_in_push()` diffs the entire `origin/{branch}..HEAD` range. In a shared worktree, this includes commits from ALL agents, not just the current one. This means Agent B's push can be blocked because Agent A committed files that Agent B's role isn't allowed to touch.

The function has two modes:
- **Primary**: `git diff --name-only {remote}/{branch}..HEAD` — returns all changed files in the range
- **Fallback**: Per-commit `git rev-list` + `git diff-tree` — still returns the union of all commits

Neither mode scopes to a specific agent's commits.

### Agent Restrictions (gateway/agent_restrictions.py)

A comprehensive role-based file access system already exists:
- `AgentFilePattern` class with `allowed_patterns` and `blocked_patterns` per role
- `check_agent_file_access()` validates file lists against role patterns
- `validate_agent_push()` is the main entry point for gateway push validation
- Patterns are well-defined for all 15+ roles (coder, tester, documenter, refiner, reviewers, etc.)

This enforcement works correctly — the problem is that `get_changed_files_in_push()` feeds it files from OTHER agents' commits.

### Auto-Commit (gateway/post_agent_commit.py)

`auto_commit_worktree()` runs on container exit and commits uncommitted changes. It filters files by **phase restrictions** but does NOT filter by **agent role restrictions**. This means if a coder agent writes test files (which slip past because there's no write-time enforcement), those files get swept into the WIP commit and can cause CI failures.

### Git Identity (sandbox/entrypoint.py:591-597)

All agents share the identity `egg <egg@localhost>`:

```python
run_cmd(["git", "config", "--global", "user.name", "egg"], as_user=user_tuple)
run_cmd(["git", "config", "--global", "user.email", "egg@localhost"], as_user=user_tuple)
```

There's no way to distinguish which agent authored which commit in `git log`.

### Agent SDK (shared/egg_agent/)

The Agent SDK has no tool interception hooks. `Write`, `Edit`, and `NotebookEdit` operations are not checked against role restrictions before execution. Agents discover they can't push disallowed files only after spending tokens writing them.

## Constraints

- **Gateway is the hard security boundary** — all other enforcement layers are optimizations. Any design must keep push-time validation as the authoritative gate.
- **BRC protocol requires committed+pushed artifacts** — reviewers read from the branch, not the worktree. Per-agent isolation must not break the BRC proposal/review workflow.
- **`EGG_AGENT_ROLE` is already available** in agent containers via `concurrent_executor.py:get_agent_env()`. No new plumbing needed for role awareness.
- **Disk usage** — git worktrees share the object store; only working tree files are duplicated. Marginal overhead.
- **Backward compatibility** — existing single-agent pipelines must continue to work. Per-agent worktrees should be transparent when only one agent runs.
- **Push retry logic** — agents already handle push retries. With per-agent worktrees and mutually exclusive file patterns, `git pull --rebase` cannot produce merge conflicts.
- **Subsumes #1470 and #1480** — this design resolves both issues holistically rather than patching symptoms individually.

## Options Considered

### Option A: Issue's Proposed Design (Full Layered Enforcement)

**Approach**: Implement all six layers from the issue: (1) per-agent worktrees, (2) shared branch with pull-before-push, (3) scoped push file detection, (4) per-agent git author, (5) SDK tool interception, (6) auto-commit role filtering.

**Pros**:
- Defense in depth — each layer catches different failure modes
- Early feedback saves tokens (SDK interception catches writes before they happen)
- Per-agent worktrees eliminate all stomping/cross-contamination risks
- Scoped file detection fixes false positives in push validation
- Auto-commit filtering prevents unreviewed disallowed files from being committed
- Per-agent git author improves auditability at zero cost
- Each piece is self-contained and testable independently

**Cons**:
- Largest scope — touches 6+ components across orchestrator, gateway, sandbox, and shared packages
- Per-agent worktrees change the fundamental execution model for concurrent pipelines
- SDK tool interception adds a new enforcement layer that needs maintenance as patterns change
- Bash file redirects bypass SDK interception (by design, but worth noting)

### Option B: Minimal Fix — Auto-Commit Filtering + Scoped Push Detection Only

**Approach**: Fix only the two proximate causes: (1) add agent-role filtering to `auto_commit_worktree()`, (2) scope `get_changed_files_in_push()` to the current agent's commits (via commit author attribution).

**Pros**:
- Smallest scope — only gateway changes
- Directly fixes #1470 and #1480
- No changes to orchestrator, sandbox, or Agent SDK

**Cons**:
- Agents can still stomp each other's uncommitted work in shared worktrees
- Agents still waste tokens writing disallowed files (no early feedback)
- Commit attribution via author email is fragile — requires matching author to role
- Doesn't prevent the root cause (shared worktree), only patches symptoms
- Future issues will continue to arise from the shared worktree model

### Option C: Per-Agent Worktrees + Auto-Commit Filtering (No SDK Interception)

**Approach**: Implement per-agent worktrees (the structural fix) and auto-commit role filtering (the safety net), but skip SDK tool interception and per-agent git author. Scoped push detection becomes trivial with per-agent worktrees.

**Pros**:
- Addresses the root cause (shared worktrees) and the safety net (auto-commit)
- Smaller scope than Option A — no Agent SDK changes, no entrypoint changes
- Per-agent worktrees make scoped push detection automatic (no code changes to `get_changed_files_in_push()`)
- Still eliminates stomping, cross-contamination, and false positive push blocks

**Cons**:
- Agents still waste tokens on disallowed writes (no early feedback from SDK)
- Git log doesn't show which agent authored commits (less debuggable)
- Misses the "drive better delegation" benefit of SDK interception

## Recommended Approach

**Option A: Full Layered Enforcement** — the issue's proposed design.

Justification:

1. **The root cause is structural** (shared worktrees), so the fix should be structural (per-agent worktrees). Option B patches symptoms.

2. **SDK tool interception is high ROI** — the cost is modest (a pre-execution hook in the Agent SDK), but the benefit is significant: agents learn immediately that they can't write to a file, saving tokens and driving delegation behavior. This is especially valuable for multi-agent pipelines where role boundaries are the primary coordination mechanism.

3. **Per-agent git author is trivial** — a one-line change in `sandbox/entrypoint.py` that dramatically improves debuggability.

4. **Each layer is independently valuable and testable** — the design is not all-or-nothing. Layers can be implemented and shipped incrementally.

5. **The issue has already resolved the design decisions** — shared branch strategy, push retry logic, no commit attribution needed for enforcement (per-agent worktrees handle it structurally), etc. The analysis confirms these decisions are sound.

## Complexity Assessment

**high** — This is an architectural change spanning orchestrator (worktree creation), gateway (push detection, auto-commit filtering), sandbox (git identity), and shared packages (Agent SDK tool interception). The changes touch the concurrent execution model which is a core subsystem. Multiple independent components can be implemented in parallel phases.

## Open Questions

> **Infrastructure note**: `egg-contract add-decision` and `egg-contract add-feedback` commands failed because the gateway session for this container does not have `agent_role` set in its session metadata, causing the contract mutation API to reject all writes with "Cannot determine agent role." The contract file exists at the pipeline worktree path (`/home/egg/.egg-worktrees/issue-1481-v2/egg`) but mutations are blocked. Decisions and feedback are documented below with structured markers for the pipeline to process.

<!-- DECISION: decision-1 -->
### Decision 1: Rollout Strategy for Per-Agent Worktrees

**Question**: Should per-agent worktree isolation be the default for all pipelines, or opt-in?

| Option | Description |
|--------|-------------|
| Default for all pipelines | Breaking change for any workflow depending on shared worktrees |
| Opt-in via pipeline config flag | e.g., `per_agent_worktrees: true` — safest rollout |
| Default on, with opt-out escape hatch | e.g., `per_agent_worktrees: false` — recommended balance |
| Other (explain in reply) | |

**Context**: The issue states no workflows depend on agents seeing each other's uncommitted work. If true, default-on is safe. However, if there are edge cases (e.g., a coder needing to see tester's WIP), opt-in may be prudent for initial rollout.

<!-- DECISION: decision-2 -->
### Decision 2: SDK Tool Interception Scope

**Question**: For the Agent SDK soft enforcement, which tool operations should be intercepted?

| Option | Description |
|--------|-------------|
| Write and Edit only | Bash file redirects not intercepted (issue's proposal) |
| Write, Edit, and NotebookEdit | All explicit file-write tools |
| Write, Edit, NotebookEdit, and best-effort Bash detection | Intercept obvious redirects like `>` and `>>` |
| Other (explain in reply) | |

**Context**: The issue proposes Write and Edit only, with Bash unintercepted. Adding NotebookEdit is trivial. Bash detection adds complexity and fragility for marginal gain since the gateway is the hard enforcement boundary.

<!-- DECISION: decision-3 -->
### Decision 3: Agent-Outputs File Keying

**Question**: Should `.egg-state/agent-outputs/` files be enforced to be keyed by role, or remain convention-only?

| Option | Description |
|--------|-------------|
| Convention only | Agents write `{identifier}-{role}-output.json` by convention |
| Enforce via pattern | Each agent can only write files matching `*-{role}-*` within `agent-outputs/` |
| Other (explain in reply) | |

**Context**: If agents can overwrite each other's output files, per-agent worktrees prevent this at the filesystem level but the files would still diverge across worktrees. Pattern enforcement would also protect the merged branch.

<!-- FEEDBACK: feedback-1 -->
### Feedback Questions

1. **Are there any pipelines or workflows that intentionally depend on agents seeing each other's uncommitted work in a shared worktree?** If so, what is the use case?

2. **Should SDK tool interception be implemented in `egg_agent` (headless Agent SDK) only, or also in the `claude` CLI interactive path?** The issue mentions the Agent SDK, but interactive users may also benefit from early feedback on file restrictions.

3. **What is the expected behavior if an agent's `git pull --rebase` encounters a conflict (e.g., due to a bug in role pattern configuration causing overlapping writes)?** Should the agent retry, abort, or signal an error to the orchestrator?

4. **Should the auto-commit role filtering treat `.egg-state/agent-outputs/` specially (always allow) or defer to the existing `AgentFilePattern` definitions?**

---

*Authored-by: egg*


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

## Resolved Questions

**Decision 1 (Rollout)**: Default on for all pipelines, no opt-out. No workflows depend on shared uncommitted state.

**Decision 2 (SDK scope)**: Write, Edit, and NotebookEdit. Bash not intercepted. Agent SDK (egg_agent) only, not the interactive claude CLI.

**Decision 3 (Agent-outputs keying)**: Convention only. Per-agent worktrees prevent filesystem stomping; no need for pattern enforcement.

**Feedback 1 (Shared worktree consumers)**: No pipelines depend on agents seeing each other's uncommitted work.

**Feedback 2 (SDK interception path)**: Agent SDK (egg_agent) only, not the interactive claude CLI path.

**Feedback 3 (Rebase conflict behavior)**: Signal an error to the orchestrator. The agent can't fix a role pattern config bug itself.

**Feedback 4 (Auto-commit agent-outputs handling)**: N/A — auto_commit_worktree() is being removed entirely. Replaced with: on container exit, if worktree has uncommitted changes, orchestrator creates a HITL decision ('Agent X timed out with uncommitted changes. Recover or discard?'). No unreviewed code auto-pushed to branch. The worktree persists after container exit for manual recovery if needed.

**Design change**: Item 6 changed from 'add role filtering to auto_commit_worktree()' to 'remove auto-commit-push entirely'. Reason: auto-commits bypass BRC consensus, commit disallowed files (#1480), and create WIP commits that break CI. With per-agent worktrees, uncommitted work is preserved in the worktree on disk — no need to auto-push it.
