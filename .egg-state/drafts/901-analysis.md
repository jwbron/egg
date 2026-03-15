# Analysis: Refine Phase Agent Runaway Grep + Push Branch Mismatch Caused 26-Min Hang

> Issue: #901 | Phase: refine

## Problem Statement

During a pipeline run for issue #805, the refine agent hung for 26+ minutes due to two cascading failures: (1) push rejections caused by accumulated branch history containing files the refine phase isn't allowed to modify, leading the agent to improvise a push to an unexpected branch name, and (2) a runaway `grep -rn "Contract for issue" /` command that searched the entire container filesystem at 100% CPU for 22+ minutes until the container was killed.

The desired outcome is to prevent both failure modes: push denials from branch history contamination should be resolved cleanly without agent improvisation, and unbounded filesystem searches should be blocked or time-limited.

## Current Behavior

### Branch History Contamination and Push Validation

All phases in a pipeline share a **single branch** (`egg/{pipeline_id}/work`) and a single worktree. Each phase's commits are pushed to this branch, and subsequent phases inherit the full history via `_sync_worktree_with_remote()` (`orchestrator/routes/pipelines.py:1835-1934`).

When the gateway validates a push, it computes the diff using `get_changed_files_in_push()` (`gateway/git_client.py:1108-1210`). The diff strategy is:

1. **Primary**: `git diff --name-only origin/{branch}..HEAD` (compare remote branch tip to local HEAD)
2. **Fallback 1**: `git diff --name-only origin/main..HEAD` (if remote branch doesn't exist)
3. **Fallback 2**: `git diff --name-only origin/master..HEAD`

This means that when a refine agent pushes, the diff includes **all files changed since the remote branch tip**, including files committed by prior pipeline phases (plan drafts, implement results, etc.). The refine phase's allowed patterns (`.egg-state/contracts/*`, `.egg-state/drafts/*analysis*`, `.egg-state/checkpoints/*`, `.egg-state/agent-outputs/*`, `.egg-state/reviews/*`) reject those historical files, causing the push to fail.

The push denial error includes a hint (`gateway/gateway.py:808-814`):

```
"Branch contains files outside .egg-state/ from a previous phase.
 Create a clean branch from origin/main with only your state files."
```

However, this hint is misleading in the pipeline context — the agent is not supposed to create arbitrary branches, and the gateway's branch-lock in pipeline mode (`git checkout/switch` blocked) prevents the agent from acting on it.

### No Shell Command Guardrails

There are **no guardrails** preventing agents from running unbounded filesystem searches. The sandbox provides:

- A 2-hour agent session timeout (`sandbox/llm/claude/config.py:19-25`)
- Network filtering via proxy
- Gateway enforcement on git operations
- Read-only filesystem mounts for certain `.egg-state/` directories

But there is **no command filtering, no per-command timeout, and no restriction** on searching outside `~/repos/`. The agent's Bash tool in Claude Code executes commands directly with no wrapper or validation hook. The only system-level timeout is the 2-hour session timeout, which is far too long to catch a runaway grep.

### Completion Signal Without Branch Validation

When the agent signaled completion via `egg-orch signal complete`, the orchestrator accepted it without verifying that new commits existed on the expected branch (`orchestrator/routes/signals.py:134-213`). The orchestrator stores `Pipeline.branch` as metadata (`orchestrator/models.py:270`) but never validates that the agent actually pushed to that branch. This means the pipeline continued with a stale branch while the actual draft was on `egg/issue-805-refine`.

### Post-Agent Auto-Commit

The gateway's `auto_commit_worktree()` (`gateway/post_agent_commit.py:126-333`) handles uncommitted work when containers exit, but it only addresses uncommitted local changes. It doesn't help when the agent already committed and pushed to the wrong branch name.

## Constraints

- **Branch sharing across phases**: The single-branch-per-pipeline design is intentional — it allows each phase to see prior phases' work. Changing this requires careful architectural consideration.
- **Gateway diff strategy**: The two-dot diff (`origin/{branch}..HEAD`) is correct for detecting what the push would change on the remote. The problem is that prior phases' commits are on the same branch, so they show up in the diff when the remote is behind.
- **Pipeline mode branch lock**: In pipeline mode, the gateway blocks `git checkout/switch` to new branches. This prevents the agent from working around push failures by creating new branches — but it also means the agent has no escape valve when the expected push path fails.
- **Claude Code Bash tool**: Individual command timeouts are configurable per-call via the tool's `timeout` parameter, but this is up to the model to set. There is no system-level per-command timeout enforced by the sandbox.
- **Backward compatibility**: Any changes to the push validation or branch management must not break existing working pipelines.

## Options Considered

### Root Cause 1: Push Failures from Branch History

#### Option A: Scope-Limited Push Diff (Gateway-Side)

**Approach**: Modify `get_changed_files_in_push()` in `gateway/git_client.py` to compute the diff differently in pipeline mode. Instead of diffing the full branch against `origin/{branch}` or `origin/main`, diff only the **new commits being pushed** (i.e., commits not yet on the remote branch). This would use something like `git diff origin/{branch}...HEAD` (three-dot, merge-base) or filter files to only those touched by commits since the last known remote HEAD.

**Pros**:
- Directly addresses the root cause: only the current phase's changes are evaluated
- No changes needed to the agent or orchestrator
- Preserves the single-branch-per-pipeline model

**Cons**:
- Three-dot diff may miss files if the remote has been updated independently
- Complexity in determining "new commits" vs. "all commits" — could introduce subtle security gaps if an attacker crafts commits to bypass phase restrictions
- The existing two-dot diff is the correct security-conservative approach; weakening it reduces defense-in-depth

#### Option B: Pre-Phase Branch Reset (Orchestrator-Side)

**Approach**: Before spawning a new phase's agent, have the orchestrator (via `_sync_worktree_with_remote()`) reset the worktree so that the local branch tip matches `origin/{branch}`. This way, when the agent pushes, the diff between `origin/{branch}..HEAD` only contains the current phase's work.

**Pros**:
- No changes to the gateway's security-critical diff logic
- Simple: ensure `origin/{branch}` is up-to-date before the agent starts
- The existing `_sync_worktree_with_remote()` already has this logic but only resets when local is "behind" remote — it would just need to ensure the fetch + reset always happens

**Cons**:
- The sync logic already handles the case where local is behind remote (line 1922), but skips when local is ahead. If a previous agent pushed but the orchestrator didn't fetch, there's a window for stale state.
- Requires the orchestrator to always push state changes before spawning the next agent and then fetch/reset the worktree

#### Option C: Phase-Aware File Filtering in Push Validation

**Approach**: Modify the gateway's phase restriction check to exclude files that were already on the remote branch before the current session started. The gateway session already has a `created_at` timestamp; combine this with `git log --since` to identify which files were changed in the current session's commits only.

**Pros**:
- Surgically targets the problem without changing the diff strategy
- Gateway sessions already track metadata that could support this

**Cons**:
- Time-based filtering is fragile (clock skew, commit timestamps can be manipulated)
- Adds complexity to the security-critical push validation path
- The gateway session's `created_at` may not align perfectly with the first commit

#### Option D: Per-Phase Branch Creation

**Approach**: Instead of reusing a single `egg/{pipeline_id}/work` branch across all phases, create a fresh branch from `origin/main` (or `origin/{branch}`) for each phase. Branches could be named `egg/{pipeline_id}/refine`, `egg/{pipeline_id}/plan`, etc.

**Pros**:
- Cleanly eliminates branch history contamination
- Each phase starts with a clean slate
- Matches the mental model of isolated phases

**Cons**:
- Requires changes to the orchestrator's pipeline model and worktree management
- Later phases need access to prior phases' `.egg-state/` files — either via cherry-pick, merge, or reading from the remote directly
- Increases branch proliferation on the remote
- Most significant architectural change of all options

### Root Cause 2: Runaway Shell Commands

#### Option E: Agent Prompt Guardrails (Soft)

**Approach**: Add explicit instructions to the agent system prompt (in `sandbox/.claude/rules/` or `CLAUDE.md`) warning against searching from `/`, running unbounded `find`/`grep` operations, and recommending scoping all searches to `~/repos/`.

**Pros**:
- Zero code changes — only prompt/documentation updates
- Addresses the most common case (agent searching outside workspace)
- Quick to implement

**Cons**:
- Soft guardrail: the model can still ignore instructions
- Doesn't prevent all forms of runaway commands
- Not enforceable at the system level

#### Option F: Shell Command Timeout Wrapper (Hard)

**Approach**: Implement a Bash wrapper or pre-execution hook that enforces a per-command timeout (e.g., 120 seconds). Could be done via a shell function, a wrapper script, or Claude Code's `BashTool` configuration (if supported). Alternatively, use `ulimit` or `timeout` command wrapping in the container entrypoint.

**Pros**:
- Hard enforcement: commands that run too long are killed
- Protects against all forms of runaway commands, not just `grep`
- Container resource usage stays bounded

**Cons**:
- Legitimate long-running operations (large test suites, builds) may be killed
- Claude Code's Bash tool already supports per-call timeouts, but only if the model uses them
- Wrapping all commands may require changes to how Claude Code's Bash tool interacts with the shell

#### Option G: Path-Scoped Command Filtering (Hard)

**Approach**: Implement a shell wrapper that blocks or rewrites commands that attempt to search outside `~/repos/`. For example, intercept `grep -r ... /` and either reject it or rewrite the path to `~/repos/`.

**Pros**:
- Directly prevents the specific failure mode
- Could catch other forms of filesystem exploration outside the workspace

**Cons**:
- Fragile: command patterns are diverse and hard to filter reliably
- May break legitimate use cases (e.g., reading `/etc/` config files)
- Complex to implement correctly without false positives

### Root Cause 3: Completion Signal Without Validation

#### Option H: Branch Commit Verification on Completion

**Approach**: When the orchestrator receives `egg-orch signal complete`, verify that the expected branch (`pipeline.branch`) has new commits since the phase started. If no new commits are found, either reject the completion signal or emit a warning.

**Pros**:
- Catches the specific failure where the agent pushes to the wrong branch
- Provides early detection of pipeline state corruption
- Could be a warning initially, upgraded to a hard check later

**Cons**:
- Some phases may legitimately complete without new commits (e.g., analysis that determines "no changes needed")
- Requires the orchestrator to track the branch tip at phase start for comparison
- Adds latency to completion signal processing (needs git fetch)

#### Option I: Gateway Branch-Name Enforcement on Push

**Approach**: When a pipeline agent pushes to a refspec that doesn't match `pipeline.branch`, the gateway rejects the push. The gateway session already knows the expected branch; it just needs to enforce that the push target matches.

**Pros**:
- Prevents the agent from improvising branch names entirely
- Hard enforcement at the push layer (already the security boundary)
- Simple check: compare push refspec to session branch

**Cons**:
- May need to handle edge cases (e.g., push to `origin/branch:branch` vs. `origin branch`)
- The gateway already enforces branch ownership via `egg/` prefix — this adds a stricter check for pipeline sessions

## Recommended Approach

The issue involves three distinct root causes, each requiring a targeted fix. The recommended combination:

1. **For push failures (Root Cause 1): Option B (Pre-Phase Branch Reset)** — This is the simplest and safest approach. By ensuring `_sync_worktree_with_remote()` always fetches and resets the worktree to match `origin/{branch}` before spawning a new phase's agent, the diff computed at push time will only contain the current phase's new work. This avoids modifying the gateway's security-critical diff logic. The existing sync function already has most of the needed logic; it just needs to reliably reset local to match remote before each phase starts.

2. **For runaway commands (Root Cause 2): Option E (Prompt Guardrails) + Option F (Shell Command Timeout)** — The prompt guardrails are a quick win that addresses the most likely scenario (agent searching from `/`). The shell command timeout is a defense-in-depth measure that protects against all forms of runaway commands. Both should be implemented: prompt changes are immediate, while the timeout wrapper may require more investigation into Claude Code's Bash tool configuration.

3. **For wrong-branch push (Root Cause 3): Option I (Gateway Branch-Name Enforcement)** — In pipeline mode, the gateway should enforce that the push refspec matches the session's expected branch. This is a natural extension of the gateway's existing push validation and prevents the agent from silently pushing to an unexpected branch. Combined with Option H (branch commit verification) as a softer warning on the orchestrator side.

## Open Questions

1. **Pre-phase branch reset behavior**: When `_sync_worktree_with_remote()` encounters a local branch that is *ahead* of remote (has unpushed local commits), it currently skips the reset. Should it force-reset anyway (losing local-only commits), or should it push those commits first? This affects whether orphaned local commits from crashed agents are preserved.

2. **Per-command timeout value**: What is a reasonable per-command timeout for agent shell operations? The current default is 120 seconds (2 minutes) in Claude Code's Bash tool. Should the system enforce a lower timeout (e.g., 60 seconds) or keep the default and only add guardrails for specific patterns (e.g., commands that reference `/` as a search path)?

3. **Gateway push refspec enforcement strictness**: When the gateway enforces that the push target matches the session's branch, should this be a hard block (HTTP 403) or a warning that still allows the push? A hard block prevents the wrong-branch scenario but may frustrate agents that hit edge cases.

4. **Commit verification on completion**: Should the orchestrator require new commits on the expected branch for a completion signal to be accepted? Or should it be a non-blocking warning? Some phases might legitimately produce no new commits.

5. **Phase-specific diff scoping**: If Option B (pre-phase reset) is insufficient in some edge cases (e.g., orchestrator crashes between fetching and resetting), should we also implement a fallback like Option C (session-aware file filtering)? Or is the pre-phase reset reliable enough as a single mechanism?

6. **Existing pipeline runs**: Are there in-flight pipelines that depend on the current behavior of branches accumulating history across phases? Would changing the sync behavior require a migration or flag to handle already-running pipelines?

7. **Prompt guardrail scope**: The agent instructions in `CLAUDE.md` and `sandbox/.claude/rules/` already mention the `~/repos/` workspace, but don't explicitly warn against searching outside it. Should the warning be added to the global `CLAUDE.md` (affects all agents) or only to the sandbox-specific rules?

8. **Post-agent auto-commit interaction**: The `auto_commit_worktree()` function (`gateway/post_agent_commit.py:126-333`) also encounters phase restrictions when committing. If the pre-phase reset is implemented, does the auto-commit flow need changes, or does it already handle this correctly since it filters blocked files before committing?

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: high
parallel_phases: true
```
